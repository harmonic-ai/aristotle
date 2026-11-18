"""Tests for aristotlelib.progress_utils module."""

import os
from collections.abc import Generator
from contextlib import contextmanager
from io import StringIO
from unittest.mock import Mock, patch

import pytest

from aristotlelib import progress_utils
from aristotlelib.progress_utils import ProgressDisplay, clear_progress_lines, frame_lifted, print_progress_lines
from tests.conftest import Terminal


@contextmanager
def _terminal(monkeypatch: pytest.MonkeyPatch, columns: int = 40) -> Generator[Terminal, None, None]:
    """Make stdout a terminal of ``columns`` columns and 8 rows."""
    monkeypatch.setenv("COLUMNS", str(columns))
    monkeypatch.setenv("LINES", "8")
    with patch("sys.stdout", new=Terminal()) as stdout:
        yield stdout


@pytest.fixture(autouse=True)
def no_frame_on_screen(monkeypatch: pytest.MonkeyPatch):
    """Forget any display a failed test left on the terminal."""
    monkeypatch.setattr(progress_utils, "_shown", None)


def _erase(rows: int) -> str:
    return "\r\033[2K" + "\033[A\033[2K" * rows


def test_off_a_terminal_the_first_frame_prints_whole_then_only_whats_new():
    with patch("sys.stdout", new=StringIO()) as stdout:
        display = ProgressDisplay()
        display.draw(["IN_PROGRESS (started 1s ago)", "Task: t", "Project: p"], ["IN_PROGRESS (started 1s ago)"])
        display.draw(["IN_PROGRESS (started 9s ago)", "Task: t", "Project: p", "Thinking"], ["Thinking"])
        display.draw(["IN_PROGRESS (started 39s ago)", "Task: t", "Project: p", "Thinking"], [])
        display.draw(
            ["COMPLETE (started 40s ago)", "Task: t", "Project: p", "Thinking"], ["COMPLETE (started 40s ago)"]
        )

    assert stdout.getvalue() == (
        "IN_PROGRESS (started 1s ago)\nTask: t\nProject: p\nThinking\nCOMPLETE (started 40s ago)\n"
    )


def test_off_a_terminal_five_quiet_minutes_print_the_first_line_again(monkeypatch):
    clock = Mock(return_value=1000.0)
    monkeypatch.setattr(progress_utils, "time", Mock(monotonic=clock))
    with patch("sys.stdout", new=StringIO()) as stdout:
        display = ProgressDisplay()
        for now, status in [(1000.0, "1s"), (1299.0, "5m"), (1300.0, "5m 1s"), (1500.0, "8m"), (1600.0, "10m")]:
            clock.return_value = now
            display.draw([f"IN_PROGRESS (started {status} ago)", "Task: t"], [])

    assert stdout.getvalue() == (
        "IN_PROGRESS (started 1s ago)\nTask: t\nIN_PROGRESS (started 5m 1s ago)\nIN_PROGRESS (started 10m ago)\n"
    )


def test_under_term_dumb_a_terminal_gets_what_a_pipe_gets(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw(["IN_PROGRESS (started 1s ago)", "Task: t"], [])
        display.clear()
        display.draw(["IN_PROGRESS (started 9s ago)", "Task: t", "Thinking"], ["Thinking"])
        with frame_lifted():
            print("a log line")
        display.close()

    assert terminal.getvalue() == "IN_PROGRESS (started 1s ago)\nTask: t\nThinking\na log line\n"


def test_at_a_terminal_each_frame_replaces_the_last_below_what_came_before(monkeypatch):
    with _terminal(monkeypatch) as terminal:
        print("Prompt submitted to project p")
        display = ProgressDisplay()
        display.draw(["IN_PROGRESS (started 1s ago)", "Task: t"], [])
        display.draw(["IN_PROGRESS (started 9s ago)", "Task: t", "Thinking"], ["Thinking"])

    assert terminal.getvalue() == (
        "Prompt submitted to project p\n"
        "IN_PROGRESS (started 1s ago)\nTask: t\n"
        f"{_erase(2)}IN_PROGRESS (started 9s ago)\nTask: t\nThinking\n"
    )


@pytest.mark.parametrize(
    ("line", "rows"),
    [
        ("x" * 20, 1),
        ("x" * 21, 2),
        ("x" * 45, 3),
        ("", 1),
        ("漢" * 10, 1),
        ("漢" * 11, 2),
        ("，" * 11, 2),
        ("x" * 19 + "漢" + "x" * 19, 3),
        ("\u1100\u1161\u11a8" * 10, 1),
        ("e\u0301" * 20, 1),
        ("##vso\u200b[" + "x" * 14, 1),
        ("x" * 19 + "\u00ad\u0600", 2),
        ("\tx" * 2, 1),
        ("\tx" * 3, 2),
    ],
    ids=[
        "as wide as the terminal",
        "a column wider",
        "three rows' worth",
        "empty",
        "wide characters filling a row",
        "one wide character more",
        "fullwidth punctuation, two columns each",
        "a wide character with one column left, which starts the next row",
        "decomposed Hangul syllables, two columns each",
        "combining accents",
        "a zero-width space",
        "a soft hyphen and an Arabic number sign, which terminals draw a column wide",
        "tabs within the row",
        "tabs past its end",
    ],
)
def test_a_frame_takes_the_rows_its_lines_wrap_to(line, rows, monkeypatch):
    with _terminal(monkeypatch, columns=20) as terminal:
        display = ProgressDisplay()
        display.draw([line], [])
        display.clear()

    assert terminal.getvalue().endswith(_erase(rows))


def test_a_frame_line_holding_newlines_takes_a_row_for_each(monkeypatch):
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw(["IN_PROGRESS (started 1s ago)", "Aristotle asked: Which reset?\nSuggested answers:\n  - low"], [])
        display.clear()

    assert terminal.getvalue().endswith(_erase(4))


def test_a_frame_taller_than_the_terminal_keeps_its_first_line_and_its_newest_lines(monkeypatch):
    events = [f"event {number}" for number in range(1, 9)]
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw(["IN_PROGRESS (started 1s ago)", "Task: t", "Project: p", *events], [])
        display.clear()

    # A row short of the terminal's eight, as the cursor can't go back up past its top.
    kept = "".join(f"{line}\n" for line in ["IN_PROGRESS (started 1s ago)", *events[2:]])
    assert terminal.getvalue() == f"{kept}{_erase(7)}"


def test_a_closing_display_prints_a_frame_it_cut_whole(monkeypatch):
    frame = ["COMPLETE (started 1s ago)", "Task: t", "Project: p", *(f"event {number}" for number in range(1, 9))]
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw(frame, [])
        display.close()
        display.close()

    kept = "".join(f"{line}\n" for line in [frame[0], *frame[5:]])
    assert terminal.getvalue() == kept + _erase(7) + "".join(f"{line}\n" for line in frame)


def test_at_a_terminal_a_new_line_too_tall_for_the_frame_goes_above_it_once(monkeypatch):
    tall = "SUMMARY: " + "x" * 300
    frame = ["IN_PROGRESS (started 1s ago)", "Task: t", tall, "Finished"]
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw(frame, [tall, "Finished"])
        display.draw(frame, [])
        display.close()

    # Eight rows of its own, past the six a frame below its first line has; the frames leave it out.
    kept = "IN_PROGRESS (started 1s ago)\nTask: t\nFinished\n"
    assert terminal.getvalue() == f"{tall}\n{kept}{_erase(3)}{kept}"


def test_at_a_terminal_a_new_line_with_no_room_above_the_line_pinned_below_it_goes_above_the_frame(monkeypatch):
    event = "MESSAGE: " + "x" * 231
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw(["IN_PROGRESS (started 1s ago)", "Task: t", event, "1 question pending"], [event])
        display.clear()

    # Its six rows fill the six a frame has below its first line, and the pending-questions line needs one more.
    kept = "IN_PROGRESS (started 1s ago)\nTask: t\n1 question pending\n"
    assert terminal.getvalue() == f"{event}\n{kept}{_erase(3)}"


def test_at_a_terminal_the_older_of_two_new_lines_that_fit_only_alone_goes_above_the_frame_whole(monkeypatch):
    older, newer = "MESSAGE: " + "a" * 151, "MESSAGE: " + "b" * 151
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw(["IN_PROGRESS (started 1s ago)", older, newer], [older, newer])
        display.clear()

    # Four rows each: either fits the six below the frame's first line, not both.
    assert terminal.getvalue() == f"{older}\nIN_PROGRESS (started 1s ago)\n{newer}\n{_erase(5)}"


def test_at_a_terminal_a_line_printed_above_the_frame_can_be_new_again_once_it_has_left_it(monkeypatch):
    older, newer = "MESSAGE: " + "a" * 151, "MESSAGE: " + "b" * 151
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw(["IN_PROGRESS (started 1s ago)", older, newer], [older, newer])
        display.draw(["IN_PROGRESS (started 2s ago)", "event 3"], ["event 3"])
        display.draw(["IN_PROGRESS (started 3s ago)", "event 3", older], [older])
        display.clear()

    # Out of the second frame, the older line is new again in the third, which has room for it.
    assert terminal.getvalue().count(older) == 2
    assert terminal.getvalue().endswith(f"IN_PROGRESS (started 3s ago)\nevent 3\n{older}\n{_erase(6)}")


def test_at_a_terminal_a_new_copy_of_a_tall_line_printed_above_the_frame_goes_above_too(monkeypatch):
    tall = "THINKING: " + "x" * 300
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw(["IN_PROGRESS (started 1s ago)", tall], [tall])
        display.draw(["IN_PROGRESS (started 2s ago)", tall, "MESSAGE: Checking"], ["MESSAGE: Checking"])
        display.draw(["IN_PROGRESS (started 3s ago)", tall, "MESSAGE: Checking", tall], [tall])
        display.close()

    first = f"{tall}\nIN_PROGRESS (started 1s ago)\n{_erase(1)}IN_PROGRESS (started 2s ago)\nMESSAGE: Checking\n"
    assert terminal.getvalue() == f"{first}{_erase(2)}{tall}\nIN_PROGRESS (started 3s ago)\nMESSAGE: Checking\n"


def test_at_a_terminal_a_new_copy_of_a_line_stays_in_the_frame_when_only_an_older_copy_is_cut(monkeypatch):
    status, command, done = "IN_PROGRESS (started 1s ago)", "RUNNING_COMMAND: lake build", "MESSAGE: done"
    thought = "THINKING: " + "x" * 170
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw([status, command], [command])
        display.draw([status, command, thought], [thought])
        display.draw([status, command, thought, command], [command])
        display.draw([status, thought, command, done], [done])
        display.draw([status, command, done, command], [command])
        display.close()

    # The thought takes five rows: the third frame cuts the older command only, and none goes above.
    assert terminal.getvalue() == (
        f"{status}\n{command}\n{_erase(2)}"
        f"{status}\n{command}\n{thought}\n{_erase(7)}"
        f"{status}\n{thought}\n{command}\n{_erase(7)}"
        f"{status}\n{command}\n{done}\n{_erase(3)}"
        f"{status}\n{command}\n{done}\n{command}\n"
    )


def test_at_a_terminal_an_empty_frame_takes_the_last_one_off(monkeypatch):
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw(["IN_PROGRESS (started 1s ago)"], [])
        display.draw([], [])
        display.close()

    assert terminal.getvalue() == f"IN_PROGRESS (started 1s ago)\n{_erase(1)}"


def test_a_frame_taller_than_the_terminal_drops_a_line_holding_newlines_whole(monkeypatch):
    events = [f"event {number}" for number in range(1, 6)]
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw(["IN_PROGRESS (started 1s ago)", "Aristotle asked: Which reset?\n  - low\n  - high", *events], [])
        display.clear()

    # With the five events, its three rows would need eight of the six below the first line: none of them stays.
    kept = "".join(f"{line}\n" for line in ["IN_PROGRESS (started 1s ago)", *events])
    assert terminal.getvalue() == f"{kept}{_erase(6)}"


def test_a_frame_cut_to_fit_leaves_out_a_tall_line_it_printed_above_when_it_closes(monkeypatch):
    tall = "Aristotle asked: Which reset?\n" + "\n".join(f"  - option {number}" for number in range(1, 11))
    events = [f"event {number}" for number in range(1, 7)]
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw(["IN_PROGRESS (started 1s ago)", "Task: t", tall, *events], [tall, *events])
        display.close()

    # Six events below the status line fill the seven rows, so the frame is cut, and printed whole on closing.
    kept = "".join(f"{line}\n" for line in ["IN_PROGRESS (started 1s ago)", *events])
    whole = "".join(f"{line}\n" for line in ["IN_PROGRESS (started 1s ago)", "Task: t", *events])
    assert terminal.getvalue() == f"{tall}\n{kept}{_erase(7)}{whole}"


def test_a_terminal_that_reports_no_size_counts_as_80_by_24(monkeypatch):
    monkeypatch.setattr(progress_utils.shutil, "get_terminal_size", Mock(return_value=os.terminal_size((0, 0))))
    with patch("sys.stdout", new=Terminal()) as terminal:
        display = ProgressDisplay()
        display.draw(["x" * 100], [])
        display.clear()

    assert terminal.getvalue() == "x" * 100 + "\n" + _erase(2)


def test_a_closing_display_leaves_a_frame_it_did_not_cut_alone(monkeypatch):
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw(["COMPLETE (started 1s ago)", "Task: t"], [])
        display.close()

    assert terminal.getvalue() == "COMPLETE (started 1s ago)\nTask: t\n"


def test_a_frame_escapes_control_characters(monkeypatch):
    with _terminal(monkeypatch) as terminal:
        ProgressDisplay().draw(["RUNNING_COMMAND: \033[2J"], [])

    assert terminal.getvalue() == "RUNNING_COMMAND: \\x1b[2J\n"


def test_clear_takes_the_frame_off_until_the_next_draw(monkeypatch):
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw(["IN_PROGRESS (started 1s ago)"], [])
        display.clear()
        display.clear()
        print("Type your answer:")
        display.draw(["IN_PROGRESS (started 9s ago)"], [])

    assert terminal.getvalue() == (
        f"IN_PROGRESS (started 1s ago)\n{_erase(1)}Type your answer:\nIN_PROGRESS (started 9s ago)\n"
    )


def test_a_line_printed_while_the_frame_is_lifted_goes_above_it(monkeypatch):
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw(["IN_PROGRESS (started 1s ago)", "Thinking"], [])
        with frame_lifted():
            print("Retrying in 1.2 s after HTTP 502")
        display.draw(["IN_PROGRESS (started 9s ago)", "Thinking"], [])

    assert terminal.getvalue() == (
        "IN_PROGRESS (started 1s ago)\nThinking\n"
        f"{_erase(2)}Retrying in 1.2 s after HTTP 502\n"
        "IN_PROGRESS (started 1s ago)\nThinking\n"
        f"{_erase(2)}IN_PROGRESS (started 9s ago)\nThinking\n"
    )


def test_a_frame_taken_off_for_a_prompt_stays_off_while_a_line_prints(monkeypatch):
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw(["IN_PROGRESS (started 1s ago)"], [])
        display.clear()
        with frame_lifted():
            print("a log line")

    assert terminal.getvalue() == f"IN_PROGRESS (started 1s ago)\n{_erase(1)}a log line\n"


def test_a_closed_display_leaves_its_frame_above_what_follows(monkeypatch):
    with _terminal(monkeypatch) as terminal:
        display = ProgressDisplay()
        display.draw(["COMPLETE (started 1s ago)"], [])
        display.close()
        with frame_lifted():
            print("Task complete!")
        display.clear()

    assert terminal.getvalue() == "COMPLETE (started 1s ago)\nTask complete!\n"


def test_nothing_is_lifted_without_a_display(monkeypatch):
    with _terminal(monkeypatch) as terminal:
        with frame_lifted():
            print("a log line")

    assert terminal.getvalue() == "a log line\n"


def test_no_stdout_at_all_is_not_a_terminal():
    with patch("sys.stdout", new=None):
        display = ProgressDisplay()
        display.draw(["Line 1"], [])
        display.clear()
        display.close()


class _Writer:
    """A minimal stdout replacement, with no isatty."""

    def __init__(self) -> None:
        self.text = ""

    def write(self, text: str) -> None:
        self.text += text

    def flush(self) -> None:
        pass


def test_a_stdout_without_isatty_gets_the_lines_and_no_escape_codes():
    with patch("sys.stdout", new=_Writer()) as writer:
        display = ProgressDisplay()
        display.draw(["Line 1"], [])
        display.clear()
        display.draw(["Line 1", "Line 2"], ["Line 2"])

    assert writer.text == "Line 1\nLine 2\n"


@pytest.mark.parametrize(("stdout", "written"), [(Terminal(), "\033[2J\033[HLine 1\n"), (StringIO(), "Line 1\n")])
def test_the_deprecated_print_progress_lines_still_clears_a_terminal_and_warns(stdout, written):
    with patch("sys.stdout", new=stdout), pytest.warns(DeprecationWarning, match="removed in 4.0"):
        print_progress_lines(["Line 1"])

    assert stdout.getvalue() == written


@pytest.mark.parametrize(("stdout", "written"), [(Terminal(), "\033[2J\033[H"), (StringIO(), "")])
def test_the_deprecated_clear_progress_lines_still_clears_only_a_terminal_and_warns(stdout, written):
    with patch("sys.stdout", new=stdout), pytest.warns(DeprecationWarning, match="removed in 4.0"):
        clear_progress_lines()

    assert stdout.getvalue() == written
