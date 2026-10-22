"""Tests for the line-based question flow."""

import asyncio
import threading
import time
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import pydantic
import pytest

from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.question_flow import can_prompt, one_line, pending_footer, read_stdin_line, run_question_flow
from aristotlelib.question import Question, QuestionClosed

LONG_QUESTION = (
    "I've drafted three counter properties.\nPlease confirm the wording.\n\n"
    "1. SATURATES: once count reaches 63 it stays there until reset.\n"
    "2. HOLDS: count keeps its value on an edge where incr is low.\n\n"
    "Is this what you intend?"
)


@pytest.fixture(autouse=True)
def _fresh_stdin_reader(monkeypatch):
    """Each test starts with no stdin read in flight."""
    from aristotlelib.cli import question_flow

    monkeypatch.setattr(question_flow, "_outstanding_read", None)
    yield


def _question(number: int, suggestions: list[str] | None = None, content: str | None = None) -> Question:
    return Question(
        question_id=f"q-{number}",
        event_id=f"event-{number}",
        project_id="project-123",
        agent_run_id="task-456",
        content=f"Question number {number}?" if content is None else content,
        suggestions=[f"Option A{number}", f"Option B{number}"] if suggestions is None else suggestions,
        created_at=datetime(2024, 1, 1, 0, number),
        updated_at=datetime(2024, 1, 1, 0, number),
    )


def _scripted(*lines: str):
    """A read_line that returns the given lines, then raises EOFError."""
    remaining = list(lines)

    async def read_line() -> str:
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    return read_line


class _Recorder:
    """Captures the flow's output and the answers sent."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.answers: list[tuple[str, str]] = []

    def write(self, line: str) -> None:
        self.lines.append(line)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


async def _run(questions, *lines, deadline=60.0, read_line=None, failing=None, run_is_live=None):
    """Run the flow over scripted lines. ``failing`` maps question ids to the error answer() raises."""
    recorder = _Recorder()
    failing = failing or {}

    async def fake_answer(self, text):
        if self.question_id in failing:
            raise failing[self.question_id]
        recorder.answers.append((self.question_id, text))

    with patch.object(Question, "answer", autospec=True, side_effect=fake_answer):
        result = await run_question_flow(
            questions,
            read_line=read_line or _scripted(*lines),
            write=recorder.write,
            deadline_seconds=deadline,
            run_is_live=run_is_live or AsyncMock(return_value=True),
        )
    return result, recorder


async def test_pick_suggestions_by_number_and_submit():
    q1, q2 = _question(1), _question(2)

    result, recorder = await _run([q1, q2], "1", "2", "")

    assert recorder.answers == [("q-1", "Option A1"), ("q-2", "Option B2")]
    assert result.answered == [q1, q2]
    assert result.unanswered == []
    assert not result.cancelled
    assert "Question 1 of 2" in recorder.lines
    assert "Question 2 of 2" in recorder.lines
    assert "  1. Option A1" in recorder.lines
    assert "  2. Option B1" in recorder.lines
    assert "Review your answers" in recorder.lines
    assert "Answered question 1: Option A1" in recorder.lines
    assert "Answered question 2: Option B2" in recorder.lines
    assert result.summary == ["Answered question 1: Option A1", "Answered question 2: Option B2"]


async def test_free_text_answer():
    result, recorder = await _run([_question(1)], "Use a synchronous reset", "")

    assert recorder.answers == [("q-1", "Use a synchronous reset")]
    assert "Your answer: Use a synchronous reset" in recorder.lines


async def test_enter_moves_on_and_p_comes_back():
    result, recorder = await _run([_question(1), _question(2)], "", "p", "1", "2", "")

    assert recorder.answers == [("q-1", "Option A1"), ("q-2", "Option B2")]
    # Question 1 was shown, left, and shown again after "p".
    assert recorder.lines.count("Question 1 of 2") == 2


async def test_p_on_the_first_question_stays_put():
    result, recorder = await _run([_question(1)], "p", "1", "")

    assert "This is the first question." in recorder.lines
    assert recorder.answers == [("q-1", "Option A1")]


async def test_skip_leaves_the_question_unanswered():
    q1, q2 = _question(1), _question(2)

    result, recorder = await _run([q1, q2], "s", "1", "")

    assert recorder.answers == [("q-2", "Option A2")]
    assert result.answered == [q2]
    assert result.unanswered == [q1]
    assert result.pending_count == 1
    assert "     (skipped)" in recorder.lines
    assert "Question 1 still pending; answer later with: aristotle questions project-123" in recorder.lines


async def test_cancel_at_review_sends_nothing():
    q1, q2 = _question(1), _question(2)

    result, recorder = await _run([q1, q2], "1", "2", "c")

    assert recorder.answers == []
    assert result.cancelled
    assert result.answered == []
    assert result.unanswered == [q1, q2]
    assert "Cancelled. Nothing was sent; the questions stay pending." in recorder.lines
    assert result.summary == ["Cancelled. Nothing was sent; the questions stay pending."]


async def test_q_cancels_from_a_question():
    result, recorder = await _run([_question(1), _question(2)], "1", "q")

    assert recorder.answers == []
    assert result.cancelled


async def test_edit_from_review_reopens_that_question():
    result, recorder = await _run([_question(1), _question(2)], "1", "1", "e2", "2", "")

    assert recorder.answers == [("q-1", "Option A1"), ("q-2", "Option B2")]
    # The reopened question shows what had been chosen before it was changed.
    assert "Current answer: Option A2" in recorder.lines
    assert recorder.lines.count("Review your answers") == 2


async def test_edit_from_review_returns_to_the_review():
    """Forward from a reopened question returns to the review."""
    result, recorder = await _run([_question(1), _question(2)], "1", "2", "e1", "2", "")

    assert recorder.answers == [("q-1", "Option B1"), ("q-2", "Option B2")]
    assert recorder.lines.count("Question 1 of 2") == 2
    assert recorder.lines.count("Question 2 of 2") == 1
    assert recorder.lines.count("Review your answers") == 2


async def test_skip_after_reopen_clears_the_earlier_choice():
    q1, q2 = _question(1), _question(2)

    result, recorder = await _run([q1, q2], "1", "2", "e1", "s", "")

    assert recorder.answers == [("q-2", "Option B2")]
    assert result.unanswered == [q1]
    assert "     (skipped)" in recorder.lines


async def test_p_from_a_reopened_question_walks_the_set_normally():
    result, recorder = await _run([_question(1), _question(2)], "1", "2", "e2", "p", "2", "", "")

    assert recorder.answers == [("q-1", "Option B1"), ("q-2", "Option B2")]
    assert recorder.lines.count("Question 2 of 2") == 3
    assert recorder.lines.count("Review your answers") == 2


async def test_review_rejects_nonsense_and_asks_again():
    result, recorder = await _run([_question(1), _question(2)], "1", "1", "e9", "banana", "")

    assert recorder.answers == [("q-1", "Option A1"), ("q-2", "Option A2")]
    assert recorder.lines.count("Enter to send, e<number> to reopen a question (e1-e2), or c to cancel.") == 2


async def test_a_lone_question_s_confirm_repeats_its_keys_after_nonsense():
    result, recorder = await _run([_question(1)], "1", "e9", "banana", "")

    assert recorder.answers == [("q-1", "Option A1")]
    assert recorder.lines.count("Enter: send it   e1: change it   c: cancel, send nothing") == 3


async def test_a_lone_question_is_confirmed_by_its_answer_without_being_shown_again():
    result, recorder = await _run([_question(1, content=LONG_QUESTION)], "1", "")

    assert recorder.answers == [("q-1", "Option A1")]
    assert sum("Is this what you intend?" in line for line in recorder.lines) == 1
    assert "Review your answers" not in recorder.lines
    assert recorder.lines[-4:] == [
        "",
        "Your answer: Option A1",
        "Enter: send it   e1: change it   c: cancel, send nothing",
        "Answered question 1: Option A1",
    ]


async def test_e1_at_a_lone_question_s_confirm_changes_the_answer():
    result, recorder = await _run([_question(1)], "1", "e1", "2", "")

    assert recorder.answers == [("q-1", "Option B1")]
    assert recorder.lines.count("Question 1 of 1") == 2
    assert "Current answer: Option A1" in recorder.lines
    assert "Your answer: Option B1" in recorder.lines


@pytest.mark.parametrize("command", ["c", "q"])
async def test_cancel_at_a_lone_question_s_confirm_sends_nothing(command):
    q1 = _question(1)

    result, recorder = await _run([q1], "1", command)

    assert recorder.answers == []
    assert result.cancelled
    assert result.unanswered == [q1]


async def test_enter_at_a_lone_skipped_question_s_confirm_leaves_it_pending():
    q1 = _question(1)

    result, recorder = await _run([q1], "s", "")

    assert recorder.answers == []
    assert not result.cancelled
    assert result.unanswered == [q1]
    assert recorder.lines[-3:] == [
        "",
        "Nothing chosen. e1: answer it   Enter or c: leave it pending",
        "Question 1 still pending; answer later with: aristotle questions project-123",
    ]


async def test_the_review_shows_each_question_on_one_line_cut_to_the_terminal(monkeypatch):
    monkeypatch.setenv("COLUMNS", "60")

    result, recorder = await _run([_question(1, content=LONG_QUESTION), _question(2)], "1", "2", "")

    review = recorder.lines.index("Review your answers")
    assert recorder.lines[review + 1 : review + 5] == [
        "  1. I've drafted three counter properties. Please confi...",
        "     Option A1",
        "  2. Question number 2?",
        "     Option B2",
    ]
    assert len(recorder.lines[review + 1]) == 59
    assert sum("Is this what you intend?" in line for line in recorder.lines) == 1


async def test_the_review_keeps_part_of_each_question_in_a_narrow_terminal(monkeypatch):
    monkeypatch.setenv("COLUMNS", "10")

    result, recorder = await _run([_question(1, content=LONG_QUESTION), _question(2)], "1", "2", "")

    review = recorder.lines.index("Review your answers")
    assert recorder.lines[review + 1] == "  1. I've drafted thre..."


def test_one_line_collapses_whitespace_and_cuts_to_the_width():
    assert one_line(" Which\n\n  reset?\t", 20) == "Which reset?"
    assert one_line("x" * 10, 10) == "x" * 10
    assert one_line("x" * 11, 10) == "x" * 7 + "..."


async def test_deadline_expiry_submits_what_was_chosen():
    q1, q2 = _question(1), _question(2)
    lines = ["1"]

    async def slow_read_line() -> str:
        if lines:
            return lines.pop(0)
        # The deadline must fire before this returns.
        await asyncio.sleep(5)
        return "2"

    result, recorder = await _run([q1, q2], deadline=0.2, read_line=slow_read_line)

    assert result.timed_out
    assert recorder.answers == [("q-1", "Option A1")]
    assert result.answered == [q1]
    assert result.unanswered == [q2]
    assert "Out of time. Sending the answers chosen so far." in recorder.lines
    assert "Question 2 still pending; answer later with: aristotle questions project-123" in recorder.lines


async def test_deadline_expiry_submits_what_was_chosen_with_an_injected_clock():
    """The pre-read deadline check, driven by a patched clock."""
    q1, q2 = _question(1), _question(2)
    now = [0.0]
    typed = ["1"]

    async def read_line() -> str:
        # Each answer takes longer than the whole budget.
        now[0] += 700
        return typed.pop(0) if typed else "2"

    with patch("time.monotonic", side_effect=lambda: now[0]):
        result, recorder = await _run([q1, q2], deadline=600, read_line=read_line)

    assert result.timed_out
    assert recorder.answers == [("q-1", "Option A1")]
    assert result.unanswered == [q2]
    assert "Out of time. Sending the answers chosen so far." in recorder.lines


async def test_invalid_number_is_rejected_and_reprompted():
    result, recorder = await _run([_question(1)], "3", "0", "2", "")

    assert recorder.answers == [("q-1", "Option B1")]
    assert 'Pick a number from 1 to 2, or quote it ("3") to answer with that text.' in recorder.lines
    assert 'Pick a number from 1 to 2, or quote it ("0") to answer with that text.' in recorder.lines


async def test_a_digit_int_rejects_is_an_answer_not_a_crash():
    """Digits str.isdigit accepts but int() rejects are read as text."""
    result, recorder = await _run([_question(1), _question(2)], "\u00b2", "\u0663", "")

    assert recorder.answers == [("q-1", "\u00b2"), ("q-2", "\u0663")]


async def test_a_superscript_at_the_review_is_rejected_and_reprompted():
    result, recorder = await _run([_question(1)], "1", "e\u00b2", "")

    assert recorder.answers == [("q-1", "Option A1")]
    assert recorder.lines.count("Enter: send it   e1: change it   c: cancel, send nothing") == 2


async def test_a_run_of_digits_longer_than_int_parses_is_an_answer_not_a_crash():
    digits = "1" * 5000

    result, recorder = await _run([_question(1)], digits, "e" + digits, "")

    assert recorder.answers == [("q-1", digits)]
    assert recorder.lines.count("Enter: send it   e1: change it   c: cancel, send nothing") == 2


async def test_empty_suggestions_still_allow_free_text():
    result, recorder = await _run([_question(1, suggestions=[])], "42", "")

    assert recorder.answers == [("q-1", "42")]
    # No numbered list between the text and the prompt.
    assert recorder.lines[1:4] == ["Question 1 of 1", "Question number 1?", "Type your answer."]


async def test_quoting_answers_with_a_reserved_letter_or_a_number():
    result, recorder = await _run([_question(1), _question(2)], '"n"', "'2'", "")

    assert recorder.answers == [("q-1", "n"), ("q-2", "2")]


async def test_an_empty_quoted_answer_is_refused():
    result, recorder = await _run([_question(1)], '""', "1", "")

    assert "An answer cannot be empty." in recorder.lines
    assert recorder.answers == [("q-1", "Option A1")]


async def test_a_closed_question_is_reported_and_the_rest_still_go():
    q1, q2 = _question(1), _question(2)
    failing = {"q-1": QuestionClosed("This question is already closed")}

    result, recorder = await _run([q1, q2], "1", "2", "", failing=failing)

    assert recorder.answers == [("q-2", "Option B2")]
    assert result.closed == [q1]
    assert result.answered == [q2]
    assert result.unanswered == []
    assert "Question 1: This question is already closed" in recorder.lines


async def test_a_server_error_keeps_the_question_pending():
    q1, q2 = _question(1), _question(2)
    failing = {"q-1": AristotleAPIError("API request failed with status 500: boom", status_code=500)}

    result, recorder = await _run([q1, q2], "1", "2", "", failing=failing)

    assert recorder.answers == [("q-2", "Option B2")]
    assert result.unanswered == [q1]
    assert result.answered == [q2]
    assert "Question 1 could not be answered: API request failed with status 500: boom" in recorder.lines


async def test_a_malformed_answer_response_keeps_the_question_pending():
    """A pydantic ValidationError, itself a ValueError, is not treated as a closed question."""
    q1, q2 = _question(1), _question(2)
    with pytest.raises(pydantic.ValidationError) as excinfo:
        Question.model_validate({"question_id": "q-1"})
    failing = {"q-1": excinfo.value}

    result, recorder = await _run([q1, q2], "1", "2", "", failing=failing)

    assert recorder.answers == [("q-2", "Option B2")]
    assert result.closed == []
    assert result.unanswered == [q1]
    assert result.answered == [q2]
    assert any(line.startswith("Question 1 could not be answered: ") for line in recorder.lines)
    assert "Question 1 still pending; answer later with: aristotle questions project-123" in recorder.lines


async def test_eof_is_a_cancel():
    result, recorder = await _run([_question(1), _question(2)])

    assert result.cancelled
    assert recorder.answers == []


async def test_eof_at_review_is_a_cancel():
    result, recorder = await _run([_question(1)], "1")

    assert result.cancelled
    assert recorder.answers == []


async def test_enter_at_review_with_nothing_chosen_sends_nothing():
    q1, q2 = _question(1), _question(2)

    result, recorder = await _run([q1, q2], "", "", "")

    assert recorder.answers == []
    assert not result.cancelled
    assert result.unanswered == [q1, q2]
    assert "Nothing chosen. e<number>: reopen a question (e1-e2)   Enter or c: leave them all pending" in recorder.lines


async def test_no_questions_is_a_no_op():
    result, recorder = await _run([])

    assert result.answered == []
    assert result.unanswered == []
    assert recorder.lines == []


async def test_single_suggestion_prompt_reads_naturally():
    result, recorder = await _run([_question(1, suggestions=["Only this"])], "1", "")

    assert "Type 1 to pick the suggestion, or type your own answer." in recorder.lines
    assert recorder.answers == [("q-1", "Only this")]


def test_pending_footer_pluralises():
    assert pending_footer(1, "project-123") == "1 question pending — answer with: aristotle questions project-123"
    assert pending_footer(3, "project-123") == "3 questions pending — answer with: aristotle questions project-123"


async def test_read_stdin_line_returns_the_line_and_passes_eof_through():
    with patch("builtins.input", return_value="hello"):
        assert await read_stdin_line() == "hello"
    with patch("builtins.input", side_effect=EOFError):
        with pytest.raises(EOFError):
            await read_stdin_line()


async def test_read_stdin_line_asks_again_for_a_line_that_does_not_decode(capsys):
    undecodable = UnicodeDecodeError("utf-8", b"caf\xc3!", 3, 4, "invalid continuation byte")
    with patch("builtins.input", side_effect=[undecodable, "café"]) as mock_input:
        assert await read_stdin_line() == "café"

    assert mock_input.call_count == 2
    assert "That line could not be read as text; type it again." in capsys.readouterr().out


def test_read_stdin_line_does_not_keep_the_loop_alive_after_a_timeout():
    """An abandoned read must not keep the loop alive at exit."""
    release = threading.Event()

    def blocked_input(prompt: str) -> str:
        # Bounded so a regression fails the time check instead of hanging.
        release.wait(10)
        return "late"

    async def abandon_a_read() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(read_stdin_line(), timeout=0.05)

    started = time.monotonic()
    try:
        with patch("builtins.input", blocked_input):
            asyncio.run(abandon_a_read())
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert elapsed < 5


async def test_an_abandoned_read_hands_its_line_to_the_next_prompt_instead_of_losing_it():
    """A line typed at the next prompt completes the abandoned read; no second reader competes for input."""
    typed = threading.Event()
    calls: list[int] = []

    def slow_input(prompt: str = "") -> str:
        calls.append(1)
        typed.wait(5)
        return "answer for question 2"

    with patch("builtins.input", slow_input):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(read_stdin_line(), timeout=0.05)
        next_prompt = asyncio.create_task(read_stdin_line())
        await asyncio.sleep(0.05)
        typed.set()
        assert await next_prompt == "answer for question 2"

    assert len(calls) == 1


async def test_a_line_typed_while_no_prompt_is_showing_is_not_taken_as_the_next_answer():
    from aristotlelib.cli import question_flow

    typed = threading.Event()

    def stale_input(prompt: str = "") -> str:
        typed.wait(5)
        return "typed between prompts"

    with patch("builtins.input", stale_input):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(read_stdin_line(), timeout=0.05)
        typed.set()
        for _ in range(100):
            if question_flow._outstanding_read.done():
                break
            await asyncio.sleep(0.01)
        assert question_flow._outstanding_read.done()

    with patch("builtins.input", return_value="answer for the prompt on screen"):
        assert await read_stdin_line() == "answer for the prompt on screen"


async def test_a_run_that_ended_before_sending_gets_no_answers():
    questions = [_question(1, ["Yes", "No"]), _question(2)]
    run_is_live = AsyncMock(return_value=False)

    result, recorder = await _run(questions, "1", "free text", "", run_is_live=run_is_live)

    run_is_live.assert_awaited_once()
    assert recorder.answers == []
    assert result.run_ended
    assert result.unanswered == questions
    assert result.answered == []
    assert recorder.lines[-1] == "The run has ended, so nothing was sent; no answer can reach Aristotle now."


async def test_the_run_is_not_checked_when_nothing_was_chosen():
    run_is_live = AsyncMock(return_value=False)

    result, recorder = await _run([_question(1)], "s", "", run_is_live=run_is_live)

    run_is_live.assert_not_awaited()
    assert not result.run_ended
    assert len(result.unanswered) == 1


def _terminal(stdin_tty: bool = True, stdout_tty: bool = True):
    """Patch stdin and stdout with streams reporting the given isatty()."""
    stdin, stdout = Mock(), Mock()
    stdin.isatty.return_value = stdin_tty
    stdin.fileno.return_value = 0
    stdout.isatty.return_value = stdout_tty
    return patch.multiple("aristotlelib.cli.question_flow.sys", stdin=stdin, stdout=stdout)


@pytest.mark.parametrize(("stdin_tty", "stdout_tty"), [(False, True), (True, False), (False, False)])
def test_can_prompt_needs_stdin_and_stdout_to_be_terminals(stdin_tty, stdout_tty):
    with (
        _terminal(stdin_tty, stdout_tty),
        patch("os.tcgetpgrp", return_value=7, create=True),
        patch("os.getpgrp", return_value=7, create=True),
    ):
        assert can_prompt() is False


def test_can_prompt_in_the_foreground_job():
    with (
        _terminal(),
        patch("os.tcgetpgrp", return_value=7, create=True),
        patch("os.getpgrp", return_value=7, create=True),
    ):
        assert can_prompt() is True


def test_can_prompt_refuses_a_background_job():
    """Reading the terminal from a background job would stop the process with SIGTTIN."""
    with (
        _terminal(),
        patch("os.tcgetpgrp", return_value=7, create=True),
        patch("os.getpgrp", return_value=8, create=True),
    ):
        assert can_prompt() is False


def test_can_prompt_when_stdin_is_not_the_controlling_terminal():
    """tcgetpgrp fails on a terminal that is not the controlling one, which cannot stop the process."""
    with _terminal(), patch("os.tcgetpgrp", side_effect=OSError(25, "Inappropriate ioctl"), create=True):
        assert can_prompt() is True


def test_can_prompt_without_job_control():
    with _terminal(), patch("aristotlelib.cli.question_flow.os", Mock(spec=[])):
        assert can_prompt() is True


def test_can_prompt_without_stdin():
    with patch("aristotlelib.cli.question_flow.sys.stdin", None):
        assert can_prompt() is False


async def test_nothing_is_sent_when_the_run_cannot_be_checked():
    questions = [_question(1), _question(2)]
    run_is_live = AsyncMock(side_effect=AristotleAPIError("bad gateway", status_code=502))

    result, recorder = await _run(questions, "1", "", "", run_is_live=run_is_live)

    assert recorder.answers == []
    assert result.unanswered == questions
    assert not result.run_ended
    assert recorder.lines[-2:] == [
        "Could not check that the run is still live, so nothing was sent: bad gateway",
        "Answer again with: aristotle questions project-123",
    ]
