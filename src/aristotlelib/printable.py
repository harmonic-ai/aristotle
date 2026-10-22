"""Escaping for text the CLI prints, so text from a repository or from Aristotle cannot act on a terminal or a CI log."""

import logging
import os
import re

_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")
"""Control characters but tab and newline, the Unicode line and paragraph separators, and lone surrogates."""

_LOGGING_COMMAND = re.compile(r"##(vso|teamcity)\[")
"""Azure Pipelines and TeamCity run these anywhere in a line."""

_WORKFLOW_COMMAND = re.compile(r"^([^!-~\n]*)::", re.MULTILINE)
"""GitHub runs a line as a workflow command if it starts with ``::`` after whitespace, which anything but printable
ASCII may become when the runner decodes the output in another encoding."""

_BRACKETED_COMMAND = re.compile(r"##\[(?![^\s\]]{0,64}:|\s|[*+]\s*\])")
"""GitHub also runs ``##[<command>]`` anywhere in a line, the name running to the first space, and, while commands
are stopped, a stop token so; a first word with a colon in its first 65 characters, none, or ``*`` or ``+``, as an
SVA delay range has (``##[1:3]``, ``##[1:LATENCY]``, ``##[ 0 : $ ]``, ``##[*]``), names no command."""


def _escape(match: re.Match[str]) -> str:
    character = match.group()
    if "\udc80" <= character <= "\udcff":
        # A byte that wasn't UTF-8, kept by surrogateescape, as in a file name.
        return f"\\x{ord(character) - 0xDC00:02x}"
    return character.encode("unicode_escape").decode("ascii")


def escape_controls(text: str) -> str:
    """``text`` with control characters but tab and newline escaped, and a carriage return before a newline dropped."""
    return _CONTROL.sub(_escape, text.replace("\r\n", "\n"))


def printable(text: str) -> str:
    """escape_controls(``text``), with no line a CI system would run as a command.

    ``##vso[`` and ``##teamcity[`` take a zero-width space; on GitHub Actions a backslash also goes before a line's
    leading ``::``, and after every ``##[`` that could name a command, as _BRACKETED_COMMAND tells.
    """
    text = _LOGGING_COMMAND.sub("##\\1\u200b[", escape_controls(text))
    if os.environ.get("GITHUB_ACTIONS"):
        text = _BRACKETED_COMMAND.sub(r"##[\\", _WORKFLOW_COMMAND.sub(r"\1\\::", text))
    return text


def print_line(text: str = "") -> None:
    """print() ``text`` made printable()."""
    print(printable(text))


class PrintableFormatter(logging.Formatter):
    """Formats a record with another formatter, then makes the result printable()."""

    def __init__(self, inner: logging.Formatter | None) -> None:
        super().__init__()
        self._inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        return printable(self._inner.format(record))


def escape_logged_text() -> None:
    """Have every handler on the root logger print its records through printable(); calling it again changes nothing."""
    for handler in logging.root.handlers:
        if not isinstance(handler.formatter, PrintableFormatter):
            handler.setFormatter(PrintableFormatter(handler.formatter))
