import collections
import contextlib
import os
import shutil
import sys
import time
import unicodedata
import warnings
from collections.abc import Generator

from aristotlelib.printable import print_line, printable

_CLEAR_SCREEN = "\033[2J\033[H"

_DEPRECATED = "{} is deprecated and will be removed in 4.0: the CLI's wait no longer uses it."

# How long a display that can't redraw stays silent before it prints the status again: some CI services stop a job
# after ten minutes without output.
_QUIET_SECONDS = 300

_shown: "ProgressDisplay | None" = None
"""The display whose frame is on the terminal, until it closes."""

_DRAWN_FORMAT_CHARACTERS = frozenset(
    "\u00ad\u0600\u0601\u0602\u0603\u0604\u0605\u06dd\u070f\u0890\u0891\u08e2\U000110bd\U000110cd"
)
"""The format characters a terminal draws a column wide, as glibc's wcwidth() counts them: the soft hyphen, and the
signs that go before a number, such as the Arabic number sign."""


def stdout_is_terminal() -> bool:
    """Whether stdout is a terminal; a replacement stdout without ``isatty`` is not."""
    isatty = getattr(sys.stdout, "isatty", None)
    return isatty is not None and isatty()


def print_progress_lines(lines: list[str]) -> None:
    """Print lines, after clearing the screen if stdout is a terminal. Deprecated, and removed in 4.0."""
    warnings.warn(_DEPRECATED.format("print_progress_lines()"), DeprecationWarning, stacklevel=2)
    if not lines:
        return
    if stdout_is_terminal():
        print(_CLEAR_SCREEN, end="")
    for line in lines:
        print_line(line)
    print("", end="", flush=True)


def clear_progress_lines() -> None:
    """Clear the screen if stdout is a terminal; write nothing otherwise. Deprecated, and removed in 4.0."""
    warnings.warn(_DEPRECATED.format("clear_progress_lines()"), DeprecationWarning, stacklevel=2)
    if stdout_is_terminal():
        print(_CLEAR_SCREEN, end="", flush=True)


def _columns(character: str) -> int:
    """The columns ``character`` takes at a terminal: two for a wide one, none for a combining or invisible one, nor
    for the vowel and final consonant of a decomposed Hangul syllable, which share its first character's columns."""
    if unicodedata.category(character) in ("Mn", "Me", "Cf") and character not in _DRAWN_FORMAT_CHARACTERS:
        return 0
    if "\u1160" <= character <= "\u11ff" or "\ud7b0" <= character <= "\ud7ff":
        return 0
    return 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1


def _rows(line: str, columns: int) -> int:
    """The rows ``line`` takes at a terminal ``columns`` wide, which starts a character that doesn't fit on the next."""
    rows, column = 1, 0
    for character in line:
        width = _columns(character)
        if column + width > columns:
            rows, column = rows + 1, 0
        column += width
    return rows


def _height(text: str, columns: int) -> int:
    """The rows ``text``, made printable(), takes at a terminal ``columns`` wide."""
    return sum(_rows(line, columns) for line in printable(text).expandtabs().split("\n"))


def _first_kept(rows: list[int], height: int) -> int:
    """The index of the first line after the first that a frame whose lines take ``rows`` keeps at a terminal ``height``
    tall, which holds its first line and the newest that fit a row short of that height: the cursor can't go back up
    past the terminal's top."""
    start = 1
    while start < len(rows) and rows[0] + sum(rows[start:]) > height - 1:
        start += 1
    return start


def _with_ages(frame: list[str]) -> list[tuple[str, int]]:
    """Each line of ``frame`` with its age there: how many copies of it come after it."""
    later: collections.Counter[str] = collections.Counter()
    aged: list[tuple[str, int]] = []
    for line in reversed(frame):
        aged.append((line, later[line]))
        later[line] += 1
    return aged[::-1]


def _terminal_size() -> tuple[int, int]:
    """The terminal's columns and rows: 80 and 24 where it reports none, as Python 3.10 passes on from such a pty."""
    columns, rows = shutil.get_terminal_size()
    return columns or 80, rows or 24


def _write(text: str) -> None:
    print(text, end="", flush=True)


class ProgressDisplay:
    """A wait's progress on stdout. At a terminal that can move the cursor, each frame replaces the one before, below
    what was printed before the first; anywhere else, as in a pipe, a CI log or under TERM=dumb, the first frame is
    printed whole, then only the lines each later one says are new, and its first line again after a quiet stretch."""

    def __init__(self) -> None:
        # Whether frames replace each other at a terminal; otherwise only what's new is printed.
        self.redraws = stdout_is_terminal() and os.environ.get("TERM") != "dumb"
        self._drawn = False
        self._printed_at = 0.0
        # The last frame's lines at a terminal, but those printed above it; the rows they take there, 0 while they're
        # off, and whether lines were cut.
        self._lines: list[str] = []
        self._rows = 0
        self._cut = False
        # The copies of each line printed above the frame, by their age there.
        self._above: dict[str, set[int]] = {}

    def draw(self, frame: list[str], new: list[str]) -> None:
        """Show ``frame``, whose lines new since the last frame are ``new``; at a terminal, a new line the frame can't
        show whole goes above it once, as a logged message does. As in a window of events, a frame loses a line's
        oldest copies first, and a new line is its newest copy."""
        global _shown
        if not self.redraws:
            lines = new if self._drawn else frame
            if not lines and time.monotonic() - self._printed_at >= _QUIET_SECONDS:
                lines = frame[:1]
            for line in lines:
                print_line(line)
            if lines:
                self._printed_at = time.monotonic()
            self._drawn = True
            _write("")
            return
        columns, height = _terminal_size()
        copies, fresh = collections.Counter(frame), collections.Counter(new)
        self._age_above(copies, fresh)
        aged = [(line, age) for line, age in _with_ages(frame) if age not in self._above.get(line, ())]
        rows = [_height(line, columns) for line, _ in aged]
        # A line's newest copies are its new ones: one the frame can't show whole goes above it instead, once.
        dropped = [(line, age) for line, age in aged[1 : _first_kept(rows, height)] if age < fresh[line]]
        for line, age in dropped:
            self._above.setdefault(line, set()).add(age)
        self._lines = [line for line, age in aged if (line, age) not in dropped]
        above = "".join(f"{printable(line)}\n" for line, _ in dropped)
        _write(self._erase() + above + self._render(self._lines, columns, height))
        _shown = self

    def clear(self) -> None:
        """Take the frame off the terminal, as for a prompt, until the next draw."""
        _write(self._erase())

    def close(self) -> None:
        """Leave the frame on the terminal, whole, above whatever is printed next."""
        global _shown
        if self._rows and self._cut:
            _write(self._erase() + self._render(self._lines, *_terminal_size(), whole=True))
        self._rows = 0
        if _shown is self:
            _shown = None

    @contextlib.contextmanager
    def _lifted(self) -> Generator[None, None, None]:
        """Take the frame off the terminal while the body prints, then draw it again below; one that's off stays off."""
        if not self._rows:
            yield
            return
        self.clear()
        try:
            yield
        finally:
            _write(self._render(self._lines, *_terminal_size()))

    def _age_above(self, copies: collections.Counter[str], fresh: collections.Counter[str]) -> None:
        """Add each line's ``fresh`` copies to the ages of its copies printed above, and forget those the frame no longer
        holds: it holds the youngest ``copies`` of each line."""
        aged: dict[str, set[int]] = {}
        for line, ages in self._above.items():
            held = {age + fresh[line] for age in ages if age + fresh[line] < copies[line]}
            if held:
                aged[line] = held
        self._above = aged

    def _erase(self) -> str:
        """What erases the frame a row at a time, from the row below it up to its first row, where it leaves the cursor.
        Not an erase to the screen's end: from the top-left cell, tmux and GNU screen save the screen to the scrollback
        first."""
        rows, self._rows = self._rows, 0
        return "\r\033[2K" + "\033[A\033[2K" * rows if rows else ""

    def _render(self, frame: list[str], columns: int, height: int, whole: bool = False) -> str:
        """``frame`` as printed at a terminal ``columns`` by ``height``: made printable() and, unless ``whole``, kept a
        row short of its height by dropping lines after the first, the oldest first; sets ``_rows`` to the rows it
        takes."""
        rows = [_height(line, columns) for line in frame]
        start = 1 if whole else _first_kept(rows, height)
        self._cut = start > 1
        self._rows = sum(rows[:1] + rows[start:])
        return "".join(f"{printable(line).expandtabs()}\n" for line in frame[:1] + frame[start:])


def frame_lifted() -> contextlib.AbstractContextManager[None]:
    """A context in which no progress frame is on the terminal; one that was is drawn again after it."""
    return contextlib.nullcontext() if _shown is None else _shown._lifted()
