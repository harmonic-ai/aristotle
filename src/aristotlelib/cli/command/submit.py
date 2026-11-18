import argparse
import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

from aristotlelib import local_file_utils
from aristotlelib.agent_task import follow_hint
from aristotlelib.api_request import (
    AristotleAPIError,
    AristotleOutcomeUnknownError,
    AristotleProjectLimitError,
    AristotleRequestClient,
)
from aristotlelib.ci.runner import describe_upload
from aristotlelib.cli.api_action import (
    NON_INTERACTIVE_HELP,
    APIAction,
    add_prompt_arguments,
    carry_on_hint,
    download_command,
    download_hint,
    fetch_properties_and_tasks,
    interrupted_save_hint,
    judge_properties,
    log_next_steps,
    log_unjudged,
    print_json,
    print_questions_pending,
    read_prompt,
    refused_run,
    resume_commands,
    task_exit_code,
    wait_command,
    with_latest_run,
)
from aristotlelib.cli.command.rename import project_name_argument
from aristotlelib.cli.command.show import log_property_counts
from aristotlelib.cli.login_flow import billed_to
from aristotlelib.exit_codes import EXIT_INFRASTRUCTURE_ERROR, EXIT_SUCCESS, EXIT_USAGE_ERROR
from aristotlelib.local_file_utils import LeanProjectError
from aristotlelib.project import (
    LEAN_FILES_WARNING,
    PROJECT_NOT_CREATED,
    AgentQuestionsSetting,
    Archived,
    ArchiveError,
    Project,
    build_archive,
    check_readable,
    check_withheld,
    create_sent,
    validate_destination,
    validate_project_dir,
    validate_prompt,
)
from aristotlelib.results import NO_SUMMARY, SubmitResult, summarize_properties
from aristotlelib.shell import shell_hint
from aristotlelib.verilog import bundle as verilog_bundle
from aristotlelib.verilog import filelist
from aristotlelib.verilog.filelist import FilelistError

PROMPT_ONLY_WARNING = (
    "WARNING: No --project-dir, -f or -F was given, so only the prompt goes up. Unless the prompt includes the code "
    "itself or asks Aristotle to design new hardware, the run ends without verifying anything. To verify your code, "
    "submit the folder that holds it:\n"
    '  aristotle submit "<prompt>" --project-dir <folder>'
)

PROMPT_ONLY_REMINDER = "WARNING: Only the prompt went up, since no --project-dir, -f or -F was given."


class _BlockOption(argparse.Action):
    """Keeps ``--top``, ``-f``, ``-F`` and ``--blackbox``, with their values, in the order given, as lines of
    ``aristotle.f``."""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, [*(getattr(namespace, self.dest) or []), str(option_string), str(values)])


def read_block(
    project_dir: Path | None,
    arguments: list[str],
    *,
    list_outside: bool = True,
    report: bool = True,
    blackboxes_only: bool = False,
) -> verilog_bundle.Bundle | None:
    """The Verilog block that ``project_dir``'s ``aristotle.f`` and then the command line's ``arguments`` describe,
    logged with ``report``, with each file from outside the folder when ``list_outside``, and its black boxes without
    ``project_dir``, whose files build_archive() reads before naming them; None with neither. With
    ``blackboxes_only`` and no ``arguments``, for a caller that takes only the black boxes, the block may name no
    source file.

    Raises:
        FilelistError: a filelist can't be read, or is a credential.
        verilog_bundle.BundleError: the block names no source file, unless ``blackboxes_only`` is given without
            ``arguments``, or it can't be uploaded as described.
        LeanProjectError: check_withheld() refuses the folder the block is read from.
    """
    committed = None if project_dir is None else verilog_bundle.folder_filelist(project_dir)
    if committed is None and not arguments:
        return None
    root = project_dir or Path.cwd()
    # The folder a block alone is read from too: its files go up by their names below it.
    check_withheld(root)
    reader = filelist.Reader(check=lambda named, real, origin: verilog_bundle.check_filelist(named, real, origin, root))
    if committed is not None:
        reader.read_file(committed)
    reader.read_arguments(arguments, Path.cwd())
    if any(t.origin.file is None for t in reader.result.tops):
        # A --top on the command line replaces the filelists' tops, wherever it stands among -f and -F.
        reader.result.ignored += [
            filelist.Ignored(f"--top {t.value}", t.origin, "the command line's --top replaces it")
            for t in reader.result.tops
            if t.origin.file is not None
        ]
        reader.result.tops = [t for t in reader.result.tops if t.origin.file is None]
    sources = (filelist.Kind.SOURCE, filelist.Kind.LIBRARY_FILE)
    if arguments and not any(e.kind in sources for e in reader.result.entries):
        message = "The Verilog block names no source file. Pass a filelist that names its sources with -f or -F"
        # The prompt stands in for --top and --blackbox only where no filelist is given.
        if committed is None and {"-f", "-F"}.isdisjoint(arguments[::2]):
            message += (
                ", or, with --project-dir, name the top module and any black box in the prompt instead, though a "
                "black box's model in the folder still goes up"
            )
        raise verilog_bundle.BundleError(f"{message}.")
    block = verilog_bundle.build(
        reader.result,
        root,
        committed=committed,
        whole_folder=project_dir is not None,
        arguments=bool(arguments),
        blackboxes_only=blackboxes_only,
    )
    if not report:
        return block
    notes, warnings = verilog_bundle.report(block, files=list_outside, blackboxes=project_dir is None)
    for note in notes:
        logging.info(note)
    for warning in warnings:
        logging.warning(f"WARNING: {warning}")
    return block


def _found_by_name(name: str) -> str:
    """The command that finds the project named ``name``, and how to tell it from an older project so named."""
    return (
        shell_hint("find it by its name with", ["aristotle", "show", "--", name])
        + "\nwhich shows how old the project's run is: one older than this command belongs to another project of that "
        "name."
    )


def _interrupted_create_hint(name: str | None) -> str:
    """What to do after an interrupt during the create, which Aristotle may carry out all the same once the rest of the
    upload arrives."""
    found = "." if name is None else f", or {_found_by_name(name)}"
    return (
        "Interrupted during the create: the project may have been created and its run started, though it may take a "
        f"few minutes to show up. Look in `aristotle list` for a project newer than this command{found} Cancel it "
        "with `aristotle cancel <id>`."
    )


def _unwaited(project: Project) -> SubmitResult:
    """The --json result of a wait that couldn't start: the new project, with no task, counts or properties."""
    return SubmitResult(project=project, task=None, web_url=project.web_url(), **NO_SUMMARY._asdict())


def _report_interrupt_after_create(project: Project, wait_hint: str | None, as_json: bool) -> None:
    """Say that an interrupt before the wait leaves the new project's run going, and how to follow it; print the
    project as _unwaited() under --json."""
    interrupted = "Interrupted" if wait_hint is None else "Interrupted before the wait started"
    logging.info(f"{interrupted}: the project's run goes on.\n" + follow_hint(project.project_id, carry_on=wait_hint))
    if as_json:
        print_json(_unwaited(project))


def block_prompt(prompt: str, block: verilog_bundle.Bundle | None) -> str:
    """``prompt``, pointing Aristotle at the block's ``aristotle.f`` when one goes up, and naming its black boxes, as
    ``ci`` does."""
    return prompt if block is None else f"{prompt}\n\n{verilog_bundle.prompt_lines(block)}\n"


def project_dir_argument(value: str) -> Path:
    """argparse type for --project-dir; rejects the empty string."""
    try:
        return validate_project_dir(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


class SubmitAction(APIAction):
    @property
    def command_name(self) -> str:
        return "submit"

    @property
    def description(self) -> str:
        return (
            "Submit code for Aristotle to verify, a folder with --project-dir or a Verilog block with -f or -F, and "
            "the property to prove in the prompt. Each run spends credits."
        )

    @classmethod
    def priority(cls) -> int:
        return 10

    def add_action_arguments(self) -> None:
        add_prompt_arguments(self.parser, "The property to prove")

        self.parser.add_argument(
            "--project-dir",
            type=project_dir_argument,
            help="The folder of Verilog, C, or Rust code to verify.",
        )

        self.parser.add_argument(
            "--include-ignored",
            action="store_true",
            help="Also upload the files that git ignores.",
        )

        self.parser.add_argument(
            "--name",
            type=project_name_argument,
            help="Project name (default: the folder, module or prompt).",
        )

        self.parser.add_argument(
            "--wait",
            action="store_true",
            help="Wait for the run to finish.",
        )

        self.parser.add_argument(
            "--destination",
            # Expanded here, as a shell would: --destination=~/out reaches argparse with the ~ unexpanded.
            type=os.path.expanduser,
            help="Where --wait saves the result archive.",
        )

        self.parser.add_argument(
            "--json",
            action="store_true",
            help="Print the project, its task and properties as JSON.",
        )

        self.parser.add_argument(
            "--non-interactive",
            action="store_true",
            help=NON_INTERACTIVE_HELP,
        )

        verilog = self.parser.add_argument_group(
            "Verilog block", f"Each option adds a line to the folder's {verilog_bundle.FILELIST_NAME}."
        )
        verilog.add_argument(
            "--top",
            action=_BlockOption,
            dest="block_arguments",
            metavar="MODULE",
            help="The block's top module.",
        )
        verilog.add_argument(
            "-f",
            action=_BlockOption,
            dest="block_arguments",
            metavar="FILELIST",
            help="A filelist, its paths relative to the current folder.",
        )
        verilog.add_argument(
            "-F",
            action=_BlockOption,
            dest="block_arguments",
            metavar="FILELIST",
            help="A filelist, its paths relative to its own folder.",
        )
        verilog.add_argument(
            "--blackbox",
            action=_BlockOption,
            dest="block_arguments",
            metavar="MODULE",
            help="A module for Aristotle to treat as a black box.",
        )

        self.parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would go up, and send nothing.",
        )

        self.parser.add_argument(
            "--no-questions",
            action="store_true",
            help="Don't let Aristotle ask questions.",
        )

    def preflight(self, args: argparse.Namespace) -> int | None:
        args.prompt = read_prompt(self.parser, args)
        try:
            validate_prompt(args.prompt)
        except ValueError as e:
            self.parser.error(str(e))
        if args.wait or args.non_interactive:
            if args.dry_run:
                self.parser.error("--dry-run sends nothing, so there is nothing to wait for.")
            try:
                validate_destination(args.destination or ".")
            except ValueError as e:
                self.parser.error(str(e) if args.destination else f"{e}. Name another place with --destination.")
        if args.project_dir:
            if not os.path.isdir(args.project_dir):
                self.parser.error(f"--project-dir must be a valid directory: {args.project_dir}")
        # os.path, not pathlib: before 3.14, Path.is_dir() raises for a prompt too long to be a path.
        elif os.path.isdir(args.prompt):
            self.parser.error(f"The prompt '{args.prompt}' is a directory. Did you mean to use --project-dir?")
        elif os.path.isfile(args.prompt):
            self.parser.error(f"The prompt '{args.prompt}' is a file. Did you mean to use --prompt-file?")
        elif args.include_ignored:
            self.parser.error("--include-ignored needs --project-dir, the folder whose ignored files it uploads.")
        if args.destination is not None and not (args.wait or args.non_interactive):
            logging.warning(
                "WARNING: --destination needs --wait or --non-interactive, so it is ignored, and 4.0 will refuse it. "
                "Once the run has finished, `aristotle download <project-id> --destination <path>` saves the result "
                "archive."
            )
        return None

    def run(self, args: argparse.Namespace) -> int:
        # A dry run only reads local files, so it must work without a key.
        self.local_only = args.dry_run
        return super().run(args)

    async def run_action(self, args: argparse.Namespace) -> int | None:
        if args.non_interactive:
            args.wait = args.json = True
        agent_questions_setting = (
            AgentQuestionsSetting.DISABLED if args.no_questions else AgentQuestionsSetting.TIMEOUT_15_MIN
        )

        block_arguments = args.block_arguments or []
        prompt_only = not (args.project_dir or block_arguments)
        if args.dry_run:
            return self._dry_run(args.prompt, args.project_dir, block_arguments, args.json, args.include_ignored)

        try:
            if not prompt_only:
                if args.project_dir:
                    # Before the block is read from it, as create_from_directory checks it before reading it.
                    check_readable(args.project_dir)
                block = read_block(args.project_dir, block_arguments)
                if args.project_dir:
                    project = await Project.create_from_directory(
                        prompt=block_prompt(args.prompt, block),
                        project_dir=args.project_dir,
                        agent_questions_setting=agent_questions_setting,
                        name=args.name,
                        block=block,
                        include_ignored=args.include_ignored,
                    )
                else:
                    assert block is not None
                    project = await _create_from_block(
                        block_prompt(args.prompt, block), block, agent_questions_setting, args.name
                    )
            else:
                logging.warning(PROMPT_ONLY_WARNING)
                project = await Project.create(
                    prompt=args.prompt,
                    agent_questions_setting=agent_questions_setting,
                    name=args.name,
                )
        except (LeanProjectError, FilelistError, verilog_bundle.BundleError) as e:
            logging.error(str(e))
            return EXIT_USAGE_ERROR
        except ArchiveError as e:
            logging.error(str(e))
            return EXIT_INFRASTRUCTURE_ERROR
        except AristotleOutcomeUnknownError as e:
            if args.name is None:
                raise
            raise AristotleOutcomeUnknownError(f"{e} Or {_found_by_name(args.name)}", e.status_code) from e
        except AristotleProjectLimitError as e:
            raise refused_run(e, PROJECT_NOT_CREATED) from e
        except (KeyboardInterrupt, asyncio.CancelledError):
            if create_sent.get():
                logging.warning(_interrupted_create_hint(args.name))
            else:
                logging.warning("Interrupted before the create was sent: no project was created.")
            raise

        project.log_created()
        if args.name is not None and project.description == args.name:
            logging.info(f"Project name: {project.description}")
        elif args.name is not None:
            # A server that doesn't take a name on create.
            try:
                await project.rename(args.name)
            except AristotleAPIError as e:
                logging.warning(
                    f"WARNING: Project {project.project_id} was created, but naming it failed: {e}\n"
                    + shell_hint("Name it with", ["aristotle", "rename", project.project_id, "--", args.name])
                )
            else:
                logging.info(f"Project name: {project.description}")
        wait_hint: str | None = None
        if args.wait:
            wait_hint = shell_hint(
                "Wait for it with",
                wait_command(
                    project.project_id, None, args.destination, non_interactive=args.non_interactive, json=args.json
                ),
            )
        try:
            async with AristotleRequestClient() as client:
                payer = await billed_to(client)
        except (KeyboardInterrupt, asyncio.CancelledError):
            _report_interrupt_after_create(project, wait_hint, args.json)
            raise
        if payer is not None:
            stop = "" if payer.endswith((".", "!", "?")) else "."
            logging.info(f"This run is billed to {payer}{stop}")
        if args.wait:
            listing_error: AristotleAPIError | None = None
            try:
                tasks, _ = await project.get_tasks(limit=1)
            except AristotleAPIError as e:
                tasks, listing_error = [], e
            except (KeyboardInterrupt, asyncio.CancelledError):
                _report_interrupt_after_create(project, wait_hint, args.json)
                raise
            if not tasks:
                if listing_error is None:
                    why = "Aristotle listed no task for it, so the wait couldn't start."
                else:
                    why = f"listing its task failed, so the wait couldn't start: {listing_error}"
                logging.error(f"Project {project.project_id} was created, but {why}\n{wait_hint}")
                if args.json:
                    print_json(_unwaited(project))
                return EXIT_INFRASTRUCTURE_ERROR
            task = tasks[0]
            judged = None
            result_path = None
            save_failed = False
            # Whether an interrupt now would leave the finished task's files unsaved.
            unsaved = False
            try:
                questions = await task.wait_for_completion(
                    interactive=not args.json,
                    stop_on_questions=args.non_interactive,
                    title=project.description,
                    carry_on=carry_on_hint(
                        task, args.destination, non_interactive=args.non_interactive, json=args.json
                    ),
                    resume=resume_commands(
                        task, args.destination, non_interactive=args.non_interactive, json=args.json
                    ),
                )
                if not questions:
                    unsaved = not task.live
                    properties = None
                    if not args.json:
                        properties, judged_by = await fetch_properties_and_tasks(project.project_id, task)
                        log_property_counts(properties, judged_by)
                    refresh_error = None
                    # Should the refresh fail, the result's project still says how its run, the task, ended.
                    with_latest_run(project, task)
                    try:
                        await project.refresh()
                    except AristotleAPIError as e:
                        refresh_error = e
                    if task.live:
                        # The wait ended early; the project's files are a snapshot of a run still in progress.
                        logging.info(
                            "Nothing was downloaded, since the task has not finished. "
                            + shell_hint(
                                "Once it has, get its files with",
                                download_command(project.project_id, args.destination),
                            )
                        )
                    elif refresh_error is not None:
                        logging.error(
                            f"The project's files were not saved, since the project could not be refreshed: {refresh_error}\n"
                            + download_hint(project.project_id, args.destination)
                        )
                        save_failed = True
                    elif project.has_files:
                        try:
                            result_path = await project.get_files(destination=args.destination)
                        except (OSError, AristotleAPIError) as e:
                            logging.error(
                                f"The project's files were not saved: {e}\n"
                                + download_hint(project.project_id, args.destination)
                            )
                            save_failed = True
                        else:
                            logging.info(f"Saved the project's files to {result_path}")
                    unsaved = False
                    # Fetched before anything is printed, so the JSON is printed once however the command is cut short.
                    if args.json:
                        judged = await judge_properties(project.project_id, task)
                    elif not task.live:
                        log_next_steps(task, bool(properties))
            except (KeyboardInterrupt, asyncio.CancelledError):
                if unsaved:
                    logging.warning(interrupted_save_hint(project.project_id, args.destination))
                if args.json:
                    # Cut short, even in the download, it still says where the run stands; the counts go unfetched.
                    print_json(
                        SubmitResult(
                            project=with_latest_run(project, task),
                            task=task,
                            web_url=project.web_url(),
                            result_path=None if result_path is None else str(result_path),
                            **NO_SUMMARY._asdict(),
                        )
                    )
                raise
            finally:
                if prompt_only and not args.json:
                    # However the wait ends: the warning came before it, and its last frame and closing lines can push
                    # the warning off screen.
                    logging.warning(PROMPT_ONLY_REMINDER)
            if questions:
                return print_questions_pending(task, questions, args.destination)
            if judged is not None:
                summary, unjudged = judged
                print_json(
                    SubmitResult(
                        project=project,
                        task=task,
                        web_url=project.web_url(),
                        result_path=None if result_path is None else str(result_path),
                        **summary._asdict(),
                    )
                )
                if unjudged is not None:
                    # The run was created: 3, not the exit code of a refusal to retry, which would run it again.
                    log_unjudged(project.project_id, unjudged)
                    return EXIT_INFRASTRUCTURE_ERROR
            return EXIT_INFRASTRUCTURE_ERROR if save_failed else task_exit_code(task.status)
        if not args.no_questions:
            logging.info(f"If Aristotle asks anything, answer with:\n  aristotle questions {project.project_id}")
        if args.json:
            try:
                tasks, _ = await project.get_tasks(limit=1)
            except AristotleAPIError as e:
                # The project exists; failing here would invite a second submit.
                logging.warning(f"WARNING: Project {project.project_id} was created, but listing its task failed: {e}")
                tasks = []
            summary = summarize_properties(
                *await fetch_properties_and_tasks(project.project_id, tasks[0] if tasks else None)
            )
            if not tasks:
                # Just submitted: no count stands, even when the task couldn't be listed.
                summary = NO_SUMMARY._replace(properties=summary.properties)
            print_json(
                SubmitResult(
                    project=project, task=tasks[0] if tasks else None, web_url=project.web_url(), **summary._asdict()
                )
            )
        return None

    def _dry_run(
        self, prompt: str, project_dir: Path | None, block_arguments: list[str], as_json: bool, include_ignored: bool
    ) -> int:
        """Archive what submit would upload, to a temporary file, and say what it holds; nothing is sent."""
        if project_dir is None and not block_arguments:
            self.parser.error("--dry-run needs --project-dir, -f or -F: without them only the prompt is sent.")
        try:
            # In a run's order, so that both stop on the same problem first: create_from_directory's checks come after
            # the block is read, check_readable's before it too.
            if project_dir is not None:
                check_readable(project_dir)
            block = read_block(project_dir, block_arguments, list_outside=False)
            if project_dir is not None:
                local_file_utils.check_lake_manifest_references_local_deps(project_dir)
            with tempfile.TemporaryDirectory() as temp_dir:
                archive = Path(temp_dir) / "project.tar.gz"
                archived = build_archive(project_dir, archive, block, include_ignored=include_ignored)
                size = archive.stat().st_size
        except (LeanProjectError, FilelistError, verilog_bundle.BundleError) as e:
            logging.error(str(e))
            return EXIT_USAGE_ERROR
        except ArchiveError as e:
            logging.error(str(e))
            return EXIT_INFRASTRUCTURE_ERROR
        if archived.holds_lean():
            logging.warning(LEAN_FILES_WARNING)
        if block is not None:
            logging.info(verilog_bundle.listing(block))
        report_dry_run(archived, size, block_prompt(prompt, block), block, as_json)
        return EXIT_SUCCESS


def report_dry_run(
    archived: Archived,
    size: int,
    prompt: str,
    block: verilog_bundle.Bundle | None,
    as_json: bool,
    note: str | None = None,
) -> None:
    """Log the folder's files a dry run archived, ``size`` bytes compressed, apart from ``block``'s, those an ignore rule
    left out, ``note``, the ``prompt`` that would be sent and the upload's size; with ``as_json``, print them as one
    JSON object."""
    block_names = {m.archive for m in block.members} | {block.filelist_name} if block is not None else set()
    if others := sorted(n for n in archived.names if n not in block_names):
        other = " other" if block is not None else ""
        added = set(archived.added)
        logging.info(
            f"The folder's {len(others)}{other} {'file that goes' if len(others) == 1 else 'files that go'} up:"
            + "".join(f"\n  {name}" + ("  (--include-ignored)" if name in added else "") for name in others)
        )
    ignored = len(archived.left_out)
    if ignored:
        logging.info(
            f"Left out, since an ignore rule matches {'it' if ignored == 1 else 'them'}:"
            + "".join(f"\n  {local_file_utils.archive_name(p)}" for p in archived.left_out)
        )
    if note is not None:
        logging.info(note)
    logging.info("The prompt, as it would be sent:\n" + "\n".join(f"  {line}" for line in prompt.splitlines()))
    counts = local_file_utils.ignore_counts(ignored, len(archived.included))
    logging.info(f"Dry run: would upload {describe_upload(len(archived.names), size)}{counts}.")
    if as_json:
        result = {
            "files": len(archived.names),
            "size_bytes": size,
            "paths": sorted(archived.names),
            "left_out": [local_file_utils.archive_name(p) for p in archived.left_out],
            "prompt": prompt,
            "block": None if block is None else verilog_bundle.summary(block),
        }
        print(json.dumps(result, indent=2))


async def _create_from_block(
    prompt: str, block: verilog_bundle.Bundle, agent_questions_setting: AgentQuestionsSetting, name: str | None
) -> Project:
    """Create a project named ``name``, if given, whose upload is ``block``'s files and its ``aristotle.f``, and
    nothing else.

    Raises:
        LeanProjectError: the archive is over the limit.
        verilog_bundle.BundleError: a file of the block changed after it was read.
    """
    validate_prompt(prompt)
    tops = block.filelist.tops
    with tempfile.TemporaryDirectory() as temp_dir:
        archive = Path(temp_dir) / "project.tar.gz"
        logging.info(build_archive(None, archive, block).summary())
        return await Project.create(
            prompt=prompt,
            tar_file_path=archive,
            public_file_path=f"{tops[0].value if tops else 'block'}.tar.gz",
            agent_questions_setting=agent_questions_setting,
            name=name,
        )
