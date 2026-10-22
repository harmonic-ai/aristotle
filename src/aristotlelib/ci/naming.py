"""The default name of the project a CI run creates."""

import logging
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from aristotlelib.ci import context, scope
from aristotlelib.ci.manifest import MANIFEST_NAME
from aristotlelib.printable import escape_controls

logger = logging.getLogger("aristotle")

GIT_TIMEOUT_SECONDS = 5
"""Longest each git call made for the name may take; a part git cannot give in time is left out."""

MAX_GLOBS_IN_NAME = 3


def _git(root: Path, *args: str) -> str | None:
    """The stripped output of a read-only git command, or None when it fails or prints nothing."""
    try:
        output = scope._run_git(root, *args, timeout=GIT_TIMEOUT_SECONDS).strip()
    except scope.ScopeError as e:
        logger.debug(f"Naming the project without git {' '.join(args)}: {e}")
        return None
    return output or None


def repository_name(remote_url: str) -> str | None:
    """The last segment of a git remote's path, less ``.git``; never its user, password or host."""
    # A remote helper's <transport>::<address>; the address is a URL, or an ext:: command line.
    remote_url = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*::", "", remote_url)
    if "://" in remote_url:
        try:
            path = unquote(urlsplit(remote_url).path)
        except ValueError:
            # Newer urlsplit refuses a bracketed host that is not an IP address, as in git's ssh://[host]:port/path.
            path = unquote(re.sub(r"^[^/?#]*|[?#].*", "", remote_url.split("://", 1)[1], flags=re.DOTALL))
    elif re.match(r"[^/\\]*:", remote_url) and not re.match(r"[A-Za-z]:[/\\]", remote_url):
        # scp-like syntax, [user@]host:path, where git skips a bracketed host as [host:port] or user@[::1] first; a
        # Windows drive letter is a local path.
        start = remote_url.find("@[") + 1
        end = remote_url.find("]", start) if remote_url.startswith("[", start) else 0
        path = remote_url[max(end, 0) :].partition(":")[2]
    else:
        path = remote_url
    name = re.split(r"[/\\]", path.rstrip("/\\"))[-1].removesuffix(".git")
    return name or None


def _repository(root: Path, build: context.CIContext) -> str:
    """Origin's repository name, else the CI system's, else the name of the checkout's top folder."""
    url = _git(root, "remote", "get-url", "origin")
    if url and (name := repository_name(url)):
        return name
    if build.repo_slug and (name := build.repo_slug.rstrip("/").rsplit("/", 1)[-1]):
        return name
    toplevel = _git(root, "rev-parse", "--show-toplevel")
    return Path(toplevel).name if toplevel else root.name


def _revision(root: Path, build: context.CIContext) -> str:
    """The branch the CI system reports, else git's current one, then HEAD's short commit, with ``+changes`` when a
    tracked file under ``root`` differs from it, staged or not, as the upload then does; either part may be missing.
    Submodules, which the upload leaves out, don't count; tracked files it leaves out otherwise, such as a credential
    file or one an ignore rule holds back, still do."""
    branch = build.branch or _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    commit = _git(root, "rev-parse", "--short", "HEAD")
    # status, not diff: diff rewrites the index under its lock, which a timeout's kill would leave behind. Without
    # renames: only whether there is a change matters, and pairing many of them up can take longer than the timeout.
    status = ("-c", "status.renames=false", "--no-optional-locks", "status", "--porcelain", "--untracked-files=no")
    if commit and _git(root, *status, "--ignore-submodules=all", "--", "."):
        logger.info(f"Checking uncommitted changes, so the project's name says {commit}+changes.")
        commit += "+changes"
    return " ".join(part for part in (branch, commit) if part)


def default_name(root: Path, build: context.CIContext, prefix: str, run: str) -> str:
    """``<repository>[/<prefix>]: <run> @ <branch> <commit>``, without the parts git and the CI system don't give."""
    location = _repository(root, build)
    if prefix.strip("/"):
        location += "/" + prefix.strip("/")
    name = f"{location}: {run}"
    revision = _revision(root, build)
    # A byte of a path or branch that isn't UTF-8 becomes \xNN, as the API takes only Unicode text.
    return escape_controls(f"{name} @ {revision}" if revision else name)


def ci_run(root: Path, manifest_path: Path) -> str:
    """``ci``, then the manifest's path under ``root``, or its file name outside it, unless it is the default one."""
    path = manifest_path.resolve()
    if path == root / MANIFEST_NAME:
        return "ci"
    try:
        return f"ci {path.relative_to(root).as_posix()}"
    except ValueError:
        return f"ci {path.name}"


def review_run(scope_name: scope.Scope, paths: list[str] | None) -> str:
    """``review <scope>``, then the first MAX_GLOBS_IN_NAME ``paths``."""
    run = f"review {scope_name}"
    if paths:
        run += " " + ", ".join(paths[:MAX_GLOBS_IN_NAME])
        if len(paths) > MAX_GLOBS_IN_NAME:
            run += f" and {len(paths) - MAX_GLOBS_IN_NAME} more"
    return run
