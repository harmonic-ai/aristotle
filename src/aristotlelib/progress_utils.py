import sys

from aristotlelib.printable import print_line

_CLEAR_SCREEN = "\033[2J\033[H"


def stdout_is_terminal() -> bool:
    """Whether stdout is a terminal; a replacement stdout without ``isatty`` is not."""
    isatty = getattr(sys.stdout, "isatty", None)
    return isatty is not None and isatty()


def print_progress_lines(lines: list[str]) -> None:
    """Print lines, after clearing the screen if stdout is a terminal."""
    if not lines:
        return
    if stdout_is_terminal():
        print(_CLEAR_SCREEN, end="")
    for line in lines:
        print_line(line)
    print("", end="", flush=True)


def clear_progress_lines() -> None:
    """Clear the screen if stdout is a terminal; write nothing otherwise."""
    if stdout_is_terminal():
        print(_CLEAR_SCREEN, end="", flush=True)
