"""``aristotle ci`` — check the properties in ``aristotle.toml`` as a build step; and what it shares with ``review``."""

import argparse
import asyncio
import contextlib
import dataclasses
import logging
import math
import os
import posixpath
import signal
import tempfile
import time
from abc import abstractmethod
from collections.abc import Callable, Generator, Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import TypeVar

from aristotlelib import local_file_utils
from aristotlelib.agent_task import TERMINAL_STATUSES, TaskStatus, follow_hint
from aristotlelib.api_request import AristotleAPIError, set_surface
from aristotlelib.ci import console, context, github, junit, manifest, naming, results, runner, scope
from aristotlelib.ci.record import RunRecord
from aristotlelib.cli import ci_setup
from aristotlelib.cli.api_action import PROJECT_REFERENCE_HELP, APIAction, project_from_reference
from aristotlelib.declaration import DeclarationFailureReason, DeclarationStatus
from aristotlelib.exit_codes import (
    EXIT_INFRASTRUCTURE_ERROR,
    EXIT_INTERRUPTED,
    EXIT_RATE_LIMITED,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    EXIT_VERIFICATION_FAILED,
)
from aristotlelib.project import Project, validate_project_name
from aristotlelib.verilog import bundle as verilog_bundle
from aristotlelib.verilog import filelist
from aristotlelib.verilog.filelist import FilelistError

logger = logging.getLogger("aristotle")

DEFAULT_TIMEOUT_MINUTES = 60
"""A run that exhausts the budget is reported unchecked and fails the build, so the default errs long."""
MAX_TIMEOUT_MINUTES = manifest.MAX_WALL_TIME_SECONDS // 60
DEFAULT_JUNIT_PATH = "aristotle-results.xml"
MIN_RUN_MINUTES = 1
"""A run submitted after waiting for a slot is left a quarter of the budget, and never less than this."""
SLOT_WAIT_NOTE = "Waiting for a free slot counts toward --timeout."
"""Said wherever the time budget ends a step: --timeout is the whole step's, a wait for a slot or in the queue
included."""

T = TypeVar("T")


class _Errors(logging.Handler):
    """Keeps each error logged while it is attached."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def env_default(name: str, fallback: str) -> str:
    # CI systems pass an unset input as the empty string, so empty means "not provided".
    return os.environ.get(name) or fallback


def env_type(
    env: str, convert: Callable[[str], T], choices: Iterable[object] | None = None, aliases: Iterable[str] = ()
) -> Callable[[str], T]:
    """An argparse type that also validates ``choices``, since argparse checks neither against a string default;
    ``aliases``, old names ``convert`` still takes, pass too, though the error leaves them out."""
    allowed = [str(c) for c in choices] if choices is not None else None
    hidden = set(aliases)

    def parse(value: str) -> T:
        if allowed is not None and value not in allowed and value not in hidden:
            raise argparse.ArgumentTypeError(f"{value!r} is not one of {', '.join(allowed)} (from the flag or {env})")
        try:
            return convert(value)
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"{value!r} is not valid (from the flag or {env}): {e}") from e

    return parse


def _positive_minutes(value: str) -> float:
    try:
        minutes = float(value)
    except ValueError:
        minutes = math.nan
    if not math.isfinite(minutes) or minutes <= 0 or minutes > MAX_TIMEOUT_MINUTES:
        raise ValueError(f"the timeout must be a positive number of minutes, at most {MAX_TIMEOUT_MINUTES}")
    return minutes


class Noted(argparse.Action):
    """Store the value, or ``const`` for a flag (``nargs=0``), and add the dest to the namespace's ``given_options``."""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, self.const if self.nargs == 0 else values)
        setattr(namespace, "given_options", {*getattr(namespace, "given_options", ()), self.dest})


class ReviewOption(argparse.Action):
    """An `aristotle review` option given to `aristotle ci`: a usage error that says where it belongs, whatever its form."""

    def __call__(self, parser, namespace, values, option_string=None):
        # `--paths` (the Action input's spelling) is named as review's `--path`.
        option = "--path" if option_string == "--paths" else option_string
        parser.error(
            f"{option} is an option of `aristotle review`. `aristotle ci` checks the properties in "
            f"{manifest.MANIFEST_NAME} instead; `aristotle ci setup` writes one."
        )


class AppendOverEnv(argparse.Action):
    """Append, except that the first flag replaces globs from the environment instead of adding to them."""

    def __call__(self, parser, namespace, values, option_string=None):
        current = getattr(namespace, self.dest)
        setattr(namespace, self.dest, [*([] if current is self.default else current or []), values])


def env_paths(name: str) -> list[str] | None:
    """Read newline- or comma-separated globs from an environment variable."""
    raw = os.environ.get(name)
    if not raw:
        return None
    parts = [part.strip() for chunk in raw.splitlines() for part in chunk.split(",")]
    return [part for part in parts if part] or None


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    return Path(raw) if raw else None


def _folder_argument(value: str) -> str:
    """argparse type for --allow-untracked; rejects the empty string."""
    if not value:
        raise argparse.ArgumentTypeError("names no folder")
    return value


@dataclasses.dataclass(frozen=True)
class AllowedFolder:
    """A folder --allow-untracked names: as given, and where it is, with no link on the way."""

    given: str
    path: str
    kept_out: tuple[tuple[tuple[int, int], str], ...] = ()
    """The identity of each folder of _kept_out(), with what it holds, so that no other path to one passes through."""


def allowed_folders(root: Path, given: Iterable[str]) -> list[AllowedFolder]:
    """The folders ``given`` to --allow-untracked, a relative one from the current directory; ``root`` is the folder the
    step uploads.

    Raises:
        ValueError: one is a path filelist.never_read() refuses, isn't a folder, is reached through a link, or is or
            holds a folder of _kept_out().
    """
    if not (given := list(given)):
        return []
    kept_out = _kept_out(root)
    identities = tuple((identity, why) for kept, why in kept_out if (identity := _identity(str(kept))) is not None)
    folders = []
    for value in given:
        if why := filelist.never_read(value):
            raise ValueError(f"--allow-untracked names {why}.")
        path = os.path.normpath(os.path.abspath(value))
        real = os.path.realpath(path)
        if not os.path.isdir(real):
            state = "is not a folder" if os.path.lexists(path) else "doesn't exist"
            raise ValueError(f"--allow-untracked {value}: {path} {state}.")
        if os.path.normcase(real) != os.path.normcase(path):
            raise ValueError(
                f"--allow-untracked {value}: {path} is reached through a link, to {real}. Name the folder by the path "
                "it leads to instead."
            )
        for kept, why in kept_out:
            if relation := _holds(path, kept):
                raise ValueError(
                    f"--allow-untracked {value} {relation} {why}, so a change could send them. Name the folders that "
                    "hold the generated files, or the shared ones, instead."
                )
        folders.append(AllowedFolder(value, path, identities))
    return folders


def _kept_out(root: Path) -> list[tuple[Path, str]]:
    """The folders that hold files of other steps or tools, credentials among them, under names no rule matches, which
    --allow-untracked can't name, nor one above them, each with what it holds: ``root``, the folder the step uploads,
    context.credential_folders(), the home folder, and the temporary folders."""
    return [
        (root, f"the folder the step uploads, {root}, which may hold files git doesn't track, such as credentials"),
        *(
            (folder, f"{folder}, where the CI system keeps the job's credentials, such as its token or secret files")
            for folder in context.credential_folders()
        ),
        (Path(home := os.path.expanduser("~")), f"the home folder, {home}, where tools keep credentials"),
        *(
            (folder, f"a temporary folder, {folder}, where steps and tools keep files")
            for folder in _temporary_folders()
        ),
    ]


def _temporary_folders() -> list[Path]:
    """The temporary folders that exist: Python's, those TMPDIR, TEMP and TMP name, and, outside Windows, the system's
    own, whichever a job's variables point at."""
    names = [tempfile.gettempdir(), *(os.environ.get(name, "") for name in ("TMPDIR", "TEMP", "TMP"))]
    if os.name != "nt":
        names += ["/tmp", "/var/tmp", "/usr/tmp"]
    folders = dict.fromkeys(os.path.normpath(os.path.abspath(name)) for name in names if name)
    return [Path(folder) for folder in folders if os.path.isdir(folder)]


def _identity(path: str) -> tuple[int, int] | None:
    """The identity of the file or folder at ``path``, links followed; None if it has none or can't be read."""
    try:
        return local_file_utils.file_identity(os.stat(path))
    except OSError:
        return None


def _holds(folder: str, path: Path) -> str | None:
    """``is`` when the folder at ``folder`` is ``path``, ``holds`` when it is a folder above it, by name, as ``path``
    is named or resolved, or by identity, however either is spelled; None otherwise."""
    spelled = os.path.normcase(folder)
    names = {os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.realpath(path))}
    if spelled in names:
        return "is"
    if any(_is_within(name, spelled) for name in names):
        return "holds"
    real = Path(os.path.realpath(path))
    identity = _identity(folder)
    chain = [_identity(str(p)) for p in (real, *real.parents)]
    if identity is None or identity not in chain:
        return None
    return "is" if chain[0] == identity else "holds"


REVIEW_VARIABLES = ("ARISTOTLE_SCOPE", "ARISTOTLE_PATHS", "ARISTOTLE_BASE_REF", "ARISTOTLE_INSTRUCTIONS")
"""Configuration for ``aristotle review`` that ``aristotle ci`` does not read."""


class VerifyAction(APIAction):
    """Submit the checkout for verification, wait for the verdict, and report each property as a test."""

    default_fail_on = results.FailOn.FAILURE
    timeout_default_help = str(DEFAULT_TIMEOUT_MINUTES)
    on_timeout_default_help = "cancel"
    local_root_help = "the repository's top level"
    name_example = "rtl/hw: ci @ main 1a2b3c4"
    _allowed: tuple[AllowedFolder, ...] = ()
    """The folders --allow-untracked names, as preflight reads them."""

    @abstractmethod
    def add_run_arguments(self) -> None:
        """Add the arguments that say what the run verifies."""

    @staticmethod
    def root(directory: Path | None, build: context.CIContext, holding: str | None = None) -> Path:
        """The folder a run is rooted at: ``directory``, else the checkout the CI system reports; outside CI, from a
        subfolder too, scope.local_root's for ``holding``."""
        if directory:
            return directory.resolve()
        if build.system == context.CISystem.LOCAL:
            return scope.local_root(build.workspace, holding)
        return build.workspace.resolve()

    def add_action_arguments(self) -> None:
        self.add_run_arguments()
        self.parser.add_argument(
            "--timeout",
            action=Noted,
            type=env_type("ARISTOTLE_TIMEOUT", _positive_minutes),
            # None when unset, so that a default from elsewhere can apply.
            default=os.environ.get("ARISTOTLE_TIMEOUT") or None,
            help=(
                "Wall-clock budget in minutes for the whole command, waiting for a free slot included, at most "
                f"{MAX_TIMEOUT_MINUTES} (default: {self.timeout_default_help})."
            ),
        )
        self.parser.add_argument(
            "--on-timeout",
            action=Noted,
            type=env_type("ARISTOTLE_ON_TIMEOUT", str, ["cancel", "leave"]),
            metavar="{cancel,leave}",
            default=env_default("ARISTOTLE_ON_TIMEOUT", "cancel"),
            help=(
                "Whether to cancel the run when the budget is exhausted or the job is cancelled "
                f"(default: {self.on_timeout_default_help})."
            ),
        )
        self.parser.add_argument(
            "--fail-on",
            action=Noted,
            type=env_type("ARISTOTLE_FAIL_ON", results.FailOn, list(results.FailOn), results.FAIL_ON_ALIASES),
            metavar="{" + ",".join(map(str, results.FailOn)) + "}",
            default=env_default("ARISTOTLE_FAIL_ON", self.default_fail_on.value),
            help=(
                f"Which results fail the build (default: {self.default_fail_on}). 'never' exits 0 once a run "
                "finishes, even if the run failed; it still fails when no run can be started or followed. 'no-verdict' "
                "also fails it on any property without a verdict: in progress, unresolved, unchecked or not "
                "registered. 'unresolved', its old name, still works."
            ),
        )
        self.parser.add_argument(
            "--junit",
            action=Noted,
            type=Path,
            default=env_default("ARISTOTLE_JUNIT", DEFAULT_JUNIT_PATH),
            help=f"Where to write the JUnit report (default: {DEFAULT_JUNIT_PATH}).",
        )
        self.parser.add_argument(
            "--no-junit",
            action=Noted,
            nargs=0,
            const=True,
            default=False,
            help="Skip writing the JUnit report.",
        )
        self.parser.add_argument(
            "--directory",
            type=Path,
            default=_env_path("ARISTOTLE_DIRECTORY"),
            help=(
                "The repository, or the subproject in it, to check (default: the checkout the CI system reports; "
                f"outside CI, {self.local_root_help})."
            ),
        )
        self.parser.add_argument(
            "--allow-untracked",
            action="append",
            type=_folder_argument,
            default=[],
            metavar="FOLDER",
            help=(
                "Let a file the root aristotle.f reaches go up from FOLDER though git doesn't track it in the folder "
                "the step uploads, such as a generated source, or a file in a shared folder outside it. Repeatable. A "
                "relative FOLDER is from the current directory. FOLDER can't be a network path, be reached through a "
                "link, or be or hold the folder the step uploads, the CI system's folder of the job's credential "
                "files, the home folder or a temporary folder. Only the command line gives it, never a file in the "
                "repository. A file in a .git folder, named like a credential, or in /proc, /sys or /dev outside the "
                "folder still stops the step, as does one a link leads out of every FOLDER."
            ),
        )
        self.parser.add_argument(
            "--name",
            action=Noted,
            type=env_type("ARISTOTLE_NAME", validate_project_name),
            default=os.environ.get("ARISTOTLE_NAME") or None,
            help=(
                "Name for the project the run creates, shown by `aristotle list` and the web app (default: the "
                "repository and the directory in it, what the run checks, and the branch and commit, as "
                f"'{self.name_example}')."
            ),
        )
        self.parser.add_argument(
            "--dry-run",
            action=Noted,
            nargs=0,
            const=True,
            default=False,
            help="Resolve and print what would be verified, then exit without submitting.",
        )

    def _read_allowed(self, root: Path, args: argparse.Namespace) -> int | None:
        """Read the folders --allow-untracked names, a relative one from the current directory, for ``root``, the folder
        the step uploads; EXIT_USAGE_ERROR, logged, when one can't be taken."""
        try:
            self._allowed = tuple(allowed_folders(root, args.allow_untracked))
        except ValueError as e:
            logger.error(str(e))
            return EXIT_USAGE_ERROR
        return None

    def run(self, args: argparse.Namespace) -> int:
        # GitHub names the repository of the action a step runs, and none for a `run:` step.
        set_surface("action" if os.environ.get("GITHUB_ACTION_REPOSITORY") else "ci")
        # A dry run only reads the local repository, so it must work without a key.
        self.local_only = args.dry_run
        self._reported = False
        errors = _Errors()
        # The root logger: the shared command code logs its errors there, and the "aristotle" logger's reach it.
        logging.getLogger().addHandler(errors)
        # CI cancels a job with SIGTERM (after SIGINT on GitHub); as PID 1 in the container, Python would ignore it.
        previous = signal.signal(signal.SIGTERM, signal.default_int_handler)
        try:
            code = super().run(args)
        except KeyboardInterrupt:
            logger.error("Interrupted before the run finished.")
            code = EXIT_INTERRUPTED
        except BrokenPipeError:
            raise
        except Exception as e:
            # No traceback in the build log; exit on the infrastructure code, not the verification one.
            logger.error(f"Aristotle could not complete the run: {e}")
            logger.debug("Aristotle CI failed", exc_info=True)
            code = EXIT_INFRASTRUCTURE_ERROR
        finally:
            signal.signal(signal.SIGTERM, previous)
            logging.getLogger().removeHandler(errors)
        # The run's report annotates what ended it; without one, only the log would say.
        if code and not self._reported and context.detect_system() == context.CISystem.GITHUB:
            _guarded(
                "the GitHub annotations",
                github.write_error_annotations,
                f"aristotle {self.command_name}",
                errors.messages,
            )
        return code

    def _dry_run(
        self,
        root: Path,
        scope_result: scope.ScopeResult,
        name: str,
        checked: manifest.Manifest | None = None,
        instructions: str | None = None,
    ) -> int:
        """Archive the upload as a run would, to a temporary file, and say what would go up, or why nothing could."""
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                packaged = runner.package(
                    root, scope_result, Path(temp_dir) / "aristotle-ci.tar.gz", checked, instructions
                )
            except runner.UnusableUploadError as e:
                logger.error(str(e))
                return EXIT_USAGE_ERROR
            except runner.RunnerError as e:
                logger.error(str(e))
                return EXIT_INFRASTRUCTURE_ERROR
        if scope_result.block is not None:
            logger.info(verilog_bundle.listing(scope_result.block))
        logger.info(f"Dry run: would upload {packaged.summary}.")
        logger.info(f'Dry run: would name the project "{name}".')
        if scope_result.block is not None and (instruction := verilog_bundle.blackbox_instruction(scope_result.block)):
            logger.info(f"Dry run: would ask in the prompt: {instruction}")
        if packaged.record is not None:
            logger.info(f"Dry run: would end the prompt with the run's record: {packaged.record.line}")
        return EXIT_SUCCESS

    def _read_block(
        self,
        root: Path,
        scope_result: scope.ScopeResult,
        whole: bool = True,
        allowed: Sequence[AllowedFolder] = (),
    ) -> scope.ScopeResult:
        """With a root ``aristotle.f`` git tracks, ignore rules aside, add the block it describes, logged as submit
        logs it, with each file only a folder of ``allowed`` lets through, and take the files its black boxes leave
        out from the upload, or warn of one git doesn't track: for a run of the ``whole`` checkout, or when Verilog is
        in focus or held back in scope.

        Raises:
            FilelistError: the filelist can't be read.
            verilog_bundle.BundleError: the block can't be uploaded as described, reaches a file git doesn't track
                and no folder of ``allowed`` holds, or a tracked .v, .sv, .vh or .svh file that would go up declares a
                black box beside another design unit.
            scope.ScopeError: git couldn't list the tracked files.
        """
        # From every tracked path: the upload set leaves out what an ignore rule matches, which may be the
        # filelist or every source it names, and the block takes those back.
        tracked = scope.tracked_paths(root)
        canonical = root_filelist(tracked)
        if canonical is None and allowed:
            logger.warning(
                f"--allow-untracked lets through only files a root {verilog_bundle.FILELIST_NAME} reaches, and git "
                f"tracks none in {root}, so it changes nothing."
            )
        held_back_verilog = any(p.suffix.lower() in scope.VERILOG_EXTENSIONS for p in scope_result.held_back_focus)
        if not (whole or scope_result.has_verilog_focus or held_back_verilog):
            return scope_result
        if canonical is None:
            self._warn_untracked_filelist(root, tracked)
            return scope_result
        if not os.path.isfile(root / canonical):
            raise FilelistError(
                f"{canonical.as_posix()} is tracked, but the checkout has no file there, so the block it describes "
                "can't be read."
            )
        read: dict[Path, AllowedFolder] = {}
        let_through: dict[Path, AllowedFolder] = {}

        def check_filelist(path: Path, real: Path, origin: filelist.Origin) -> None:
            if folder := self._check_filelist(root, tracked, allowed, path, origin):
                read[path] = folder

        def check_file(path: Path, reason: str) -> None:
            if folder := self._check_block_file(root, tracked, allowed, path, reason):
                let_through[path] = folder

        reader = filelist.Reader(
            check=check_filelist,
            no_variables=(
                f"since `aristotle {self.command_name}` expands no environment variable in aristotle.f, so a "
                "change can't send the job's environment"
            ),
        )
        reader.read_file(root / canonical)
        block = verilog_bundle.build(reader.result, root, committed=root / canonical, check=check_file)
        upload_files = scope_result.upload_files
        if block.blackboxes:
            block_paths = {m.archive for m in block.members}
            block.leave_out_unnamed(
                [
                    root / p
                    for p in upload_files
                    if p.suffix.lower() in verilog_bundle.VERILOG_SUFFIXES
                    and p.as_posix() not in block_paths
                    and not local_file_utils.is_secret_path(p, root)
                ],
                "tracked, so it goes up",
            )
            upload_files = [p for p in upload_files if not block.leaves_out(root / p)]
        _check_outside_paths(root, block, {p.as_posix() for p in upload_files})
        logger.info(f"Using the Verilog filelist committed at {canonical.as_posix()}")
        # Files from outside the folder are listed with the folders --allow-untracked names, below.
        notes, warnings = verilog_bundle.report(block, files=False)
        for note in notes:
            logger.info(note)
        for warning in warnings:
            logger.warning(f"WARNING: {warning}")
        members = [(m, let_through[m.path]) for m in block.members if m.path in let_through]
        if lines := _allowed_lines(block, members, list(read.items())):
            logger.info(lines)
        return dataclasses.replace(
            scope_result, block=block, upload_files=upload_files, allowed=frozenset(m.archive for m, _ in members)
        )

    def _warn_untracked_filelist(self, root: Path, tracked: set[str] | None = None) -> None:
        """Warn of a root ``aristotle.f`` in ``root`` that git doesn't track, by ``tracked`` if given, else as git lists
        them, which is neither read nor uploaded; nothing when git can't say."""
        if tracked is None:
            try:
                tracked = scope.tracked_paths(root)
            except scope.ScopeError:
                return
        if root_filelist(tracked) is None and (untracked := verilog_bundle.folder_filelist(root)) is not None:
            logger.warning(
                f"{untracked.name} isn't tracked by git, so `aristotle {self.command_name}` doesn't read it or upload "
                "it. Commit it to use it."
            )

    def _check_block_file(
        self, root: Path, tracked: set[str], allowed: Sequence[AllowedFolder], path: Path, reason: str
    ) -> AllowedFolder | None:
        """Refuses, before it is read, a file the block reaches that isn't tracked by git in ``root``, as ``tracked``
        lists it, unless it resolves into a folder of ``allowed``, through no other path to a folder of _kept_out(),
        which is returned; and one that is in a .git folder, named like a credential, or in /proc, /sys or /dev,
        whatever ``allowed`` says.

        Raises:
            verilog_bundle.BundleError: naming it.
        """
        problem, folder = _block_problem(root, tracked, allowed, path)
        if problem:
            refusal = self._file_refusal(problem, "uploads only files", bool(allowed), _namable(root, path))
            raise verilog_bundle.BundleError(f"{filelist.shown(path)} ({reason}) {refusal}")
        return folder

    def _check_filelist(
        self, root: Path, tracked: set[str], allowed: Sequence[AllowedFolder], path: Path, origin: filelist.Origin
    ) -> AllowedFolder | None:
        """Refuses, before it is read, a filelist as _check_block_file() refuses a file, and returns the folder of
        ``allowed`` it resolves into when only that lets it through.

        Raises:
            FilelistError: naming it.
        """
        problem, folder = _block_problem(root, tracked, allowed, path)
        if problem:
            where = f"{origin}: " if origin.file is not None else ""
            refusal = self._file_refusal(problem, "reads only filelists", bool(allowed), _namable(root, path))
            raise FilelistError(f"{where}{filelist.shown(path)} {refusal}")
        return folder

    def _file_refusal(self, problem: str, does: str, allowed: bool = False, namable: bool = True) -> str:
        """The rest of the message refusing a file with ``problem``, for a step that ``does`` this with what git
        tracks, and, if ``allowed``, with what --allow-untracked lets through; naming the option if ``namable``, when
        a folder it can take could hold the file."""
        if problem == _IN_GIT_FOLDER or problem.endswith(SYSTEM_FOLDERS):
            return f"{problem}, which is never uploaded."
        if problem.endswith("named like a credential"):
            return f"{problem}, and such files are never uploaded. Leave it out of the block."
        if problem.endswith("a folder that holds credentials"):
            return f"{problem}, and such files are never uploaded. Leave it out of the block."
        if problem.endswith((_ESCAPES, _ANOTHER_PATH)):
            return f"{problem}."
        leads = problem.endswith(_LEADS_OUTSIDE)
        if allowed:
            rule = f"no folder --allow-untracked names holds {'what it leads to' if leads else 'it'}"
        else:
            rule = f"`aristotle {self.command_name}` {does} git tracks there"
        if problem == _OUTSIDE:
            advice, option = "Commit the file inside that folder and name it from there", "name a folder that holds it"
        elif leads:
            advice, option = "Commit what the link leads to in its place", "name a folder that holds that"
        else:
            advice, option = "Commit it", "name a folder that holds it"
        ways = f"{advice}, {option} with --allow-untracked" if namable else advice
        return f"{problem}, and {rule}. {ways}, or submit the block with `aristotle submit`."

    async def _verify(
        self,
        args: argparse.Namespace,
        build: context.CIContext,
        root: Path,
        scope_result: scope.ScopeResult,
        budget_minutes: float,
        name: str,
        checked: manifest.Manifest | None = None,
        instructions: str | None = None,
    ) -> int:
        """Run the checkout through Aristotle and report it: ``checked``'s properties, else whatever the run finds."""
        started = time.monotonic()
        budget = budget_minutes * 60
        deadline = started + budget

        with tempfile.TemporaryDirectory() as temp_dir:
            tarball_path = Path(temp_dir) / "aristotle-ci.tar.gz"
            try:
                project = await runner.submit(
                    root,
                    scope_result,
                    tarball_path,
                    resubmit_by=deadline - max(MIN_RUN_MINUTES * 60, budget / 4),
                    extra_instructions=instructions,
                    manifest=checked,
                    name=name,
                    cancel_on_interrupt=args.on_timeout == "cancel",
                )
            except runner.SlotTimeoutError as e:
                logger.error(str(e))
                logger.info(
                    f"{SLOT_WAIT_NOTE} Cancel a project in progress, raise --timeout, or run CI as a dedicated account "
                    "with its own key."
                )
                return EXIT_RATE_LIMITED
            except runner.UnusableUploadError as e:
                logger.error(str(e))
                return EXIT_USAGE_ERROR
            except runner.RunnerError as e:
                logger.error(str(e))
                return EXIT_INFRASTRUCTURE_ERROR

        logger.info(f"Run submitted: {project.web_url()}")
        if time.monotonic() - started >= 60:
            logger.info(f"{(deadline - time.monotonic()) / 60:.0f} min of the time budget is left for the run.")
        return await self._follow(args, build, root, scope_result, project, started, deadline, checked)

    async def _follow(
        self,
        args: argparse.Namespace,
        build: context.CIContext,
        root: Path,
        scope_result: scope.ScopeResult,
        project: Project,
        started: float,
        deadline: float,
        checked: manifest.Manifest | None = None,
    ) -> int:
        """Wait for ``project``'s run until ``deadline`` and report it as _verify does, timed from ``started``."""
        try:
            final_status = await runner.wait_for_completion(
                project,
                timeout_seconds=deadline - time.monotonic(),
                cancel_on_timeout=args.on_timeout == "cancel",
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            if args.on_timeout == "leave" and (hint := self._later_hint(project)):
                logger.info(hint.rstrip("\n"))
            raise
        except runner.RunnerError as e:
            logger.error(str(e))
            return EXIT_INFRASTRUCTURE_ERROR

        try:
            declarations = await runner.fetch_declarations(project)
            stopped_summary = (
                await runner.fetch_stopped_summary(project) if final_status == TaskStatus.COMPLETE_WITH_ERRORS else None
            )
        except AristotleAPIError as e:
            if not runner.is_transient(e):
                raise
            logger.error(f"The run finished, but its results could not be fetched: {e}")
            logger.info(f"See them at {project.web_url()}")
            return EXIT_INFRASTRUCTURE_ERROR
        run_results = results.RunResults.from_declarations(
            declarations,
            duration_seconds=time.monotonic() - started,
            project_url=project.web_url(),
            timed_out=final_status is None,
            never_started=final_status == TaskStatus.QUEUED,
            task_failed=final_status == TaskStatus.FAILED,
            # This job's own cancels (timeout, interrupt) never return CANCELED.
            canceled=final_status == TaskStatus.CANCELED,
            out_of_budget=final_status == TaskStatus.OUT_OF_BUDGET,
            path_prefix=scope_result.prefix,
            requested=checked.properties if checked is not None else None,
            stopped_summary=stopped_summary,
        )
        registered_none = not (
            run_results.properties or run_results.never_started or run_results.cut_short or run_results.stopped_summary
        )
        # The summary usually names a failure's counterexample, and says why a property has no verdict or why a run
        # registered nothing or ran out of budget. A run the time budget cut may still be going: its summary isn't final.
        if not run_results.timed_out and (
            run_results.failed or run_results.without_verdict or run_results.out_of_budget or registered_none
        ):
            # A stopped run's summary is already fetched, without its stop marker.
            summary = stopped_summary if stopped_summary is not None else await _fetch_summary(project)
            if summary != runner.NO_STOPPED_SUMMARY:
                run_results = dataclasses.replace(run_results, run_summary=summary)

        left = None
        if run_results.timed_out and args.on_timeout == "leave":
            state = "is still queued, and starts later" if run_results.never_started else "goes on"
            left = f"The run {state}, as {getattr(args, 'left_by', '--on-timeout leave is set')}."
            run_results = dataclasses.replace(
                run_results, left_note=f"{left} {self._later_hint(project).rstrip()}".rstrip()
            )

        self._report(run_results, args, build, root)
        if run_results.failed:
            logger.info(
                "The run's files, with any reproducer its summary names, are in its project. Get them with:\n"
                f"  aristotle download {project.project_id}"
            )

        if run_results.task_failed:
            logger.error(
                "The verification run failed due to an internal error. The team at Harmonic has been notified."
            )
        if run_results.canceled:
            logger.error("The verification run was cancelled outside this job before it finished.")
        if run_results.cut_short:
            access = await _language_access(root, scope_result, checked) if run_results.nothing_registered else {}
            logger.error(_cut_short_error(run_results, project.web_url(), access))
        if run_results.never_started:
            logger.error(
                f"Nothing was verified. {results.NEVER_STARTED_REASON} {SLOT_WAIT_NOTE} Raise it, or, if your other "
                "runs in progress held the run up, cancel one you don't need, or run CI as a dedicated account with "
                "its own key."
            )
        elif run_results.nothing_verified:
            access = await _language_access(root, scope_result, checked)
            logger.error(
                f"No property in the manifest was verified. {_causes_sentence(_likely_causes(run_results, access))} "
                f"{_summary_pointer(run_results)} {project.web_url()}"
            )
        if run_results.not_registered and not (run_results.cut_short and run_results.nothing_registered):
            logger.warning(_not_registered_warning(run_results))
        if registered_none:
            access = await _language_access(root, scope_result, checked)
            off, unknown = _language_causes(access)
            causes = f" {_causes_sentence(off + unknown)}" if off or unknown else ""
            logger.warning(
                f"The run registered no properties, so nothing was verified.{causes} --fail-on no-verdict fails a "
                f"run with no properties. {_summary_pointer(run_results)} {project.web_url()}"
            )
        if left is not None:
            logger.info(f"{left}\n{self._later_hint(project)}{follow_hint(project.project_id)}")

        exit_code = run_results.exit_code(args.fail_on)
        if exit_code == EXIT_SUCCESS and args.fail_on == results.FailOn.FAILURE and run_results.from_manifest:
            if run_results.without_verdict:
                logger.warning(_without_verdict_warning(run_results))
        if (
            exit_code == EXIT_VERIFICATION_FAILED
            and args.fail_on == results.FailOn.NO_VERDICT
            and run_results.without_verdict
            and not (run_results.failed or run_results.stopped_summary or run_results.nothing_verified)
        ):
            # The summary line counts a property unresolved or unchecked apart from the ones in progress.
            logger.error(f"{_no_verdict(run_results)} --fail-on no-verdict fails the build on a property without one.")
        if exit_code == EXIT_SUCCESS and (
            run_results.failed
            or run_results.stopped_summary
            or run_results.task_failed
            or run_results.canceled
            or run_results.cut_short
            or run_results.nothing_verified
        ):
            logger.info("--fail-on never is set, so the build is not being failed.")
        return exit_code

    def _later_hint(self, project: Project) -> str:
        """How to report on ``project``'s run in a later command, as a line; empty where there is no way."""
        return ""

    def _report(
        self,
        run_results: results.RunResults,
        args: argparse.Namespace,
        build: context.CIContext,
        root: Path,
    ) -> None:
        """Write every report; any failure but a closed pipe is logged and passed over, so the exit code stands."""
        self._reported = True
        on_github = build.system == context.CISystem.GITHUB
        _guarded("the console report", console.report, run_results, root)
        self._write_junit(run_results, args, build, root)
        if on_github:
            _guarded("the GitHub annotations and job summary", github.report, run_results, root)

    def _write_junit(
        self,
        run_results: results.RunResults,
        args: argparse.Namespace,
        build: context.CIContext,
        root: Path,
    ) -> None:
        if args.no_junit:
            return
        if not _guarded(
            f"the JUnit report to {args.junit}",
            lambda: junit.write(run_results, args.junit, root, _checkout_folders(build, args.directory)),
        ):
            return
        if build.system == context.CISystem.GITHUB:
            _guarded("the junit step output", github.write_output, "junit", str(args.junit))


class CIAction(VerifyAction):
    """Check the properties in the manifest and report each as a test."""

    timeout_default_help = f"check.max_wall_time_seconds from the manifest, else {DEFAULT_TIMEOUT_MINUTES}"
    on_timeout_default_help = "cancel, or leave with --project"
    local_root_help = (
        f"the nearest folder up to the repository's top level that holds {manifest.MANIFEST_NAME}, else the top level; "
        "with --manifest, the top level"
    )
    _build: context.CIContext
    _root: Path
    _manifest_path: Path
    _manifest: manifest.Manifest
    _project_source = "--project"
    """What named the run to report on: --project, or ARISTOTLE_PROJECT."""

    @property
    def command_name(self) -> str:
        return "ci"

    @property
    def description(self) -> str:
        return f"Check the properties in {manifest.MANIFEST_NAME} and report them to your CI system."

    @classmethod
    def priority(cls) -> int:
        return 15

    def add_run_arguments(self) -> None:
        commands = self.parser.add_subparsers(dest="ci_command", title="commands", metavar="{setup}")
        ci_setup.SetupAction(commands).add_arguments()
        self.parser.add_argument(
            "--manifest",
            type=Path,
            default=_env_path("ARISTOTLE_MANIFEST"),
            help=f"The manifest of properties to check (default: {manifest.MANIFEST_NAME} in --directory).",
        )
        self.parser.add_argument(
            "--project",
            action=Noted,
            metavar="PROJECT",
            default=os.environ.get("ARISTOTLE_PROJECT") or None,
            help=(
                "Report on a run already started instead of starting one, such as a run left going with "
                f"--on-timeout leave: {PROJECT_REFERENCE_HELP}. It uploads nothing, waits for the run within the "
                "time budget, and reports the manifest's properties as a new run would. It exits 2 first unless "
                "`aristotle ci` started the project's only run on the files this checkout would upload, and the same "
                "properties. When the budget runs out or the command is stopped, it leaves the run going unless "
                "--on-timeout cancel is given."
            ),
        )
        # Caught here, so that argparse doesn't read the option's value as a command or report it as unrecognized.
        self.parser.add_argument(
            "--scope",
            "--path",
            "--paths",
            "--base-ref",
            "--instructions",
            action=ReviewOption,
            nargs="?",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )

    def preflight(self, args: argparse.Namespace) -> int | None:
        self._build = context.detect()
        searched = not args.directory and not args.manifest and self._build.system == context.CISystem.LOCAL
        self._root = self.root(args.directory, self._build, None if args.manifest else manifest.MANIFEST_NAME)
        logger.info(f"Aristotle CI — {self._build.describe()}")
        given: set[str] = getattr(args, "given_options", set())
        if args.project is not None:
            source = self._project_source = "--project" if "project" in given else "ARISTOTLE_PROJECT"
            if source == "ARISTOTLE_PROJECT":
                # Said, as a variable left set would otherwise report on an old run as if it were this one.
                logger.info(f"Reporting on the run ARISTOTLE_PROJECT names: {args.project}")
            if "dry_run" in given:
                self.parser.error(f"--dry-run starts no run, and {source} reports on a run already started.")
            if "name" in given:
                logger.info(f"--name names the project a new run creates, so {source} ignores it.")
            # Following a run must not end it unless asked to.
            if "on_timeout" not in given and not os.environ.get("ARISTOTLE_ON_TIMEOUT"):
                args.on_timeout = "leave"
                args.left_by = f"{source} leaves it going"
        if ignored := [name for name in REVIEW_VARIABLES if os.environ.get(name)]:
            logger.warning(
                f"{', '.join(ignored)} {'configures' if len(ignored) == 1 else 'configure'} `aristotle review`; "
                f"`aristotle ci` checks the properties in the manifest and ignores "
                f"{'it' if len(ignored) == 1 else 'them'}."
            )

        path = self._manifest_path = args.manifest or self._root / manifest.MANIFEST_NAME
        try:
            self._manifest = manifest.load(path)
        except FileNotFoundError:
            if args.manifest:
                logger.error(f"Manifest {path} not found.")
            else:
                try:
                    # Outside a repository, that is the reason, and setup can't help.
                    scope.require_repository(self._root)
                except scope.ScopeError as e:
                    logger.error(str(e))
                    return EXIT_USAGE_ERROR
                logger.error(
                    f"No {manifest.MANIFEST_NAME} found. Write one, or have `aristotle ci setup` propose one: at a "
                    "terminal, or from a finished project with --from <project>."
                )
                start = self._build.workspace.resolve()
                if searched and start != self._root:
                    where = f"in {start} and each folder above it up to {self._root}"
                else:
                    where = f"for {path}"
                logger.info(f"Looked {where}; pass --manifest to read another file.")
            return EXIT_USAGE_ERROR
        except manifest.ManifestError as e:
            logger.error(str(e))
            if e.foreign and not args.manifest:
                logger.info(f"If {path} is not Aristotle's manifest, pass --manifest to read the one that is.")
            return EXIT_USAGE_ERROR

        properties = self._manifest.properties
        logger.info(f"Checking {len(properties)} {'property' if len(properties) == 1 else 'properties'} from {path}:")
        for requested in properties:
            logger.info(f"  {requested.id}  ({requested.kind})")
        if not_enforced := self._manifest.not_enforced():
            logger.info(f"Accepted but not yet enforced: {', '.join(not_enforced)}.")
        return self._read_allowed(self._root, args)

    async def run_action(self, args: argparse.Namespace) -> int:
        try:
            # A run already started uploads nothing, so there is nothing to warn of leaving out.
            scope_result = scope.resolve_checkout(
                self._root, warn=args.project is None, allow_empty=tracks_root_filelist(self._root)
            )
        except scope.ScopeError as e:
            if args.project is None:
                self._warn_untracked_filelist(self._root)
            logger.error(str(e))
            return EXIT_USAGE_ERROR

        if args.project is not None:
            started = time.monotonic()
            try:
                # The block a fresh run would upload, for the run's record to be checked against; quietly, as nothing
                # goes up.
                with _quietly():
                    scope_result = self._read_block(self._root, scope_result, allowed=self._allowed)
            except (FilelistError, verilog_bundle.BundleError, scope.ScopeError) as e:
                logger.error(str(e))
                return EXIT_USAGE_ERROR
            project = await project_from_reference(args.project)
            try:
                refusal = await _refusal(project, self._root, scope_result, self._manifest, self._project_source)
            except runner.RunnerError as e:
                logger.error(str(e))
                return EXIT_INFRASTRUCTURE_ERROR
            if refusal:
                logger.error(refusal)
                return EXIT_USAGE_ERROR
            deadline = started + self._budget_minutes(args) * 60
            logger.info(f"Following the run: {project.web_url()}")
            return await self._follow(
                args, self._build, self._root, scope_result, project, started, deadline, self._manifest
            )

        try:
            scope_result = self._read_block(self._root, scope_result, allowed=self._allowed)
        except (FilelistError, verilog_bundle.BundleError, scope.ScopeError) as e:
            logger.error(str(e))
            return EXIT_USAGE_ERROR
        scope.warn_held_back(self._root, scope_result)
        name = args.name or naming.default_name(
            self._root, self._build, scope_result.prefix, naming.ci_run(self._root, self._manifest_path)
        )

        if args.dry_run:
            return self._dry_run(self._root, scope_result, name, checked=self._manifest)

        return await self._verify(
            args, self._build, self._root, scope_result, self._budget_minutes(args), name, checked=self._manifest
        )

    def _budget_minutes(self, args: argparse.Namespace) -> float:
        """--timeout, else the manifest's check.max_wall_time_seconds, which is logged, else DEFAULT_TIMEOUT_MINUTES."""
        if args.timeout is not None:
            return args.timeout
        if self._manifest.max_wall_time_seconds is not None:
            minutes = self._manifest.max_wall_time_seconds / 60
            logger.info(f"Time budget: {minutes:g} min, from check.max_wall_time_seconds.")
            return minutes
        return DEFAULT_TIMEOUT_MINUTES

    def _later_hint(self, project: Project) -> str:
        return f"Report on it later by running the same command with --project {project.project_id}.\n"


SUMMARY_FETCH_SECONDS = 30
"""Longest fetching the run's summary may take before the reports go out without it."""


@contextlib.contextmanager
def _quietly() -> Generator[None, None, None]:
    """Hold back the CLI's notes and warnings, for work a run logs that --project repeats only to compare."""
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(level)


def _checkout_record(root: Path, scope_result: scope.ScopeResult, checked: manifest.Manifest) -> RunRecord:
    """The record of a run `aristotle ci` would start from ``root`` as it stands. With a block, of the archive a run
    would upload, quietly: its aristotle.f may be generated, its includes rewritten, and files an ignore rule holds back
    go up, so its upload isn't the checkout's files.

    Raises:
        runner.RunnerError: a file of the block couldn't be archived.
    """
    if scope_result.block is None:
        return RunRecord.of_checkout(root, scope_result.upload_files, checked)
    with tempfile.TemporaryDirectory() as temp_dir, _quietly():
        tarball_path = Path(temp_dir) / "upload.tar.gz"
        upload = runner.archive(root, scope_result, tarball_path)
        return RunRecord.of_upload(root, tarball_path, upload.uploaded, checked)


async def _refusal(
    project: Project,
    root: Path,
    scope_result: scope.ScopeResult,
    checked: manifest.Manifest,
    source: str = "--project",
) -> str | None:
    """Why ``source``, --project or ARISTOTLE_PROJECT, can't report ``project``'s run as the checkout at ``root``'s, by
    the record its prompt ends with; None when `aristotle ci` started it, and nothing else ran in the project, on the
    files this checkout would upload and the properties ``checked`` lists."""
    fresh = "start a fresh run with plain `aristotle ci`"
    if source == "ARISTOTLE_PROJECT":
        # Plain `aristotle ci` would read the variable again, and refuse the same run.
        fresh = f"unset ARISTOTLE_PROJECT and {fresh}"
    tasks, _ = await project.get_tasks(limit=2)
    if len(tasks) > 1:
        return (
            f"Project {project.project_id} has more than the one run `aristotle ci` starts, such as a follow-up, so its "
            f"results aren't only what that run checked. To check this checkout, {fresh}."
        )
    record = RunRecord.ending(await runner.fetch_prompt(tasks[0]) or "") if tasks else None
    if record is None:
        return (
            f"Project {project.project_id} wasn't started by `aristotle ci`, or was started by an older version that "
            f"doesn't record which files and manifest its run checks. To check this checkout, {fresh}."
        )
    here = _checkout_record(root, scope_result, checked)
    # The record shows `aristotle ci` started the run, so whoever collects it likely started it too; left going, a fresh
    # run would pay for both, and wait on it where an account runs one project at a time.
    going = (
        f" That run hasn't ended: if a fresh run replaces it, stop it first with `aristotle cancel {project.project_id}`."
        if tasks[0].status not in TERMINAL_STATUSES
        else ""
    )
    if record.files != here.files:
        started, now = (f"commit {commit[:12]}" if commit else "no commit" for commit in (record.commit, here.commit))
        if record.commit == here.commit:
            return (
                f"Project {project.project_id}'s run checks other files than this checkout's, though both are at "
                f"{now}: other uncommitted changes, other files --allow-untracked lets through, another --directory "
                "or --manifest, another folder to run from, or other line-ending or LFS settings can do that. Report "
                f"on that run from the files it started with, or {fresh} to check these.{going}"
            )
        way_on = (
            f"If it started from a clean checkout, check out {started} to report on it; to check this one, {fresh}."
            if record.commit
            else f"To check this checkout, {fresh}."
        )
        return (
            f"Project {project.project_id}'s run checks other files than this checkout's: it started at {started}, and "
            f"this checkout is at {now}. {way_on}{going}"
        )
    if record.manifest != here.manifest:
        return (
            f"{checked.path} doesn't list the properties project {project.project_id}'s run checks: an id, kind, "
            f"statement or assumption differs. Pass the manifest the run was started with, or {fresh}.{going}"
        )
    return None


_IN_GIT_FOLDER = "is inside a .git folder"
_OUTSIDE = "is outside the folder the step uploads"
_LEADS_OUTSIDE = "a link that leads outside the folder the step uploads"
_UNTRACKED = "isn't tracked by git"
_LEADS_UNTRACKED = "leads to a file git doesn't track"
_ESCAPES = "outside every folder --allow-untracked names"
_ANOTHER_PATH = "by another path"

SYSTEM_FOLDERS = ("/proc", "/sys", "/dev")
"""Folders of the system's own state, such as /proc/self/environ, whose files never go up from outside the folder
the step uploads."""


def tracks_root_filelist(root: Path) -> bool:
    """Whether git tracks a root ``aristotle.f`` in ``root``, ignore rules aside: its block brings files an ignore rule
    holds back. False when git can't say, for the scope to report why."""
    try:
        return root_filelist(scope.tracked_paths(root)) is not None
    except scope.ScopeError:
        return False


def root_filelist(tracked: set[str]) -> Path | None:
    """The root ``aristotle.f`` among the ``tracked`` paths, in any case, as spelled; the exact spelling first."""
    names = sorted(
        (t for t in tracked if t.lower() == verilog_bundle.FILELIST_NAME),
        key=lambda t: (t != verilog_bundle.FILELIST_NAME, t),
    )
    return Path(names[0]) if names else None


def _block_problem(
    root: Path, tracked: set[str], allowed: Sequence[AllowedFolder], path: Path
) -> tuple[str | None, AllowedFolder | None]:
    """Why ``path`` can't go up from ``root``, as _tracked_problem() says, unless git's not tracking it there is the
    only reason and it resolves into a folder of ``allowed``, which is then returned instead."""
    problem = _tracked_problem(root, tracked, path)
    if problem is None or not (problem in (_OUTSIDE, _UNTRACKED, _LEADS_UNTRACKED) or problem.endswith(_LEADS_OUTSIDE)):
        return problem, None
    real = os.path.normcase(os.path.realpath(path))
    if folder := next((f for f in allowed if _is_within(real, os.path.normcase(f.path))), None):
        if crossed := _crossing(os.path.realpath(path), folder):
            return f"is in {folder.given}, but {crossed[0]} is {crossed[1]}, {_ANOTHER_PATH}", None
        return None, folder
    named = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    if folder := next((f for f in allowed if _is_within(named, os.path.normcase(f.path))), None):
        return f"is in {folder.given}, but a link leads it to {os.path.realpath(path)}, {_ESCAPES}", None
    return problem, None


def _crossing(real: str, folder: AllowedFolder) -> tuple[str, str] | None:
    """The first folder from the file at ``real``, a resolved path, up to ``folder``, which is by identity a folder of
    _kept_out() reached by another path, such as a bind mount, with what that folder holds; None if none is."""
    top = os.path.normcase(folder.path)
    current = os.path.dirname(real)
    while _is_within(os.path.normcase(current), top):
        identity = _identity(current)
        if why := next((why for kept, why in folder.kept_out if kept == identity), None):
            return current, why
        if os.path.normcase(current) == top:
            break
        current = os.path.dirname(current)
    return None


def _allowed_lines(
    block: verilog_bundle.Bundle,
    members: list[tuple[verilog_bundle.Member, AllowedFolder]],
    filelists: list[tuple[Path, AllowedFolder]],
) -> str:
    """The lines naming each of ``block``'s ``members`` and ``filelists`` read that only --allow-untracked lets through,
    with where each is when that isn't its place in the upload, and the folder that lets it through; empty for none."""
    lines = []
    if members:
        width = max(len(m.archive) for m, _ in members)
        count = f"{len(members)} {'file' if len(members) == 1 else 'files'}"
        lines.append(
            f"Uploading {count} git doesn't track in the folder the step uploads, which --allow-untracked lets through:"
        )
        for member, folder in members:
            moved = block.is_outside(member) or block.is_linked_out(member)
            source = f" <- {os.path.realpath(member.path)}" if moved else ""
            lines.append(f"  {member.archive.ljust(width)}{source}  (--allow-untracked {folder.given})")
    if filelists:
        count = f"{len(filelists)} {'filelist' if len(filelists) == 1 else 'filelists'}"
        lines.append(
            f"Reading {count} git doesn't track in the folder the step uploads, which --allow-untracked lets through:"
        )
        lines += [f"  {filelist.shown(path)}  (--allow-untracked {folder.given})" for path, folder in filelists]
    return "\n".join(lines)


def _system_folder(path: str) -> str | None:
    """The folder of SYSTEM_FOLDERS that ``path``, absolute, after any number of leading slashes, is in; None outside
    them, and on Windows."""
    if os.name == "nt":
        return None
    path = "/" + path.lstrip("/")
    return next((folder for folder in SYSTEM_FOLDERS if _is_within(path, folder)), None)


def _namable(root: Path, path: Path) -> bool:
    """Whether a folder --allow-untracked can take could hold the file ``path`` resolves to: its folder is no folder of
    _kept_out(), nor above one."""
    folder = os.path.dirname(os.path.realpath(path))
    return not any(_holds(folder, kept) for kept, _ in _kept_out(root))


def _check_outside_paths(root: Path, block: verilog_bundle.Bundle, upload: set[str]) -> None:
    """Stops on a file of ``block`` from outside ``root`` that would go up at a path of ``upload``, the folder's files
    that go up, or where it or another of the block's files would need a file to be a folder, as submit does.

    Raises:
        verilog_bundle.BundleError: naming both.
    """
    outside = sorted(m.archive for m in block.members if block.is_outside(m))
    if clash := [path for path in outside if path in upload]:
        raise verilog_bundle.BundleError(
            f"{root} has a file at {clash[0]}, where a file its aristotle.f names from outside it would go up. Rename "
            "that file or its folder."
        )
    names = upload | {m.archive for m in block.members}
    folders: set[str] = set()
    for name in names:
        parent = posixpath.dirname(name)
        while parent and parent not in folders:
            folders.add(parent)
            parent = posixpath.dirname(parent)
    for path in outside:
        if file := next((str(p) for p in PurePosixPath(path).parents if str(p) in names), None):
            overlap = (file, path)
        elif path in folders:
            overlap = (path, min(name for name in names if name.startswith(f"{path}/")))
        else:
            continue
        raise verilog_bundle.BundleError(
            f"{root}'s upload can't hold both {overlap[0]} and {overlap[1]}, which needs {overlap[0]} to be a folder. "
            "Rename one of them or its folder."
        )


def _tracked_problem(root: Path, tracked: set[str], path: Path) -> str | None:
    """Why ``path``, or the file a link at it leads to, can't come from ``root`` in a step that sends only files git
    tracks there, as ``tracked`` lists them; None if it can. A .git folder, in any case, or a git directory under any
    name where it resolves, then a credential, then a folder of SYSTEM_FOLDERS outside ``root`` come first, so no
    advice leads to any, each judged as named and where it leads: below ``root``, or outside it, in full."""
    named, top = os.path.normpath(os.path.abspath(path)), os.path.normpath(os.path.abspath(root))
    real, real_top = os.path.realpath(path), os.path.realpath(root)
    named_in, real_in = _is_within(named, top), _is_within(real, real_top)
    inside = Path(os.path.relpath(named, top)) if named_in else Path(named)
    inside_real = Path(os.path.relpath(real, real_top)) if real_in else Path(real)
    if (
        local_file_utils.in_git_folder(inside)
        or local_file_utils.in_git_folder(inside_real)
        or local_file_utils.in_git_dir(Path(real))
    ):
        return _IN_GIT_FOLDER
    if local_file_utils.is_secret_file(inside):
        return "is named like a credential"
    if local_file_utils.is_in_secret_folder(inside, Path(top) if named_in else None):
        return "is in a folder that holds credentials"
    if local_file_utils.is_secret_file(inside_real):
        return "leads to a file named like a credential"
    if local_file_utils.is_in_secret_folder(inside_real, Path(real_top) if real_in else None):
        return "leads into a folder that holds credentials"
    if not named_in and (system := _system_folder(named)):
        return f"is in {system}"
    if not real_in and (system := _system_folder(real)):
        return f"leads into {system}"
    if not named_in:
        return _OUTSIDE
    if not real_in:
        link = _link_out(top, real_top, inside)
        return (
            f"is {_LEADS_OUTSIDE}"
            if link == Path(named)
            else f"is reached through {filelist.shown(link)}, {_LEADS_OUTSIDE}"
        )
    # By the file the bytes come from: git lists no path through a linked folder.
    if inside_real.as_posix() not in tracked:
        return _UNTRACKED if os.path.normcase(named) == os.path.normcase(real) else _LEADS_UNTRACKED
    return None


def _is_within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _link_out(top: str, real_top: str, inside: Path) -> Path:
    """The first path from ``top`` along ``inside`` that resolves outside ``real_top``: the link it leaves through."""
    for i in range(1, len(inside.parts) + 1):
        candidate = os.path.join(top, *inside.parts[:i])
        if not _is_within(os.path.realpath(candidate), real_top):
            return Path(candidate)
    return Path(top, inside)


async def _fetch_summary(project: Project) -> str | None:
    """The run's summary; None, with a warning, when it can't be fetched within SUMMARY_FETCH_SECONDS."""
    try:
        return await asyncio.wait_for(runner.fetch_summary(project), SUMMARY_FETCH_SECONDS)
    except asyncio.TimeoutError:
        reason = f"no answer in {SUMMARY_FETCH_SECONDS} s"
    except Exception as e:
        # The verdict is settled, so no failure here may change the exit code.
        reason = str(e) or type(e).__name__
        logger.debug("Fetching the run's summary failed", exc_info=True)
    logger.warning(f"Could not fetch the run's summary: {reason}")
    return None


_KIND_LANGUAGES = {manifest.Kind.SVA: "Verilog", manifest.Kind.C_FUNCTION_NO_UB: "C"}
"""The language each kind of manifest property is about; a natural-language property can be about any."""


async def _language_access(
    root: Path, scope_result: scope.ScopeResult, checked: manifest.Manifest | None
) -> dict[str, bool | None]:
    """Whether the account may verify each language Aristotle finds in the upload that the run is about: those of the
    files in scope, or of the manifest's kinds of property, when none is in natural language."""
    # With the block's files, which go up even when an ignore rule holds them back, or a link at one leads to one, read
    # from where they are: one from outside the folder isn't at its place in the upload.
    block = [m.path for m in scope_result.block.members] if scope_result.block is not None else []
    found = scope.gated_languages(root, scope_result.upload_files, block)
    if checked is None:
        about = {scope.GATED_EXTENSIONS.get(p.suffix.lower()) for p in scope_result.focus_files}
    elif all(p.kind in _KIND_LANGUAGES for p in checked.properties):
        about = {_KIND_LANGUAGES[p.kind] for p in checked.properties}
    else:
        about = set(found)
    return await runner.language_access([language for language in found if language in about])


def _summary_pointer(run_results: results.RunResults) -> str:
    """Where the run's summary is, before the run's URL: above, when the log printed it."""
    return "Its summary is above; the run is at" if run_results.run_summary else "See the run's summary at"


def _cut_short_error(run_results: results.RunResults, url: str, access: dict[str, bool | None]) -> str:
    """The error for a run that ran out of budget or time: why its results are unchecked, or why it has none."""
    timed_out = not run_results.out_of_budget
    note = f" {SLOT_WAIT_NOTE}" if timed_out else ""
    if not run_results.nothing_registered:
        advice = f" Raise --timeout.{note}" if timed_out else ""
        return f"{run_results.unchecked_reason} None of them counts as proved or refuted.{advice}"
    see = f"See how far it got at {url}" if timed_out else f"{_summary_pointer(run_results)} {url}"
    off, unknown = _language_causes(access)
    if not off and not unknown:
        advice = f", and raise --timeout if it needs longer.{note}" if timed_out else ""
        return f"{run_results.unchecked_reason} {see}{advice}"
    longer = ["the run needs longer than the time budget, so raise --timeout"] if timed_out else []
    return f"{run_results.unchecked_reason} {_causes_sentence(off + longer + unknown)}{note} {see}"


def _no_verdict(run_results: results.RunResults) -> str:
    """Which properties have no verdict, and where Aristotle may say why."""
    without = run_results.without_verdict
    whose = "the manifest's" if run_results.from_manifest else "the run's"
    ids = ", ".join(p.name for p in without)
    why = " Aristotle's summary of the run, above, may say why." if run_results.run_summary else ""
    return f"{len(without)} of {whose} {run_results.total} properties have no verdict: {ids}.{why}"


def _without_verdict_warning(run_results: results.RunResults) -> str:
    """Which manifest properties have no verdict in a run whose build passes under --fail-on failure, and how to fail
    it on them."""
    return (
        f"{_no_verdict(run_results)} The build still passes: --fail-on failure doesn't fail it on a property without "
        "a verdict, and --fail-on no-verdict does."
    )


def _not_registered_warning(run_results: results.RunResults) -> str:
    """Which manifest ids the run has no property under, and when it stopped, if it didn't finish."""
    missing = run_results.not_registered
    ids = f"id{'s' if len(missing) > 1 else ''} {', '.join(p.name for p in missing)}"
    them, they_are = ("them", "they are") if len(missing) > 1 else ("it", "it is")
    if stop := run_results.stop_clause:
        return (
            f"The run had no property under the manifest {ids} when {stop}, so {they_are} reported without a verdict."
        )
    return (
        f"Aristotle registered no property under the manifest {ids}, or withdrew {them}, "
        f"so {they_are} reported without a verdict."
    )


def _language_causes(access: dict[str, bool | None]) -> tuple[list[str], list[str]]:
    """Causes in the account's languages: those Aristotle says are not enabled, and those it can't say about."""
    off = [
        f"{language} is not enabled for this account, so Aristotle left it out"
        for language, on in access.items()
        if on is False
    ]
    unknown = [
        f"{language} may not be enabled for this account, which would leave it out"
        if language in runner.LANGUAGE_ACCESS_ENDPOINTS
        # Aristotle has no way to ask about Rust, so this cause is only possible, never confirmed.
        else f"{language} is left out if this account can't verify it, which Aristotle gives no way to check"
        for language, on in access.items()
        if on is None
    ]
    return off, unknown


def _likely_causes(run_results: results.RunResults, access: dict[str, bool | None]) -> list[str]:
    """Why a manifest run verified nothing, leaving out causes the results and the account's languages rule out."""
    registered = [p.declaration for p in run_results.properties if p.declaration is not None]
    off, unknown = _language_causes(access)
    causes = list(off)
    if run_results.not_registered:
        causes.append(f"Aristotle registered nothing under {'some of ' if registered else ''}the manifest's ids")
    if any(d.status != DeclarationStatus.FAILED for d in registered):
        causes.append("a property needs a decision only you can make, which its description names")
    if any(d.failure_reason == DeclarationFailureReason.OUT_OF_BUDGET for d in registered):
        causes.append("Aristotle ran out of budget first")
    if any(d.failure_reason == DeclarationFailureReason.PROOF_CHECK_FAILED for d in registered):
        causes.append("a proof Aristotle had no longer checks, so a re-run should re-prove it")
    named = (DeclarationFailureReason.OUT_OF_BUDGET, DeclarationFailureReason.PROOF_CHECK_FAILED)
    if any(d.status == DeclarationStatus.FAILED and d.failure_reason not in named for d in registered):
        causes.append("Aristotle stopped short of a verdict, for the reason the property's result names")
    return causes + unknown


def _causes_sentence(causes: list[str]) -> str:
    """``causes`` as a sentence: "Likely cause: a.", "Likely causes: a; or b.", "Likely causes: a; b; or c."."""
    if len(causes) == 1:
        return f"Likely cause: {causes[0]}."
    return f"Likely causes: {'; '.join(causes[:-1])}; or {causes[-1]}."


def _checkout_folders(build: context.CIContext, directory: Path | None) -> list[Path]:
    """The folders besides the one a run checks that a checkout's links can be in: the CI system's workspace, and the
    top level of the repository of the current folder and of ``directory`` as given."""
    starts = [Path(os.curdir), *([directory] if directory is not None else [])]
    tops = [top for start in starts if (top := scope.top_level(start)) is not None]
    return ([] if build.system == context.CISystem.LOCAL else [build.workspace]) + tops


def _guarded(what: str, reporter: Callable[..., object], *reporter_args: object) -> bool:
    """Run ``reporter``, logging a failure so a broken report cannot mask the verdict; a closed pipe still raises."""
    try:
        reporter(*reporter_args)
    except BrokenPipeError:
        raise
    except Exception as e:
        logger.warning(f"Could not write {what}: {e}")
        logger.debug(f"Writing {what} failed", exc_info=True)
        return False
    return True
