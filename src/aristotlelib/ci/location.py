"""Resolution of a declaration to a file and line in the checkout."""

import dataclasses
import functools
import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from aristotlelib.declaration import Declaration

logger = logging.getLogger("aristotle")

MAX_SEARCHED_FILE_BYTES = 4 * 1024 * 1024
"""Files larger than this are not searched; the recorded line is used instead."""


@dataclasses.dataclass(frozen=True)
class SourceLocation:
    """A workspace-relative path, and a line within it when one is known."""

    path: str
    line: int | None = None


def _in_workspace(path: str | None, workspace: Path) -> str | None:
    """Return ``path`` as a workspace-relative file that exists, or None."""
    if not path:
        return None

    # Windows rules on every platform: POSIX rules pass `C:\keys` and `..\..`, which escape on a Windows runner.
    strict = PureWindowsPath(path)
    if strict.anchor or ".." in strict.parts:
        logger.debug(f"Ignoring out-of-workspace declaration path {path!r}")
        return None

    relative = PurePosixPath(path)
    # os.path, not pathlib: before 3.14, Path.is_file() raises for a path out of reach.
    if not os.path.isfile(workspace / relative):
        return None
    return relative.as_posix()


@functools.lru_cache(maxsize=128)
def _lines(absolute: Path) -> tuple[str, ...] | None:
    """The file's lines, or None when it cannot be searched. Cached: the checkout does not change during a run."""
    try:
        if absolute.stat().st_size > MAX_SEARCHED_FILE_BYTES:
            return None
        return tuple(absolute.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return None


def _anchored_line(absolute: Path, anchor: str | None) -> int | None:
    """The 1-based line whose stripped text equals the anchor, or None unless exactly one line matches."""
    wanted = (anchor or "").strip()
    if not wanted:
        return None

    lines = _lines(absolute)
    if lines is None:
        return None

    matches = [number for number, line in enumerate(lines, start=1) if line.strip() == wanted]
    if len(matches) != 1:
        logger.debug(f"{anchor!r} matches {len(matches)} lines of {absolute}; falling back to the recorded line")
        return None
    return matches[0]


def lines_in_checkout(path: str, workspace: Path) -> tuple[str, ...] | None:
    """The lines of the workspace-relative file ``path``, or None when it is not a searchable file there."""
    relative = _in_workspace(path, workspace)
    return None if relative is None else _lines(workspace / relative)


def line_in_checkout(declaration: Declaration, workspace: Path | None = None) -> int | None:
    """The declaration's line in ``source_file``: the anchor match, else the recorded line."""
    stored = declaration.source_line if declaration.source_line and declaration.source_line > 0 else None
    if workspace is None or not declaration.source_file:
        return stored

    relative = _in_workspace(declaration.source_file, workspace)
    if relative is None:
        return stored
    return _anchored_line(workspace / relative, declaration.source_anchor) or stored


def resolve(declaration: Declaration, workspace: Path) -> SourceLocation | None:
    """Locate a declaration in the checkout: the source file, else the generated artifact, else None."""
    source = _in_workspace(declaration.source_file, workspace)
    if source is not None:
        return SourceLocation(source, line_in_checkout(declaration, workspace))

    generated = _in_workspace(declaration.filename, workspace)
    if generated is not None:
        return SourceLocation(generated)

    return None


def describe(declaration: Declaration, workspace: Path | None = None) -> str | None:
    """``source_file:line`` for prose, or None when there is no source file."""
    if not declaration.source_file:
        return None

    line = line_in_checkout(declaration, workspace)
    return f"{declaration.source_file}:{line}" if line else declaration.source_file


def reported_path(declaration: Declaration) -> str | None:
    """The source file, else the generated artifact, without checking that either exists."""
    return declaration.source_file or declaration.filename or None
