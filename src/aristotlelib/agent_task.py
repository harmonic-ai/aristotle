import asyncio
import logging
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx

from aristotlelib import api_request, progress_utils
from aristotlelib.aristotle_object import AristotleObject
from aristotlelib.cli.question_flow import (
    DEFAULT_DEADLINE_SECONDS,
    can_prompt,
    pending_footer,
    read_stdin_line,
    run_question_flow,
)
from aristotlelib.date_utils import UTCDatetime, format_elapsed_time
from aristotlelib.declaration import Declaration, DeclarationType
from aristotlelib.event import Event, EventStatus, EventType
from aristotlelib.printable import print_line, printable
from aristotlelib.question import Question
from aristotlelib.shell import shell_hint

logger = logging.getLogger("aristotle")


# How often the wait loop re-lists questions and refreshes the task, events or not.
QUESTION_REFRESH_SECONDS = 30

# Listings an unclaimed question placeholder waits for before it is shown without a question record.
UNCLAIMED_EVENT_LISTINGS = 2

# How long Ctrl-C waits for a refresh that confirms a run is still queued before saying so.
INTERRUPT_REFRESH_SECONDS = 5

# The longest heading a status shows: a project named after its whole prompt is cut to its first line and this.
HEADING_LENGTH = 80


class TaskStatus(Enum):
    UNKNOWN = "UNKNOWN"  # Fallback for unrecognized statuses from server
    QUEUED = "QUEUED"  # Task will start soon
    IN_PROGRESS = "IN_PROGRESS"  # Task is actively in progress
    COMPLETE = "COMPLETE"  # Task successfully completed
    COMPLETE_WITH_ERRORS = (
        "COMPLETE_WITH_ERRORS"  # Aristotle made progress but was not able to fully complete the request
    )
    OUT_OF_BUDGET = "OUT_OF_BUDGET"  # Task could not complete in time
    FAILED = "FAILED"  # Unexpected error
    CANCELED = "CANCELED"  # Task was canceled

    @classmethod
    def _missing_(cls, value):
        logger.warning(f"Unknown {cls.__name__} value '{value}'; treating as UNKNOWN")
        return cls.UNKNOWN


# The statuses a task ends in; until another task runs, nothing changes its properties.
TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.COMPLETE,
        TaskStatus.COMPLETE_WITH_ERRORS,
        TaskStatus.OUT_OF_BUDGET,
        TaskStatus.FAILED,
        TaskStatus.CANCELED,
    }
)


def _never() -> bool:
    return False


def _always() -> bool:
    return True


def _failure(error: Exception) -> str:
    """What failed, for the middle of a sentence: an API error's status, without the advice its message may give, or a
    transport error's type and message, without a closing full stop."""
    if isinstance(error, api_request.AristotleAPIError) and error.status_code is not None:
        return f"HTTP {error.status_code}"
    return (f"{type(error).__name__}: {error}" if str(error) else type(error).__name__).rstrip(".")


def _prompt_line(text: str) -> None:
    """Print a line of the per-event prompt on stderr, made printable(), whether or not logging is set up."""
    print(printable(text), file=sys.stderr)


def follow_hint(project_id: str, read_only: bool = False, carry_on: str | None = None) -> str:
    """How to follow a run you stopped waiting for, by ``carry_on`` where given, and, if it is yours, cancel it."""
    hint = carry_on if carry_on is not None else f"Follow it with: aristotle show {project_id}"
    return hint if read_only else f"{hint}\nCancel it with: aristotle cancel {project_id}"


def queued_notice(project_id: str, read_only: bool = False, carry_on: str | None = None) -> str:
    """What a wait that ends on a run confirmed still queued says: it never started, and what to do."""
    whose = "its owner's" if read_only else "your"
    return (
        f"The run has not started yet: it is still queued, behind {whose} other runs in progress or waiting for "
        f"capacity.\n{follow_hint(project_id, read_only, carry_on)}"
    )


_BAD_ENDINGS = {
    TaskStatus.FAILED: "Task failed due to an internal error. The team at Harmonic has been notified.",
    TaskStatus.CANCELED: "Task was canceled.",
    TaskStatus.OUT_OF_BUDGET: "Task ran out of budget.",
}
ENDED_BADLY = frozenset(_BAD_ENDINGS)
"""The statuses ending_notice() explains."""


def ending_notice(
    status: TaskStatus, project_id: str, read_only: bool = False, resume: Sequence[Sequence[str]] | None = None
) -> str | None:
    """What a task that ended FAILED, CANCELED or OUT_OF_BUDGET leaves to do, with the commands that resume it on the
    same project: ``resume``, each a list of words, where given, else `aristotle continue <project-id> Continue --wait`;
    None for any other status. Only the project's owner can continue it."""
    ending = _BAD_ENDINGS.get(status)
    if ending is None or read_only:
        return ending
    again = "Try it again" if status == TaskStatus.FAILED else "Resume it"
    commands = resume or [["aristotle", "continue", project_id, "Continue", "--wait"]]
    return f"{ending} {shell_hint(f'{again} on the same project with', *commands)}"


class AgentTask(AristotleObject):
    project_id: str
    agent_task_id: str
    status: TaskStatus
    created_at: UTCDatetime
    last_updated_at: UTCDatetime
    # When the run's job started, shortly before the run reports IN_PROGRESS. None while it is queued, for a run
    # cancelled before it started, and from a server that doesn't send it; a run failed before starting may carry one.
    started_at: UTCDatetime | None = None
    # Deprecated: None from current servers, which send started_at instead.
    percent_complete: int | None = None
    file_name: str | None
    description: str | None
    output_summary: str | None
    # Set for someone else's shared project; the answer endpoint takes the owner only.
    read_only: bool = False

    endpoint_prefix = "task"

    @property
    def object_id(self) -> str:
        return self.agent_task_id

    @property
    def live(self) -> bool:
        """Whether the run is queued or in progress, so an answer can still reach the agent."""
        return self.status in (TaskStatus.QUEUED, TaskStatus.IN_PROGRESS)

    async def refresh_live(self) -> bool:
        """Refresh the task and return ``live``; a failed refresh raises AristotleAPIError."""
        await self.refresh()
        return self.live

    def __str__(self) -> str:
        return "\n".join(self._status_lines())

    def _status_lines(self, title: str | None = None) -> list[str]:
        """The status and elapsed time, ``title`` or else the task's description, and the task and project ids."""
        created_at_utc = self.created_at.replace(tzinfo=timezone.utc)
        elapsed_seconds = int((datetime.now(timezone.utc) - created_at_utc).total_seconds())
        # created_at is when the run was submitted. One cancelled, failed or in a status this client doesn't know, with
        # neither a start time nor progress recorded, never left the queue: a server records one of them when a run
        # starts, and a cancel keeps it.
        never_started = self.status == TaskStatus.QUEUED or (
            self.status in (TaskStatus.CANCELED, TaskStatus.FAILED, TaskStatus.UNKNOWN)
            and self.started_at is None
            and not self.percent_complete
        )
        elapsed_str = format_elapsed_time(elapsed_seconds, "submitted" if never_started else "started")

        lines = [f"{self.status.name} ({elapsed_str})"]
        if heading := (title or self.description or "").strip().split("\n", 1)[0].rstrip():
            lines.append(heading if len(heading) <= HEADING_LENGTH else heading[: HEADING_LENGTH - 1].rstrip() + "…")
        lines.extend([f"Task: {self.agent_task_id}", f"Project: {self.project_id}"])
        return lines

    async def get_events(
        self, pagination_key: str | None = None, limit: int = 50, newest_first: bool = True
    ) -> tuple[list[Event], str | None]:
        """List task events, ordered by creation date.

        Args:
            pagination_key: Key to start from when paginating through events.
            limit: Maximum number of events to return. Must be between 1 and 100.
            newest_first: If true, shows most recent events first. If false, shows the oldest first.

        Returns:
            Tuple of list of events and the new pagination key.
        """
        if not 1 <= limit <= 100:
            raise ValueError("Limit must be between 1 and 100")

        params: dict[str, Any] = {"pagination_key": pagination_key, "limit": limit, "ascending": not newest_first}
        async with api_request.AristotleRequestClient() as client:
            response = await client.get(f"/{self.endpoint_prefix}/{self.agent_task_id}/events", params=params)
            response_data = response.json()
            events: list[Event] = [Event.model_validate(event) for event in response_data["events"]]
            pagination_key = response_data.get("pagination_key")
            if pagination_key is not None and not isinstance(pagination_key, str):
                raise TypeError("pagination_key must be a string or None")
            return events, pagination_key

    async def cancel(self) -> None:
        """Cancel the task if it is in progress or queued; a task already CANCELED counts as cancelled, and one that
        has otherwise finished raises a 404 that says so."""
        async with api_request.AristotleRequestClient() as client:
            try:
                # Sending it twice cancels once, so a lost reply is sent again like a GET's.
                response = await client.post(f"/{self.endpoint_prefix}/{self.agent_task_id}/cancel", idempotent=True)
            except api_request.AristotleAPIError as e:
                # Aristotle refuses with a 404 any task no longer in progress or queued, one this cancel ended included.
                if e.status_code != 404:
                    raise
                refusal = e
            else:
                self._update_from_response(response.json())
                return
        await self.refresh()
        if self.status in TERMINAL_STATUSES and self.status != TaskStatus.CANCELED:
            raise api_request.AristotleAPIError(
                f"Task {self.agent_task_id} has already finished ({self.status.name}), so it can't be cancelled.",
                refusal.status_code,
            )
        if self.status != TaskStatus.CANCELED:
            raise refusal

    async def show(
        self, num_events: int = 0, pagination_key: str | None = None, title: str | None = None
    ) -> str | None:
        """Display task info, headed by ``title`` where given, as a wait's display is, and recent events."""
        logger.info("\n".join(self._status_lines(title)))
        if self.output_summary:
            logger.info(self.output_summary)

        if num_events <= 0:
            return None

        events, next_pagination_key = await self.get_events(limit=num_events, pagination_key=pagination_key)
        for event in reversed(events):
            logger.info(event)
        if next_pagination_key:
            logger.info(f"Pagination key for next page: {next_pagination_key}")
        return next_pagination_key

    async def _prompt_for_answer(self, event: Event, interactive: bool = True) -> bool:
        """Prompt user to answer an agent question with a timeout (the per-event fallback path); False if not prompted."""
        if interactive:
            progress_utils.clear_progress_lines()
        if not interactive or not can_prompt() or self.read_only:
            # Non-interactive, not at a terminal, or not the owner: log the question and do not read stdin.
            logger.info(str(event))
            return False
        _prompt_line(str(event))

        start_time = datetime.now()
        timeout_seconds = 60 * 15 - 60  # 15 minute timeout, with a 1 minute buffer

        while True:
            elapsed = (datetime.now() - start_time).total_seconds()
            remaining = timeout_seconds - elapsed
            if remaining <= 0:
                logger.info("No answer provided in time, continuing...")
                return True

            _prompt_line("Type your answer:")
            try:
                answer = await asyncio.wait_for(read_stdin_line(), timeout=remaining)
                answer = answer.strip()
                if not answer:
                    continue

                await event.answer(answer)
                return True
            except asyncio.TimeoutError:
                logger.info("No answer provided in time, continuing...")
                return True
            except api_request.AristotleAPIError as e:
                if e.status_code in (400, 409):
                    # The event route refuses a closed question with 400, and with 409 once its task has ended.
                    logger.info("This question is already closed; the answer was not sent.")
                else:
                    logger.error(f"Error answering question: {e}")
                return True
            except Exception as e:
                logger.error(f"Error answering question: {e}")
                return True

    async def _refreshed_within(self, seconds: float) -> bool:
        """Refresh the task within ``seconds``; False on any failure, a timeout or a second interrupt."""
        try:
            await asyncio.wait_for(self.refresh(), seconds)
        except (Exception, KeyboardInterrupt, asyncio.CancelledError):
            return False
        return True

    async def _refresh_quietly(self, quiet: Callable[[], bool] = _never) -> bool:
        """Refresh the task; a transient API error is logged, at debug level with its retries while ``quiet()`` holds,
        and the last known status kept."""
        try:
            with api_request.quietly(quiet):
                await self.refresh()
        except api_request.AristotleAPIError as e:
            (logger.debug if quiet() else logger.warning)(f"Could not refresh the task status: {e}")
            return False
        return True

    async def _handle_stream_event(
        self, event_data, watcher, remember, refresh, show_progress, last_refresh, refresh_interval
    ):
        """Render one streamed event, refreshing if the interval has passed; returns the new last-refresh time."""
        try:
            event = Event.model_validate(event_data)
        except Exception:
            logger.debug("Invalid event ignored")
            return last_refresh

        # An unanswered question, legacy SENT or COMPLETE placeholder; the watcher decides what to show.
        if event.event_type == EventType.AGENT_QUESTION and event.explanation is None:
            remember(await watcher.on_question_event(event))
        else:
            remember([str(event)], collapse=True)

        now = datetime.now()
        if (now - last_refresh).total_seconds() >= refresh_interval:
            await refresh()
            last_refresh = now

        show_progress()
        return last_refresh

    async def wait_for_completion(
        self,
        num_events: int = 3,
        *,
        interactive: bool = False,
        stop_on_questions: bool = False,
        title: str | None = None,
        resumable: bool = True,
        carry_on: str | None = None,
        resume: Sequence[Sequence[str]] | None = None,
    ) -> list[Question]:
        """Wait for the task; interactive, as the CLI waits, draws progress on stdout, headed by ``title``, such as the
        project's name, where given, and prompts on stdin at a terminal, where without it the wait uses neither and
        logs open questions; stop_on_questions returns open questions early. A task that ends badly is said to resume
        with ``resume``, the commands that do, where given, else ``aristotle continue``, only when ``resumable``, as it
        isn't for a caller with its own next step. A wait that ends before the task does names ``carry_on``, the
        command that carries it on, where given, instead of `aristotle show <project-id>`."""

        def next_step(verb: str) -> str:
            """The command that carries on: ``carry_on``, which may span lines, or `aristotle show`, which follows."""
            return f"\n{carry_on}" if carry_on is not None else f" Use 'aristotle show {self.project_id}' to {verb}."

        # Rendered events and question-flow outcomes, newest last, capped at num_events.
        recent: list[str] = []
        last_refresh = datetime.min
        started = datetime.now()
        refresh_interval = QUESTION_REFRESH_SECONDS
        watcher = _QuestionWatcher(self, interactive=interactive)
        # While the stream rides out an outage, the line saying so; the refreshes stay quiet meanwhile.
        outage: str | None = None
        # The project's properties as last counted for the display; None while none are listed.
        counts: str | None = None
        # The tasks the counts are judged against, once listed: the earlier ones have ended, so one listing serves.
        judged_by: list[AgentTask] | None = None
        # The count under way, which runs beside the loop: no frame or event waits for it.
        counting: asyncio.Future | None = None

        def remember(entries: list[str], *, collapse: bool = False) -> None:
            """Keep ``entries`` for the display, the newest num_events; with ``collapse``, not one that repeats the
            newest kept, so a run of the same activity shows once."""
            if num_events > 0:
                for entry in entries:
                    if not (collapse and recent and recent[-1] == entry):
                        recent.append(entry)
                del recent[:-num_events]

        async def count_properties() -> None:
            """Count the project's properties for the display as `show` counts them, judged against its tasks; a
            failure, its retries included, is logged at debug level, and the last count kept."""
            nonlocal counts, judged_by
            # Imported here: both modules import this one.
            from aristotlelib.cli.api_action import fetch_tasks
            from aristotlelib.cli.command.show import format_property_counts

            try:
                with api_request.quietly(_always):
                    properties = await Declaration.list_all_for_project(self.project_id, DeclarationType.PROPERTY)
                    tasks = (
                        judged_by
                        if judged_by is not None
                        else await fetch_tasks(self.project_id, properties, self, strict=True)
                    )
            except Exception as e:
                logger.debug(f"Could not count the project's properties: {e}")
                return
            judged_by = tasks
            counts = f"Properties: {format_property_counts(properties, tasks)}" if properties else None

        def recount() -> None:
            """Start a count for the display while the run goes on and none is under way; the next frame shows it."""
            nonlocal counting
            if interactive and self.live and (counting is None or counting.done()):
                counting = asyncio.ensure_future(count_properties())

        async def refresh(quiet: Callable[[], bool] = _never) -> None:
            """Refresh the task and re-list its questions, logging their failures and retries at debug level while
            ``quiet()`` holds, then start a count of its project's properties."""
            await self._refresh_quietly(quiet=quiet)
            remember(await watcher.refresh(quiet=quiet))
            recount()

        def show_progress() -> None:
            if not interactive:
                return
            lines = self._status_lines(title)
            if counts is not None and self.live:
                # Only while the run goes on: the counts that stand once it ends are the caller's to report.
                lines.append(counts)
            lines += recent
            if outage is not None:
                # Each frame clears the terminal, the logged line included.
                lines.append(outage)
            if watcher.pending_count and self.live:
                # Pinned last, so it stays put while the events above it scroll.
                lines.append(pending_footer(watcher.pending_count, self.project_id, read_only=self.read_only))
            progress_utils.print_progress_lines(lines)

        def questions_to_hand_back() -> list[Question]:
            return list(watcher.pending) if stop_on_questions and not self.read_only else []

        # Show initial status before events start streaming
        show_progress()

        try:
            # The stream does not replay earlier events, so a question asked before this attached is found by listing.
            remember(await watcher.refresh())
            show_progress()
            if pending := questions_to_hand_back():
                return pending

            def end_outage() -> None:
                """Say the connection is back if the stream was riding out an outage, as a line or a request got
                through."""
                nonlocal outage
                if outage is not None:
                    outage = None
                    logger.info("The connection to Aristotle is back.")

            def on_outage(error: Exception | None) -> None:
                """Log an outage's start and end. It runs in the stream's read, during a prompt too, so it draws no
                frame."""
                nonlocal outage
                if error is None:
                    end_outage()
                    return
                minutes = api_request.STREAM_OUTAGE_SECONDS // 60
                outage = (
                    f"The connection to Aristotle failed: {_failure(error)}. Trying again for up to {minutes} minutes "
                    "while the task goes on."
                )
                logger.warning(outage)

            def riding_out() -> bool:
                return outage is not None

            async def still_live() -> bool:
                # Should the stream then fail to reconnect, it reports the outage itself.
                await self._refresh_quietly(quiet=_always)
                return self.live

            async with api_request.AristotleRequestClient() as client:
                stream = client.stream_sse(
                    f"/{self.endpoint_prefix}/{self.agent_task_id}/events-stream",
                    reconnect_if=still_live,
                    live=lambda: self.live,
                    on_outage=on_outage,
                ).__aiter__()
                # The next-event read outlives refresh timeouts; cancelling it would close the stream.
                next_event: asyncio.Future = asyncio.ensure_future(stream.__anext__())
                try:
                    while True:
                        since = max(last_refresh, started)
                        wait = max(0.0, refresh_interval - (datetime.now() - since).total_seconds())
                        done, _ = await asyncio.wait({next_event}, timeout=wait)
                        if not done:
                            await refresh(quiet=riding_out)
                            last_refresh = datetime.now()
                            show_progress()
                            if pending := questions_to_hand_back():
                                # Its listing got through.
                                end_outage()
                                return pending
                            continue
                        try:
                            event_data = next_event.result()
                        except StopAsyncIteration:
                            break
                        next_event = asyncio.ensure_future(stream.__anext__())
                        last_refresh = await self._handle_stream_event(
                            event_data, watcher, remember, refresh, show_progress, last_refresh, refresh_interval
                        )
                        if pending := questions_to_hand_back():
                            return pending
                finally:
                    if not next_event.done():
                        next_event.cancel()
                    elif not next_event.cancelled():
                        # A read that finished during a flow (e.g. the stream ended) has its exception retrieved.
                        next_event.exception()

            if not await self._refresh_quietly():
                if self.live:
                    logger.error(
                        f"The event stream ended, and the task's status could not be checked.{next_step('follow it')}"
                    )
                return []
            # The stream may end without a line, as when the run ends in an outage; the refresh got through.
            end_outage()
            # The last frame, redrawn with the status the task ended on, stays above the outcome.
            show_progress()

            if self.status in ENDED_BADLY:
                notice = ending_notice(self.status, self.project_id, self.read_only or not resumable, resume)
                (logger.error if self.status == TaskStatus.FAILED else logger.info)(notice)
            elif self.status == TaskStatus.COMPLETE:
                logger.info("Task complete!")
            elif self.status == TaskStatus.COMPLETE_WITH_ERRORS:
                logger.info(
                    "Aristotle made progress but did not fully complete the request. Its summary says what is left."
                )
            elif self.status == TaskStatus.QUEUED:
                logger.error(
                    "The event stream ended before the run started.\n"
                    f"{queued_notice(self.project_id, self.read_only, carry_on)}"
                )
            elif self.live:
                logger.error(f"The event stream ended before the task finished.{next_step('follow it')}")

        except httpx.TransportError:
            # The stream raises one only once its connections have failed for STREAM_OUTAGE_SECONDS.
            minutes = api_request.STREAM_OUTAGE_SECONDS // 60
            logger.error(
                f"The connection to Aristotle kept failing for {minutes} minutes, so the wait stopped. The task goes "
                f"on.\n{follow_hint(self.project_id, self.read_only, carry_on)}"
            )
        except api_request.AristotleAPIError as e:
            # The stream raises a 429 or 5xx only once it has given up on it; any other status is final.
            if e.status_code is None or (e.status_code != 429 and e.status_code < 500):
                raise
            logger.error(f"The task could not be followed: {str(e).rstrip('.')}.{next_step('try again')}")
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Ctrl-C arrives as a cancellation on Python 3.11+; either way the run carries on server-side.
            if self.status != TaskStatus.QUEUED:
                logger.info(
                    f"Task {self.agent_task_id} is still running.\n{follow_hint(self.project_id, self.read_only, carry_on)}"
                )
            elif not await self._refreshed_within(INTERRUPT_REFRESH_SECONDS):
                logger.info(
                    f"Task {self.agent_task_id} has not finished; its current status could not be checked.\n"
                    f"{follow_hint(self.project_id, self.read_only, carry_on)}"
                )
            elif self.status == TaskStatus.QUEUED:
                logger.info(
                    f"Task {self.agent_task_id} is still queued.\n{queued_notice(self.project_id, self.read_only, carry_on)}"
                )
            else:
                logger.info(
                    f"Task {self.agent_task_id} is still running.\n{follow_hint(self.project_id, self.read_only, carry_on)}"
                )
            raise
        finally:
            if counting is not None:
                # Its count would fill a display that is done.
                counting.cancel()
        return []


class _QuestionWatcher:
    """Question state kept across a wait; questions are listed at start, on question events and on refreshes."""

    def __init__(self, task: AgentTask, interactive: bool = True) -> None:
        self.task = task
        # False: log questions instead of prompting, and never clear the terminal.
        self.interactive = interactive
        # The pending questions while the run is live, as of the last listing.
        self.pending: list[Question] = []
        # Cleared on the first 404/405, after which only the per-event path is used.
        self.endpoint_available = True
        self._presented: set[str] = set()
        self._handled_events: set[str] = set()
        # SENT events the per-event path handled; their COMPLETE update still shows its timed-out notice.
        self._sent_events: set[str] = set()
        self._reported_ignored: set[str] = set()
        # Events awaiting a successful listing.
        self._unlisted_events: list[Event] = []
        # Listings seen by each unclaimed COMPLETE placeholder; its row may lag its event.
        self._unclaimed_listings: dict[str, int] = {}

    @property
    def pending_count(self) -> int:
        return len(self.pending)

    async def on_question_event(self, event: Event) -> list[str]:
        """React to an AGENT_QUESTION event; returns the lines the display should carry for it."""
        if event.status == EventStatus.COMPLETE and event.event_id in self._sent_events:
            self._sent_events.remove(event.event_id)
            return [str(event)]
        if event.event_id in self._handled_events:
            return []
        if not self.endpoint_available:
            return await self._handle_by_event(event)

        questions = await self._list_questions()
        if questions is not None:
            return await self._reconcile(questions, [event])
        if not self.endpoint_available:
            # The endpoint is missing: this event and any waiting on a listing take the per-event path.
            return await self._handle_by_event(*self._take_unlisted(), event)
        if event.status == EventStatus.SENT:
            # SENT is only ever a legacy question, so the per-event prompt needs no listing.
            return await self._handle_by_event(event)
        self._unlisted_events.append(event)
        return []

    async def refresh(self, quiet: Callable[[], bool] = _never) -> list[str]:
        """Re-list the task's questions, logging a failed listing at debug level while ``quiet()`` holds; returns the
        display lines for what changed."""
        if not self.endpoint_available:
            return []
        questions = await self._list_questions(quiet=quiet)
        if questions is not None:
            return await self._reconcile(questions, [])
        if not self.endpoint_available:
            return await self._handle_by_event(*self._take_unlisted())
        # Transient failure: keep the last count; the next refresh retries.
        return []

    async def _reconcile(self, questions: list[Question], events: list[Event]) -> list[str]:
        """Apply a fresh listing, then handle the events that were waiting on one."""
        events = self._take_unlisted() + events

        pending = [question for question in questions if question.pending]
        listed_events = {question.event_id for question in questions if question.event_id}
        # Events claimed by a listed question need no per-event handling.
        self._handled_events.update(listed_events)

        entries: list[str] = []
        for question in questions:
            if question.ignored and question.question_id in self._presented:
                if question.question_id not in self._reported_ignored:
                    self._reported_ignored.add(question.question_id)
                    entries.append(f"Aristotle asked: {question.content}\nThe question was closed without an answer.")

        if any(question.question_id not in self._presented for question in pending):
            # A new pending question: present the whole pending set, not just the new one.
            self._presented.update(question.question_id for question in pending)
            entries += await self._present(pending)
        else:
            self.pending = pending if self.task.live else []

        for event in events:
            if event.event_id in listed_events:
                continue
            if event.status == EventStatus.SENT:
                entries += await self._handle_by_event(event)
                continue
            # An unclaimed placeholder waits UNCLAIMED_EVENT_LISTINGS more listings for its row, then shows as asked.
            seen = self._unclaimed_listings.get(event.event_id, 0) + 1
            if seen <= UNCLAIMED_EVENT_LISTINGS:
                self._unclaimed_listings[event.event_id] = seen
                self._unlisted_events.append(event)
            else:
                self._handled_events.add(event.event_id)
                entries.append(f"Aristotle asked: {event.content}")
        return entries

    def _take_unlisted(self) -> list[Event]:
        """Take, and clear, the events waiting on a listing."""
        events, self._unlisted_events = self._unlisted_events, []
        return events

    async def _handle_by_event(self, *events: Event) -> list[str]:
        """Per-event path: prompt for a SENT question; render a COMPLETE one as its timed-out notice."""
        entries: list[str] = []
        for event in events:
            self._handled_events.add(event.event_id)
            if event.status == EventStatus.SENT:
                self._sent_events.add(event.event_id)
                if not await self.task._prompt_for_answer(event, interactive=self.interactive):
                    # Not prompted: keep the question on the display (interactive only), which clears on every redraw.
                    entries.append(str(event))
            else:
                entries.append(str(event))
        return entries

    async def _list_questions(self, quiet: Callable[[], bool] = _never) -> list[Question] | None:
        try:
            with api_request.quietly(quiet):
                return await Question.list_for_task(self.task.agent_task_id)
        except api_request.AristotleAPIError as e:
            if e.status_code in (404, 405):
                self.endpoint_available = False
            else:
                (logger.debug if quiet() else logger.warning)(f"Could not list the task's questions: {e}")
            return None

    async def _present(self, pending: list[Question]) -> list[str]:
        asked = [f"Aristotle asked: {question.content}" for question in pending]
        # A failed refresh keeps the last known status; the flow checks again before sending.
        await self.task._refresh_quietly()
        if not self.task.live:
            # An ended run: show what was asked, with no flow and no footer.
            self.pending = []
            return ["\n".join([*asked, "The run has ended, so no answer can reach Aristotle now."])]
        if not self.interactive or not can_prompt() or self.task.read_only:
            # Non-interactive, not at a terminal, or not the owner: show the questions and the footer, not a prompt.
            for line in asked:
                logger.info(line)
            self.pending = pending
            logger.info(pending_footer(self.pending_count, self.task.project_id, read_only=self.task.read_only))
            # Also in the display (interactive only), which clears the terminal on every redraw.
            return ["\n".join(asked)]

        progress_utils.clear_progress_lines()
        result = await run_question_flow(
            pending,
            read_line=read_stdin_line,
            write=print_line,
            deadline_seconds=DEFAULT_DEADLINE_SECONDS,
            run_is_live=self.task.refresh_live,
        )
        # No answer can reach a run that ended during the flow, so nothing counts as pending.
        self.pending = [] if result.run_ended else result.unanswered
        # One display entry, so the outcome scrolls off together.
        return ["\n".join(result.summary)] if result.summary else []
