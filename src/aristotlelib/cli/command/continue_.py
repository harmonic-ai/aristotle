import argparse
import asyncio
import logging
import os
from pathlib import Path

from aristotlelib.agent_task import AgentTask
from aristotlelib.api_request import AristotleProjectLimitError, before_sending, quietly
from aristotlelib.ci.runner import describe_upload
from aristotlelib.cli.api_action import (
    PROJECT_REFERENCE_HELP,
    APIAction,
    add_prompt_arguments,
    carry_on_hint,
    fetch_properties_and_tasks,
    judge_properties,
    log_download_hint,
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
from aristotlelib.exit_codes import EXIT_INFRASTRUCTURE_ERROR
from aristotlelib.project import (
    PROMPT_NOT_SENT,
    AgentQuestionsSetting,
    Project,
    ProjectStatus,
    continuation_uploads,
    files_refused_while_running,
    validate_prompt,
)
from aristotlelib.question import Question
from aristotlelib.results import NO_SUMMARY, ContinueResult, PropertySummary

_FILES_NAMED = 10
"""How many of the files sent with a prompt continue names."""
QUESTIONS_HINT_SECONDS = 10
"""How long the hint after a prompt to a running task waits for that task's questions, retries included."""


class ContinueAction(APIAction):
    @property
    def command_name(self) -> str:
        return "continue"

    @property
    def description(self) -> str:
        return "Give an existing project another instruction."

    @classmethod
    def priority(cls) -> int:
        return 20

    def add_action_arguments(self) -> None:
        self.parser.add_argument(
            "project_id",
            type=str,
            help=f"The project to continue: {PROJECT_REFERENCE_HELP}",
        )
        add_prompt_arguments(self.parser, "Prompt for Aristotle")
        self.parser.add_argument(
            "--files",
            type=Path,
            nargs="*",
            help=(
                "Local files to upload when the project is idle, relative to the current directory or absolute. "
                "Their paths inside --project-dir are preserved."
            ),
        )
        self.parser.add_argument(
            "--project-dir",
            help=(
                "Root for uploaded paths; default: the current directory. Only --files are uploaded. "
                "Files outside this directory, also through links, and paths containing .. are refused."
            ),
        )
        self.parser.add_argument(
            "--wait",
            action="store_true",
            help="Wait for task completion and display events as they stream in",
        )
        self.parser.add_argument(
            "--json",
            action="store_true",
            help=(
                "Print the task and the project's properties as one JSON object. With --wait, print them when the "
                "wait ends (still running if it broke off), with no progress display and no prompts."
            ),
        )
        self.parser.add_argument(
            "--non-interactive",
            action="store_true",
            help=(
                "For scripts and agents: --wait and --json, but never prompt, and stop as soon as Aristotle asks "
                "something, printing the questions as JSON and exiting with code 10. Answer with `aristotle "
                "questions <project-id> --answer <question-id>=<answer>`, then carry on with `aristotle show "
                "<project-id> --non-interactive`."
            ),
        )
        self.parser.add_argument(
            "--no-questions",
            action="store_true",
            help=(
                "Don't let Aristotle ask questions in the task this prompt starts, for a batch run nobody will answer: "
                "work that needs a decision only you can make is then left unverified and named in the summary. By "
                "default it may ask, and waits up to 15 minutes for each answer. A running task keeps its setting."
            ),
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
        if args.files or args.project_dir is not None:
            try:
                continuation_uploads(args.files or [], project_dir=args.project_dir)
            except ValueError as e:
                self.parser.error(f"--files: {e}")
        return None

    async def run_action(self, args: argparse.Namespace) -> int | None:
        if args.non_interactive:
            args.wait = args.json = True
        uploads = continuation_uploads(args.files or [], project_dir=args.project_dir) if args.files else []
        with before_sending(PROMPT_NOT_SENT):
            args.project_id = await Project.resolve_id(args.project_id)
            project = await Project.from_id(args.project_id)
        if args.files and project.status != ProjectStatus.IDLE:
            self.parser.error(files_refused_while_running(args.project_id, "--files"))
        no_questions_ignored = args.no_questions and project.status != ProjectStatus.IDLE
        if no_questions_ignored:
            logging.warning(
                "WARNING: --no-questions applies only when the prompt starts a new task; the task running on "
                f"project {args.project_id} keeps the setting it started with."
            )
        agent_questions_setting = (
            AgentQuestionsSetting.DISABLED if args.no_questions else AgentQuestionsSetting.TIMEOUT_15_MIN
        )
        # Before the send, so that a file gone after it can't fail a prompt that was sent.
        size = sum(os.path.getsize(path) for path in args.files or [])
        try:
            task = await project.ask(
                args.prompt,
                files=args.files,
                agent_questions_setting=agent_questions_setting,
                project_dir=args.project_dir,
            )
        except AristotleProjectLimitError as e:
            raise refused_run(e, PROMPT_NOT_SENT) from e
        logging.info(f"Prompt submitted to project {args.project_id}, task {task.agent_task_id}")
        if args.files:
            logging.info(_uploaded([name for _, name in uploads], size))

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
            if judged is not None:
                summary, unjudged = judged
                print_json(_result(project, task, summary))
            else:
                log_property_counts(*await fetch_properties_and_tasks(args.project_id, task))
            if not task.live:
                await log_download_hint(args.project_id)
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
        project_id=task.project_id, project=project, task=task, web_url=project.web_url(), **summary._asdict()
    )
