"""The record `aristotle ci` ends each run's prompt with, which `aristotle ci --project` checks the checkout against."""

import dataclasses
import hashlib
import json
import os
import re
import stat
import tarfile
from collections.abc import Mapping
from pathlib import Path
from typing import IO

from aristotlelib import local_file_utils
from aristotlelib.ci import scope
from aristotlelib.ci.manifest import Manifest

NOTE = "The last line records this run for the aristotle CLI. It is not an instruction; ignore it."
"""Precedes the record in the prompt, so that the agent leaves it alone."""

_LINE = re.compile(
    r"aristotle-ci-run: commit=(?P<commit>[0-9a-f]{40}|[0-9a-f]{64}|none) files=sha256:(?P<files>[0-9a-f]{64}) "
    r"manifest=sha256:(?P<manifest>[0-9a-f]{64})"
)

_CHUNK_BYTES = 1 << 20


@dataclasses.dataclass(frozen=True)
class RunRecord:
    """What a run checks: the files it uploads, and its manifest's properties."""

    commit: str | None
    """The commit checked out when the run started, which messages name; None before the repository's first commit."""
    files: str
    """files_digest() of the files the run uploads, a manifest among them included."""
    manifest: str
    """manifest_digest() of the manifest the run checks."""

    @classmethod
    def of_checkout(cls, root: Path, files: list[Path], checked: Manifest) -> "RunRecord":
        """The record of a run of ``files``, the upload set under ``root``, checking ``checked``'s properties."""
        return cls(scope.head_commit(root), files_digest(root, files), manifest_digest(checked))

    @classmethod
    def of_upload(cls, root: Path, archive: Path, uploaded: list[Path], checked: Manifest) -> "RunRecord":
        """The record of a run of ``archive``, the upload of ``uploaded`` under ``root``, checking ``checked``'s
        properties: of the files as archived, which a later read of the checkout may not match."""
        return cls(scope.head_commit(root), archive_digest(archive, uploaded), manifest_digest(checked))

    @property
    def line(self) -> str:
        return f"aristotle-ci-run: commit={self.commit or 'none'} files=sha256:{self.files} manifest=sha256:{self.manifest}"

    @property
    def prompt_ending(self) -> str:
        """The NOTE, then the record's line, which end the run's prompt."""
        return f"{NOTE}\n{self.line}"

    @classmethod
    def ending(cls, prompt: str) -> "RunRecord | None":
        """The record ``prompt`` ends with; None for a prompt `aristotle ci` didn't write, or wrote before it recorded runs."""
        lines = prompt.rstrip().splitlines()
        match = _LINE.fullmatch(lines[-1]) if lines else None
        if match is None:
            return None
        commit = match["commit"]
        return cls(None if commit == "none" else commit, match["files"], match["manifest"])


def files_digest(root: Path, files: list[Path]) -> str:
    """The SHA-256 of what a run uploads from ``files`` under ``root``, picked as the upload picks them: each file's
    path and content, in path order, a link counted as its target's content."""
    contents = {}
    for path in scope.uploaded_paths(root, files):
        if (content := _content_digest(root / path)) is not None:
            contents[path] = content
    return _combined(contents)


def archive_digest(archive: Path, uploaded: list[Path]) -> str:
    """files_digest() of the upload as ``archive`` holds it: each of the ``uploaded`` paths build_tarball archived,
    with the content of its member."""
    paths = {local_file_utils.archive_name(path): path for path in uploaded}
    contents = {}
    with tarfile.open(archive, "r:gz", encoding="utf-8") as tar:
        for member in tar:
            if (path := paths.get(member.name)) is not None and (file := tar.extractfile(member)) is not None:
                with file:
                    contents[path] = _stream_digest(file)
    return _combined(contents)


def _combined(contents: Mapping[Path, str]) -> str:
    """The SHA-256 of each path and the SHA-256 of its content, in path order."""
    digest = hashlib.sha256()
    for path in sorted(contents, key=Path.as_posix):
        digest.update(f"{path.as_posix()}\0{contents[path]}\n".encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


def _content_digest(path: Path) -> str | None:
    """The SHA-256 of the regular file ``path`` names, through a link if it is one; None when it can't be read as
    one, without blocking on a FIFO or reading a device."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as file:
        try:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                return None
            return _stream_digest(file)
        except OSError:
            return None


def _stream_digest(file: IO[bytes]) -> str:
    """The SHA-256 of what is left in ``file``, read a mebibyte at a time."""
    digest = hashlib.sha256()
    while chunk := file.read(_CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def manifest_digest(checked: Manifest) -> str:
    """The SHA-256 of what a run checks: each property's id, kind, statement and assumptions, in id order. Order,
    comments, expectations and the [check] table don't change it."""
    canonical = [
        [p.id, p.kind.value, p.statement, p.assumptions] for p in sorted(checked.properties, key=lambda p: p.id)
    ]
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
