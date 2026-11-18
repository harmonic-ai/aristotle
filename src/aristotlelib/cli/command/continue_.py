import argparse
import asyncio
import logging
import os
import tempfile
from pathlib import Path

from aristotlelib import local_file_utils
from aristotlelib.agent_task import AgentTask
from aristotlelib.api_request import AristotleProjectLimitError, before_sending, quietly
from aristotlelib.ci.runner import describe_upload
from aristotlelib.cli.api_action import (
    NON_INTERACTIVE_HELP,
    PROJECT_REFERENCE_HELP,
    APIAction,
    add_prompt_arguments,
    carry_on_hint,
    fetch_properties_and_tasks,
    judge_properties,
    log_download_hint,
    log_next_steps,
    log_unjudged,
    print_json,
    print_questions_pending,
    read_prompt,
    refresh_quietly,
    refused_run,
    resume_commands,
    task_exit_code,
    with_latest_run,
)
from aristotlelib.cli.command.show import log_property_counts
from aristotlelib.cli.command.submit import read_block, report_dry_run
from aristotlelib.exit_codes import EXIT_INFRASTRUCTURE_ERROR, EXIT_SUCCESS, EXIT_USAGE_ERROR
from aristotlelib.local_file_utils import LeanProjectError
from aristotlelib.project import (
    PROMPT_NOT_SENT,
    AgentQuestionsSetting,
    ArchiveError,
    FolderNotAppliedError,
    Project,
    ProjectStatus,
    build_archive,
    check_readable,
    continuation_uploads,
    filelist_note,
    files_refused_while_running,
    validate_project_dir,
    validate_prompt,
)
from aristotlelib.question import Question
from aristotlelib.results import NO_SUMMARY, ContinueResult, PropertySummary
from aristotlelib.verilog import bundle as verilog_bundle
from aristotlelib.verilog.filelist import FilelistError

_FILES_NAMED = 10
"""How many of the files sent with a prompt continue names."""
QUESTIONS_HINT_SECONDS = 10
"""How long the hint after a prompt to a running task waits for that task's questions, retries included."""
REPLACES = (
    "Each file that goes up replaces the project's file at the same path, even one Aristotle has edited since, or is "
    "added; the project's other files stay."
)
"""What a folder sent with a prompt does to the project's files, as --dry-run says it."""


class ContinueAction(APIAction):
    @property
    def command_name(self) -> str:
        return "continue"

    @property
    def description(self) -> str:
        return (
            "Give a project another instruction. A running task picks it up; on a finished project it starts a new "
            "run, which spends credits. To read a result, use show, properties or download instead."
        )

    @classmethod
    def priority(cls) -> int:
        return 20

    def add_action_arguments(self) -> None:
        self.parser.add_argument(
            "project_id",
            type=str,
            help=f"The project: {PROJECT_REFERENCE_HELP}",
        )
        add_prompt_arguments(self.parser, "Prompt for Aristotle")
        self.parser.add_argument(
            "--files",
            type=Path,
            nargs="*",
            help="Local files to upload when the project is idle.",
        )
        self.parser.add_argument(
            "--project-dir",
            help="The root of --files' paths, or the folder to send.",
        )
        self.parser.add_argument(
            "--send-folder",
            action="store_true",
            help="Upload the whole --project-dir folder with the prompt.",
        )
        self.parser.add_argument(
            "--include-ignored",
            action="store_true",
            help="With --send-folder, also upload files git ignores.",
        )
        self.parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what --send-folder would upload; send nothing.",
        )
        self.parser.add_argument(
            "--wait",
            action="store_true",
            help="Wait for the task to finish, showing its progress.",
        )
        self.parser.add_argument(
            "--json",
            action="store_true",
            help="Print the task and the project's properties as JSON.",
        )
        self.parser.add_argument(
            "--non-interactive",
            action="store_true",
            help=NON_INTERACTIVE_HELP,
        )
        self.parser.add_argument(
            "--no-questions",
            action="store_true",
            help="Don't let Aristotle ask questions in a new task.",
        )

    def preflight(self, args: argparse.Namespace) -> int | None:
        if args.prompt is None and args.prompt_file is None and args.files and not os.path.isfile(args.files[-1]):
            self.parser.error(
                f"--files took '{args.files[-1]}' as a file, so no prompt was given. Give the prompt before --files, "
                "which takes every argument after it."
            )
        args.prompt = read_prompt(self.parser, args)
        try:
            validate_prompt(args.prompt)
        except ValueError as e:
            self.parser.error(str(e))
        if args.send_folder:
            if args.project_dir is None:
                self.parser.error("--send-folder needs --project-dir, the folder to send.")
            if args.files is not None:
                self.parser.error("--send-folder sends the whole folder, so it takes no --files.")
            try:
                args.project_dir = validate_project_dir(args.project_dir)
            except ValueError as e:
                self.parser.error(f"--project-dir: {e}")
            if not os.path.isdir(args.project_dir):
                self.parser.error(f"--project-dir must be a valid directory: {args.project_dir}")
        elif args.include_ignored:
            self.parser.error(
                "--include-ignored needs --send-folder, which sends the folder whose ignored files it uploads."
            )
        elif args.dry_run:
            self.parser.error("--dry-run needs --send-folder, which sends the folder whose files it lists.")
        elif args.project_dir is not None and args.files is None:
            self.parser.error(
                "--project-dir needs --send-folder, to send the whole folder, or --files, to send some of its files. "
                "To send the prompt alone, drop --project-dir."
            )
        if args.dry_run and (args.wait or args.non_interactive):
            self.parser.error("--dry-run sends nothing, so there is nothing to wait for.")
        if not args.send_folder and (args.files or args.project_dir is not None):
            try:
                continuation_uploads(args.files or [], project_dir=args.project_dir)
            except ValueError as e:
                self.parser.error(f"--files: {e}")
        return None

    def run(self, args: argparse.Namespace) -> int:
        # A dry run only reads local files, so it must work without a key.
        self.local_only = args.dry_run
        return super().run(args)

    async def run_action(self, args: argparse.Namespace) -> int | None:
        if args.dry_run:
            return self._dry_run(args)
        if args.non_interactive:
            args.wait = args.json = True
        uploads = continuation_uploads(args.files, project_dir=args.project_dir) if args.files else []
        with before_sending(PROMPT_NOT_SENT):
            args.project_id = await Project.resolve_id(args.project_id)
            project = await Project.from_id(args.project_id)
        folder = args.project_dir if args.send_folder else None
        if args.files:
            sending = "--files"
        else:
            sending = "--send-folder" if folder is not None else ""
        if sending and project.status != ProjectStatus.IDLE:
            self.parser.error(files_refused_while_running(args.project_id, sending, without=_left_out(sending, args)))
        no_questions_ignored = args.no_questions and project.status != ProjectStatus.IDLE
        if no_questions_ignored:
            logging.warning(
                "WARNING: --no-questions applies only when the prompt starts a new task; the task running on "
                f"project {args.project_id} keeps the setting it started with."
            )
        agent_questions_setting = (
            AgentQuestionsSetting.DISABLED if args.no_questions else AgentQuestionsSetting.TIMEOUT_15_MIN
        )
        block = None
        if folder is not None:
            try:
                # submit's checks and reader, for the black boxes whose files stay out of the upload.
                check_readable(folder)
                block = read_block(folder, [], report=False, blackboxes_only=True)
            except (LeanProjectError, FilelistError, verilog_bundle.BundleError) as e:
                logging.error(str(e))
                return EXIT_USAGE_ERROR
        # Before the send, so that a file gone after it can't fail a prompt that was sent.
        size = sum(os.path.getsize(path) for path in args.files or [])
        not_applied = None
        try:
            task = await project.ask(
                args.prompt,
                files=args.files,
                agent_questions_setting=agent_questions_setting,
                project_dir=args.project_dir,
                send_folder=args.send_folder,
                include_ignored=args.include_ignored,
                block=block,
            )
        except AristotleProjectLimitError as e:
            raise refused_run(e, PROMPT_NOT_SENT) from e
        except (LeanProjectError, verilog_bundle.BundleError) as e:
            logging.error(str(e))
            return EXIT_USAGE_ERROR
        except ArchiveError as e:
            logging.error(str(e))
            return EXIT_INFRASTRUCTURE_ERROR
        except FolderNotAppliedError as e:
            task, not_applied = e.task, e
        except ValueError:
            # Project.ask's refusal when its refresh finds a task started since the check above; else a real error.
            if not sending or project.status == ProjectStatus.IDLE:
                raise
            self.parser.error(files_refused_while_running(args.project_id, sending, without=_left_out(sending, args)))
        logging.info(f"Prompt submitted to project {args.project_id}, task {task.agent_task_id}")
        if args.files:
            logging.info(_uploaded([name for _, name in uploads], size))
        if not_applied is not None:
            # The prompt was sent: no wait, and 3, not the exit code of a refusal to retry, which would send it again.
            logging.error(str(not_applied))
            if args.json:
                summary, _ = await judge_properties(task.project_id, task)
                await refresh_quietly(project, task)
                print_json(_result(project, task, summary))
            return EXIT_INFRASTRUCTURE_ERROR
        if task.files_applied is not None:
            count = task.files_applied
            logging.info(f"Aristotle applied {count} {'file' if count == 1 else 'files'} from {folder} to the project.")

        if args.wait:
            try:
                questions = await task.wait_for_completion(
                    interactive=not args.json,
                    stop_on_questions=args.non_interactive,
                    title=project.description,
                    carry_on=carry_on_hint(task, None, non_interactive=args.non_interactive, json=args.json),
                    resume=resume_commands(task, None, non_interactive=args.non_interactive, json=args.json),
                )
                # Fetched before anything is printed, so the JSON is printed once however the command is cut short.
                judged = await judge_properties(task.project_id, task) if args.json and not questions else None
                if judged is not None:
                    # It ran while the wait went on; the result says how it stands now.
                    await refresh_quietly(project, task)
            except (KeyboardInterrupt, asyncio.CancelledError):
                if args.json:
                    # Cut short, it still says where the task, the project's newest, stands; the counts go unfetched.
                    print_json(_result(with_latest_run(project, task), task, NO_SUMMARY))
                raise
            if questions:
                return print_questions_pending(task, questions)
            unjudged = None
            properties = None
            if judged is not None:
                summary, unjudged = judged
                print_json(_result(project, task, summary))
            else:
                properties, judged_by = await fetch_properties_and_tasks(args.project_id, task)
                log_property_counts(properties, judged_by)
            if not task.live:
                await log_download_hint(args.project_id)
                if not args.json:
                    log_next_steps(task, bool(properties))
            if unjudged is not None:
                # The prompt was sent: 3, not the exit code of a refusal to retry, which would send it again.
                log_unjudged(task.project_id, unjudged)
                return EXIT_INFRASTRUCTURE_ERROR
            return task_exit_code(task.status)
        if not args.no_questions or no_questions_ignored:
            logging.info(await _answer_hint(args.project_id, task, running=project.status == ProjectStatus.RUNNING))
        if args.json:
            summary, unjudged = await judge_properties(task.project_id, task)
            # It stood idle or running when the prompt went in, which may have started a task since.
            await refresh_quietly(project, task)
            print_json(_result(project, task, summary))
            if unjudged is not None:
                # The follow-up was sent: a failing exit code would invite sending it again.
                log_unjudged(task.project_id, unjudged, level=logging.WARNING)
        return None

    def _dry_run(self, args: argparse.Namespace) -> int:
        """Archive the folder continue would send with the prompt, to a temporary file, and say what it holds; nothing
        is sent."""
        try:
            # In a send's order: the folder's block, then Project.ask's checks and its archive.
            check_readable(args.project_dir)
            block = read_block(args.project_dir, [], report=False, blackboxes_only=True)
            local_file_utils.check_lake_manifest_references_local_deps(args.project_dir)
            with tempfile.TemporaryDirectory() as temp_dir:
                archive = Path(temp_dir) / "project.tar.gz"
                archived = build_archive(
                    args.project_dir, archive, block, include_ignored=args.include_ignored, follow_up=True
                )
                size = archive.stat().st_size
        except (LeanProjectError, FilelistError, verilog_bundle.BundleError) as e:
            logging.error(str(e))
            return EXIT_USAGE_ERROR
        except ArchiveError as e:
            logging.error(str(e))
            return EXIT_INFRASTRUCTURE_ERROR
        note = "\n".join(line for line in (REPLACES, filelist_note(archived, block)) if line is not None)
        report_dry_run(archived, size, args.prompt, None, args.json, note=note)
        return EXIT_SUCCESS


async def _answer_hint(project_id: str, task: AgentTask, *, running: bool) -> str:
    """How to answer Aristotle: that it is waiting for answers when the prompt went to ``task`` as a ``running``
    project's live task, and it has pending questions, else what to do if it asks. A listing that fails or outlasts
    QUESTIONS_HINT_SECONDS gives the latter; it and its retries are debug-logged."""
    if running and task.live:
        try:
            with quietly(lambda: True):
                questions = await asyncio.wait_for(
                    Question.list_for_task(task.agent_task_id, run_live=True), QUESTIONS_HINT_SECONDS
                )
        except Exception as e:
            logging.debug(f"Questions unavailable: {e!r}")
            questions = []
        if any(question.pending for question in questions):
            return f"Aristotle is waiting for answers. Answer with:\n  aristotle questions {project_id}"
    return f"If Aristotle asks anything, answer with:\n  aristotle questions {project_id}"


def _left_out(sending: str, args: argparse.Namespace) -> str:
    """The options to leave out to send the prompt alone to a running task: ``sending``, plus --include-ignored and
    --project-dir where given, since preflight refuses those without it."""
    if args.include_ignored:
        # --include-ignored needs --send-folder, which needs --project-dir, so both are set.
        return f"{sending}, --include-ignored and --project-dir"
    if args.project_dir is not None:
        return f"{sending} and --project-dir"
    return sending


def _uploaded(files: list[str], size: int) -> str:
    """The ``files`` sent with a prompt, ``size`` bytes in all: their count and size, and the names the first
    _FILES_NAMED went up under."""
    lines = [f"Uploaded {describe_upload(len(files), size)} with the prompt:"]
    lines += [f"  {name}" for name in files[:_FILES_NAMED]]
    if len(files) > _FILES_NAMED:
        lines.append(f"  and {len(files) - _FILES_NAMED} more")
    return "\n".join(lines)


def _result(project: Project, task: AgentTask, summary: PropertySummary) -> ContinueResult:
    return ContinueResult(
        project_id=task.project_id,
        project=project,
        task=task,
        web_url=project.web_url(),
        files_applied=task.files_applied,
        **summary._asdict(),
    )
