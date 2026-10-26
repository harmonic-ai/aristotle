import asyncio
import contextvars
import dataclasses
import errno
import io
import json
import logging
import os
import re
import stat
import tarfile
import tempfile
import time
import uuid
from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import ExitStack, asynccontextmanager, nullcontext, suppress
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from urllib.parse import quote, urlencode

import pydantic

from aristotlelib import api_request, local_file_utils
from aristotlelib.agent_task import AgentTask, TaskStatus
from aristotlelib.aristotle_object import AristotleObject, SafeIntEnum
from aristotlelib.date_utils import UTCDatetime, format_relative_time
from aristotlelib.declaration import Declaration, DeclarationType
from aristotlelib.endpoints import WEB_URL
from aristotlelib.shell import shell_hint
from aristotlelib.verilog import bundle as verilog_bundle

logger = logging.getLogger("aristotle")
ARISTOTLE_WEB_BASE_URL = WEB_URL


def validate_prompt(prompt: str) -> None:
    if not prompt.strip():
        raise ValueError("Prompt cannot be empty")


def files_refused_while_running(project_id: str, files: str) -> str:
    """The refusal of ``files``, as the caller names them, with a prompt to project ``project_id`` while it runs a task,
    saying what to do instead."""
    return (
        f"Files can't be added to project {project_id} while it runs a task. Send the prompt without {files} to reach "
        f"that task: its agent reads it right away. Or wait for the task to finish (`aristotle show {project_id} "
        f"--wait`), then send the prompt with {files}. "
        # Holds until a task started after a cancel carries over the cancelled task's prompts.
        "Cancelling the task first would lose the prompts it was given, except the project's first."
    )


def validate_project_name(name: str) -> str:
    """Return the name without surrounding whitespace; raise ValueError if nothing is left."""
    name = name.strip()
    if not name:
        raise ValueError("Project name cannot be empty")
    return name


def validate_project_dir(project_dir: Path | str) -> Path:
    """Return ``project_dir`` as a Path; raise ValueError for the empty string, which Path reads as "."."""
    if isinstance(project_dir, str) and not project_dir:
        raise ValueError("Project directory cannot be empty; use . for the current directory")
    return Path(project_dir)


def continuation_uploads(
    files: Sequence[Path | str], *, project_dir: Path | str | None = None
) -> list[tuple[Path, str]]:
    """Local files and their upload paths relative to ``project_dir`` (cwd by default).

    Relative file arguments still name files from cwd. Keep their names inside the root, including names of links
    within it; refuse traversal, files outside it (also through links), duplicate paths and names multipart cannot
    preserve. Raise ValueError before any request when a selection cannot be sent.
    """
    root = Path(os.path.abspath(validate_project_dir(project_dir if project_dir is not None else ".")))
    if not os.path.isdir(root):
        raise ValueError(f"Project directory is not a directory: {root}")
    real_root = Path(os.path.realpath(root))
    uploads: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for file in files:
        path = Path(file)
        if ".." in path.parts:
            raise ValueError(f"File path {path} must not contain ..")
        path = Path(os.path.abspath(path))
        if not path.is_relative_to(root) or not Path(os.path.realpath(path)).is_relative_to(real_root):
            raise ValueError(f"File {path} is outside project directory {root}")
        if not os.path.isfile(path):
            raise ValueError(f"{path} is not a file")
        if not os.access(path, os.R_OK):
            raise ValueError(f"{path} can't be read")
        relative = path.relative_to(root)
        name = relative.as_posix()
        if not local_file_utils.is_utf8_text(name):
            if not local_file_utils.is_utf8_name(relative):
                raise ValueError(
                    f"the name of {local_file_utils.archive_name(path)} is not UTF-8, which Aristotle can't take; "
                    "rename the file to send it"
                )
            name = local_file_utils.archive_name(relative)
        if name[1:2] == ":" or any(char in '\\"' or ord(char) < 32 or ord(char) == 127 for char in name):
            raise ValueError(f"File path {name!r} cannot be preserved in an upload; rename the file to send it")
        if name in seen:
            raise ValueError(f"File path {name!r} is selected more than once")
        seen.add(name)
        uploads.append((path, name))
    return uploads


def _folder_name(folder: Path) -> str:
    """``folder``'s name as given, so that a link keeps its own; for "." or "..", which have none, the name of the folder
    the OS opens, as the walk reads it; empty for a root or a working folder that is gone."""
    if folder.name not in ("", ".."):
        return folder.name
    try:
        return Path(os.path.realpath(folder)).name
    except OSError:
        return ""


def validate_project_id(project_id: str) -> str:
    """Return the id in canonical UUID form; raise ValueError if it is not a UUID."""
    try:
        return str(uuid.UUID(project_id))
    except ValueError:
        raise ValueError(f"'{project_id}' is not a project id (a UUID, as `aristotle list` shows)") from None


def same_project_id(project_id: str, other: str) -> bool:
    """Whether two ids are equal, or the same UUID whatever their case and dashes."""
    if project_id == other:
        return True
    try:
        return validate_project_id(project_id) == validate_project_id(other)
    except ValueError:
        return False


_UNSAFE_IN_FILE_NAME = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
#: Names Windows takes for a device, whatever their case and extension, as Python 3.13's os.path.isreserved has them.
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{c}" for c in "123456789\u00b9\u00b2\u00b3"}
    | {f"LPT{c}" for c in "123456789\u00b9\u00b2\u00b3"}
)


def _device_name(name: str) -> bool:
    """Whether Windows takes ``name`` for a device, whatever its case and extension, such as CON or nul.tar.gz."""
    return name.partition(".")[0].rstrip(" ").upper() in _WINDOWS_DEVICE_NAMES


def _safe_file_name(name: str) -> str:
    """The name as one file name on any OS: separators, controls and <>:"|?* become "_", outer dots and spaces go, and
    a Windows device name, such as CON or nul.tar.gz, gets a leading "_"."""
    name = _UNSAFE_IN_FILE_NAME.sub("_", name).strip(". ")
    if _device_name(name):
        return f"_{name}"
    return name


#: Errors refusing a file beside ``path``, its permissions or access control list, or the move over it, which writing
#: ``path`` in place may still get past: a folder that takes no new file or is mounted read-only, another user's file
#: in a sticky folder, a file mounted on its own, a file system that can't move a file or set its mode.
_REFUSALS = frozenset(
    {errno.EACCES, errno.EPERM, errno.EBUSY, errno.EXDEV, errno.EROFS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}
)


#: Extended attributes holding an access control list, which a replacement must carry not to widen who can read it.
_ACL_ATTRIBUTES = ("system.posix_acl_access", "system.nfs4_acl")


def _carry_acl(source: Path, fd: int) -> None:
    """Give the file open as ``fd`` the access control list ``source`` has beyond its mode, and none it hasn't: one the
    file took from its folder's default goes, as it could grant what ``source`` doesn't. Where NFSv4 won't let it go,
    a PermissionError, so the caller writes in place instead, as for any refusal."""
    if not hasattr(os, "listxattr"):
        return
    try:
        names = os.listxattr(source)
    except OSError as e:
        if e.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            return
        raise
    inherited = os.listxattr(fd)
    for name in _ACL_ATTRIBUTES:
        if name in names:
            os.setxattr(fd, name, os.getxattr(source, name))
        elif name in inherited:
            try:
                os.removexattr(fd, name)
            except OSError as e:
                # Linux's NFSv4 client never removes its ACL (EINVAL), a refusal like those in _REFUSALS.
                if e.errno != errno.EINVAL:
                    raise
                raise PermissionError(errno.EPERM, f"{name} can't be dropped") from e


def _write_whole(path: Path, content: bytes) -> None:
    """Write ``content`` to ``path``, keeping what was there if the write fails: a new file, or a regular file that can
    be written, is replaced whole. Where that is refused, and for anything else, such as a device, a pipe, a link or a
    read-only file, ``path`` is written in place."""
    try:
        mode: int | None = os.lstat(path).st_mode
    except FileNotFoundError:
        mode = None
    replaceable = mode is None or (stat.S_ISREG(mode) and os.access(path, os.W_OK))
    if not (replaceable and _replace(path, content, mode)):
        path.write_bytes(content)


def _replace(path: Path, content: bytes, mode: int | None) -> bool:
    """Write ``content`` beside ``path``, never readable more widely than ``mode`` and ``path``'s access control list,
    then move it into place with them. On failure ``path`` is as it was and nothing is left beside it; False where the
    file beside it, its access or the move was refused, rather than the write."""
    # Beside a file it replaces, the new one starts owner-only: no one opens it before it has that file's ACL and mode.
    created = 0o666 if mode is None else 0o600
    temporary = path.with_name(f".aristotle-{uuid.uuid4().hex[:8]}.part")
    writing = False
    try:
        with open(temporary, "xb", opener=lambda name, flags: os.open(name, flags, created)) as file:
            if mode is not None:
                _carry_acl(path, file.fileno())
            writing = True
            file.write(content)
        writing = False
        if mode is not None:
            os.chmod(temporary, mode & 0o777)
        os.replace(temporary, path)
    except BaseException as e:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        if not writing and isinstance(e, OSError) and e.errno in _REFUSALS:
            return False
        raise
    return True


def _names_directory(destination: Path | str) -> bool:
    """Whether destination is an existing directory, or a string ending in a path separator."""
    return os.path.isdir(Path(destination)) or (isinstance(destination, str) and destination.endswith(("/", os.sep)))


def validate_destination(destination: Path | str, *, folder: bool = False) -> None:
    """Raise ValueError if get_files could not save to destination, or, with ``folder``, save_files under it, as a
    folder: a non-directory on its path, or no write access; with ``folder``, also a place inside a .git folder or a
    git directory."""
    path = Path(destination)
    directory = folder or _names_directory(destination)
    what = "the files" if folder else "the archive"
    if folder and _in_git_files(path):
        raise ValueError(f"Cannot save the files to {destination}: it is inside a .git folder or a git directory")
    if os.path.exists(path) and not directory:
        target = path
    else:
        target = path if directory else path.parent
        while not os.path.lexists(target) and target != target.parent:
            target = target.parent
        if not os.path.isdir(target):
            raise ValueError(f"Cannot save {what} to {destination}: {target} is not a directory")
    if not os.access(target, (os.W_OK | os.X_OK) if os.path.isdir(target) else os.W_OK):
        raise ValueError(f"Cannot save {what} to {destination}: {target} is not writable")


def _windows_unsavable(segment: str) -> bool:
    """Whether Windows would save ``segment`` under another name, as a stream, or as a device."""
    return bool(_UNSAFE_IN_FILE_NAME.search(segment)) or segment.endswith((".", " ")) or _device_name(segment)


def _as_git_compares(segment: str) -> str:
    """``segment`` as git compares it with a name it looks for on any file system: without an NTFS stream, and as
    local_file_utils.folded_name() folds it."""
    return local_file_utils.folded_name(segment.split(":", 1)[0])


def _names_dot_git(segment: str) -> bool:
    """Whether git reads ``segment`` as .git on some file system, or as its NTFS short name GIT~1."""
    return _as_git_compares(segment) in (".git", "git~1")


def _in_git_files(path: Path | str) -> bool:
    """Whether ``path``, as named or with links followed, is or is inside a .git folder or file, in any spelling
    _names_dot_git() takes, or is inside a git directory by what it holds, as a bare repository is."""
    if any(_names_dot_git(part) for part in Path(os.path.abspath(path)).parts):
        return True
    real = Path(os.path.realpath(path))
    return any(_names_dot_git(part) for part in real.parts) or local_file_utils.in_git_dir(real)


def file_path_error(file_path: str) -> str | None:
    """Why ``file_path`` isn't a path in a project that saves as named inside any folder: relative, of named segments
    joined by "/", with no drive, no .git folder and no file git reads as a repository's HEAD, and on Windows of names it
    can hold; None when it is."""
    if not file_path:
        return "an empty path names no file"
    if "\0" in file_path:
        return "a path in the project can't hold a NUL"
    if "\\" in file_path:
        return "a path in the project uses /, not a backslash"
    if file_path.startswith("/"):
        return "a path in the project is relative, so it can't start with /"
    # One character and a colon is a drive to Windows, as C: is, and as 1: is after subst.
    if file_path[1:2] == ":":
        return "a path in the project can't start with a drive"
    segments = file_path.split("/")
    if ".." in segments or "." in segments:
        return f"a path in the project can't have a {'..' if '..' in segments else '.'} segment"
    if "" in segments:
        return "a path in the project can't have an empty segment, as // or a / at its end leaves"
    if any(_names_dot_git(segment) for segment in segments):
        return "a path in the project can't name a .git folder"
    if _as_git_compares(segments[-1]) == "head":
        return "a path in the project can't name a file HEAD: git takes a folder that holds one for a repository"
    if os.name == "nt":
        for segment in segments:
            if _windows_unsavable(segment):
                return f"Windows can't save a file or folder named {segment!r}"
    return None


def saved_path(folder: Path | str, file_path: str) -> Path:
    """Where Project.save_files saves ``file_path``, a path in the project: at that path under ``folder``.

    Raises:
        ValueError: If file_path_error() refuses ``file_path``, ``folder`` is inside a .git folder or a git directory,
            or links lead its place out of ``folder``, into a .git folder or a git directory, or to a file HEAD.
    """
    if (error := file_path_error(file_path)) is not None:
        raise ValueError(error)
    if _in_git_files(folder):
        raise ValueError(f"{folder} is inside a .git folder or a git directory")
    target = Path(folder, *file_path.split("/"))
    real = Path(os.path.realpath(target))
    if not real.is_relative_to(os.path.realpath(folder)):
        raise ValueError(f"{target} is outside {folder} once links are followed")
    if _in_git_files(target):
        raise ValueError(f"{target} is inside a .git folder or a git directory once links are followed")
    if _as_git_compares(real.name) == "head":
        raise ValueError(
            f"{target} is a file HEAD once links are followed: git takes a folder that holds one for a repository"
        )
    return target


ARTIFACTS_FOLDER = "aristotle-artifacts"
"""Where `aristotle download --artifact` saves the files without --destination, as the commands the CLI prints do."""

_PLAIN_PATH = re.compile(r"[A-Za-z0-9_.+/-]+")
"""Characters every shell reads as written outside quotes."""


def _pastable(file_path: str) -> bool:
    """Whether ``file_path`` is one file_path_error() takes on every OS, of characters no shell reads otherwise."""
    return (
        bool(_PLAIN_PATH.fullmatch(file_path))
        # Python 3.10's argparse drops a value of --, even after =.
        and file_path != "--"
        and file_path_error(file_path) is None
        and not any(_windows_unsavable(segment) for segment in file_path.split("/"))
    )


def download_artifacts_hint(project_id: str, file_paths: Sequence[str]) -> str | None:
    """How to save into ARTIFACTS_FOLDER, by default, those of ``file_paths``, paths in the project, that every OS saves
    as named and every shell reads as written, unquoted: the `aristotle download` command, the same for each shell, its
    lead counting them when it leaves any out; None when it leaves out all."""
    pastable = [file_path for file_path in file_paths if _pastable(file_path)]
    if not pastable:
        return None
    command = ["aristotle", "download", project_id]
    for file_path in pastable:
        # Else a path that starts with "-" reads as an option.
        command += [f"--artifact={file_path}"] if file_path.startswith("-") else ["--artifact", file_path]
    whole = len(pastable) == len(file_paths)
    lead = "Download them with" if whole else f"Download {len(pastable)} of the {len(file_paths)} with"
    return shell_hint(lead, command)


@dataclasses.dataclass(frozen=True)
class Archived:
    """What build_archive() archived."""

    names: list[str]
    """Each file, by its path in the archive."""
    left_out: list[Path]
    """The folder's files an ignore rule kept out, relative to it."""
    included: list[str] = dataclasses.field(default_factory=list)
    """With ``include_ignored``, the files that went up though an ignore rule matches them, by their paths in the
    archive; else none."""
    added: list[str] = dataclasses.field(default_factory=list)
    """The files that went up only because of ``include_ignored``, by their paths in the archive: those of ``included``
    that no block needs, and the links to them."""

    def summary(self) -> str:
        return local_file_utils.upload_summary(len(self.names), len(self.left_out), len(self.included))


class ArchiveError(OSError):
    """A file couldn't be archived, as when one of the block's went away after it was read. It carries the original
    error's errno, strerror and filename, and reads as local_file_utils.archive_error words it."""

    message: str = ""

    @classmethod
    def wrapping(cls, error: OSError) -> "ArchiveError":
        # OSError's own constructor, so that pickle and copy, which call it with errno, strerror and filename, work.
        archive_error = cls(error.errno, error.strerror, error.filename)
        archive_error.message = local_file_utils.archive_error(error)
        return archive_error

    def __str__(self) -> str:
        return self.message or super().__str__()


def check_readable(project_dir: Path) -> None:
    """Raise LeanProjectError if ``project_dir`` can't be listed or entered, or check_withheld() refuses it. Before
    anything reads it: such a folder would otherwise pass for one with nothing to upload, fail on the first path in it,
    or upload what it holds."""
    check_withheld(project_dir)
    try:
        with os.scandir(project_dir) as entries:
            next(entries, None)
        if os.name != "nt":
            # Fails without the search permission a lookup in it needs, which listing doesn't; pathlib drops the ".".
            os.stat(os.path.join(project_dir, os.curdir))
    except OSError as e:
        raise local_file_utils.LeanProjectError(
            f"Nothing to upload: {project_dir} can't be read ({e.strerror or e})."
        ) from e


def check_withheld(folder: Path) -> None:
    """Raise LeanProjectError if ``folder`` is in a .git folder or a git directory, or in a folder that holds
    credentials, as named or resolved: the files below it would go up under names no rule withholds."""
    absolute, real = Path(os.path.abspath(folder)), Path(os.path.realpath(folder))
    if local_file_utils.is_git_dir(real):
        raise local_file_utils.LeanProjectError(
            f"Nothing to upload: {folder} is a git directory, and nothing in one is uploaded."
        )
    if local_file_utils.in_git_folder(absolute) or local_file_utils.in_git_folder(real):
        raise local_file_utils.LeanProjectError(
            f"Nothing to upload: {folder} is inside a .git folder, and nothing in one is uploaded."
        )
    if local_file_utils.in_git_dir(real):
        raise local_file_utils.LeanProjectError(
            f"Nothing to upload: {folder} is inside a git directory, and nothing in one is uploaded."
        )
    if local_file_utils.is_in_secret_folder(absolute / "_") or local_file_utils.is_in_secret_folder(real / "_"):
        raise local_file_utils.LeanProjectError(
            f"Nothing to upload: {folder} is in a folder that holds credentials, and nothing in one is uploaded."
        )


def build_archive(
    project_dir: Path | None,
    tar_file_path: Path,
    block: verilog_bundle.Bundle | None = None,
    *,
    include_ignored: bool = False,
) -> Archived:
    """Archive what submit uploads to ``tar_file_path``: ``project_dir`` as Project.create_from_directory describes
    it, with ``include_ignored`` too, with ``block``'s files and without those its black boxes leave out, from the
    folder too, which the ``aristotle`` logger then names; without ``project_dir``, ``block``'s files and its
    ``aristotle.f`` alone.

    Raises:
        LeanProjectError: nothing in ``project_dir`` would go up, or without ``block`` an ignore rule leaves out each of
            its Verilog sources and no .lean, .c or .rs file goes up either, git could not say which files it
            ignores, ``project_dir`` has an ``_ext`` folder where the block's files from outside it would go, or the
            archive is over local_file_utils.MAX_FILE_SIZE_BYTES.
        verilog_bundle.BundleError: a file of the block changed after it was read, or a .v, .sv, .vh or .svh file in
            ``project_dir`` that would go up declares a black box beside another design unit.
        ArchiveError: a file couldn't be read, or was replaced while it was archived, or the archive written.
    """
    try:
        return _build_archive(project_dir, tar_file_path, block, include_ignored)
    except OSError as e:
        raise ArchiveError.wrapping(e) from e


def _build_archive(
    project_dir: Path | None, tar_file_path: Path, block: verilog_bundle.Bundle | None, include_ignored: bool
) -> Archived:
    # Built-in exclusions and .gitignore are checked separately so the summary counts only the latter.
    candidates: list[Path] = []
    empty = False
    found = 0
    skipped: list[Path] = []
    unreadable: list[Path] = []
    junctions: set[Path] = set()
    if project_dir is not None:
        for dirpath, dirnames, filenames in os.walk(
            project_dir, onerror=lambda e: unreadable.append(Path(e.filename).relative_to(project_dir))
        ):
            rel_dir = Path(dirpath).relative_to(project_dir)
            if rel_dir == Path("."):
                empty = not dirnames and not filenames
            # A git directory under any name too, as a vendored lib.git with a token in its config.
            dirnames[:] = [
                d
                for d in local_file_utils.get_pruned_dirnames(dirnames, rel_dir)
                if not local_file_utils.is_git_dir(Path(dirpath, d))
            ]
            if include_ignored:
                junctions.update(rel_dir / d for d in dirnames if local_file_utils.is_junction(Path(dirpath, d)))
            for filename in filenames:
                found += 1
                rel_path = rel_dir / filename
                if local_file_utils.should_skip_input_file(rel_path):
                    skipped.append(rel_path)
                    logger.debug(f"Skipping ignored file: {rel_path}")
                    continue
                if block is not None and block.leaves_out(project_dir / rel_path):
                    continue
                candidates.append(rel_path)
    reached = (
        frozenset([Path(block.filelist_name), *(Path(m.archive) for m in block.members if not block.is_outside(m))])
        if block
        else frozenset()
    )
    # With include_ignored every candidate goes up whatever the ignore rules, so none is named as held back.
    ignored = (
        local_file_utils.gitignored_files(
            project_dir, candidates, frozenset(candidates) if include_ignored else reached
        )
        if project_dir is not None
        else set()
    )
    if junctions:
        # include_ignored adds nothing reached through a junction, as nothing goes up through a folder's link.
        behind_junction = {p for p in ignored if not junctions.isdisjoint(p.parents)}
        candidates = [p for p in candidates if p not in behind_junction]
        ignored -= behind_junction
    gitignored = set() if include_ignored else ignored
    if block is not None and project_dir is not None and block.blackboxes:
        block.leave_out_unnamed(
            [
                project_dir / p
                for p in candidates
                if p.suffix.lower() in verilog_bundle.VERILOG_SUFFIXES
                and p not in gitignored
                and p not in reached
                and not local_file_utils.is_secret_path(p, project_dir)
                and local_file_utils.is_utf8_name(p)
            ],
            "goes up with the folder",
            ignorable=not include_ignored,
        )
        candidates = [p for p in candidates if not block.leaves_out(project_dir / p)]
        logger.info(verilog_bundle.blackbox_note(block))

    secrets: list[Path] = []
    results: list[Path] = []
    not_utf8: list[Path] = []
    archived: set[tuple[int, int]] = set()
    symlinks: list[tuple[Path, Path, str]] = []
    skipped_symlinks: dict[Path, str] = {}
    members = {m.archive: m for m in block.members} if block is not None else {}
    generated = block.filelist_text if block is not None else None
    filelist_name = block.filelist_name if block is not None else verilog_bundle.FILELIST_NAME
    if block is not None and generated is None and block.committed is not None:
        members.setdefault(filelist_name, verilog_bundle.Member(filelist_name, block.committed, ""))
    outside = {m.archive for m in members.values() if block is not None and block.is_outside(m)}
    if clash := sorted(outside & {p.as_posix() for p in candidates if p not in gitignored}):
        raise local_file_utils.LeanProjectError(
            f"{project_dir} has a file at {clash[0]}, where a file its aristotle.f names from outside it would go "
            "up. Rename that file or its folder."
        )
    going_up = {p.as_posix() for p in candidates if p not in gitignored} | set(members)
    if overlap := _file_and_folder(going_up | ({filelist_name} if generated is not None else set())):
        upload = f"{project_dir}'s upload" if project_dir is not None else "The upload"
        raise local_file_utils.LeanProjectError(
            f"{upload} can't hold both {overlap[0]} and {overlap[1]}, which needs {overlap[0]} to be a folder. Rename "
            "one of them or its folder."
        )
    reached_ignored: list[str] = []
    included: list[str] = []
    linked: list[str] = []
    included_files: set[tuple[int, int] | None] = set()
    names: list[str] = []
    # In UTF-8, as Aristotle reads it, whatever the locale's encoding; with dereference, no file goes in as a hard link.
    with tarfile.open(tar_file_path, "w:gz", encoding="utf-8", dereference=True) as tar:
        for rel_path in candidates:
            assert project_dir is not None
            if generated is not None and rel_path.as_posix() == filelist_name:
                continue
            if rel_path.as_posix() in outside:
                # An ignored copy where a file from outside goes; that file goes up after the walk.
                continue
            if (member := members.pop(rel_path.as_posix(), None)) is not None:
                verilog_bundle.write_member(tar, block, member, archived)
                names.append(member.archive)
                if rel_path in ignored:
                    reached_ignored.append(member.archive)
                continue
            if rel_path in gitignored:
                logger.debug(f"Skipping gitignored file: {rel_path}")
                continue
            if local_file_utils.is_secret_path(rel_path, project_dir):
                secrets.append(rel_path)
                continue
            if local_file_utils.is_result_archive(rel_path):
                results.append(rel_path)
                continue
            arcname = str(rel_path)
            if not local_file_utils.is_utf8_text(arcname):
                if not local_file_utils.is_utf8_name(rel_path):
                    not_utf8.append(rel_path)
                    continue
                # UTF-8 that the locale can't decode, as under an ASCII one, goes up as the text it spells.
                arcname = local_file_utils.archive_name(rel_path)

            file_path = project_dir / rel_path
            file_stat = os.lstat(file_path)
            if stat.S_ISLNK(file_stat.st_mode):
                symlinks.append((file_path, rel_path, arcname))
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                logger.debug(f"Skipping non-regular file: {rel_path}")
                skipped.append(rel_path)
                continue

            local_file_utils.add_file(tar, file_path, arcname, file_stat)
            names.append(rel_path.as_posix())
            identity = local_file_utils.file_identity(file_stat)
            if identity is not None:
                archived.add(identity)
            if rel_path in ignored:
                included.append(rel_path.as_posix())
                included_files.add(identity)

        # The block's files the walk didn't reach: from outside the folder, or behind a linked folder.
        for member in members.values():
            verilog_bundle.write_member(tar, block, member, archived)
            names.append(member.archive)

        # After the walk, so that every file a link may point to has been archived.
        for link, rel_path, arcname in symlinks:
            try:
                target = local_file_utils.add_symlinked_file(tar, link, arcname, archived)
            except local_file_utils.SkippedSymlinkError as e:
                skipped_symlinks[rel_path] = str(e)
                continue
            names.append(rel_path.as_posix())
            if rel_path in ignored:
                included.append(rel_path.as_posix())
            elif target in included_files:
                linked.append(rel_path.as_posix())

        # Last, so that it names only the -I folders that hold an uploaded file.
        if block is not None and generated is not None:
            block.filelist_text = block.generate(set(names))
            verilog_bundle.write_text(tar, filelist_name, block.filelist_text)
            names.append(filelist_name)

    local_file_utils.log_excluded_secrets(sorted(secrets))
    if results:
        one = len(results) == 1
        logger.info(
            f"Not uploading {len(results)} {'result archive an earlier run' if one else 'result archives earlier runs'}"
            f" saved; rename {'it' if one else 'one'} to upload it:{local_file_utils.listing(sorted(results))}"
        )
    if reached_ignored:
        logger.info(
            f"Uploading {len(reached_ignored)} {'file' if len(reached_ignored) == 1 else 'files'} an ignore rule "
            f"matches, since the Verilog block needs {'it' if len(reached_ignored) == 1 else 'them'}: "
            + ", ".join(sorted(reached_ignored))
        )
    if not_utf8:
        logger.warning(local_file_utils.not_utf8_warning(sorted(not_utf8), "file"))
    if skipped_symlinks:
        logger.warning("WARNING: " + local_file_utils.skipped_symlinks_warning(skipped_symlinks))
    # Not when nothing goes up and a folder couldn't be read, which nothing_to_upload names.
    if project_dir is not None and block is None and (names or not unreadable):
        _check_verilog_left_out(project_dir, names, gitignored)
    if project_dir is not None and not names:
        raise local_file_utils.LeanProjectError(
            local_file_utils.nothing_to_upload(
                project_dir, empty, found, len(gitignored), sorted(skipped), sorted(unreadable)
            )
        )
    if too_large := local_file_utils.upload_too_large(tar_file_path.stat().st_size):
        raise local_file_utils.LeanProjectError(
            f"{too_large} Exclude large files with .gitignore or submit a smaller folder."
        )
    # A committed aristotle.f an ignore rule matches goes up, or a generated one does in its place.
    kept = {*reached_ignored, *([filelist_name] if block is not None else [])}
    archive_names = [local_file_utils.archive_name(Path(name)) for name in names]
    return Archived(
        archive_names,
        sorted(p for p in gitignored if p.as_posix() not in kept),
        _archive_names([*included, *reached_ignored] if include_ignored else []),
        _archive_names([*included, *linked]),
    )


def _archive_names(names: list[str]) -> list[str]:
    """``names``, paths as the walk spells them, as archive_name() spells them in the archive, sorted."""
    return sorted(local_file_utils.archive_name(Path(name)) for name in names)


_VERILOG_SOURCES = (".v", ".sv")
"""Suffixes of a Verilog source file; a header, such as ``.vh``, isn't one."""

_OTHER_SOURCES = (".lean", ".c", ".rs")
"""Suffixes of a Lean, C or Rust file."""

_LEFT_OUT_SHOWN = 5
"""How many of the Verilog sources an ignore rule leaves out _check_verilog_left_out names."""


def _check_verilog_left_out(project_dir: Path, names: list[str], ignored: set[Path]) -> None:
    """When ``ignored``, the files of ``project_dir`` an ignore rule leaves out, hold a Verilog source and ``names``, the
    upload's paths, hold none: raise LeanProjectError if ``names`` hold no .lean, .c or .rs file either, else warn."""
    suffixes = [PurePosixPath(name).suffix.lower() for name in names]
    if any(suffix in _VERILOG_SOURCES for suffix in suffixes):
        return
    left_out = sorted(p for p in ignored if p.suffix.lower() in _VERILOG_SOURCES)
    if not left_out:
        return
    stop = not any(suffix in _OTHER_SOURCES for suffix in suffixes)
    rules = local_file_utils.left_out_rules(project_dir, left_out[:_LEFT_OUT_SHOWN])
    lines = [
        f"No Verilog source {'would go' if stop else 'goes'} up from {project_dir}: an ignore rule matches each of its "
        ".v and .sv files:"
    ]
    lines += [f"  {local_file_utils.archive_name(p)}" + (f"  ({rule})" if rule else "") for p, rule in rules.items()]
    if len(left_out) > _LEFT_OUT_SHOWN:
        lines.append(f"  and {len(left_out) - _LEFT_OUT_SHOWN} more")
    how = (
        f"{local_file_utils.INCLUDE_IGNORED_HINT}, or describe the design with a filelist, which uploads every file it "
        f"names, even one an ignore rule matches: {project_dir / verilog_bundle.FILELIST_NAME}, -F <filelist>, or "
        "--top <module> with -f <filelist> (from Python, the block argument)."
    )
    if stop:
        lines.append(
            "No .lean, .c or .rs file would go up either, so the run would have no design to verify. To upload them, "
            + how
        )
        raise local_file_utils.LeanProjectError("\n".join(lines))
    lines.append(f"To verify them too, {how}")
    logger.warning("WARNING: " + "\n".join(lines))


def _file_and_folder(names: set[str]) -> tuple[str, str] | None:
    """Two of ``names``, paths in an upload, that it can't hold together, a file and a name inside it; None if no name
    is another's folder. An archive with both can't be extracted."""
    for name in sorted(names):
        for parent in PurePosixPath(name).parents:
            if str(parent) in names:
                return str(parent), name
    return None


class ProjectHasNoFilesError(api_request.AristotleAPIError):
    """The project has neither output nor input files to download."""


PROJECT_ID_PREFIX_MIN_LENGTH = 8
"""Fewest hex digits of an id that Project.resolve_id accepts as a prefix."""

PROJECT_LOOKUP_LIMIT = 2000
"""How many of the caller's most recent projects Project.resolve_id searches for a name or id prefix."""

create_sent: contextvars.ContextVar[bool] = contextvars.ContextVar("create_sent", default=False)
"""Set once a Project.create() in this context starts sending its request, after which Aristotle may act on it however
the command ends."""

PROMPT_NOT_SENT = "The prompt was not sent, so it is safe to send it again."
"""What an AristotleNotProcessedError for a prompt says to do."""

PROJECT_NOT_CREATED = "No project was created, so it is safe to submit it again."
"""What an AristotleNotProcessedError for a create says to do."""

lookup_at_terminal: contextvars.ContextVar[bool] = contextvars.ContextVar("lookup_at_terminal", default=False)
"""Whether someone at a terminal waits on this context's lookups, which then say they are searching only after
_LOOKUP_NOTE_SECONDS, rather than as their second page starts."""

_LOOKUP_PAGE_SIZE = 100
_LOOKUP_PAGE_INTERVAL = 0.5
"""Least seconds between asking for two lookup pages, counting a refused page from its answered retry, once the
per-second limit has refused one of the lookup's requests; the server allows each user 10 requests a second."""
_LOOKUP_NOTE_SECONDS = 1.5
_AMBIGUOUS_MATCHES_SHOWN = 10


class AmbiguousProjectError(ValueError):
    """A project name or id prefix matches more than one project; ``matches`` holds them, newest first."""

    def __init__(self, reference: str, matches: list["Project"]):
        lines = [f"'{reference}' matches {len(matches)} projects. Give the id of one instead:"]
        for project in matches[:_AMBIGUOUS_MATCHES_SHOWN]:
            created = format_relative_time(project.created_at)
            lines.append(
                f"  {project.project_id}  {project.description or '-'} ({project.status.name}, created {created})"
            )
        if len(matches) > _AMBIGUOUS_MATCHES_SHOWN:
            lines.append(f"  and {len(matches) - _AMBIGUOUS_MATCHES_SHOWN} more")
        super().__init__("\n".join(lines))
        self.reference = reference
        self.matches = matches

    def __reduce__(self) -> tuple[type["AmbiguousProjectError"], tuple[str, list["Project"]]]:
        """Rebuilds from the constructor's arguments; args holds only the message."""
        return type(self), (self.reference, self.matches)


def _no_match_message(reference: str, by_prefix: bool, short_hex: bool, searched_all: bool) -> str:
    """Why resolve_id found nothing: the kind of lookup made, the window searched, and a prefix that is too short."""
    if by_prefix:
        message = f"No project has the id prefix or name '{reference}'"
    else:
        message = f"No project is named '{reference}'"
    if searched_all:
        message += "."
    else:
        message += f" among your {PROJECT_LOOKUP_LIMIT:,} most recent projects. Use the full id of an older one."
    if short_hex:
        message += f" An id prefix needs at least {PROJECT_ID_PREFIX_MIN_LENGTH} characters."
    return message


@asynccontextmanager
async def _logged_if_slow(message: str) -> AsyncGenerator[None, None]:
    """Log ``message`` at INFO if the context is still open after _LOOKUP_NOTE_SECONDS."""
    timer = asyncio.get_running_loop().call_later(_LOOKUP_NOTE_SECONDS, logger.info, message)
    try:
        yield
    finally:
        timer.cancel()


def project_web_url(project_id: str) -> str:
    """The project's page in Aristotle's web app."""
    return f"{api_request.get_web_url()}/projects/{quote(project_id, safe='')}"


class ProjectStatus(SafeIntEnum):
    UNKNOWN = 0
    RUNNING = 1
    IDLE = 2


class AgentQuestionsSetting(SafeIntEnum):
    DISABLED = 1
    TIMEOUT_15_MIN = 2


class Project(AristotleObject):
    project_id: str
    created_at: UTCDatetime
    last_updated: UTCDatetime
    description: str | None = pydantic.Field(
        validation_alias=pydantic.AliasChoices("description", "name"), serialization_alias="name"
    )
    """The project's name, which the CLI's --json gives as ``name``."""
    status: ProjectStatus
    latest_run_status: TaskStatus | None = None
    """The status of the project's latest run, as a task's is named; null before its first run."""
    has_input: bool
    has_files: bool

    @pydantic.field_validator("latest_run_status", mode="before")
    @classmethod
    def _quiet_unknown_run_status(cls, value: Any) -> Any:
        """A run status this version doesn't know, such as the NOT_STARTED of a run whose upload never finished, as
        UNKNOWN without TaskStatus's warning, which a listing would repeat for every such project."""
        if isinstance(value, str) and value not in {status.value for status in TaskStatus}:
            logger.debug(f"Unknown latest run status {value!r}; treating as UNKNOWN")
            return TaskStatus.UNKNOWN
        return value

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def status_name(self) -> str:
        """``status``'s name: ``RUNNING``, ``IDLE``, or ``UNKNOWN`` for one this version doesn't know."""
        return self.status.name

    endpoint_prefix = "project"

    @property
    def object_id(self) -> str:
        return self.project_id

    def __str__(self) -> str:
        ret = f"Project {self.project_id} ({self.status.name})"
        if self.description:
            ret += f"\n{self.description}"
        ret += f"\nCreated: {format_relative_time(self.created_at)}\nLast updated: {format_relative_time(self.last_updated)}"
        return ret

    def web_url(self) -> str:
        return project_web_url(self.project_id)

    def log_created(self) -> None:
        logger.info(f"Project created: {self.project_id}")
        logger.info(f"Open in Aristotle Web: {self.web_url()}")

    @classmethod
    async def create(
        cls,
        prompt: str,
        tar_file_path: Path | str | None = None,
        public_file_path: str | None = None,
        agent_questions_setting: AgentQuestionsSetting = AgentQuestionsSetting.TIMEOUT_15_MIN,
        name: str | None = None,
    ) -> "Project":
        """Create a project from a prompt and a tar file.

        Args:
            prompt: Tells Aristotle what to do.
            tar_file_path: Path to the tar file to upload.
            public_file_path: Public path to use for the uploaded file. If None, uses the tar_file_path.
            agent_questions_setting: Whether the agent can ask questions. By default it can, and waits up to 15
                minutes for each answer; DISABLED turns questions off.
            name: The project's name, without surrounding whitespace; a server that doesn't take one names it as
                it would without, which the returned project's ``description`` shows.

        Returns:
            The created Project instance.

        Raises:
            ValueError: If the prompt is empty, or the name is.
            LeanProjectError: If the tar file is over local_file_utils.MAX_FILE_SIZE_BYTES.
        """
        validate_prompt(prompt)
        fields: dict[str, Any] = {"prompt": prompt, "agent_questions_setting": agent_questions_setting.value}
        if name is not None:
            fields["name"] = validate_project_name(name)
        body = json.dumps(fields)
        with ExitStack() as stack:
            files = None
            # The prompt goes in the same body: form-encoded, which only grows it, or as a part beside the archive.
            size = len(urlencode({"body": body}))
            if tar_file_path is not None:
                public_file_path = public_file_path or str(tar_file_path)
                archive = stack.enter_context(open(tar_file_path, "rb"))
                status = os.fstat(archive.fileno())
                # A file, not bytes: httpx writes a file in chunks, each within the write timeout.
                content: BinaryIO = archive
                archive_size = status.st_size
                if not stat.S_ISREG(status.st_mode):
                    # A pipe has no size to declare up front, so it is read, to one byte past the limit.
                    content = io.BytesIO(archive.read(local_file_utils.MAX_FILE_SIZE_BYTES + 1))
                    archive_size = len(content.getbuffer())
                if too_large := local_file_utils.upload_too_large(archive_size):
                    raise local_file_utils.LeanProjectError(too_large)
                size += archive_size
                files = [("input", (public_file_path, content, "application/x-tar"))]

            async with api_request.AristotleRequestClient() as client:
                # A Ctrl-C during the archive's build, which asyncio.run defers to the next wait, lands here, unsent.
                await asyncio.sleep(0)
                create_sent.set(True)
                try:
                    response = await client.post(
                        f"/{cls.endpoint_prefix}",
                        data={"body": body},
                        files=files,
                        idempotency_key=str(uuid.uuid4()),
                        timeout=api_request.upload_timeout(size),
                    )
                except api_request.AristotleOutcomeUnknownError as e:
                    # TODO: send it again under its key once the server says it takes keys.
                    raise api_request.AristotleOutcomeUnknownError(
                        f"{e} If it did, the project was created, though it may take a few minutes to show up: check "
                        "`aristotle list` before submitting again.",
                        e.status_code,
                    ) from e
                except api_request.AristotleNotProcessedError as e:
                    raise api_request.AristotleNotProcessedError(f"{e} {PROJECT_NOT_CREATED}", e.status_code) from e
                return cls.model_validate(response.json())

    @classmethod
    async def create_from_directory(
        cls,
        prompt: str,
        project_dir: Path | str,
        agent_questions_setting: AgentQuestionsSetting = AgentQuestionsSetting.TIMEOUT_15_MIN,
        name: str | None = None,
        block: verilog_bundle.Bundle | None = None,
        *,
        include_ignored: bool = False,
    ) -> "Project":
        """Create a project from a directory by creating a tar.gz archive.

        Skips files Aristotle wouldn't need to process, files an ignore rule matches unless ``include_ignored`` (a
        tracked one only by a rule in one of the repository's .gitignore files; outside a git work tree, or in a folder
        git ignores, those the directory's top-level .gitignore excludes) but never ``.lake`` or
        ``local_file_utils.LEAN_BUILD_FILES``, files that local_file_utils.is_secret_path calls credentials, by name or
        by folder, result archives an earlier run saved, and files whose names aren't UTF-8; the ``aristotle`` logger
        names which tracked files, which credential files, which result archives and which names that aren't UTF-8 it
        left out, and, without ``block``, the Verilog sources an ignore rule leaves out when none goes up but a .lean,
        .c or .rs file does.

        Args:
            prompt: Tells Aristotle what to do.
            project_dir: Path to the directory with any supplementary files, e.g. a Verilog, C or Rust project.
            agent_questions_setting: Whether the agent can ask questions. By default it can, and waits up to 15
                minutes for each answer; DISABLED turns questions off.
            name: The project's name, as for create().
            block: The Verilog block ``project_dir``'s ``aristotle.f`` and the command line's options describe. Every
                file it reaches goes up, at its path in the block, whatever the ignore rules, with its ``aristotle.f``,
                generated or as it is; the files its black boxes leave out don't, from the folder either, nor does
                a .v, .sv, .vh or .svh file there that declares nothing but black boxes.
            include_ignored: Upload the files an ignore rule matches too, which the ``aristotle`` logger's summary
                counts.

        Returns:
            The created Project instance.

        Raises:
            ValueError: If the prompt or the name is empty, or the path is empty or not a directory.
            LeanProjectError: If Aristotle cannot take the directory, nothing in it would go up, or without ``block``
                an ignore rule leaves out each of its Verilog sources and no .lean, .c or .rs file goes up either, git
                cannot say which files it ignores, or the archive is over local_file_utils.MAX_FILE_SIZE_BYTES.
            verilog_bundle.BundleError: If a file of ``block`` changed after it was read, or a .v, .sv, .vh or .svh
                file in ``project_dir`` that would go up declares a black box beside another design unit.
            ArchiveError: If a file can't be read, or is replaced while it is archived, or the archive written: an
                OSError with the original's errno, strerror and filename, which it wraps.
        """
        validate_prompt(prompt)
        if name is not None:
            name = validate_project_name(name)
        project_dir = validate_project_dir(project_dir)
        if not os.path.isdir(project_dir):
            raise ValueError(f"Path is not a directory: {project_dir}")
        check_readable(project_dir)
        local_file_utils.check_lake_manifest_references_local_deps(project_dir)

        file_name = local_file_utils.upload_name(_folder_name(project_dir) or "project")
        with tempfile.TemporaryDirectory() as temp_dir:
            # A fixed local name: the folder's, once escaped, may be too long for a file name.
            tar_file_path = Path(temp_dir) / "project.tar.gz"
            archived = build_archive(project_dir, tar_file_path, block, include_ignored=include_ignored)
            # After the archive, so that a folder with nothing to upload stops before the toolchain's request.
            if local_file_utils.has_any_lean_files(project_dir):
                toolchain_version = local_file_utils.get_lean_toolchain_version(project_dir)
                expected_toolchain = await local_file_utils.get_expected_toolchain()

                warning_message = None
                if toolchain_version is None:
                    warning_message = "Your project contains .lean files but no lean-toolchain is present.\n"
                    if expected_toolchain:
                        warning_message += f"Aristotle works best with Lean Toolchain {expected_toolchain}\n"
                elif expected_toolchain and toolchain_version != expected_toolchain:
                    warning_message = (
                        f"Your Lean toolchain version ({toolchain_version}) does not match Aristotle's.\n"
                        f"Aristotle works best with Lean Toolchain {expected_toolchain}\n"
                    )

                if warning_message:
                    logger.warning("WARNING: " + warning_message)

                # Check if .lake folder exists - warn if missing since user may have forgotten to lake build
                lake_folder = project_dir / ".lake"
                # os.path, not pathlib: before 3.14, Path.is_dir() raises for a path out of reach.
                if not os.path.isdir(lake_folder):
                    logger.warning(
                        "WARNING: Your project contains .lean files but no .lake folder.\n"
                        "Aristotle works better with access to your project's dependencies.\n"
                        "Did you forget to run `lake build`?\n"
                    )

            logger.info(archived.summary())
            return await cls.create(
                prompt=prompt,
                tar_file_path=tar_file_path,
                public_file_path=f"{file_name}.tar.gz",
                agent_questions_setting=agent_questions_setting,
                name=name,
            )

    @classmethod
    async def list_projects(
        cls,
        pagination_key: str | None = None,
        limit: int = 30,
        status: ProjectStatus | list[ProjectStatus] | None = None,
        latest_run_status: list[TaskStatus] | None = None,
    ) -> tuple[list["Project"], str | None]:
        """List projects, ordered by creation date (most recent first).

        Args:
            pagination_key: Key to start from when paginating through projects.
            limit: Maximum number of projects to return. Must be between 1 and 100.
            status: Optional project status filter. Can be a single ProjectStatus,
                   a list of ProjectStatus values, or None for all projects.
            latest_run_status: Optional filter on the status of each project's newest task.

        Returns:
            Tuple of list of projects and the new pagination key.
        """
        if not 1 <= limit <= 100:
            raise ValueError("Limit must be between 1 and 100")

        params: dict[str, Any] = {"pagination_key": pagination_key, "limit": limit}
        if status is not None:
            if isinstance(status, list):
                params["status"] = [s.value for s in status]
            else:
                params["status"] = status.value
        if latest_run_status is not None:
            params["latest_run_status"] = [s.value for s in latest_run_status]

        async with api_request.AristotleRequestClient() as client:
            return await cls._list_page(client, params)

    @classmethod
    async def _list_page(
        cls, client: api_request.AristotleRequestClient, params: dict[str, Any]
    ) -> tuple[list["Project"], str | None]:
        """One page of the caller's projects, and the key of the next, over an open client."""
        response = await client.get(f"/{cls.endpoint_prefix}", params=params)
        response_data = response.json()
        projects: list["Project"] = [cls.model_validate(project) for project in response_data["projects"]]
        pagination_key = response_data.get("pagination_key")
        if pagination_key is not None and not isinstance(pagination_key, str):
            raise TypeError("pagination_key must be a string or None")
        return projects, pagination_key

    @classmethod
    async def resolve_id(cls, reference: str) -> str:
        """Return the id of the project that a UUID, a unique id prefix or a name refers to.

        A UUID, in any case and with or without dashes, is returned in canonical form without a request.
        Anything else is looked up among the caller's PROJECT_LOOKUP_LIMIT most recent projects: an id prefix
        of at least PROJECT_ID_PREFIX_MIN_LENGTH hex digits (case and dashes ignored) or an exact name, and
        failing both, a name in another case. Surrounding whitespace is ignored.

        Raises:
            AristotleNotFoundError: If no project matches.
            AmbiguousProjectError: If more than one project matches.
        """
        project_id, _ = await cls._resolve(reference)
        return project_id

    @classmethod
    async def _resolve(cls, reference: str) -> tuple[str, "Project | None"]:
        """resolve_id's id, with the project as the lookup listed it; None for a UUID, which takes no request."""
        reference = reference.strip()
        try:
            return validate_project_id(reference), None
        except ValueError:
            pass
        if not reference:
            raise api_request.AristotleNotFoundError("project", reference)

        digits = reference.replace("-", "").lower()
        is_hex = all(c in "0123456789abcdef" for c in digits)
        by_prefix = is_hex and len(digits) >= PROJECT_ID_PREFIX_MIN_LENGTH
        folded = reference.casefold()

        matched: list[Project] = []
        other_case: list[Project] = []
        searched_all = False
        pagination_key = None
        note = (
            f"Searching your {PROJECT_LOOKUP_LIMIT:,} most recent projects for '{reference}'. "
            "A full project id needs no search."
        )
        at_terminal = lookup_at_terminal.get()
        # One connection for every page, paced as any request is, and slower once the per-second limit refuses one.
        async with (
            api_request.AristotleRequestClient() as client,
            _logged_if_slow(note) if at_terminal else nullcontext(),
        ):
            started = 0.0
            for page in range(PROJECT_LOOKUP_LIMIT // _LOOKUP_PAGE_SIZE):
                if page == 1 and not at_terminal:
                    logger.info(note)
                if client.rate_limit_refusals:
                    await asyncio.sleep(max(0.0, started + _LOOKUP_PAGE_INTERVAL - time.monotonic()))
                started, refusals = time.monotonic(), client.rate_limit_refusals
                params = {"pagination_key": pagination_key, "limit": _LOOKUP_PAGE_SIZE}
                projects, pagination_key = await cls._list_page(client, params)
                if client.rate_limit_refusals > refusals:
                    started = time.monotonic()
                for project in projects:
                    name = (project.description or "").strip()
                    if (by_prefix and project.project_id.replace("-", "").lower().startswith(digits)) or (
                        name and name == reference
                    ):
                        matched.append(project)
                    elif name and name.casefold() == folded:
                        other_case.append(project)
                if not pagination_key or not projects:
                    searched_all = True
                    break

        matches = matched or other_case
        if len(matches) == 1:
            return matches[0].project_id, matches[0]
        if matches:
            raise AmbiguousProjectError(reference, matches)
        short_hex = is_hex and 0 < len(digits) < PROJECT_ID_PREFIX_MIN_LENGTH
        raise api_request.AristotleNotFoundError(
            "project", reference, message=_no_match_message(reference, by_prefix, short_hex, searched_all)
        )

    @classmethod
    async def rename_by_id(cls, project_id: str, name: str) -> "Project":
        """Rename one of your projects.

        Args:
            project_id: The project's UUID.
            name: The new name. Surrounding whitespace is removed.

        Returns:
            The renamed Project.

        Raises:
            ValueError: If the id is not a UUID or the name is empty.
            AristotleAPIError: With status 404 if you own no project with this id.
        """
        project_id = validate_project_id(project_id)
        name = validate_project_name(name)
        async with api_request.AristotleRequestClient() as client:
            response = await client.post(
                f"/{cls.endpoint_prefix}/{project_id}/rename", json={"name": name}, idempotent=True
            )
            return cls.model_validate(response.json())

    async def rename(self, name: str) -> None:
        """Rename this project and update it from the server's reply; see rename_by_id."""
        renamed = await self.rename_by_id(self.project_id, name)
        self._update_from_response(renamed.model_dump())

    async def ask(
        self,
        prompt: str,
        files: list[Path | str] | None = None,
        agent_questions_setting: AgentQuestionsSetting = AgentQuestionsSetting.TIMEOUT_15_MIN,
        *,
        project_dir: Path | str | None = None,
    ) -> AgentTask:
        """Send a prompt to the project.

        Args:
            prompt: The prompt to send.
            files: Optional local file paths, relative to cwd or absolute. Only allowed when the project is idle.
            agent_questions_setting: Whether the agent can ask questions. By default it can, and waits up to 15
                minutes for each answer; DISABLED turns questions off. A running task keeps its own setting.
            project_dir: The local project root, cwd by default. Files keep their paths relative to this root;
                selecting fewer files never changes it. Files outside it, also through links, are refused.

        Returns:
            The agent task that was prompted.

        Raises:
            ValueError: Before anything is sent, if the prompt is empty, the file selection cannot be uploaded
                under project_dir, or there are files and the project runs a task.
        """
        validate_prompt(prompt)
        # Read once: the check and the upload both go through it.
        files = list(files or [])
        uploads = continuation_uploads(files, project_dir=project_dir) if files or project_dir is not None else []
        if files:
            with api_request.before_sending(PROMPT_NOT_SENT):
                await self.refresh()
            if self.status != ProjectStatus.IDLE:
                raise ValueError(files_refused_while_running(self.project_id, "files"))

        data = {
            "prompt": prompt,
            "agent_questions_setting": str(agent_questions_setting.value),
        }

        with ExitStack() as stack:
            files_data: list[tuple[str, tuple[Any, ...]]] = []
            # The prompt goes in the same body, as a part beside the files.
            size = len(prompt.encode())
            for file_path, name in uploads:
                f = stack.enter_context(open(file_path, "rb"))
                size += os.fstat(f.fileno()).st_size
                files_data.append(("files", (name, f, "application/octet-stream")))

            async with api_request.AristotleRequestClient() as client:
                try:
                    # httpx uses multipart when both data and files are provided
                    response = await client.post(
                        f"/{self.endpoint_prefix}/{self.project_id}/ask",
                        data=data,
                        files=files_data if files_data else [("_", (None, ""))],
                        idempotency_key=str(uuid.uuid4()),
                        timeout=api_request.upload_timeout(size),
                    )
                except api_request.AristotleOutcomeUnknownError as e:
                    # TODO: send it again under its key once the server says it takes keys.
                    raise api_request.AristotleOutcomeUnknownError(
                        f"{e} If it did, the prompt was sent, though its task may take a few minutes to show up: "
                        f"check `aristotle tasks {self.project_id}` before sending it again.",
                        e.status_code,
                    ) from e
                except api_request.AristotleNotProcessedError as e:
                    raise api_request.AristotleNotProcessedError(f"{e} {PROMPT_NOT_SENT}", e.status_code) from e
                return AgentTask.model_validate(response.json())

    async def get_files(self, destination: Path | str | None = None) -> Path:
        """Download the project files.

        Args:
            destination: Where to save the archive, making missing directories and overwriting a file. A
                directory, or a string ending in a path separator, gets it under its default name; so does the
                current directory if None.

        Returns:
            Path to the downloaded file

        Raises:
            ProjectHasNoFilesError: If the project has no files yet.
            OSError: If the archive could not be saved.
        """
        await self.refresh()
        if self.has_files:
            endpoint = f"/{self.endpoint_prefix}/{self.project_id}/result"
        elif self.has_input:
            endpoint = f"/{self.endpoint_prefix}/{self.project_id}/input"
        else:
            raise ProjectHasNoFilesError(f"Project {self.project_id} has no files to download yet.")

        async with api_request.AristotleRequestClient() as client:
            response = await client.get(endpoint)

        # After the client has closed: nothing awaits between the archive's save and the return.
        if destination is None or _names_directory(destination):
            path = Path(destination or ".") / self._archive_name(response.headers.get("content-disposition", ""))
        else:
            path = Path(destination)

        path.parent.mkdir(parents=True, exist_ok=True)
        _write_whole(path, response.content)
        return path

    def _archive_name(self, content_disposition: str) -> str:
        """The server's file name for the archive, else one from the project's name or id, as a single safe name."""
        if "filename=" in content_disposition:
            name = content_disposition.split("filename=")[1].strip('"')
        elif self.description:
            name = f"{self.description[:50]}_aristotle.tar.gz"
        else:
            name = f"{self.project_id}_aristotle.tar.gz"
        return _safe_file_name(name) or f"{self.project_id}_aristotle.tar.gz"

    async def save_files(
        self, file_paths: Iterable[str], folder: Path | str
    ) -> AsyncGenerator[tuple[str, Path | None], None]:
        """Download each of ``file_paths``, paths in the project such as a declaration's ``artifact_paths``, from its
        current output, else its input, in order and over one connection, and save it where saved_path() says, making
        missing folders and replacing a file there; yield each with where it was saved, or None when the project has no
        file there.

        Raises:
            ValueError: If saved_path() refuses a path, checked before its request and again before its write.
            AristotleNotFoundError: If the project can't be seen.
            OSError: If a file could not be saved.
        """
        endpoint = f"/{self.endpoint_prefix}/{self.project_id}/file/download"
        async with api_request.AristotleRequestClient() as client:
            for file_path in file_paths:
                saved_path(folder, file_path)
                content: bytes | None = None
                try:
                    content = (await client.get(endpoint, params={"file_path": file_path})).content
                except api_request.AristotleAPIError as e:
                    if e.status_code == 403 and not isinstance(e, api_request._IntermediaryError):
                        raise api_request.AristotleNotFoundError(self.endpoint_prefix, self.project_id, 403) from e
                    if e.status_code != 404:
                        raise
                if content is None:
                    yield file_path, None
                    continue
                # Links may have changed while the file downloaded.
                target = saved_path(folder, file_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_whole(target, content)
                yield file_path, target

    async def get_declarations(self, declaration_type: DeclarationType | None = None) -> list[Declaration]:
        """Return this project's declarations, most recently updated first, optionally filtered by type."""
        return await Declaration.list_all_for_project(self.project_id, declaration_type=declaration_type)

    async def get_tasks(
        self, pagination_key: str | None = None, limit: int = 10, newest_first: bool = True
    ) -> tuple[list[AgentTask], str | None]:
        """List project tasks, ordered by creation date.

        Args:
            pagination_key: Key to start from when paginating through tasks.
            limit: Maximum number of tasks to return. Must be between 1 and 100.
            newest_first: If true, shows most recent tasks first. If false, shows the oldest first.

        Returns:
            Tuple of list of tasks and the new pagination key.
        """
        if not 1 <= limit <= 100:
            raise ValueError("Limit must be between 1 and 100")

        params: dict[str, Any] = {"pagination_key": pagination_key, "limit": limit, "ascending": not newest_first}
        async with api_request.AristotleRequestClient() as client:
            response = await client.get(f"/{self.endpoint_prefix}/{self.project_id}/tasks", params=params)
            response_data = response.json()
            tasks: list[AgentTask] = [AgentTask.model_validate(task) for task in response_data["agent_tasks"]]
            pagination_key = response_data.get("pagination_key")
            if pagination_key is not None and not isinstance(pagination_key, str):
                raise TypeError("pagination_key must be a string or None")
            return tasks, pagination_key
