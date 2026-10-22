"""``aristotle review`` — an open-ended audit: Aristotle works out what should hold of the code in scope and checks it."""

import argparse
import contextlib
import dataclasses
import logging
import os
from pathlib import Path

from aristotlelib import local_file_utils
from aristotlelib.ci import context, naming, results, scope
from aristotlelib.cli.command.ci import (
    DEFAULT_TIMEOUT_MINUTES,
    AppendOverEnv,
    VerifyAction,
    env_default,
    env_paths,
    env_type,
    tracks_root_filelist,
)
from aristotlelib.exit_codes import EXIT_SUCCESS, EXIT_USAGE_ERROR, EXIT_VERIFICATION_FAILED
from aristotlelib.verilog import bundle as verilog_bundle
from aristotlelib.verilog.filelist import FilelistError

logger = logging.getLogger("aristotle")


class ReviewAction(VerifyAction):
    """Verify the files in scope with the properties Aristotle proposes; advisory unless --fail-on says otherwise."""

    default_fail_on = results.FailOn.NEVER
    name_example = "rtl/hw: review changed @ main 1a2b3c4"

    @property
    def command_name(self) -> str:
        return "review"

    @property
    def description(self) -> str:
        return "Have Aristotle work out what should hold of this checkout and check it. Advisory by default."

    @classmethod
    def priority(cls) -> int:
        return 16

    def add_run_arguments(self) -> None:
        self.parser.add_argument(
            "--scope",
            type=env_type("ARISTOTLE_SCOPE", scope.Scope, list(scope.Scope)),
            metavar="{" + ",".join(map(str, scope.Scope)) + "}",
            default=env_default("ARISTOTLE_SCOPE", scope.Scope.CHANGED.value),
            help=(
                "Which files to verify (default: changed). 'changed' takes the source files changed since --base-ref "
                "or the base the CI system reports: a pull request's target; on GitLab, the default branch, for a "
                "pipeline on another branch; or, on GitHub and on GitLab's default branch, the commit before a push. "
                "Elsewhere it takes those changed by the last commit; the log says which. 'paths' takes the files "
                "--path matches, and 'all' every source file."
            ),
        )
        self.parser.add_argument(
            "--path",
            dest="paths",
            action=AppendOverEnv,
            default=env_paths("ARISTOTLE_PATHS"),
            help=(
                "Glob of files to verify. Repeatable. With --scope changed, the default, only the changed files it "
                "matches are verified; with --scope paths, which requires it, every file it matches is."
            ),
        )
        self.parser.add_argument(
            "--base-ref",
            type=str,
            default=os.environ.get("ARISTOTLE_BASE_REF"),
            help="Branch this change is proposed against, as `main` or `origin/main`. Detected from the CI environment by default.",
        )
        self.parser.add_argument(
            "--instructions",
            type=str,
            default=os.environ.get("ARISTOTLE_INSTRUCTIONS"),
            help="Extra instructions appended to the verification prompt.",
        )

    def preflight(self, args: argparse.Namespace) -> int | None:
        try:
            scope.check_arguments(args.scope, args.paths)
        except scope.ScopeError as e:
            self.parser.error(str(e))
        return self._read_allowed(self.root(args.directory, context.detect()), args) if args.allow_untracked else None

    async def run_action(self, args: argparse.Namespace) -> int:
        build = context.detect()
        # From a subfolder too, the whole repository, as a CI system would check it out.
        root = self.root(args.directory, build)
        logger.info(f"Aristotle review — {build.describe()}")
        if os.environ.get("ARISTOTLE_MANIFEST"):
            logger.warning(
                "ARISTOTLE_MANIFEST configures `aristotle ci`; `aristotle review` reads no manifest and ignores it."
            )

        whole = args.scope == scope.Scope.ALL
        try:
            scope_result = scope.resolve(
                root,
                scope=args.scope,
                paths=args.paths,
                base_ref=args.base_ref or build.base_ref,
                push_before=build.push_before,
                allow_no_sources=whole and tracks_root_filelist(root),
            )
        except scope.ScopeError as e:
            if whole:
                self._warn_untracked_filelist(root)
            logger.error(str(e))
            return EXIT_USAGE_ERROR

        if scope_result.is_empty and not whole and not scope_result.held_back_focus:
            scope.warn_held_back(root, scope_result)
            # Only --scope changed can be empty here; the other scopes raise ScopeError instead.
            return self._nothing_changed(args, build, root, scope_result.not_utf8)

        try:
            scope_result = self._read_block(root, scope_result, whole=whole, allowed=self._allowed)
        except (FilelistError, verilog_bundle.BundleError, scope.ScopeError) as e:
            logger.error(str(e))
            return EXIT_USAGE_ERROR
        block = scope_result.block
        if block is not None and scope_result.held_back_focus:
            scope_result = _take_held_back(root, scope_result, block)
        if block is not None and (boxed := [p for p in scope_result.focus_files if block.leaves_out(root / p)]):
            scope_result = dataclasses.replace(
                scope_result, focus_files=[p for p in scope_result.focus_files if p not in boxed]
            )
            if scope_result.is_empty and not whole and not scope_result.held_back_focus:
                scope.warn_held_back(root, scope_result)
                if args.scope == scope.Scope.PATHS:
                    logger.error(
                        "Each file --path matches is one a --blackbox line in aristotle.f leaves out of the upload, so "
                        f"nothing would be verified: {', '.join(p.as_posix() for p in boxed)}."
                    )
                    return EXIT_USAGE_ERROR
                return self._nothing_changed(args, build, root, scope_result.not_utf8, boxed)
        if scope_result.is_empty and not whole:
            # What's left in scope is held back by an ignore rule, and no block brings it.
            scope.warn_held_back(root, scope_result)
            logger.error(_held_back_error(args, scope_result.held_back_focus))
            return EXIT_USAGE_ERROR
        if whole and scope_result.block is not None:
            # Its sources go up whatever the ignore rules say, so a review of everything verifies them too, each once:
            # one reached through a linked folder may be in focus already under its own path.
            focus = list(scope_result.focus_files)
            seen = {os.path.realpath(root / p) for p in focus}
            for member in scope_result.block.members:
                if scope.is_source_file(path := Path(member.archive)):
                    real = os.path.realpath(member.path)
                    if real not in seen:
                        seen.add(real)
                        focus.append(path)
            scope_result = dataclasses.replace(scope_result, focus_files=sorted(focus))
        if scope_result.is_empty:
            scope.warn_held_back(root, scope_result)
            logger.error(scope.no_sources_message(root))
            return EXIT_USAGE_ERROR

        logger.info(f"Verifying {len(scope_result.focus_files)} file(s):")
        for path in scope_result.focus_files:
            logger.info(f"  {path.as_posix()}")
        scope.warn_held_back(root, scope_result)
        name = args.name or naming.default_name(
            root, build, scope_result.prefix, naming.review_run(args.scope, args.paths)
        )

        if args.dry_run:
            return self._dry_run(root, scope_result, name, instructions=args.instructions)

        budget_minutes = args.timeout if args.timeout is not None else DEFAULT_TIMEOUT_MINUTES
        return await self._verify(args, build, root, scope_result, budget_minutes, name, instructions=args.instructions)

    def _nothing_changed(
        self,
        args: argparse.Namespace,
        build: context.CIContext,
        root: Path,
        not_utf8: list[Path],
        boxed: list[Path] | None = None,
    ) -> int:
        """Pass a change that touched no source file Aristotle can take, or only ``boxed`` ones, which black boxes
        leave out, still writing an empty report for pipelines that expect one; under --fail-on no-verdict, fail one
        whose only source files it left out for their names."""
        if not args.dry_run:
            self._write_junit(results.RunResults.from_declarations([], duration_seconds=0.0), args, build, root)
        within = f" matching {', '.join(args.paths)}" if args.paths else ""
        if not_utf8:
            logger.warning(
                f"This change touched only source files{within} whose names aren't UTF-8, so nothing was verified: "
                f"{', '.join(local_file_utils.archive_name(p) for p in not_utf8)}. Rename them to verify them."
            )
            return EXIT_VERIFICATION_FAILED if args.fail_on == results.FailOn.NO_VERDICT else EXIT_SUCCESS
        if boxed:
            logger.info(
                f"This change touched only source files{within} that a --blackbox line in aristotle.f leaves out of "
                f"the upload: {', '.join(p.as_posix() for p in boxed)}. Nothing to do."
            )
        else:
            logger.info(f"This change touched no verifiable source file{within}. Nothing to do.")
        if args.fail_on == results.FailOn.NO_VERDICT:
            touched = "only source files a --blackbox line in aristotle.f leaves out" if boxed else "no source file"
            logger.warning(f"--fail-on no-verdict is set, but nothing was verified: this change touched {touched}.")
        return EXIT_SUCCESS


def _take_held_back(root: Path, scope_result: scope.ScopeResult, block: verilog_bundle.Bundle) -> scope.ScopeResult:
    """``scope_result`` with the held-back files in scope that ``block`` takes moved into focus, by their paths in the
    upload and each real file once, and those its black boxes leave out moved there too, for the caller to drop: when
    nothing else would be verified, each that declares only black boxes as well."""
    taken: dict[str, Path] = {}
    for member in block.members:
        taken.setdefault(os.path.realpath(member.path), Path(member.archive))
    focus = list(scope_result.focus_files)
    seen = {os.path.realpath(root / p) for p in focus}
    held: list[Path] = []
    for path in scope_result.held_back_focus:
        real = os.path.realpath(root / path)
        if real in taken:
            if real not in seen:
                seen.add(real)
                focus.append(taken[real])
        elif block.leaves_out(root / path):
            focus.append(path)
        else:
            held.append(path)
    if held and all(block.leaves_out(root / p) for p in focus):
        for path in held:
            # One that declares a black box beside another design unit stays held back: it goes up neither way.
            if not local_file_utils.is_secret_path(path, root):
                with contextlib.suppress(verilog_bundle.BundleError):
                    block.leave_out_unnamed([root / path], "held back by an ignore rule")
        focus += [p for p in held if block.leaves_out(root / p)]
        held = [p for p in held if not block.leaves_out(root / p)]
    return dataclasses.replace(scope_result, focus_files=sorted(focus), held_back_focus=held)


def _held_back_error(args: argparse.Namespace, held: list[Path]) -> str:
    """The error stopping a review whose only files in scope to verify are ``held``, which an ignore rule holds back."""
    if args.scope == scope.Scope.PATHS:
        what = "--path matches"
    else:
        what = "this change touched" + (f" matching {', '.join(args.paths)}" if args.paths else "")
    fix = "upload it as the warning above says, or, if it is Verilog, name it in a root aristotle.f that git tracks"
    if renamed := [p for p in held if not local_file_utils.is_utf8_name(p)]:
        its = "its name isn't" if len(renamed) == 1 else "their names aren't"
        fix += f", and rename {', '.join(local_file_utils.archive_name(p) for p in renamed)}: {its} UTF-8"
    return (
        f"Nothing {what} would be verified: an ignore rule holds back "
        f"{', '.join(local_file_utils.archive_name(p) for p in held)}. To verify one, {fix}. To keep one out of the "
        "review on purpose, give --path globs that don't match it."
    )
