"""Tests for aristotlelib.progress_utils module."""

from io import StringIO
from unittest.mock import patch

import pytest

from aristotlelib.progress_utils import clear_progress_lines, print_progress_lines


def test_print_progress_lines_empty_list():
    """Test printing an empty list of lines (should do nothing)."""
    with patch("sys.stdout", new=StringIO()) as fake_stdout:
        print_progress_lines([])
        output = fake_stdout.getvalue()
        assert output == ""


def test_print_progress_lines_single_line():
    """Test printing a single progress line."""
    with patch("sys.stdout", new=StringIO()) as fake_stdout:
        print_progress_lines(["Progress: 50%"])
        output = fake_stdout.getvalue()
        assert "Progress: 50%" in output


def test_print_progress_lines_multiple_lines():
    """Test printing multiple progress lines."""
    with patch("sys.stdout", new=StringIO()) as fake_stdout:
        lines = ["Line 1", "Line 2", "Line 3"]
        print_progress_lines(lines)
        output = fake_stdout.getvalue()
        assert "Line 1" in output
        assert "Line 2" in output
        assert "Line 3" in output


class _Terminal(StringIO):
    def isatty(self) -> bool:
        return True


def test_progress_lines_clear_the_screen_first_on_a_terminal():
    with patch("sys.stdout", new=_Terminal()) as fake_stdout:
        print_progress_lines(["Line 1", "Line 2"])

    assert fake_stdout.getvalue() == "\033[2J\033[HLine 1\nLine 2\n"


def test_progress_lines_carry_no_escape_codes_when_stdout_is_not_a_terminal():
    with patch("sys.stdout", new=StringIO()) as fake_stdout:
        print_progress_lines(["Line 1", "Line 2"])

    assert fake_stdout.getvalue() == "Line 1\nLine 2\n"


@pytest.mark.parametrize(("stdout", "written"), [(_Terminal(), "\033[2J\033[H"), (StringIO(), "")])
def test_clear_progress_lines_clears_only_a_terminal(stdout, written):
    with patch("sys.stdout", new=stdout):
        clear_progress_lines()

    assert stdout.getvalue() == written


def test_no_stdout_at_all_is_not_a_terminal():
    with patch("sys.stdout", new=None):
        clear_progress_lines()
        print_progress_lines(["Line 1"])


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
        clear_progress_lines()
        print_progress_lines(["Line 1"])

    assert writer.text == "Line 1\n"
