import errno
import fnmatch
import json
import logging
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePath
from typing import BinaryIO

import pathspec

from aristotlelib import api_request

# Set up logger for this module
logger = logging.getLogger("aristotle")


MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024

MATHLIB_INCLUDED_STD_PACKAGES = (
    "Cli",
    "LeanSearchClient",
    "Qq",
    "aesop",
    "batteries",
    "importGraph",
    "mathlib",
    "plausible",
    "proofwidgets",
)

LEAN_PROJECT_IGNORED_FILE_PATTERNS = [
    ".DS_Store",
    # macOS resource fork files (binary Apple Double metadata)
    "._*",
    # Vim swap files
    "*.swp",
    "*.swo",
    "*.swn",
    # Emacs backup and autosave files
    "*~",
    "\\#*\\#",
    ".#*",
    "*.olean",
    "*.olean.lock",
    "*.olean.trace",
    "*.olean.hash",
    "*.olean.private",
    "*.olean.private.hash",
    "*.olean.server",
    "*.olean.server.hash",
    "*.ilean",
    "*.ilean.trace",
    "*.ilean.hash",
    "*.trace",
    "*.ir",
    "*.ir.hash",
    "*.o",
    "*.so",
    "*.so.hash",
    # any ir files
    "**/.lake/build/ir",
    "**/.lake/build/bin",
    # all nested .git repos
    "**/.git",
] + [f"**/.lake/packages/{pkg}" for pkg in MATHLIB_INCLUDED_STD_PACKAGES]

SECRET_FILE_PATTERNS = (
    ".env",
    ".env.*",
    ".envrc",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.ppk",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ecdsa_sk",
    "id_ed25519",
    "id_ed25519_sk",
    ".netrc",
    "_netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    ".my.cnf",
    ".htpasswd",
    ".vault-token",
    ".s3cfg",
    ".boto",
    "kubeconfig",
    "*.tfstate",
    "*.tfstate.*",
    "*.tfvars",
    "*.tfvars.json",
    "credentials",
    "credentials.json",
    "credentials.toml",
    "application_default_credentials.json",
    "*.setup.json",
)
"""Lowercase file-name globs that ci, review and submit never upload, even when tracked in git; matched ignoring case.
A hidden folder so named, such as ``.env/``, keeps everything in it out too."""

SECRET_FOLDER_NAMES = frozenset({".docker", ".ssh", ".kube", ".gnupg", ".aws", ".azure", ".terraform.d"})
"""Lowercase names of hidden folders whose files hold credentials under names no pattern matches, as
``.docker/config.json`` holds registry logins, ``.ssh/config`` and ``.kube/config`` hosts and tokens, ``.aws/config``
SSO sessions; ci, review and submit upload nothing in them."""

SECRET_FOLDER_PATHS = (
    (".config", "gcloud"),
    (".config", "gh"),
    (".config", "glab-cli"),
    ("appdata", "roaming", "gcloud"),
    ("appdata", "roaming", "github cli"),
    ("appdata", "roaming", "terraform.d"),
)
"""Lowercase folders, as consecutive names, whose files hold a CLI's credentials under names no pattern matches, as
``.config/gh/hosts.yml`` holds GitHub's token and ``.config/gcloud`` Google Cloud's, and their Windows homes under
``AppData\\Roaming``; nothing in them goes up either."""

LEAN_BUILD_FILES = frozenset({"lake-manifest.json", "lakefile.lean", "lakefile.toml", "lean-toolchain"})
"""File names that no ignore rule keeps out of ci, review or submit, at any depth."""

GIT_TIMEOUT_SECONDS = 60

# Negation patterns to always include .lake directories even if gitignored
GITIGNORE_EXEMPT_PATTERNS = [
    "!.lake",
    "!.lake/**",
    "!**/.lake",
    "!**/.lake/**",
]


def load_gitignore_spec(project_dir: Path) -> pathspec.PathSpec | None:
    """Load .gitignore patterns from a project directory.

    Appends negation patterns for .lake directories to ensure they are always
    included even if the .gitignore would otherwise exclude them.

    Args:
        project_dir: Path to the project directory containing .gitignore.

    Returns:
        A PathSpec object for matching gitignore patterns, or None if no .gitignore exists.
    """
    gitignore_path = project_dir / ".gitignore"
    if not gitignore_path.is_file():
        return None

    try:
        with open(gitignore_path, "r") as f:
            lines = f.readlines()
        # Append exemption patterns at the end so they take precedence
        lines.extend(GITIGNORE_EXEMPT_PATTERNS)
        return pathspec.PathSpec.from_lines("gitwildmatch", lines)
    except Exception as e:
        logger.warning(f"Failed to read .gitignore file: {e}")
        return None


#: Code points HFS+ ignores in a name, as git's is_hfs_dotgit lists them, so ``.g\u200cit`` names ``.git`` there.
_HFS_IGNORABLE = frozenset(
    "\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u206a\u206b\u206c\u206d\u206e\u206f\ufeff"
)


def folded_name(name: str) -> str:
    """``name`` as a case-insensitive disk matches it: case folded, as ``ſ`` is ``s``, without the code points HFS+
    ignores or the trailing dots and spaces Windows drops."""
    return "".join(c for c in name if c not in _HFS_IGNORABLE).casefold().rstrip(". ")


def is_secret_name(name: str) -> bool:
    """Whether a file or folder name matches SECRET_FILE_PATTERNS, in any case or spelling a disk takes for it."""
    name = folded_name(name)
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_FILE_PATTERNS)


def is_secret_file(path: Path) -> bool:
    """Whether the name of ``path`` matches SECRET_FILE_PATTERNS, ignoring case."""
    return is_secret_name(path.name)


def is_in_secret_folder(path: Path, root: Path | None = None) -> bool:
    """Whether a hidden folder ``path`` is in has a name that matches SECRET_FILE_PATTERNS, as ``.env/`` does, or is in
    SECRET_FOLDER_NAMES, as ``.docker/`` is, or ``path`` is in one of SECRET_FOLDER_PATHS, as ``.config/gh/`` is.
    With ``root``, the folder a relative ``path`` is in, typed or resolved, that folder's own name counts too, as a root
    that is itself ``.docker/`` does, and with the names above it, one of SECRET_FOLDER_PATHS that ends at or below
    ``root``, as a root at ``~/.config``, ``%APPDATA%`` or ``%APPDATA%\\gcloud`` does; one wholly above ``root``
    doesn't count."""
    if root is None or path.is_absolute():
        return _in_secret_folder((), path.parent.parts)
    tops = {Path(os.path.abspath(root)), Path(os.path.realpath(root))}
    longest = max(len(folder) for folder in SECRET_FOLDER_PATHS)
    return any(_in_secret_folder(top.parts[-longest:], path.parent.parts) for top in tops)


def _in_secret_folder(above: tuple[str, ...], parents: tuple[str, ...]) -> bool:
    """Whether folders ``parents`` name hold credentials, below the folders ``above`` names: the last of those counts as
    a folder of its own, and a folder of SECRET_FOLDER_PATHS that reaches it, as ``AppData/Roaming`` does."""
    own = [folded_name(part) for part in (*above[-1:], *parents)]
    if any(part.startswith(".") and (is_secret_name(part) or part in SECRET_FOLDER_NAMES) for part in own):
        return True
    parts = [folded_name(part) for part in (*above, *parents)]
    return any(
        tuple(parts[i : i + len(folder)]) == folder and i + len(folder) >= len(above)
        for folder in SECRET_FOLDER_PATHS
        for i in range(len(parts))
    )


def in_git_folder(path: PurePath) -> bool:
    """Whether ``path`` runs through a .git folder, in any case or spelling a disk takes for it, as git refuses to
    track one."""
    return any(folded_name(part) == ".git" for part in path.parts)


def in_git_dir(real: Path) -> bool:
    """Whether ``real``, a resolved path, is in a git directory by what it holds, whatever its name, below the nearest
    work tree: a ``.git`` that links to ``.repo/projects/x.git`` resolves to a name in_git_folder() can't see, while a
    worktree checked out inside a bare repository is a work tree, not part of it."""
    for folder in (real, *real.parents):
        if _marks_work_tree(folder):
            return False
        if is_git_dir(folder):
            return True
    return False


def _marks_work_tree(folder: Path) -> bool:
    """Whether ``folder`` has a .git that git reads as a work tree's: a file naming its git directory, or a folder that
    is one. git reads a folder with any other .git, such as a stray empty one, as a bare repository."""
    dot_git = folder / ".git"
    try:
        if dot_git.is_file():
            with open(dot_git, "rb") as marker:
                return marker.read(8) == b"gitdir: "
        return dot_git.is_dir() and is_git_dir(dot_git)
    except OSError:
        return False


def is_git_dir(folder: Path) -> bool:
    """Whether ``folder`` holds git's own files: a config setting the repository's format, which git writes in every
    repository and a checkout of one committed with its HEAD ignored still has; or a HEAD naming a ref or a commit, as
    git requires, beside objects and refs, or a linked worktree's commondir. Never a work tree, whose .git says so."""
    if _marks_work_tree(folder):
        return False
    try:
        with open(folder / "config", "rb") as config:
            if re.search(rb"(?mi)^\s*repositoryformatversion\s*=", config.read(4096)):
                return True
    except OSError:
        pass
    try:
        with open(folder / "HEAD", "rb") as head:
            start = head.read(80).strip()
        if not (start.startswith(b"ref: ") or re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", start)):
            return False
        return ((folder / "objects").is_dir() and (folder / "refs").is_dir()) or (folder / "commondir").is_file()
    except OSError:
        return False


def is_secret_path(path: Path, root: Path | None = None) -> bool:
    """Whether ``path`` is never uploaded as a credential: by its name, or by a folder it is in, ``root``'s own name
    counting as is_in_secret_folder() takes it. ci, review and submit all decide by this."""
    return is_secret_file(path) or is_in_secret_folder(path, root)


def secret_files(files: list[Path]) -> list[Path]:
    """The subset of ``files`` that is never uploaded."""
    return [p for p in files if is_secret_path(p)]


#: The names the CLI saves a project's archive under: the server's ``<id>-aristotle.tar.gz`` for a result and
#: ``input-<id>.tar.gz`` for an input, or ``<name>_aristotle.tar.gz`` when it names none.
_RESULT_ARCHIVE = re.compile(r".*[-_]aristotle\.tar\.gz|input-[0-9a-fA-F-]{36}\.tar\.gz")


def is_result_archive(path: Path) -> bool:
    """Whether ``path`` is named as the CLI saves a project's archive, such as ``submit --wait`` saves in the current
    folder, so it is an earlier run's output rather than part of the project."""
    return _RESULT_ARCHIVE.fullmatch(path.name) is not None


def log_excluded_secrets(excluded: list[Path]) -> None:
    """Warn that ``excluded`` were left out, naming each."""
    if excluded:
        logger.warning(f"Excluded {len(excluded)} file(s) matching secret patterns:{listing(excluded)}")


def listing(paths: list[Path]) -> str:
    """Each of ``paths`` as archive_name() spells it, to follow a colon: after a space when there are five or fewer,
    else one to a line."""
    names = [archive_name(p) for p in paths]
    return " " + ", ".join(names) if len(names) <= 5 else "".join(f"\n  {name}" for name in names)


def _name_bytes(path: Path) -> bytes:
    """``path`` as the bytes of its name on disk, the inverse of how Python decodes a file name."""
    return path.as_posix().encode(sys.getfilesystemencoding(), "surrogateescape")


def _escaped(text: str) -> str:
    """``text``'s bytes, as the file system encodes it, decoded as UTF-8 and escaped where they aren't UTF-8; a code
    point the file system encoding can't hold, such as an unpaired surrogate in a Windows name, as its escape."""
    try:
        return text.encode(sys.getfilesystemencoding(), "surrogateescape").decode("utf-8", "backslashreplace")
    except UnicodeEncodeError:
        return text.encode("utf-8", "backslashreplace").decode("utf-8")


def archive_name(path: Path) -> str:
    """The name ``path`` goes up under: its bytes decoded as UTF-8, escaped where they aren't UTF-8."""
    return _escaped(path.as_posix())


def is_utf8_name(path: Path) -> bool:
    """Whether ``path``'s name on disk is UTF-8; Aristotle fails a run whose archive holds a name that isn't."""
    try:
        _name_bytes(path).decode("utf-8")
    except UnicodeError:
        return False
    return True


def is_utf8_text(text: str) -> bool:
    """Whether ``text`` encodes as UTF-8, as a name must for tarfile to write it into an archive as UTF-8."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def upload_name(name: str) -> str:
    """``name`` as an upload's file name or a project's name: unchanged when it is UTF-8 text, else escaped as
    archive_name() does."""
    return name if is_utf8_text(name) else _escaped(name)


def not_utf8_warning(paths: list[Path], kind: str) -> str:
    """The warning that ``paths``, files of ``kind`` whose names aren't UTF-8, were left out, naming each."""
    return (
        f"Not uploading {len(paths)} {kind}(s) whose name is not UTF-8, which Aristotle can't take; "
        f"rename one to upload it:{listing(paths)}"
    )


def nothing_to_upload(
    folder: Path, empty: bool, found: int, ignored: int, skipped: list[Path], unreadable: list[Path]
) -> str:
    """Why an upload of ``folder`` would hold nothing: it is ``empty``, some of its folders are ``unreadable``, it holds
    no file the walk reaches, or each of the ``found`` is left out, ``ignored`` of them by an ignore rule, ``skipped``
    by Aristotle's own exclusions, and the rest as the warnings before name them."""
    if empty:
        return f"Nothing to upload: {folder} is empty."
    if unreadable:
        which = (
            f"its folder {unreadable[0].as_posix()}"
            if len(unreadable) == 1
            else f"{len(unreadable)} of its folders ({_first_names(unreadable)})"
        )
        return f"Nothing to upload from {folder}: {which} can't be read, and nothing else in it goes up."
    if not found:
        return f"Nothing to upload from {folder}: it holds no file Aristotle reads."
    if found == 1:
        if ignored:
            why = "its only file matches an ignore rule"
        elif skipped:
            why = f"its only file, {skipped[0].as_posix()}, is one Aristotle skips"
        else:
            why = "its only file is left out, as noted above"
        return f"Nothing to upload from {folder}: {why}."
    names = _first_names(skipped)
    rest = found - ignored - len(skipped)
    reasons = [
        (ignored, "matches an ignore rule", "match an ignore rule"),
        (len(skipped), f"is one Aristotle skips ({names})", f"are ones Aristotle skips ({names})"),
        (rest, "is left out, as noted above", "are left out, as noted above"),
    ]
    present = [(count, one, many) for count, one, many in reasons if count]
    if len(present) == 1:
        every = "both its files" if found == 2 else f"all {found} of its files"
        return f"Nothing to upload from {folder}: {every} {present[0][2]}."
    parts = [f"{count} {one if count == 1 else many}" for count, one, many in present]
    no = "neither of its files goes" if found == 2 else f"none of its {found} files goes"
    return f"Nothing to upload from {folder}: {no} up; {', '.join(parts[:-1])} and {parts[-1]}."


def _first_names(paths: list[Path]) -> str:
    """The first three of ``paths``, and an ellipsis for any more."""
    return ", ".join(path.as_posix() for path in paths[:3]) + (", …" if len(paths) > 3 else "")


def upload_summary(uploaded: int, ignored: int, included: int = 0) -> str:
    """One line: how many files were archived, and how many an ignore rule left out or, of those archived, matches."""
    return f"Uploading {uploaded} {'file' if uploaded == 1 else 'files'}{ignore_counts(ignored, included)}."


def ignore_counts(ignored: int, included: int) -> str:
    """The clause after an upload's file count saying how many files an ignore rule left out, and how many of those
    that go up it matches; "" for neither."""
    clause = ""
    if included:
        clause += f"; {included} of them {'matches' if included == 1 else 'match'} an ignore rule"
    if ignored:
        clause += f"; {ignored} left out because {'it matches' if ignored == 1 else 'they match'} an ignore rule"
    return clause


INCLUDE_IGNORED_HINT = (
    "pass --include-ignored (from Python, the include_ignored argument), which uploads the files ignore rules keep out"
)
"""The clause naming --include-ignored as the way to upload what an ignore rule keeps out of submit's folder."""


def held_back_warning(rules: dict[Path, str], include_ignored: bool = False) -> str:
    """One warning naming each tracked file an ignore rule keeps out of the upload, with its rule if known, and how to
    upload it, by INCLUDE_IGNORED_HINT too if ``include_ignored``."""
    count = len(rules)
    lines = [f"Not uploading {count} tracked {'file' if count == 1 else 'files'} that an ignore rule matches:"]
    lines += [f"  {path.as_posix()}" + (f"  ({rule})" if rule else "") for path, rule in sorted(rules.items())]
    lines.append(
        "To upload one, add a line after its rule: `!/` and its path from the folder of the rule's .gitignore, with a "
        "backslash before any *, ?, [ or \\ in it. If the rule matches a folder above the file, first let each "
        "folder through and ignore its contents again, a level at a time: for build/gen/keep.h and a `build/` rule, "
        "the lines are `!/build/`, `/build/*`, `!/build/gen/`, `/build/gen/*`, then `!/build/gen/keep.h`."
        + (f" Or {INCLUDE_IGNORED_HINT}." if include_ignored else "")
    )
    return "\n".join(lines)


def skipped_symlinks_warning(skipped: dict[Path, str]) -> str:
    """One warning naming each symlinked file left out of the upload, and why."""
    count = len(skipped)
    lines = [
        f"Left out {count} symbolic {'link' if count == 1 else 'links'}; "
        "a link is uploaded only when the file it points to is uploaded too:"
    ]
    lines += [f"  {path} ({reason})" for path, reason in sorted(skipped.items())]
    return "\n".join(lines)


class SkippedSymlinkError(Exception):
    """A symlinked file is not uploaded; the message says why."""


class FileChangedError(OSError):
    """A file, or the path to it, changed between its check and its archiving."""

    @classmethod
    def at(cls, path: Path | str) -> "FileChangedError":
        """The error for the file at ``path``."""
        return cls(None, "it changed while it was being uploaded", os.fspath(path))


class LeanProjectError(Exception):
    """Exception raised for Lean project-related errors."""

    pass


def _git(project_dir: Path, *args: str, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    """Run git in ``project_dir``; raises FileNotFoundError without git, LeanProjectError if it fails to run or stalls."""
    try:
        return subprocess.run(
            ["git", "-C", str(project_dir), *args],
            input=stdin,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        raise
    except subprocess.TimeoutExpired as e:
        raise _git_failure(project_dir, f"git {args[0]} timed out after {GIT_TIMEOUT_SECONDS}s") from e
    except OSError as e:
        raise _git_failure(project_dir, str(e)) from e


def _git_failure(project_dir: Path, detail: str) -> LeanProjectError:
    return LeanProjectError(f"Could not ask git which files in {project_dir} it ignores: {detail}")


def _check_ignore(project_dir: Path, *args: str, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    """``git check-ignore --no-index`` in ``project_dir``, which exits 0 if a path is ignored and 1 if none is.

    Raises:
        LeanProjectError: git failed or stalled.
    """
    result = _git(project_dir, "check-ignore", "--no-index", *args, stdin=stdin)
    if result.returncode not in (0, 1):
        raise _git_failure(project_dir, os.fsdecode(result.stderr).strip())
    return result


def _slashed(path: str) -> str:
    """``path`` with forward slashes, as git compares paths on every system."""
    return path.replace(os.sep, "/")


def _is_absolute(entry: str) -> bool:
    """Whether git takes a GIT_CEILING_DIRECTORIES entry as absolute: one that starts with a slash, or on Windows a
    backslash or any one character and a colon, such as ``C:work``, which os.path.isabs refuses, or ``1:`` for a drive
    subst made."""
    if os.name == "nt":
        return entry[:1] in ("/", "\\") or entry[1:2] == ":"
    return entry.startswith("/")


def _git_ceilings() -> list[str]:
    """GIT_CEILING_DIRECTORIES as git reads it: its absolute entries, each with its symlinks resolved, but as written
    after an empty entry, backslashes and all; one that can't be resolved is dropped."""
    ceilings: list[str] = []
    as_written = False
    for entry in os.environ.get("GIT_CEILING_DIRECTORIES", "").split(os.pathsep):
        if not entry:
            as_written = True
        elif _is_absolute(entry):
            try:
                ceilings.append(entry if as_written else _slashed(os.path.realpath(entry, strict=True)))
            except OSError:
                pass
    return ceilings


def _length(path: str) -> int:
    """How long ``path`` is, a root without its trailing slash, as git measures the folders it searches."""
    return len(path) - 1 if path.endswith("/") else len(path)


def _ceiling(path: str) -> int:
    """The length of the longest of _git_ceilings() that is above ``path``, or -1: git's longest_ancestor_length."""
    longest = -1
    for ceiling in _git_ceilings():
        length = _length(ceiling)
        if path.startswith(ceiling[:length] + "/") and len(path) > length + 1:
            longest = max(longest, length)
    return longest


def enclosing_repository(project_dir: Path) -> Path | None:
    """The nearest of ``project_dir`` and the folders above it that holds a ``.git``, searched as git searches: from
    ``project_dir``, up to but not into the nearest GIT_CEILING_DIRECTORIES folder above it. None if none does or its
    path cannot be resolved."""
    try:
        # macOS's getcwd() fails under a folder that cannot be searched; Path.exists() would raise before 3.14.
        resolved = project_dir.resolve()
    except OSError:
        return None
    ceiling = _ceiling(_slashed(str(resolved)))
    for folder in (resolved, *resolved.parents):
        if folder != resolved and _length(_slashed(str(folder))) <= ceiling:
            return None
        if os.path.lexists(folder / ".git"):
            return folder
    return None


def _top_level_ignored(project_dir: Path, candidates: list[Path]) -> set[Path]:
    """The ``candidates`` that ``project_dir``'s top-level .gitignore excludes."""
    spec = load_gitignore_spec(project_dir)
    return set() if spec is None else {p for p in candidates if spec.match_file(str(p))}


def _ignored_itself(project_dir: Path) -> str | None:
    """``project_dir``'s path from the top of its work tree if git ignores the folder itself, which leaves only its
    top-level .gitignore to apply; else None.

    Raises:
        FileNotFoundError: git isn't installed.
        LeanProjectError: git failed or stalled.
    """
    # git reads "." as "<dir>/", which a "<dir>/*" rule matches; ask about the folder by name, as git status does.
    prefix = _git(project_dir, "rev-parse", "--show-prefix").stdout.strip()
    if prefix and _check_ignore(project_dir, "--quiet", f"../{project_dir.resolve().name}").returncode == 0:
        return os.fsdecode(prefix)
    return None


def _git_ignored(project_dir: Path, candidates: list[Path], kept: frozenset[Path]) -> set[Path] | None:
    """The ``candidates`` git ignores, a tracked one only for a rule in one of the repository's .gitignore files, or
    in a folder git ignores those its top-level .gitignore excludes, naming the tracked ones not in ``kept``; None if
    ``project_dir`` is outside a work tree.

    Raises:
        LeanProjectError: git could not say which files it ignores.
    """
    try:
        inside = _git(project_dir, "rev-parse", "--is-inside-work-tree")
    except FileNotFoundError:
        if enclosing_repository(project_dir):
            logger.warning(f"git is not installed, so only {project_dir}'s top-level .gitignore was applied.")
        return None
    if inside.returncode != 0 or inside.stdout.strip() != b"true":
        if inside.returncode != 0 and enclosing_repository(project_dir):
            # Such as a repository git calls dubiously owned, or a worktree mounted without its main repository.
            reason = os.fsdecode(inside.stderr).strip()
            logger.warning(f"git refused {project_dir}, so only its top-level .gitignore was applied: {reason}")
        return None
    if (prefix := _ignored_itself(project_dir)) is not None:
        logger.debug(f"git ignores {project_dir} itself; applying only its top-level .gitignore")
        ignored = _top_level_ignored(project_dir, candidates)
        if held_back := _tracked(project_dir, ignored - kept):
            logger.warning(held_back_warning(dict.fromkeys(held_back, f"{prefix}.gitignore"), include_ignored=True))
        return ignored
    if not candidates:
        return set()
    # The "./" keeps a leading ":" from being read as pathspec magic; git echoes each ignored path as given.
    spelled = {b"./" + os.fsencode(p.as_posix()): p for p in candidates}
    output = _check_ignore(project_dir, "-z", "--stdin", stdin=b"".join(path + b"\0" for path in spelled)).stdout
    ignored = {spelled[path] for path in output.split(b"\0") if path}
    if held_back := _tracked(project_dir, ignored):
        # Only the repository's .gitignore files hold a tracked file back, never .git/info/exclude or a global excludes
        # file, which are this machine's alone.
        rules = _gitignore_rules(project_dir, held_back)
        if rules is not None:
            ignored -= {p for p in held_back if p not in rules}
            held_back = [p for p in held_back if p in rules]
        if named := [p for p in held_back if p not in kept]:
            logger.warning(held_back_warning({p: (rules or {}).get(p, "") for p in named}, include_ignored=True))
    return ignored


def _tracked(project_dir: Path, paths: set[Path]) -> list[Path]:
    """The ``paths``, relative to ``project_dir``, that git or a checked-out submodule tracks; none, with a warning, if
    git cannot say."""
    if not paths:
        return []
    try:
        result = _git(project_dir, "ls-files", "-z", "--recurse-submodules")
    except LeanProjectError as e:
        detail = str(e)
    else:
        if result.returncode == 0:
            listed = set(result.stdout.split(b"\0"))
            return [p for p in paths if os.fsencode(p.as_posix()) in listed]
        detail = os.fsdecode(result.stderr).strip()
    logger.warning(f"git could not say which files in {project_dir} it tracks, so none held back is named: {detail}")
    return []


def left_out_rules(project_dir: Path, paths: list[Path]) -> dict[Path, str]:
    """Each of ``paths``, files of ``project_dir`` gitignored_files() leaves out, with its rule as ignore_rules() names
    it, or ``<prefix>.gitignore`` when git ignores ``project_dir`` itself and that file alone applies; "" when git
    can't say."""
    try:
        prefix = _ignored_itself(project_dir)
    except (OSError, LeanProjectError):
        return dict.fromkeys(paths, "")
    return ignore_rules(project_dir, paths) if prefix is None else dict.fromkeys(paths, f"{prefix}.gitignore")


def ignore_rules(root: Path, paths: list[Path]) -> dict[Path, str]:
    """Each of ``paths``, files an ignore rule matches, relative to ``root``, with ``<file>:<line>: <pattern>`` for the
    rule of the repository's .gitignore files that git reads as ignoring it, or "" when none does or git can't say."""
    rules = _gitignore_rules(root, paths) or {}
    return {p: rules.get(p, "") for p in paths}


def _gitignore_rules(root: Path, paths: list[Path]) -> dict[Path, str] | None:
    """``<file>:<line>: <pattern>`` for each of ``paths``, relative to ``root``, that a rule of the repository's
    .gitignore files matches, the file named from the repository's top; None if git can't say. git reads no
    .git/info/exclude or global excludes file here, so no folder rule of theirs hides a .gitignore's."""
    spelled = {b"./" + os.fsencode(p.as_posix()): p for p in paths}
    try:
        top = _git(root, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            return None
        # As the repository reads its patterns, on a file system that ignores case too.
        ignore_case = _git(root, "config", "--bool", "core.ignoreCase").stdout.strip() == b"true"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as empty:
            # A git dir with no info/exclude, made by hand: git init would follow an inherited GIT_DIR into the user's
            # repository, and could copy an info/exclude from init.templateDir. An empty core.excludesFile names no
            # global file.
            (Path(empty) / "objects").mkdir()
            (Path(empty) / "refs").mkdir()
            (Path(empty) / "HEAD").write_text("ref: refs/heads/main\n")
            result = _git(
                root,
                f"--git-dir={empty}",
                f"--work-tree={os.fsdecode(top.stdout).rstrip(chr(10))}",
                "-c",
                "core.excludesFile=",
                "-c",
                f"core.ignoreCase={'true' if ignore_case else 'false'}",
                "check-ignore",
                "-v",
                "-z",
                "--no-index",
                "--stdin",
                stdin=b"".join(s + b"\0" for s in spelled),
            )
    except (OSError, LeanProjectError):
        return None
    if result.returncode not in (0, 1):
        return None
    fields = result.stdout.split(b"\0")
    # -v also prints a negation that matches last, which lets the path through rather than ignoring it.
    return {
        spelled[path]: f"{os.fsdecode(source)}:{os.fsdecode(line)}: {os.fsdecode(pattern)}"
        for source, line, pattern, path in zip(fields[0::4], fields[1::4], fields[2::4], fields[3::4])
        if path in spelled and source and not pattern.startswith(b"!")
    }


def gitignored_files(project_dir: Path, candidates: list[Path], kept: frozenset[Path] = frozenset()) -> set[Path]:
    """The ``candidates``, relative to ``project_dir``, that .gitignore excludes; never one of LEAN_BUILD_FILES, or a
    file under a ``.lake`` folder.

    In a git work tree git decides, from every ignore rule that applies, though only a rule in one of the repository's
    .gitignore files holds back a tracked file; elsewhere, or when git ignores ``project_dir`` itself, only
    ``project_dir``'s top-level .gitignore applies. In a work tree the ``aristotle`` logger names each tracked file held
    back, with INCLUDE_IGNORED_HINT, except those in ``kept``, which go up regardless.

    Raises:
        LeanProjectError: git could not say which files it ignores.
    """
    checked = [p for p in candidates if ".lake" not in p.parts and p.name not in LEAN_BUILD_FILES]
    ignored = _git_ignored(project_dir, checked, kept)
    return _top_level_ignored(project_dir, checked) if ignored is None else ignored


def get_pruned_dirnames(dirnames: list[str], rel_dir: Path) -> list[str]:
    """Filter directory names to skip during os.walk traversal.

    Prevents descending into large excluded directories (e.g. .lake/packages/mathlib)
    so that files inside them are never stat'd. This is the traversal-time counterpart
    to should_skip_input_file, which filters individual files after discovery.

    Args:
        dirnames: Subdirectory names yielded by os.walk for the current directory.
        rel_dir: Current directory path relative to the project root.

    Returns:
        Filtered list of directory names safe to descend into.
    """
    skip: set[str] = {".git"}

    for i, part in enumerate(rel_dir.parts):
        if part == ".lake":
            sub_parts = rel_dir.parts[i + 1 :]
            if sub_parts == ("packages",):
                skip.update(MATHLIB_INCLUDED_STD_PACKAGES)
            elif sub_parts == ("build",):
                skip.update(("ir", "bin"))
            break

    # A .git folder in any case or spelling: a case-insensitive disk takes .Git for git's own.
    return [d for d in dirnames if d not in skip and folded_name(d) != ".git"]


def should_skip_input_file(
    file_path: Path,
    base_path: Path | None = None,
    gitignore_spec: pathspec.PathSpec | None = None,
) -> bool:
    """Check if a file should be skipped based on Lean project ignore patterns and gitignore.

    Args:
        file_path: Path to the file to check.
        base_path: If provided, the file path will be made relative to this before matching.
            This is important for patterns like "**/.lake/build/ir" to match correctly.
        gitignore_spec: Optional PathSpec from load_gitignore_spec() to also check
            gitignore patterns. Should include .lake exemptions.

    Returns:
        True if the file should be skipped, False otherwise.
    """
    spec = pathspec.PathSpec.from_lines("gitwildmatch", LEAN_PROJECT_IGNORED_FILE_PATTERNS)

    if base_path is not None:
        rel_path = file_path.relative_to(base_path)
    else:
        rel_path = file_path

    rel_path_str = str(rel_path)

    # Check built-in ignore patterns first
    if spec.match_file(rel_path_str):
        return True

    # Check gitignore patterns if provided
    if gitignore_spec is not None and gitignore_spec.match_file(rel_path_str):
        return True

    return False


def file_identity(file_stat: os.stat_result) -> tuple[int, int] | None:
    """The ``(st_dev, st_ino)`` pair naming a file whatever path reaches it; None if the inode number is 0."""
    if file_stat.st_ino == 0:
        return None
    return file_stat.st_dev, file_stat.st_ino


def add_file(tar: tarfile.TarFile, path: Path | str, arcname: str, checked: os.stat_result) -> None:
    """Archive the regular file at ``path`` as the file member ``arcname``, read from a handle opened without following
    a link at ``path``, or, where the platform can't open that way, from one whose device and inode are those of
    ``checked``, its lstat; ``tar`` is opened with dereference=True, or a second name for a file goes in as a hard link.

    Raises:
        FileChangedError: ``path`` is a link or no regular file now, or, without O_NOFOLLOW, another file.
        OSError: it couldn't be opened or read.
    """
    # Compared only without O_NOFOLLOW: fstat's device and inode can differ from lstat's, as on overlayfs before 4.19.
    identity = None if hasattr(os, "O_NOFOLLOW") else file_identity(checked)
    changed = FileChangedError.at(path)
    try:
        f = _open_checked(path, identity, follow=False)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise changed from e
        raise
    if f is None:
        raise changed
    with f:
        tar.addfile(tar.gettarinfo(arcname=arcname, fileobj=f), f)


def add_symlinked_file(
    tar: tarfile.TarFile, link: Path, arcname: str, archived: set[tuple[int, int]]
) -> tuple[int, int]:
    """Archive the file ``link`` points to as the file member ``arcname``, as add_file() does, if it is a regular file
    whose identity is in ``archived``; return that identity.

    The content is read from a handle whose identity matches the one checked.

    Raises:
        SkippedSymlinkError: the link does not resolve, or points to anything else.
    """
    try:
        target = os.stat(link)
    except OSError as e:
        raise SkippedSymlinkError("broken link") from e
    identity = file_identity(target)
    if not stat.S_ISREG(target.st_mode) or identity is None or identity not in archived:
        raise SkippedSymlinkError("target not uploaded")
    try:
        f = _open_checked(link, identity, follow=True)
    except OSError as e:
        raise SkippedSymlinkError("broken link") from e
    if f is None:
        raise SkippedSymlinkError("target not uploaded")
    with f:
        tar.addfile(tar.gettarinfo(arcname=arcname, fileobj=f), f)
    return identity


def _open_checked(path: Path | str, identity: tuple[int, int] | None, *, follow: bool) -> BinaryIO | None:
    """The regular file at ``path``, opened to read, if its identity is ``identity`` or that is None; else None. Unless
    ``follow``, a link at ``path`` isn't followed where the platform can refuse to. The open doesn't block on a named
    pipe; on EAGAIN, as a lease holder makes it, a second open waits if ``path`` is still a regular file.

    Raises:
        OSError: it couldn't be opened: ELOOP for a link when not ``follow``.
    """
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | (0 if follow else getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags | getattr(os, "O_NONBLOCK", 0))
    except BlockingIOError:
        if not stat.S_ISREG((os.stat if follow else os.lstat)(path).st_mode):
            return None
        fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if stat.S_ISREG(opened.st_mode) and (identity is None or file_identity(opened) == identity):
            return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    return None


def is_junction(path: Path) -> bool:
    """Whether ``path`` is a Windows directory junction, which os.walk enters, unlike a folder's symbolic link."""
    if sys.platform != "win32":
        return False
    try:
        return os.lstat(path).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    except OSError:
        return False


def upload_too_large(size: int) -> str | None:
    """The sentence saying that an upload archive of ``size`` bytes is over MAX_FILE_SIZE_BYTES; None if it isn't."""
    if size <= MAX_FILE_SIZE_BYTES:
        return None
    mb = 1024 * 1024
    return f"The upload is {size / mb:.1f} MB compressed, over the {MAX_FILE_SIZE_BYTES // mb} MB limit."


def get_lean_toolchain_version(project_dir: Path) -> str | None:
    """
    Get the lean-toolchain version from the project.

    Looks for the outermost lean-toolchain file (starting from the top level directory).

    Args:
        project_dir: Path to the project directory

    Returns:
        The toolchain version string if found, None otherwise
    """
    toolchain_path = project_dir / "lean-toolchain"
    # os.path, not pathlib: before 3.14, Path.is_file() raises for a path out of reach.
    if os.path.isfile(toolchain_path):
        try:
            return toolchain_path.read_text().strip()
        except Exception as e:
            logger.warning(f"Failed to read lean-toolchain file: {e}")
            return None

    # If not found at top level, search for it in subdirectories
    for toolchain_file in sorted(project_dir.rglob("lean-toolchain"), key=lambda p: len(p.parts)):
        try:
            return toolchain_file.read_text().strip()
        except Exception as e:
            logger.warning(f"Failed to read lean-toolchain file at {toolchain_file}: {e}")
            return None

    return None


def has_any_lean_files(project_dir: Path) -> bool:
    """
    Check if the project directory contains any .lean files.

    Args:
        project_dir: Path to the project directory

    Returns:
        True if at least one .lean file exists, False otherwise
    """
    return any(project_dir.rglob("*.lean"))


async def get_expected_toolchain() -> str | None:
    """
    Get the expected toolchain version from the Aristotle API.

    Returns:
        The expected toolchain version string if available, None otherwise
    """
    try:
        async with api_request.AristotleRequestClient() as client:
            response = await client.get("/version")
            version_data = response.json()
            return version_data.get("lean_toolchain_version")
    except Exception as e:
        logger.warning(f"Failed to fetch expected toolchain from API: {e}")
        return None


def check_lake_manifest_references_local_deps(project_dir: Path) -> None:
    """
    Check if lake-manifest.json references any local dependencies.
    Throws an error if it does, because local deps aren't supported in the API.
    """
    manifest_path = project_dir / "lake-manifest.json"
    # os.path, not pathlib: before 3.14, Path.is_file() raises for a path out of reach.
    if not os.path.isfile(manifest_path):
        return

    try:
        with open(manifest_path, "r") as f:
            manifest_data = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to read lake-manifest.json: {e}")
        return
    else:
        packages = manifest_data.get("packages", [])
        for package in packages:
            if package.get("type") == "path":
                error_msg = f"Aristotle does not support projects with local dependencies. This project contains the local package {package.get('name')}\n"
                raise LeanProjectError(error_msg)


def archive_error(error: OSError) -> str:
    """One line saying a file couldn't be archived, which advises a retry when the file went away or changed while being
    read."""
    retry = isinstance(error, FileChangedError) or error.errno in (errno.ENOENT, errno.ESTALE)
    gone = " Try again." if retry else ""
    return f"Could not archive {error.filename or 'the upload'}: {error.strerror or error}.{gone}"
