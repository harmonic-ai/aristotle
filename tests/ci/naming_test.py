"""Tests for aristotlelib.ci.naming."""

import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from aristotlelib.ci import naming, scope
from aristotlelib.ci.context import CIContext, CISystem
from tests.ci.conftest import git


def _output(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _local(repo: Path) -> CIContext:
    return CIContext(system=CISystem.LOCAL, workspace=repo)


@pytest.fixture
def repo(git_repo: Path) -> Path:
    """``git_repo`` on branch ``main``, with origin set to a URL that carries a token."""
    git(git_repo, "branch", "-M", "main")
    git(git_repo, "remote", "add", "origin", "https://ci-token:s3cr3t@git.example.com/hw/fifo-proofs.git")
    return git_repo


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("https://github.com/example-org/rtl.git", "rtl"),
        ("https://gitlab-ci-token:s3cr3t@gitlab.example.com/group/sub/project.git", "project"),
        ("https://x-access-token:s3cr3t@github.com/example-org/rtl", "rtl"),
        ("https://github.com/example-org/rtl/", "rtl"),
        ("https://host.example.com/my%20repo.git?private_token=s3cr3t", "my repo"),
        ("ssh://git@github.com:22/example-org/rtl.git", "rtl"),
        ("git@github.com:example-org/rtl.git", "rtl"),
        ("git@github.com:rtl.git", "rtl"),
        ("[git@gitserver:2222]:rtl.git", "rtl"),
        ("git@[fd00::5]:rtl.git", "rtl"),
        ("[::1]:hw/rtl.git", "rtl"),
        ("file:///srv/git/rtl.git", "rtl"),
        ("/srv/git/rtl.git", "rtl"),
        ("../rtl", "rtl"),
        ("C:\\repos\\rtl", "rtl"),
        ("https://ci-token:s3cr3t@git.example.com", None),
        ("git@github.com:", None),
        ("ssh://[git@gitserver]:2222/org/repo.git", "repo"),
        ("https://[::1/repo.git", "repo"),
        ("https://ci-token:s3cr3t@[gitserver]/hw/rtl.git?private_token=s3cr3t#frag", "rtl"),
        ("https://[gitserver]?private_token=/s3cr3t", None),
        ("https://[gitserver]#/s3cr3t", None),
        ("persistent-https::https://ci-token:s3cr3t@git.example.com", None),
        ("persistent-https::https://ci-token:s3cr3t@git.example.com/hw/rtl.git", "rtl"),
        ("ext::ssh -i key git.example.com %S hw/rtl.git", "rtl"),
    ],
)
def test_the_repository_is_the_last_segment_of_the_remote_path(url: str, name: str | None):
    assert naming.repository_name(url) == name


def test_the_default_name_is_the_repository_the_run_and_the_branch_and_commit(repo: Path):
    commit = _output(repo, "rev-parse", "--short", "HEAD")

    name = naming.default_name(repo, _local(repo), "", "ci")

    assert name == f"fifo-proofs: ci @ main {commit}"
    assert "s3cr3t" not in name


@pytest.mark.parametrize("staged", [False, True], ids=["unstaged", "staged"])
def test_a_checkout_with_uncommitted_changes_says_so_after_the_commit(repo: Path, caplog, staged: bool):
    """Its run checks those changes, so it isn't named after HEAD alone."""
    commit = _output(repo, "rev-parse", "--short", "HEAD")
    (repo / "rtl" / "fifo.v").write_text("changed\n")
    if staged:
        _output(repo, "add", "rtl/fifo.v")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        name = naming.default_name(repo, _local(repo), "", "ci")

    assert name == f"fifo-proofs: ci @ main {commit}+changes"
    assert f"Checking uncommitted changes, so the project's name says {commit}+changes." in caplog.messages


def test_an_untracked_file_leaves_the_name_as_the_commit(repo: Path):
    """It doesn't go up."""
    commit = _output(repo, "rev-parse", "--short", "HEAD")
    (repo / "untracked.txt").write_text("not tracked\n")

    assert naming.default_name(repo, _local(repo), "", "ci").endswith(f"@ main {commit}")


def test_a_change_outside_the_root_leaves_the_name_as_the_commit(repo: Path):
    """It doesn't go up with a run rooted in a subfolder."""
    sub = repo / "sub"
    sub.mkdir()
    (sub / "a.v").write_text("module a; endmodule\n")
    _output(repo, "add", "sub/a.v")
    _output(repo, "commit", "-qm", "sub")
    commit = _output(repo, "rev-parse", "--short", "HEAD")
    (repo / "rtl" / "fifo.v").write_text("changed outside sub\n")

    assert naming.default_name(sub, _local(repo), "sub/", "ci").endswith(f"@ main {commit}")


def test_a_submodule_at_another_commit_leaves_the_name_as_the_commit(repo: Path, tmp_path_factory):
    """The upload leaves submodules out."""
    identity = ("-c", "user.email=t@example.com", "-c", "user.name=T")
    library = tmp_path_factory.mktemp("library")
    git(library, "init", "-q")
    git(library, *identity, "commit", "-q", "--allow-empty", "-m", "one")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(library), "lib")
    git(repo, "commit", "-qm", "lib")
    commit = _output(repo, "rev-parse", "--short", "HEAD")
    git(repo / "lib", *identity, "commit", "-q", "--allow-empty", "-m", "two")

    assert naming.default_name(repo, _local(repo), "", "ci").endswith(f"@ main {commit}")


def test_checking_for_changes_writes_nothing_to_the_index(repo: Path):
    """A file touched but unchanged makes diff rewrite the index under its lock; status here leaves it as it was."""
    tracked = repo / "rtl" / "fifo.v"
    os.utime(tracked, (tracked.stat().st_atime + 10, tracked.stat().st_mtime + 10))
    index = repo / ".git" / "index"
    before = (index.stat().st_ino, index.stat().st_mtime_ns)

    commit = _output(repo, "rev-parse", "--short", "HEAD")
    assert naming.default_name(repo, _local(repo), "", "ci").endswith(f"@ main {commit}")
    assert (index.stat().st_ino, index.stat().st_mtime_ns) == before


def test_a_subproject_is_named_by_its_directory_in_the_repository(repo: Path):
    commit = _output(repo, "rev-parse", "--short", "HEAD")

    assert (
        naming.default_name(repo, _local(repo), "evals/01_add/project/", "ci")
        == f"fifo-proofs/evals/01_add/project: ci @ main {commit}"
    )


def test_the_ci_systems_branch_names_a_detached_checkout(repo: Path):
    git(repo, "checkout", "-q", "--detach")
    commit = _output(repo, "rev-parse", "--short", "HEAD")
    build = CIContext(system=CISystem.GITHUB, workspace=repo, branch="feature/arbiter")

    assert naming.default_name(repo, build, "", "review changed") == (
        f"fifo-proofs: review changed @ feature/arbiter {commit}"
    )


def test_a_detached_checkout_outside_ci_is_named_by_its_commit_alone(repo: Path):
    git(repo, "checkout", "-q", "--detach")
    commit = _output(repo, "rev-parse", "--short", "HEAD")

    assert naming.default_name(repo, _local(repo), "", "ci") == f"fifo-proofs: ci @ {commit}"


def test_without_origin_the_ci_systems_repository_names_the_run(git_repo: Path):
    build = CIContext(system=CISystem.GITLAB, workspace=git_repo, repo_slug="group/sub/rtl")

    assert naming.default_name(git_repo, build, "", "ci").startswith("rtl: ci @ ")


def test_without_origin_or_a_ci_system_the_checkouts_folder_names_the_run(git_repo: Path):
    subproject = git_repo / "rtl"

    name = naming.default_name(subproject, _local(subproject), "rtl/", "ci")

    assert name.startswith(f"{git_repo.name}/rtl: ci @ ")


def test_a_repository_without_commits_is_named_without_a_commit(tmp_path: Path):
    git(tmp_path, "init", "-q", "-b", "trunk")
    (tmp_path / "fifo.v").write_text("module fifo;\nendmodule\n")
    git(tmp_path, "add", "fifo.v")

    assert naming.default_name(tmp_path, _local(tmp_path), "", "ci") == f"{tmp_path.name}: ci @ trunk"


def test_a_git_call_that_fails_or_times_out_leaves_its_part_out(repo: Path, caplog):
    real_run_git = scope._run_git

    def run_git(root: Path, *args: str, **kwargs) -> str:
        assert kwargs["timeout"] == naming.GIT_TIMEOUT_SECONDS
        if args[:2] == ("rev-parse", "--short"):
            raise scope.ScopeError("git rev-parse --short HEAD timed out after 5s.")
        return real_run_git(root, *args, **kwargs)

    with patch.object(scope, "_run_git", side_effect=run_git):
        assert naming.default_name(repo, _local(repo), "", "ci") == "fifo-proofs: ci @ main"


def test_a_byte_of_git_output_that_is_not_utf8_is_named_as_its_hex(repo: Path):
    real_run_git = scope._run_git
    commit = _output(repo, "rev-parse", "--short", "HEAD")

    def run_git(root: Path, *args: str, **kwargs) -> str:
        if args[0] == "symbolic-ref":
            return b"caf\xe9\n".decode("utf-8", "surrogateescape")
        return real_run_git(root, *args, **kwargs)

    with patch.object(scope, "_run_git", side_effect=run_git):
        name = naming.default_name(repo, _local(repo), b"r\xe9/".decode("utf-8", "surrogateescape"), "ci")

    assert name == f"fifo-proofs/r\\xe9: ci @ caf\\xe9 {commit}"
    name.encode("utf-8")


def test_a_bracketed_host_names_the_repository_on_every_python(repo: Path):
    git(repo, "remote", "set-url", "origin", "ssh://[git@gitserver]:2222/org/repo.git")
    commit = _output(repo, "rev-parse", "--short", "HEAD")

    assert naming.default_name(repo, _local(repo), "", "ci") == f"repo: ci @ main {commit}"


def test_without_git_the_run_is_named_after_the_folder(tmp_path: Path):
    with patch.object(scope, "_run_git", side_effect=scope.ScopeError("git is not installed")):
        assert naming.default_name(tmp_path, _local(tmp_path), "", "ci") == f"{tmp_path.name}: ci"


def test_the_default_manifest_is_left_out_of_a_ci_run(tmp_path: Path):
    root = tmp_path.resolve()

    assert naming.ci_run(root, root / "aristotle.toml") == "ci"


def test_another_manifest_is_named_by_its_path_under_the_root(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert naming.ci_run(tmp_path.resolve(), Path("hw") / "advisory.toml") == "ci hw/advisory.toml"


def test_a_manifest_outside_the_root_is_named_by_its_file_name(tmp_path: Path):
    root = tmp_path.resolve()

    assert naming.ci_run(root / "repo", root / "manifests" / "nightly.toml") == "ci nightly.toml"


@pytest.mark.parametrize(
    ("scope_name", "paths", "run"),
    [
        (scope.Scope.CHANGED, None, "review changed"),
        (scope.Scope.ALL, None, "review all"),
        (scope.Scope.PATHS, ["rtl/core/**"], "review paths rtl/core/**"),
        (scope.Scope.CHANGED, ["a", "b", "c", "d", "e"], "review changed a, b, c and 2 more"),
    ],
)
def test_a_review_run_is_named_by_its_scope_and_first_globs(scope_name, paths, run):
    assert naming.review_run(scope_name, paths) == run
