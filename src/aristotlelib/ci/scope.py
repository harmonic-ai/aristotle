"""Resolution of the upload set (tracked files) and the focus set (files to verify)."""

import dataclasses
import logging
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

from aristotlelib import local_file_utils, wildcard
from aristotlelib.ci import context
from aristotlelib.verilog import bundle as verilog_bundle

logger = logging.getLogger("aristotle")

#: Extensions Aristotle can verify.
SOURCE_EXTENSIONS = frozenset(
    {
        ".v",
        ".sv",
        ".svh",
        ".vh",
        ".c",
        ".h",
        ".rs",
        ".lean",
    }
)

#: The subset of SOURCE_EXTENSIONS that is Verilog, whose build configuration comes from a filelist.
VERILOG_EXTENSIONS = verilog_bundle.VERILOG_SUFFIXES

#: What makes Aristotle find Verilog in a ``.v`` or ``.sv`` file, rather than Coq, which shares ``.v``.
VERILOG_KEYWORD = re.compile(rb"\bendmodule\b")

#: The language of each extension in SOURCE_EXTENSIONS that Aristotle proves only where the account enables it.
GATED_EXTENSIONS = {**dict.fromkeys(VERILOG_EXTENSIONS, "Verilog"), ".c": "C", ".h": "C", ".rs": "Rust"}

#: Extensions conventionally used for a Verilog filelist ("dot-f file").
FILELIST_EXTENSIONS = frozenset({".f", ".flist"})

#: The filelist name written for Aristotle specifically; treated as authoritative.
CANONICAL_FILELIST = "aristotle.f"

GIT_TIMEOUT_SECONDS = 60

GIT_FETCH_TIMEOUT_SECONDS = 300

#: Depth of the first fetch of the base branch into a shallow clone; each retry deepens by double the last step.
BASE_FETCH_DEPTH = 50

#: Deepening fetches tried before falling back to fetching the full history.
BASE_FETCH_DEEPEN_ATTEMPTS = 4

REMOTE_PREFIX = "origin/"

GIT_MISSING = (
    "git is not installed, so the files to upload and verify cannot be determined. Install git, in the job's image if "
    "the job runs in a container."
)


class Scope(str, Enum):
    CHANGED = "changed"
    """Source files touched by this change, and the Lean files under a Lean build file it touched."""

    PATHS = "paths"
    """An explicit list of path globs."""

    ALL = "all"
    """Every source file in the repository."""

    def __str__(self) -> str:
        return self.value


class ScopeError(Exception):
    """Raised when the scope cannot be resolved from the repository state."""


class GitError(ScopeError):
    """A git command that ran and failed: the exit status, and what it printed on stderr."""

    def __init__(self, message: str, returncode: int, stderr: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr

    @property
    def reason(self) -> str:
        """What git printed up to its first ``fatal:`` or ``error:`` line, on one line, without the hints it adds
        after it; a warning before that line, such as ssh's, may explain it."""
        lines = [line.strip() for line in self.stderr.splitlines() if line.strip()]
        end = next((i for i, line in enumerate(lines) if line.startswith(("fatal:", "error:"))), len(lines) - 1)
        return " ".join(lines[: end + 1])


@dataclasses.dataclass(frozen=True)
class ScopeResult:
    upload_files: list[Path]
    """Repository-relative paths to include in the uploaded project."""

    focus_files: list[Path]
    """Repository-relative paths Aristotle should verify."""

    filelists: list[Path] = dataclasses.field(default_factory=list)
    """Verilog filelists found in the checkout, best candidate first. May be empty."""

    prefix: str = ""
    """Where the root sits in the repository, as git prints it ("hw/"); empty at the top."""

    not_utf8: list[Path] = dataclasses.field(default_factory=list)
    """Source files the change touched that were left out of the upload because their names aren't UTF-8."""

    block: "verilog_bundle.Bundle | None" = None
    """The Verilog block the root ``aristotle.f`` describes, whose files and filelist go up too."""

    held_back: list[Path] = dataclasses.field(default_factory=list)
    """Tracked files an ignore rule keeps out of the upload, which warn_held_back names."""

    held_back_focus: list[Path] = dataclasses.field(default_factory=list)
    """Source files in scope, by the change or by --path, that an ignore rule keeps out of the upload: verified only if
    the block brings them."""

    allowed: frozenset[str] = frozenset()
    """The paths in the upload of the block's files that go up only because --allow-untracked names a folder holding
    them: files git doesn't track in the root, or from outside it."""

    @property
    def is_empty(self) -> bool:
        return not self.focus_files

    @property
    def canonical_filelist(self) -> Path | None:
        """The root ``aristotle.f``, the only filelist used without asking the agent to choose."""
        best = self.filelists[0] if self.filelists else None
        if best is not None and best.name.lower() == CANONICAL_FILELIST and len(best.parts) == 1:
            return best
        return None

    @property
    def has_verilog_focus(self) -> bool:
        return any(p.suffix.lower() in VERILOG_EXTENSIONS for p in self.focus_files)


def _checkout_owner(root: Path) -> tuple[int, int] | None:
    """The checkout's uid and gid when this process is root and the checkout is someone else's."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    stat = os.stat(_run_git(root, "rev-parse", "--absolute-git-dir").strip())
    return None if stat.st_uid == 0 else (stat.st_uid, stat.st_gid)


def _run_git(root: Path, *args: str, timeout: float = GIT_TIMEOUT_SECONDS, writes: bool = False) -> str:
    """Run a git command for ``root`` as local_file_utils.run_git() does and return its stdout; raises ScopeError outside
    a repository, or if git is missing or fails.

    ``writes`` runs it as the checkout's owner: the image runs as root, and root-owned files left in a runner's
    .git break every later fetch and clean of that workspace.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    as_owner: dict[str, Any] = {}
    if writes and (owner := _checkout_owner(root)) is not None:
        # The owner may not be able to read root's HOME, and git reads its config from there.
        as_owner = {"user": owner[0], "group": owner[1], "extra_groups": []}
        env["HOME"] = tempfile.gettempdir()
    try:
        result = local_file_utils.run_git(
            root,
            *args,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            # As os.fsdecode decodes a path on POSIX, so that each name maps back to its bytes on disk.
            encoding=sys.getfilesystemencoding(),
            errors="surrogateescape",
            timeout=timeout,
            check=True,
            env=env,
            **as_owner,
        )
    except FileNotFoundError as e:
        raise ScopeError(GIT_MISSING) from e
    except subprocess.TimeoutExpired as e:
        raise ScopeError(f"git {' '.join(args)} timed out after {timeout:g}s.") from e
    except subprocess.CalledProcessError as e:
        raise GitError(f"git {' '.join(args)} failed: {e.stderr.strip()}", e.returncode, e.stderr) from e
    if result is None:
        raise ScopeError(f"{root} is not in a git repository.")
    return result.stdout


def head_commit(root: Path) -> str | None:
    """The full id of the commit checked out at ``root``; None before its first commit."""
    try:
        return _run_git(root, "rev-parse", "--verify", "HEAD^{commit}").strip()
    except ScopeError:
        return None


def top_level(start: Path) -> Path | None:
    """The top level of the repository ``start`` is in, as git reports it, or where git is missing or refuses the
    repository, the nearest folder from ``start`` up that holds a .git; None in no repository."""
    try:
        return Path(_run_git(start, "rev-parse", "--show-toplevel").rstrip("\n")).resolve()
    except ScopeError:
        return local_file_utils.enclosing_repository(start)


def local_root(start: Path, holding: str | None = None) -> Path:
    """Outside CI, the folder a run is rooted at: the nearest from ``start`` up to its repository's top level that holds
    a file named ``holding``, else the top level; ``start`` itself when it is in no repository."""
    start = start.resolve()
    # A top level found by its .git where git refuses the repository lets a run find its manifest and fail on git's
    # reason, not on a manifest it didn't find.
    top = top_level(start) or start
    if holding is not None and start.is_relative_to(top):
        for folder in (start, *start.parents):
            if os.path.isfile(folder / holding):
                return folder
            if folder == top:
                break
    return top


def require_repository(root: Path) -> None:
    """Raises ScopeError unless git reads ``root`` as a repository: with git's reason when it refuses one that is
    there, as one another user owns, and as _run_git words it when git is missing or stalls."""
    # os.path, not pathlib: before 3.14, Path.is_dir() raises for a path too long or out of reach.
    if not os.path.isdir(root):
        raise ScopeError(f"{root} is not a directory.")
    if not os.access(root, os.R_OK | os.X_OK):
        raise ScopeError(f"{root} can't be read.")
    if local_file_utils.enclosing_repository(root) is None:
        # A checkout made without git has no .git either.
        if shutil.which("git") is None:
            raise ScopeError(GIT_MISSING)
        raise ScopeError(
            f"{root} is not a git repository. Aristotle's CI mode uses git to determine "
            "which files to upload and verify."
        )
    try:
        _run_git(root, "rev-parse", "--git-dir")
    except ScopeError as e:
        if not isinstance(e.__cause__, subprocess.CalledProcessError):
            raise
        raise ScopeError(f"git refused {root}: {e.__cause__.stderr.strip()}") from e


def _prefix(root: Path) -> str:
    """Where ``root`` sits in its repository, as git prints a prefix ("hw/")."""
    # git prints one for where run_git() runs it only when it passes over the .git there.
    return _run_git(root, "rev-parse", "--show-prefix").rstrip("\n") + local_file_utils.git_prefix(root)


def _ls_files(root: Path, *options: str) -> list[str]:
    prefix = local_file_utils.git_prefix(root)
    output = _run_git(root, "ls-files", "-z", *options, "--", local_file_utils.git_pathspec(root))
    return [line[len(prefix) :] for line in output.split("\0") if line and line.startswith(prefix)]


def _tracked_files(root: Path, warn: bool = True) -> tuple[list[Path], list[Path], list[Path]]:
    """Every file git tracks, less those an ignore rule matches unless they are LEAN_BUILD_FILES: those whose names are
    UTF-8, the upload set, and those whose names aren't, which Aristotle fails a run on and, if ``warn``, a warning
    names; and those an ignore rule holds back."""
    tracked = _ls_files(root)
    # Tracked files stay in the index after a later .gitignore rule matches them. Only the repository's .gitignore
    # files hold one back, never .git/info/exclude or a global excludes file, which are this machine's alone.
    ignored = {
        line
        for line in _ls_files(root, "--cached", "--ignored", "--exclude-per-directory=.gitignore")
        if Path(line).name not in local_file_utils.LEAN_BUILD_FILES
    }
    kept = [Path(line) for line in tracked if line not in ignored]
    # A git directory committed as plain files, as a bare repository can be: its config may hold a remote's token.
    # Only a folder whose HEAD or config git tracks can be one in a clone, whatever an ignore rule says of them; then by
    # the path's start, for a large checkout.
    candidates = {p.parent for p in map(Path, tracked) if p.name in ("HEAD", "config")}
    git_dirs = [f for f in candidates if local_file_utils.is_git_dir(root / f)]
    starts = tuple(f"{f.as_posix()}/" for f in git_dirs)
    in_git_dirs = [p for p in kept if p.as_posix().startswith(starts)] if starts else []
    if in_git_dirs:
        left_out = set(in_git_dirs)
        kept = [p for p in kept if p not in left_out]
        if warn:
            logger.warning(
                f"Not uploading {len(in_git_dirs)} tracked file(s) in a git directory, which hold git's own files:"
                f"{local_file_utils.listing(in_git_dirs)}"
            )
    not_utf8 = [p for p in kept if not local_file_utils.is_utf8_name(p)]
    if warn:
        # A credential is named as one, as the folder walk names it: no rename makes it go up.
        local_file_utils.log_excluded_secrets([p for p in not_utf8 if local_file_utils.is_secret_path(p, root)])
        if renamable := [p for p in not_utf8 if not local_file_utils.is_secret_path(p, root)]:
            logger.warning(local_file_utils.not_utf8_warning(renamable, "tracked file"))
    return [p for p in kept if local_file_utils.is_utf8_name(p)], not_utf8, sorted(Path(i) for i in ignored)


def warn_held_back(root: Path, scope_result: ScopeResult) -> None:
    """Warn of the tracked files an ignore rule keeps out of ``scope_result``'s upload, but those its block takes, its
    filelist included, and those its black boxes leave out."""
    block = scope_result.block
    if block is None:
        _warn_held_back(root, scope_result.held_back)
        return
    needed = {m.archive for m in block.members} | {block.filelist_name}
    _warn_held_back(
        root, [p for p in scope_result.held_back if p.as_posix() not in needed and not block.leaves_out(root / p)]
    )


def _warn_held_back(root: Path, held_back: list[Path]) -> None:
    if held_back:
        logger.warning(local_file_utils.held_back_warning(local_file_utils.ignore_rules(root, held_back)))


def _merge_base(root: Path, base_ref: str) -> str | None:
    try:
        return _run_git(root, "merge-base", base_ref, "HEAD").strip()
    except ScopeError:
        logger.debug(f"No merge base against {base_ref}")
        return None


def _is_shallow(root: Path) -> bool:
    return _run_git(root, "rev-parse", "--is-shallow-repository").strip() == "true"


def _fetch_merge_base(root: Path, base_ref: str) -> str | None:
    """Fetch an ``origin/<branch>`` base from origin, deepening a shallow clone until HEAD and the base meet.

    Raises:
        ScopeError: origin has no such branch, or the branch could not be fetched at all.
    """
    if not base_ref.startswith(REMOTE_PREFIX):
        return None
    branch = base_ref[len(REMOTE_PREFIX) :]
    refspec = f"+refs/heads/{branch}:refs/remotes/{REMOTE_PREFIX}{branch}"

    def fetch(*depth_args: str) -> None:
        _run_git(
            root,
            "fetch",
            "--no-tags",
            *depth_args,
            "origin",
            refspec,
            timeout=GIT_FETCH_TIMEOUT_SECONDS,
            writes=True,
        )

    def deepen(*depth_args: str) -> bool:
        try:
            fetch(*depth_args)
        except ScopeError as e:
            logger.warning(f"Could not fetch {branch} from origin: {_reason(e)}")
            return False
        return True

    logger.info(f"Fetching {branch} from origin to find the merge base")
    shallow = _is_shallow(root)
    try:
        fetch(*([f"--depth={BASE_FETCH_DEPTH}"] if shallow else []))
    except ScopeError as e:
        if isinstance(e, GitError) and _origin_has_branch(root, branch) is False:
            # A misspelled --base-ref, which a deeper clone wouldn't fix.
            raise ScopeError(
                f"origin has no branch {branch} to compare against. Check the name given with --base-ref or "
                "ARISTOTLE_BASE_REF, or use --scope all or --scope paths."
            ) from None
        # A fetch without credentials fails too, where a full checkout, which holds the base already, needs none.
        hint = f" {context.deepen_checkout_hint()}" if _has_origin(root) else ""
        raise ScopeError(
            f"Cannot determine which files changed: {branch} could not be fetched from origin: {_reason(e)}{hint} Or "
            "use --scope all or --scope paths."
        ) from None
    merge_base = _merge_base(root, base_ref)

    step = BASE_FETCH_DEPTH
    for _ in range(BASE_FETCH_DEEPEN_ATTEMPTS):
        if merge_base is not None or not _is_shallow(root):
            return merge_base
        step *= 2
        if not deepen(f"--deepen={step}"):
            return None
        merge_base = _merge_base(root, base_ref)

    if merge_base is None and _is_shallow(root) and deepen("--unshallow"):
        merge_base = _merge_base(root, base_ref)
    return merge_base


def _reason(error: ScopeError) -> str:
    """Why a git command failed, as a sentence: what git printed up to its error, not the hints it adds after it."""
    reason = error.reason if isinstance(error, GitError) and error.reason else str(error)
    return reason if reason.endswith(".") else reason + "."


def _has_origin(root: Path) -> bool:
    """Whether the checkout has a remote named origin at all."""
    try:
        _run_git(root, "remote", "get-url", "origin")
    except ScopeError:
        return False
    return True


def _origin_has_branch(root: Path, branch: str) -> bool | None:
    """Whether origin has ``branch``; None when git can't ask it, as with no origin or no network. It only lists refs,
    so it gets git's short limit: a stalled origin has already cost a fetch's."""
    try:
        _run_git(
            root,
            "ls-remote",
            "--exit-code",
            "--heads",
            "origin",
            f"refs/heads/{branch}",
        )
    except GitError as e:
        # --exit-code makes git exit 2 when origin answered and has no such ref.
        return False if e.returncode == 2 else None
    except ScopeError:
        return None
    return True


def _as_base_ref(root: Path, base_ref: str) -> str:
    """A ref git can resolve locally as given, else ``origin/<name>``: CI checkouts have no local branches."""
    if base_ref.startswith(REMOTE_PREFIX):
        return base_ref
    try:
        _run_git(root, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}")
        return base_ref
    except ScopeError:
        return REMOTE_PREFIX + base_ref


def _push_base(root: Path, before: str) -> str | None:
    """``before`` if the commit is in the checkout or can be fetched from origin, else None."""

    def present() -> bool:
        try:
            _run_git(root, "cat-file", "-e", f"{before}^{{commit}}")
        except ScopeError:
            return False
        return True

    if present():
        return before
    # A shallow clone rarely holds the previous tip; one commit is enough to diff trees.
    depth = ["--depth=1"] if _is_shallow(root) else []
    try:
        _run_git(root, "fetch", "--no-tags", *depth, "origin", before, timeout=GIT_FETCH_TIMEOUT_SECONDS, writes=True)
    except ScopeError as e:
        logger.info(f"Could not fetch {before[:12]}, the commit before this push: {e}")
        return None
    return before if present() else None


def _changed_files(root: Path, base_ref: str | None, push_before: str | None = None) -> tuple[list[Path], list[Path]]:
    """Files the change under test adds or modifies, and files it deletes, a rename being both; raises ScopeError if a
    base branch has no merge base, or the commit before a push is neither in the checkout nor fetchable.

    The base is the merge base with ``base_ref``, else ``push_before``, else, when neither is given, the parent commit.
    """
    if base_ref:
        base_ref = _as_base_ref(root, base_ref)
        diff_target = _merge_base(root, base_ref) or _fetch_merge_base(root, base_ref)
        if diff_target is None:
            fetched = "and fetching it from origin did not find one. " if base_ref.startswith(REMOTE_PREFIX) else ""
            raise ScopeError(
                f"Cannot determine which files changed: no merge base between HEAD and {base_ref}, "
                f"{fetched}{context.deepen_checkout_hint()} Or use --scope all or --scope paths."
            )
        logger.info(f"Comparing against the merge base with {base_ref} ({diff_target[:12]})")
    elif push_before:
        diff_target = _push_base(root, push_before)
        if diff_target is None:
            raise ScopeError(
                f"Cannot determine which files changed: {push_before[:12]}, the commit before this push, is not in "
                f"the checkout and could not be fetched from origin. {context.deepen_checkout_hint()} Or set "
                "--base-ref to compare a branch, or use --scope all or --scope paths."
            )
        logger.info(f"Comparing against {diff_target[:12]}, the commit before this push")
    else:
        try:
            diff_target = _run_git(root, "rev-parse", "HEAD~1").strip()
        except ScopeError as e:
            raise ScopeError(
                "Cannot determine which files changed: no base branch and no parent commit. "
                "Use --scope all or --scope paths for a shallow or initial checkout."
            ) from e
        logger.info(f"Comparing against the previous commit ({diff_target[:12]}); set --base-ref to compare a branch")

    pathspec = local_file_utils.git_pathspec(root)
    # --relative: paths from where git runs, as the pathspec is, whatever diff.relative says.
    output = _run_git(
        root, "diff", "--relative", "--name-status", "-z", "--no-renames", diff_target, "HEAD", "--", pathspec
    )
    fields = output.split("\0")
    prefix = local_file_utils.git_prefix(root)
    entries = [
        (status, Path(path[len(prefix) :]))
        for status, path in zip(fields[::2], fields[1::2])
        if path and path.startswith(prefix)
    ]
    entries += _build_files_above(root, diff_target)
    return [p for status, p in entries if status != "D"], [p for status, p in entries if status == "D"]


def _build_files_above(root: Path, diff_target: str) -> list[tuple[str, Path]]:
    """The change's (status, path) entries for Lean's build files in the folders above ``root``, as ``../`` paths."""
    prefix = _prefix(root).rstrip("/")
    if not prefix:
        return []
    parts = PurePosixPath(prefix).parts
    specs = [
        f":(top,literal){PurePosixPath(*parts[:depth], name)}"
        for depth in range(len(parts))
        for name in sorted(local_file_utils.LEAN_BUILD_FILES)
    ]
    # diff.relative=false rather than --no-relative, which git before 2.28 rejects.
    output = _run_git(
        root,
        "-c",
        "diff.relative=false",
        "diff",
        "--name-status",
        "-z",
        "--no-renames",
        diff_target,
        "HEAD",
        "--",
        *specs,
    )
    fields = output.split("\0")
    return [(status, Path(posixpath.relpath(path, prefix))) for status, path in zip(fields[::2], fields[1::2]) if path]


def _glob(pattern: str) -> tuple[wildcard.Run | re.Pattern[str], ...]:
    """Parse a --path glob for wildcard.matches(): "**/" is zero or more directories, as in .gitignore; "*" and "?" are
    fnmatch's."""
    out: list[wildcard.Run | str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append(wildcard.Run.FOLDERS)
            i += 3
            continue
        char = pattern[i]
        close = -1
        if char == "[":
            # As in fnmatch: a leading "!" negates, and a "]" right after "[" or "[!" is literal.
            start = i + 1 + (pattern[i + 1 : i + 2] == "!")
            close = pattern.find("]", start + (pattern[start : start + 1] == "]"))
        if char == "*":
            out.append(wildcard.Run.ANY)
        elif char == "?":
            out.append(".")
        elif close != -1:
            body = pattern[i + 1 : close].replace("\\", "\\\\")
            if body.startswith("!"):
                body = "^" + body[1:]
            elif body.startswith("^"):
                body = "\\" + body
            out.append(f"[{body}]")
            i = close
        else:
            out.append(re.escape(char))
        i += 1
    try:
        return wildcard.steps(out)
    except re.error as e:
        error = e
    try:
        # A malformed class, named at its position in the whole glob's regex.
        re.compile("".join(s.value if isinstance(s, wildcard.Run) else s for s in out) + r"\Z", re.DOTALL)
    except re.error as e:
        error = e
    raise ScopeError(f"--path {pattern!r} is not a valid glob: {error}") from None


def _matching_files(candidates: list[Path], patterns: list[str]) -> list[Path]:
    """Filter ``candidates`` to those matching any glob in ``patterns``."""
    globs = [(pattern.rstrip("/") + "/", _glob(pattern)) for pattern in patterns]
    matched: list[Path] = []
    for path in candidates:
        posix = path.as_posix()
        # A bare directory prefix like "rtl/core" covers everything beneath it.
        if any(wildcard.matches(glob, posix) or posix.startswith(prefix) for prefix, glob in globs):
            matched.append(path)
    return matched


def is_source_file(path: Path) -> bool:
    """A file with one of SOURCE_EXTENSIONS, other than one of Lean's build files such as ``lakefile.lean``."""
    return path.suffix.lower() in SOURCE_EXTENSIONS and path.name not in local_file_utils.LEAN_BUILD_FILES


def _lean_files_built_by(changed: list[Path], source_files: list[Path]) -> list[Path]:
    """The Lean files in the folder, or below it, of each of Lean's build files in ``changed``; one in a folder above
    the root builds them all."""
    folders = {p.parent for p in changed if p.name in local_file_utils.LEAN_BUILD_FILES}
    if any(".." in folder.parts for folder in folders):
        folders.add(Path("."))
    return [p for p in source_files if p.suffix.lower() == ".lean" and not folders.isdisjoint(p.parents)]


def gated_languages(root: Path, paths: list[Path], taken: Sequence[Path] = ()) -> list[str]:
    """The languages enabled per account that Aristotle finds in the upload of ``paths`` under ``root``, and of
    ``taken``, files that go up whatever the upload set's rules, as a block's do, sorted, by its own rules: C for a name
    ending ``.c``, Rust for ``.rs``, and Verilog for ``.v`` or ``.sv`` containing VERILOG_KEYWORD."""
    languages: set[str] = set()
    for path in [*uploaded_paths(root, paths, taken), *taken]:
        if path.name.endswith(".c"):
            languages.add("C")
        elif path.name.endswith(".rs"):
            languages.add("Rust")
        elif "Verilog" not in languages and path.name.endswith((".v", ".sv")) and _declares_a_module(root / path):
            languages.add("Verilog")
    return sorted(languages)


def uploaded_paths(root: Path, paths: list[Path], taken: Sequence[Path] = ()) -> list[Path]:
    """The ``paths`` runner.build_tarball uploads: regular files but credentials, and links to a regular file it uploads,
    one of ``taken`` included: files that go up whatever these rules say, as a block's do."""
    regular: list[Path] = []
    links: list[Path] = []
    archived: set[tuple[int, int]] = set()
    for path in taken:
        try:
            # Through a link, as the block writes it.
            identity = local_file_utils.file_identity(os.stat(root / path))
        except OSError:
            continue
        if identity is not None:
            archived.add(identity)
    for path in paths:
        if local_file_utils.is_secret_path(path, root):
            continue
        try:
            file_stat = os.lstat(root / path)
        except OSError:
            continue
        if stat.S_ISLNK(file_stat.st_mode):
            links.append(path)
        elif stat.S_ISREG(file_stat.st_mode):
            regular.append(path)
            if (identity := local_file_utils.file_identity(file_stat)) is not None:
                archived.add(identity)
    for path in links:
        try:
            target = os.stat(root / path)
        except OSError:
            continue
        if stat.S_ISREG(target.st_mode) and local_file_utils.file_identity(target) in archived:
            regular.append(path)
    return regular


def _declares_a_module(path: Path) -> bool:
    """Whether the regular file ``path`` names, through a link if it is one, contains VERILOG_KEYWORD, read a mebibyte
    at a time."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    except OSError:
        return False
    with os.fdopen(fd, "rb") as file:
        try:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                return False
            tail = b""
            while chunk := file.read(1 << 20):
                if VERILOG_KEYWORD.search(tail + chunk):
                    return True
                tail = chunk[-16:]
        except OSError:
            return False
    return False


def find_filelists(upload_files: list[Path]) -> list[Path]:
    """Verilog filelists among ``upload_files``, best candidate first; ``.f`` is also Fortran, so only use with a Verilog focus."""
    candidates = [p for p in upload_files if p.suffix.lower() in FILELIST_EXTENSIONS]

    def rank(path: Path) -> tuple[int, int, str]:
        # A root aristotle.f first, then any aristotle.f, then shallower paths.
        is_canonical = path.name.lower() == CANONICAL_FILELIST
        return (
            0 if is_canonical and len(path.parts) == 1 else 1 if is_canonical else 2,
            len(path.parts),
            path.as_posix(),
        )

    return sorted(candidates, key=rank)


def _require_matches(source_files: list[Path], paths: list[str], extensions: str) -> None:
    """Raise ScopeError naming each ``--path`` glob that matches no tracked source file."""
    unmatched = [p for p in paths if not _matching_files(source_files, [p])]
    if unmatched:
        raise ScopeError(
            f"No tracked source file ({extensions}) matches --path {', '.join(repr(p) for p in unmatched)}. "
            "Patterns are relative to the directory being verified, for example 'rtl/**/*.sv' or 'rtl/core'."
        )


def check_arguments(scope: Scope, paths: list[str] | None) -> list[str]:
    """The --path patterns, if any; ScopeError for patterns the scope contradicts, which the arguments alone tell."""
    if scope == Scope.ALL and paths:
        # Otherwise a job meaning to narrow the run with ARISTOTLE_PATHS verifies the whole repository.
        raise ScopeError("--path narrows --scope changed or --scope paths; --scope all verifies everything.")
    if scope == Scope.PATHS and not paths:
        raise ScopeError("--scope paths requires at least one --path pattern.")
    return paths or []


def resolve(
    root: Path,
    scope: Scope,
    paths: list[str] | None = None,
    base_ref: str | None = None,
    push_before: str | None = None,
    allow_no_sources: bool = False,
) -> ScopeResult:
    """Determine the upload set and the focus set for this run.

    ``paths`` is required for ``PATHS`` and filters ``CHANGED``; ``base_ref`` and ``push_before`` are used by
    ``CHANGED``, the push's previous commit only when there is no base branch. ``allow_no_sources`` lets ``ALL`` find
    no source, or no file at all once ignore rules apply, as when a block will add its own.
    Raises ScopeError when the repository state cannot support the requested scope.
    """
    paths = check_arguments(scope, paths)
    upload_files, left_out, held_back = _upload_files(root, allow_empty=allow_no_sources and scope == Scope.ALL)
    try:
        source_files = [p for p in upload_files if is_source_file(p)]

        extensions = ", ".join(sorted(SOURCE_EXTENSIONS))
        if scope == Scope.ALL:
            focus_files = source_files
            if not focus_files and not allow_no_sources:
                # An explicit request to verify everything that finds nothing is a misconfiguration, not a pass.
                raise ScopeError(no_sources_message(root))
        elif scope == Scope.PATHS:
            _require_matches(source_files, paths, extensions)
            focus_files = _matching_files(source_files, paths)
            held_back_focus = _matching_files([p for p in held_back if is_source_file(p)], paths)
            return dataclasses.replace(
                _result(root, upload_files, focus_files, held_back), held_back_focus=held_back_focus
            )
        else:
            if paths:
                # Checked against the whole tree, not the diff: a glob that can never match would pass every build.
                _require_matches(source_files, paths, extensions)
            changed, deleted = _changed_files(root, base_ref, push_before)
            tracked = set(source_files)
            focus_files = [p for p in changed if is_source_file(p) and p in tracked]
            left_out_names = set(left_out)
            not_utf8 = [p for p in changed if is_source_file(p) and p in left_out_names]
            held_back_sources = [p for p in held_back if is_source_file(p)]
            held_back_names = set(held_back_sources)
            held_back_focus = sorted(
                {p for p in changed if p in held_back_names}
                | set(_lean_files_built_by(changed + deleted, held_back_sources))
            )
            in_focus = set(focus_files)
            built = [p for p in _lean_files_built_by(changed + deleted, source_files) if p not in in_focus]
            if paths:
                focus_files = _matching_files(focus_files, paths)
                not_utf8 = _matching_files(not_utf8, paths)
                held_back_focus = _matching_files(held_back_focus, paths)
                built = _matching_files(built, paths)
            if built:
                build_files = [p.as_posix() for p in changed + deleted if p.name in local_file_utils.LEAN_BUILD_FILES]
                folders = "its folder" if len(build_files) == 1 else "their folders"
                logger.info(
                    f"{', '.join(build_files)} changed, so the Lean files in {folders} and below are verified too."
                )
                focus_files += built
            return dataclasses.replace(
                _result(root, upload_files, focus_files, held_back), not_utf8=not_utf8, held_back_focus=held_back_focus
            )

        return _result(root, upload_files, focus_files, held_back)
    except ScopeError:
        # Raised before any block is read, so every file an ignore rule holds back may be why.
        _warn_held_back(root, held_back)
        raise


def no_sources_message(root: Path) -> str:
    """Why ``--scope all`` can't run on ``root``."""
    return f"--scope all found no tracked source files ({', '.join(sorted(SOURCE_EXTENSIONS))}) in {root}."


def tracked_paths(root: Path) -> set[str]:
    """Every path git tracks under ``root``, ``/``-separated and relative to it, ignore rules aside.

    Raises:
        ScopeError: git failed.
    """
    return set(_ls_files(root))


def tracked_source_files(root: Path) -> list[Path]:
    """The source files git tracks under ``root``, listed without the upload set's warnings; raises ScopeError outside
    a git checkout."""
    return [Path(line) for line in _ls_files(root) if is_source_file(Path(line))]


def resolve_checkout(root: Path, warn: bool = True, allow_empty: bool = False) -> ScopeResult:
    """The upload set for a manifest run: every tracked file, with every source file in focus, even none; ``warn`` as
    _tracked_files takes it, and ``allow_empty`` as _upload_files does.

    Raises ScopeError when ``root`` is not a git checkout with tracked files.
    """
    upload_files, _, held_back = _upload_files(root, warn, allow_empty)
    return _result(root, upload_files, [p for p in upload_files if is_source_file(p)], held_back)


def _upload_files(
    root: Path, warn: bool = True, allow_empty: bool = False
) -> tuple[list[Path], list[Path], list[Path]]:
    """The upload set, the tracked files left out of it for their names and those an ignore rule holds back, as
    _tracked_files gives them; an empty upload set only with ``allow_empty``, as when ignore rules hold back every
    tracked file and a block will bring its own."""
    require_repository(root)
    # As submit judges it: git's files never go up, and git would list nothing in one or fail with no work tree.
    real = Path(os.path.realpath(root))
    if local_file_utils.is_git_dir(real):
        raise ScopeError(f"{root} is a git directory, and nothing in one is uploaded.")
    if local_file_utils.in_git_dir(real):
        raise ScopeError(f"{root} is inside a git directory, and nothing in one is uploaded.")
    upload_files, not_utf8, held_back = _tracked_files(root, warn)
    if not upload_files and not allow_empty:
        if warn:
            _warn_held_back(root, held_back)
        raise ScopeError(f"No tracked files found in {root}. Has the repository been checked out?")
    return upload_files, not_utf8, held_back


def _result(root: Path, upload_files: list[Path], focus_files: list[Path], held_back: list[Path]) -> ScopeResult:
    result = ScopeResult(
        upload_files=sorted(upload_files),
        focus_files=sorted(focus_files),
        prefix=_prefix(root),
        held_back=held_back,
    )
    if result.has_verilog_focus:
        result = dataclasses.replace(result, filelists=find_filelists(result.upload_files))
    return result
