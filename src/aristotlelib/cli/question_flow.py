"""A line-based session for answering a set of Aristotle's questions."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import shutil
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from aristotlelib.question import Question, QuestionClosed

# Budget for one session from when it opens; not synchronised with the agent's own fifteen-minute wait.
DEFAULT_DEADLINE_SECONDS = 14 * 60

# The narrowest the review cuts a question to, "..." included, however narrow the terminal.
MIN_REVIEW_WIDTH = 20

ReadLine = Callable[[], Awaitable[str]]
Write = Callable[[str], None]
RunIsLive = Callable[[], Awaitable[bool]]


@dataclass
class FlowResult:
    """Where every question ended up once the session was over."""

    answered: list[Question] = field(default_factory=list)
    # Closed on the server before this session's answer arrived.
    closed: list[Question] = field(default_factory=list)
    # Skipped, never chosen, cancelled with everything else, or refused by the server.
    unanswered: list[Question] = field(default_factory=list)
    cancelled: bool = False
    timed_out: bool = False
    # The run had ended by the time the answers were ready, so none were sent.
    run_ended: bool = False
    # The outcome lines the session wrote, in order.
    summary: list[str] = field(default_factory=list)

    @property
    def pending_count(self) -> int:
        return len(self.unanswered)


class _Cancelled(Exception):
    """The user asked to stop, or stdin closed under them."""


class _OutOfTime(Exception):
    """The session deadline passed while waiting for a line."""


# The one stdin read in flight; an abandoned read is reused by the next prompt instead of racing a second reader.
_outstanding_read: concurrent.futures.Future[str] | None = None


def can_prompt() -> bool:
    """Whether to prompt for answers: stdin and stdout are terminals and this process is the foreground job."""
    if sys.stdin is None or sys.stdout is None or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    if not hasattr(os, "tcgetpgrp"):
        # No job control (Windows).
        return True
    try:
        return os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp()
    except (OSError, ValueError):
        return True


def _start_read() -> concurrent.futures.Future[str]:
    future: concurrent.futures.Future[str] = concurrent.futures.Future()

    def read() -> None:
        try:
            future.set_result(input(""))
        except BaseException as e:  # noqa: BLE001 - handed to whoever awaits the line
            future.set_exception(e)

    threading.Thread(target=read, name="aristotle-stdin", daemon=True).start()
    return future


async def read_stdin_line() -> str:
    """Read a line off the event loop; stale lines are dropped, undecodable ones re-asked; EOF raises EOFError."""
    global _outstanding_read
    while True:
        print("> ", end="", flush=True)
        if _outstanding_read is not None and _outstanding_read.done():
            # Finished while no prompt was showing: typed for an earlier prompt, not this one.
            _outstanding_read = None
        if _outstanding_read is None:
            _outstanding_read = _start_read()
        future = _outstanding_read
        while not future.done():
            await asyncio.sleep(0.02)
        _outstanding_read = None
        try:
            return future.result()
        except UnicodeDecodeError:
            print("That line could not be read as text; type it again.")


def one_line(text: str, width: int) -> str:
    """``text`` with its whitespace collapsed to single spaces, cut to ``width`` characters with "..." if longer."""
    text = " ".join(text.split())
    if len(text) > width:
        return text[: width - 3] + "..."
    return text


def pending_footer(count: int, project_id: str, read_only: bool = False) -> str:
    """The footer line pinned to the progress display while questions are pending."""
    noun = "question" if count == 1 else "questions"
    if read_only:
        return f"{count} {noun} pending — only the project's owner can answer"
    return f"{count} {noun} pending — answer with: aristotle questions {project_id}"


async def run_question_flow(
    questions: list[Question],
    *,
    read_line: ReadLine,
    write: Write,
    deadline_seconds: float,
    run_is_live: RunIsLive,
) -> FlowResult:
    """Present ``questions`` one at a time and submit the chosen answers.

    Args:
        questions: The pending questions, in the order they should be shown.
        read_line: Returns the next line of input; raising EOFError ends the session as a cancel.
        write: Prints one line of output.
        deadline_seconds: Budget for the whole session. When it runs out, whatever has been
            chosen is sent and the rest is reported as still pending.
        run_is_live: Awaited just before sending. False (the run has ended) or an exception (its status
            is unknown) means nothing is sent.
    """
    return await _Session(questions, read_line, write, deadline_seconds, run_is_live).run()


def _unquote(text: str) -> str | None:
    """The literal inside a quoted answer, or None when the text is not quoted."""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return None


def _number(text: str) -> int | None:
    """``text`` as a menu number, or None; only ASCII digits that ``int`` will parse count."""
    if not (text.isascii() and text.isdigit()):
        return None
    try:
        return int(text)
    except ValueError:
        return None


class _Session:
    def __init__(
        self,
        questions: list[Question],
        read_line: ReadLine,
        write: Write,
        deadline_seconds: float,
        run_is_live: RunIsLive,
    ) -> None:
        self.questions = questions
        self.choices: list[str | None] = [None] * len(questions)
        self.read_line = read_line
        self.write = write
        self.deadline = time.monotonic() + deadline_seconds
        self.run_is_live = run_is_live
        self.result = FlowResult()

    async def run(self) -> FlowResult:
        if not self.questions:
            return self.result

        index = 0
        reopened = False
        try:
            while True:
                if index >= len(self.questions):
                    reopen = await self._review()
                    if reopen is None:
                        break
                    index, reopened = reopen, True
                    continue
                next_index = await self._ask(index)
                if reopened and next_index > index:
                    # Forward from a reopened question returns to the review, not to the next question.
                    next_index = len(self.questions)
                index, reopened = next_index, False
        except _Cancelled:
            self._report("Cancelled. Nothing was sent; the questions stay pending.")
            self.result.unanswered = list(self.questions)
            self.result.cancelled = True
            return self.result
        except _OutOfTime:
            self._report("Out of time. Sending the answers chosen so far.")
            await self._submit()
            self.result.timed_out = True
            return self.result
        await self._submit()
        return self.result

    def _report(self, line: str) -> None:
        """Write a line and keep it in the result summary."""
        self.write(line)
        self.result.summary.append(line)

    async def _read(self) -> str:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _OutOfTime
        try:
            return await asyncio.wait_for(self.read_line(), timeout=remaining)
        except asyncio.TimeoutError:
            raise _OutOfTime from None
        except EOFError:
            # EOF leaves the cursor on the prompt line; end it first.
            self.write("")
            raise _Cancelled from None

    def _show_question(self, index: int) -> None:
        question = self.questions[index]
        suggestions = question.suggestions
        self.write("")
        self.write(f"Question {index + 1} of {len(self.questions)}")
        self.write(question.content)
        for number, suggestion in enumerate(suggestions, 1):
            self.write(f"  {number}. {suggestion}")
        if self.choices[index] is not None:
            self.write(f"Current answer: {self.choices[index]}")
        if len(suggestions) == 1:
            self.write("Type 1 to pick the suggestion, or type your own answer.")
        elif suggestions:
            self.write(f"Type 1-{len(suggestions)} to pick a suggestion, or type your own answer.")
        else:
            self.write("Type your answer.")
        self.write(
            'Enter or n: next   p: back   s: skip   q: quit, send nothing   Quote it ("n") to answer with that exact '
            "text."
        )

    async def _ask(self, index: int) -> int:
        """Show one question and return the index to move to next."""
        question = self.questions[index]
        self._show_question(index)
        while True:
            command = (await self._read()).strip()
            lowered = command.lower()
            if lowered in ("", "n"):
                return index + 1
            if lowered == "p":
                if index == 0:
                    self.write("This is the first question.")
                    continue
                return index - 1
            if lowered == "s":
                self.choices[index] = None
                return index + 1
            if lowered == "q":
                raise _Cancelled

            literal = _unquote(command)
            if literal is not None:
                if not literal:
                    self.write("An answer cannot be empty.")
                    continue
                self.choices[index] = literal
                return index + 1

            # A bare number is a suggestion pick only when there are suggestions; otherwise it is text.
            number = _number(command)
            if number is not None and question.suggestions:
                if 1 <= number <= len(question.suggestions):
                    self.choices[index] = question.suggestions[number - 1]
                    return index + 1
                self.write(
                    f"Pick a number from 1 to {len(question.suggestions)}, "
                    f'or quote it ("{command}") to answer with that text.'
                )
                continue

            self.choices[index] = command
            return index + 1

    async def _review(self) -> int | None:
        """Show each choice under its question cut to one line, or a lone question's choice alone; return the index of
        a question to reopen, or None to submit."""
        chosen = sum(choice is not None for choice in self.choices)
        last = len(self.questions)
        self.write("")
        if last == 1:
            if chosen:
                self.write(f"Your answer: {self.choices[0]}")
                actions = "Enter: send it   e1: change it   c: cancel, send nothing"
            else:
                actions = "Nothing chosen. e1: answer it   Enter or c: leave it pending"
            again = actions
        else:
            self.write("Review your answers")
            # Lines stop short of the terminal's last column, after which some consoles wrap.
            columns = shutil.get_terminal_size().columns - 1
            for position, (question, choice) in enumerate(zip(self.questions, self.choices), 1):
                prefix = f"  {position}. "
                self.write(prefix + one_line(question.content, max(columns - len(prefix), MIN_REVIEW_WIDTH)))
                self.write(f"     {choice if choice is not None else '(skipped)'}")
            reopen = f"e<number>: reopen a question (e1-e{last})"
            if chosen:
                noun = "answer" if chosen == 1 else "answers"
                actions = f"Enter: send {chosen} {noun}   {reopen}   c: cancel, send nothing"
            else:
                actions = f"Nothing chosen. {reopen}   Enter or c: leave them all pending"
            again = f"Enter to send, e<number> to reopen a question (e1-e{last}), or c to cancel."
        self.write(actions)

        while True:
            command = (await self._read()).strip().lower()
            if command == "":
                return None
            if command in ("c", "q"):
                raise _Cancelled
            if command.startswith("e"):
                number = _number(command[1:].strip())
                if number is not None and 1 <= number <= last:
                    return number - 1
            self.write(again)

    async def _submit(self) -> None:
        result = self.result
        if any(choice is not None for choice in self.choices):
            try:
                live = await self.run_is_live()
            except Exception as e:
                project_id = self.questions[0].project_id
                self._report(f"Could not check that the run is still live, so nothing was sent: {e}")
                self._report(f"Answer again with: aristotle questions {project_id}")
                result.unanswered = list(self.questions)
                return
            if not live:
                self._report("The run has ended, so nothing was sent; no answer can reach Aristotle now.")
                result.unanswered = list(self.questions)
                result.run_ended = True
                return
        pending_numbers: list[int] = []
        for number, (question, choice) in enumerate(zip(self.questions, self.choices), 1):
            if choice is None:
                result.unanswered.append(question)
                pending_numbers.append(number)
                continue
            try:
                await question.answer(choice)
            except QuestionClosed as e:
                # Closed on the server; the remaining answers are still sent.
                self._report(f"Question {number}: {e}")
                result.closed.append(question)
                continue
            except Exception as e:
                # Any other failure leaves this question pending; the remaining answers are still sent.
                self._report(f"Question {number} could not be answered: {e}")
                result.unanswered.append(question)
                pending_numbers.append(number)
                continue
            self._report(f"Answered question {number}: {choice}")
            result.answered.append(question)

        if result.unanswered:
            noun = "Question" if len(pending_numbers) == 1 else "Questions"
            numbers = ", ".join(str(number) for number in pending_numbers)
            project_id = self.questions[0].project_id
            self._report(f"{noun} {numbers} still pending; answer later with: aristotle questions {project_id}")
