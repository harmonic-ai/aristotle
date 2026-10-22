import argparse
import asyncio
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from aristotlelib import progress_utils
from aristotlelib.agent_task import ENDED_BADLY, AgentTask, TaskStatus, ending_notice
from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.api_action import (
    PROJECT_REFERENCE_HELP,
    APIAction,
    carry_on_hint,
    download_hint,
    fetch_properties,
    fetch_properties_and_tasks,
    fetch_tasks,
    interrupted_save_hint,
    judge_properties,
    log_download_hint,
    log_unjudged,
    print_json,
    print_questions_pending,
    raise_for_pagination_key,
    refresh_quietly,
    resume_commands,
    task_exit_code,
    with_latest_run,
    with_pagination_key_hint,
)
from aristotlelib.declaration import Declaration, DeclarationFailureReason, DeclarationStatus
from aristotlelib.event import Event
from aristotlelib.exit_codes import EXIT_INFRASTRUCTURE_ERROR, EXIT_NOT_FOUND
from aristotlelib.project import Project, project_web_url, same_project_id, validate_destination
from aristotlelib.results import (
    NO_SUMMARY,
    PropertyCounts,
    PropertySummary,
    TaskSnapshot,
    requested,
    summarize_properties,
)


def format_property_counts(declarations: list[Declaration], tasks: Sequence[AgentTask] = ()) -> str:
    """Count properties by outcome, with those the project's ``tasks`` show Aristotle never checked apart;
    counterexamples, unresolved properties and unchecked verdicts appear only when nonzero, as on the web."""
    counts = PropertyCounts.from_declarations(declarations, tasks)
    line = f"{counts.proved} proved · {counts.disproved} disproved"
    if counts.counterexample:
        line += f" ({counts.counterexample} by counterexample)"
    line += f" · {counts.in_progress} in progress"
    if counts.unresolved:
        line += f" · {counts.unresolved} unresolved"
    if counts.unchecked:
        line += f" · {counts.unchecked} unchecked"
    return line


def broken_proofs_note(declarations: list[Declaration]) -> str | None:
    """How many properties had a proof that no longer checks, and what to do; None when none did."""
    count = sum(d.failure_reason == DeclarationFailureReason.PROOF_CHECK_FAILED for d in declarations)
    if count == 1:
        return "The proof of 1 property no longer checks, so it isn't verified; re-run to re-prove it."
    if count:
        return f"The proofs of {count} properties no longer check, so they aren't verified; re-run to re-prove them."
    return None


def log_property_counts(properties: list[Declaration] | None, tasks: Sequence[AgentTask]) -> None:
    """Log the counts line, as format_property_counts gives it for the project's ``tasks``, then the same for the
    properties you asked for when Aristotle suggested others too, and the broken-proofs note; nothing when the
    properties are unknown or there are none."""
    # A project without properties (most Lean projects) gets no line, as on the web.
    if properties:
        logging.info(f"Properties: {format_property_counts(properties, tasks)}")
        listed = [d for d in properties if d.status != DeclarationStatus.REMOVED]
        # As --json's requested_counts, which a script that named its properties gates on.
        if 0 < len(asked := requested(listed)) < len(listed):
            logging.info(f"You asked for {len(asked)}: {format_property_counts(asked, tasks)}")
        if note := broken_proofs_note(properties):
            logging.info(note)


def _snapshot(
    project_id: str,
    project: Project | None,
    task: AgentTask | None,
    events: list[Event],
    summary: PropertySummary,
    result_path: Path | None = None,
) -> TaskSnapshot:
    """The --json result: ``task`` of the project, with its events and properties, and where its files were saved, if
    they were; ``project`` is None when it could not be fetched."""
    return TaskSnapshot(
        project_id=project_id,
        project=project,
        task=task,
        web_url=project_web_url(project_id),
        events=events,
        result_path=None if result_path is None else str(result_path),
        **summary._asdict(),
    )


def _cut_short(project: Project | None, task: AgentTask, args: argparse.Namespace, was_live: bool) -> Project | None:
    """The project for a result a Ctrl-C cuts short, with no time to refresh it: its run as ``task``, the latest, has
    it, unless --task asked for one that may be older. One that was live when fetched is the latest still, since a
    project runs one task at a time and a prompt steers the one running."""
    if project is None or (args.task_id is not None and not was_live):
        return project
    return with_latest_run(project, task)


async def _latest_task(project_id: str, project: Project | None = None) -> AgentTask | None:
    """The project's most recent task, whose end its properties stand at, listed from ``project`` where it was
    fetched; None if it lists none.

    Raises:
        Exception: as fetching the project or its tasks does.
    """
    tasks, _ = await (project or await Project.from_id(project_id)).get_tasks(limit=1)
    return tasks[0] if tasks else None


class ShowAction(APIAction):
    @property
    def command_name(self) -> str:
        return "show"

    @property
    def description(self) -> str:
        return (
            "Show task status and recent events. At a terminal, follow a running task until it ends; piped, or with "
            "--json, print it as it stands, unless --wait or --non-interactive is given."
        )

    @classmethod
    def priority(cls) -> int:
        return 60

    def add_action_arguments(self) -> None:
        self.parser.add_argument(
            "project_id",
            type=str,
            help=f"The project to show: {PROJECT_REFERENCE_HELP}",
        )
        self.parser.add_argument(
            "--task",
            type=str,
            dest="task_id",
            help="Show a specific task instead of the most recent one",
        )
        self.parser.add_argument(
            "--limit",
            type=int,
            help="Number of events to show (default: 3 while running, 0 while idle)",
        )
        self.parser.add_argument(
            "--pagination-key",
            type=str,
            help="Pagination key for events",
        )
        self.parser.add_argument(
            "--json",
            action="store_true",
            help=(
                "Print the task, its recent events and the project's properties as one JSON object. "
                "Never waits, even for a running task, unless --wait is given."
            ),
        )
        self.parser.add_argument(
            "--non-interactive",
            action="store_true",
            help=(
                "For scripts and agents: --wait and --json, but stop as soon as Aristotle asks something, printing the "
                "questions as JSON and exiting with code 10. Otherwise print the task as --json does once it has "
                "finished, and exit on how it ended, as `submit --wait` does."
            ),
        )
        self.parser.add_argument(
            "--wait",
            action="store_true",
            help=(
                "Wait for a running task to finish as `submit --wait` does, through any questions Aristotle asks, and "
                "exit on how it ended. With --json, print the task once it has finished, with no progress display "
                "and no prompts. A `submit --wait` or `continue --wait` that broke off names this to carry on."
            ),
        )
        self.parser.add_argument(
            "--destination",
            # Expanded here, as a shell would: --destination=~/out reaches argparse with the ~ unexpanded.
            type=os.path.expanduser,
            help=(
                "With --wait or --non-interactive: where to save the result archive once the task has finished, as "
                "`submit --wait` does, checked before anything is sent. Without it, the command that downloads the "
                "archive is printed instead."
            ),
        )

    def preflight(self, args: argparse.Namespace) -> int | None:
        if args.destination is not None:
            if not (args.wait or args.non_interactive):
                self.parser.error("--destination needs --wait or --non-interactive")
            try:
                validate_destination(args.destination)
            except ValueError as e:
                self.parser.error(str(e))
        if args.limit is not None and not 0 <= args.limit <= 100:
            self.parser.error("--limit must be between 0 and 100")
        if args.limit == 0 and args.pagination_key is not None:
            self.parser.error("--pagination-key and --limit 0 don't go together: --limit 0 shows no events")
        return None

    async def run_action(self, args: argparse.Namespace) -> int | None:
        if args.non_interactive:
            args.json = True
        args.project_id = await Project.resolve_id(args.project_id)
        project: Project | None = None
        if args.task_id is not None:
            task = await AgentTask.from_id(args.task_id)
            if not same_project_id(task.project_id, args.project_id):
                logging.error(
                    f"Task {args.task_id} belongs to project {task.project_id}, but project {args.project_id} provided."
                )
                return EXIT_NOT_FOUND
            if args.json:
                # The result carries the project, which a task asked for by id doesn't bring.
                try:
                    project = await Project.from_id(args.project_id)
                except AristotleAPIError as e:
                    logging.warning(f"WARNING: The project could not be fetched: {e}")
        else:
            project = await Project.from_id(args.project_id)
            tasks, _ = await project.get_tasks(limit=1)
            if not tasks:
                if args.pagination_key is not None:
                    self.parser.error("--pagination-key pages through a task's events, and the project has no task")
                if args.json:
                    summary = summarize_properties(await fetch_properties(project.project_id))
                    print_json(_snapshot(project.project_id, project, None, [], summary))
                else:
                    logging.info("No tasks found for this project.")
                return None
            task = tasks[0]

        is_running = task.status in (TaskStatus.QUEUED, TaskStatus.IN_PROGRESS)
        default_num_events = 3 if is_running else 0
        num_events = args.limit if args.limit is not None else default_num_events
        if args.pagination_key is not None:
            if num_events == 0:
                self.parser.error(
                    "--pagination-key needs --limit for a finished task, which shows no events without it"
                )
            if is_running and not args.json and (args.wait or progress_utils.stdout_is_terminal()):
                self.parser.error(
                    "--pagination-key needs --json while the task runs: following it shows only its newest events"
                )

        # A wait whose result is printed as JSON: --non-interactive, which stops for questions, or --wait --json.
        waits = args.non_interactive or args.wait
        if is_running and args.json and waits:
            try:
                questions = await task.wait_for_completion(
                    num_events=num_events,
                    interactive=False,
                    stop_on_questions=args.non_interactive,
                    carry_on=carry_on_hint(task, args.destination, non_interactive=args.non_interactive, json=True),
                    resume=resume_commands(task, args.destination, non_interactive=args.non_interactive, json=True),
                )
            except (KeyboardInterrupt, asyncio.CancelledError):
                # A slice cut short still leaves the task as it last stood, not an empty file.
                print_json(
                    _snapshot(task.project_id, _cut_short(project, task, args, is_running), task, [], NO_SUMMARY)
                )
                raise
            if questions:
                return print_questions_pending(task, questions, args.destination)

        if args.json:
            events: list[Event] = []
            pagination_key = None
            result_path: Path | None = None
            saved = True
            # Whether an interrupt now would leave the finished task's files unsaved at --destination.
            unsaved = waits and not task.live and args.destination is not None
            try:
                if waits and is_running and project is not None:
                    # It ran while the wait went on; the result says how it stands now.
                    await refresh_quietly(project, task)
                if num_events > 0:
                    try:
                        newest_first, pagination_key = await task.get_events(
                            limit=num_events, pagination_key=args.pagination_key
                        )
                    except AristotleAPIError as e:
                        # After a wait, the task is printed as it last stood even when its events can't be fetched.
                        if not (waits and is_running):
                            raise_for_pagination_key(self.parser, e, args.pagination_key)
                        logging.warning(
                            "WARNING: The task's recent events could not be fetched: "
                            + with_pagination_key_hint(e, args.pagination_key)
                        )
                        newest_first = []
                    events = newest_first[::-1]
                if unsaved:
                    # Before the result, which says where they went, as `submit --wait` does.
                    saved, result_path = await self._save_files(task.project_id, args.destination)
                    unsaved = False
                # The properties stand as the latest task left them, which a task asked for by id may predate.
                unjudged: Exception | None
                try:
                    latest = (
                        (await _latest_task(task.project_id, project) or task) if args.task_id is not None else task
                    )
                except Exception as e:
                    summary, unjudged = NO_SUMMARY, e
                else:
                    summary, unjudged = await judge_properties(task.project_id, latest)
            except (KeyboardInterrupt, asyncio.CancelledError):
                if unsaved:
                    logging.warning(interrupted_save_hint(task.project_id, args.destination))
                if waits:
                    # As for a wait cut short: the task as it last stood.
                    cut_short = _cut_short(project, task, args, is_running)
                    print_json(_snapshot(task.project_id, cut_short, task, events, NO_SUMMARY, result_path))
                raise
            print_json(_snapshot(task.project_id, project, task, events, summary, result_path))
            if pagination_key:
                logging.info(f"Pagination key for next page: {pagination_key}")
            if waits and not task.live and args.destination is None:
                await log_download_hint(task.project_id)
            if unjudged is not None:
                code = log_unjudged(task.project_id, unjudged)
                return code if saved else EXIT_INFRASTRUCTURE_ERROR
            if not saved:
                return EXIT_INFRASTRUCTURE_ERROR
            return task_exit_code(task.status) if waits else None

        if is_running and not args.wait and not progress_utils.stdout_is_terminal():
            # Following would hold a pipe open until the task ends. No counts: they stand only once it has.
            await self._show(task, num_events, args, project)
            logging.info(
                f"The task is still running. Follow it at a terminal with `aristotle show {task.project_id}`, or wait "
                f"for it with `aristotle show {task.project_id} --wait`."
            )
            return None
        if is_running:
            await task.wait_for_completion(
                num_events=num_events,
                interactive=True,
                title=project.description if project else None,
                carry_on=carry_on_hint(task, args.destination, non_interactive=False, json=False)
                if args.wait
                else None,
                resume=resume_commands(task, args.destination, non_interactive=False, json=False),
            )
            try:
                log_property_counts(*await fetch_properties_and_tasks(args.project_id, task))
            except (KeyboardInterrupt, asyncio.CancelledError):
                # The finished task's files are due at --destination: say they weren't saved.
                if args.destination is not None and not task.live:
                    logging.warning(interrupted_save_hint(task.project_id, args.destination))
                raise
            # Still live: the wait ended without seeing the task finish.
            if task.live:
                return EXIT_INFRASTRUCTURE_ERROR
            if args.wait:
                return await self._wait_result(task, args.destination)
            await log_download_hint(task.project_id)
            # Followed to its end, it exits as --wait does.
            return task_exit_code(task.status)
        properties = await fetch_properties(args.project_id)
        # A task asked for by id may predate the latest: the properties stand as the latest left them, and a follow-up
        # continues it. Fetched once, and only when one of them needs it.
        # Only the owner can continue a task, so the command, and with it the latest task, is at stake only for theirs.
        offers_continue = task.status in ENDED_BADLY and not task.read_only
        needs_latest = args.task_id is not None and (bool(properties) or offers_continue)
        fetched: AgentTask | None = task
        if needs_latest:
            try:
                fetched = await _latest_task(args.project_id, project)
            except Exception as e:
                fetched = None
                lost = [
                    *(
                        [
                            f"the counts go by task {task.agent_task_id}, and a proof or disproof a later task left "
                            "unchecked may count as checked"
                        ]
                        if properties
                        else []
                    ),
                    *(["no command to continue it is offered"] if offers_continue else []),
                ]
                logging.warning(
                    f"WARNING: The project's latest task couldn't be fetched, so {'; and '.join(lost)}: {e}"
                )
        latest = fetched or task
        log_property_counts(properties, await fetch_tasks(args.project_id, properties, latest))
        await self._show(task, num_events, args, project)
        if task.status in ENDED_BADLY:
            # A follow-up continues the latest task, so the command is for this one only when it is known to be that.
            not_latest = fetched is None or fetched.agent_task_id != task.agent_task_id
            resume = resume_commands(task, args.destination, non_interactive=False, json=False)
            notice = ending_notice(task.status, task.project_id, task.read_only or not_latest, resume)
            (logging.error if task.status == TaskStatus.FAILED else logging.info)(notice)
        return await self._wait_result(task, args.destination) if args.wait else None

    async def _wait_result(self, task: AgentTask, destination: str | None) -> int:
        """After --wait without --json, as after `submit --wait`: the files saved, or how to get them, and the exit code
        for how the task ended."""
        if destination is None:
            await log_download_hint(task.project_id)
            return task_exit_code(task.status)
        try:
            saved, _ = await self._save_files(task.project_id, destination)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logging.warning(interrupted_save_hint(task.project_id, destination))
            raise
        return task_exit_code(task.status) if saved else EXIT_INFRASTRUCTURE_ERROR

    async def _show(self, task: AgentTask, num_events: int, args: argparse.Namespace, project: Project | None) -> None:
        """Print the task and its newest events, from the page --pagination-key names if given."""
        try:
            await task.show(
                num_events=num_events,
                pagination_key=args.pagination_key,
                title=project.description if project else None,
            )
        except AristotleAPIError as e:
            raise_for_pagination_key(self.parser, e, args.pagination_key)

    @staticmethod
    async def _save_files(project_id: str, destination: str) -> tuple[bool, Path | None]:
        """Save the project's files to destination, if it has any, as `submit --wait` does: False if saving failed, and
        where the archive went, None when it wasn't saved."""
        try:
            project = await Project.from_id(project_id)
            if not project.has_files:
                return True, None
            path = await project.get_files(destination=destination)
        except (OSError, AristotleAPIError) as e:
            logging.error(f"The project's files were not saved: {e}\n{download_hint(project_id, destination)}")
            return False, None
        logging.info(f"Saved the project's files to {path}")
        return True, path
