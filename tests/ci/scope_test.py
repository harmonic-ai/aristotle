"""Tests for aristotlelib.ci.scope."""

import logging
import os
import random
import re
import subprocess
import sys
import time
import warnings
from pathlib import Path

import pytest

from aristotlelib import local_file_utils, wildcard
from aristotlelib.ci import context, naming, scope
from tests.ci.conftest import commit_bare_shaped, git, latin1_name, recording_git, refusing_git, without_git

#: Both ways of resolving a run's files, for the behaviour they share.
RESOLVERS = pytest.mark.parametrize(
    "resolve",
    [lambda root: scope.resolve(root, scope.Scope.ALL), scope.resolve_checkout],
    ids=["resolve", "resolve_checkout"],
)


def test_scope_all_selects_every_source_file(git_repo: Path):
    result = scope.resolve(git_repo, scope.Scope.ALL)

    assert result.focus_files == [Path("rtl/driver.c"), Path("rtl/fifo.v")]
    # Docs and the secret are still uploaded as context; only the focus set is filtered.
    assert Path("README.md") in result.upload_files
    assert not result.is_empty


def test_scope_all_ignores_untracked_and_gitignored_files(git_repo: Path):
    (git_repo / ".gitignore").write_text("build/\n")
    (git_repo / "build").mkdir()
    (git_repo / "build" / "generated.v").write_text("module generated;\nendmodule\n")
    (git_repo / "scratch.v").write_text("module scratch;\nendmodule\n")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-qm", "ignore build")

    result = scope.resolve(git_repo, scope.Scope.ALL)

    assert Path("build/generated.v") not in result.upload_files
    assert Path("scratch.v") not in result.upload_files


@RESOLVERS
def test_a_tracked_file_a_later_gitignore_rule_matches_is_held_back(git_repo: Path, caplog, resolve):
    (git_repo / "creds.yml").write_text("token: hunter2\n")
    (git_repo / "rtl" / "legacy.v").write_text("module legacy;\nendmodule\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "commit before ignoring")
    (git_repo / ".gitignore").write_text("creds.yml\nrtl/legacy.v\n")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-qm", "ignore without git rm --cached")

    with caplog.at_level(logging.WARNING, logger="aristotle"):
        result = resolve(git_repo)
        scope.warn_held_back(git_repo, result)

    assert Path("creds.yml") not in result.upload_files
    assert Path("rtl/legacy.v") not in result.upload_files
    assert Path("rtl/legacy.v") not in result.focus_files
    rules = {Path("creds.yml"): ".gitignore:1: creds.yml", Path("rtl/legacy.v"): ".gitignore:2: rtl/legacy.v"}
    assert local_file_utils.held_back_warning(rules) in caplog.messages


def _machine_rule(repo: Path, rule: str, where: str, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Add ``rule`` to .git/info/exclude, or to a global excludes file named .gitignore, as many are."""
    if where == "info/exclude":
        with open(repo / ".git" / "info" / "exclude", "a") as exclude:
            exclude.write(f"{rule}\n")
    else:
        (home / ".gitignore").write_text(f"{rule}\n")
        (home / "gitconfig").write_text(f"[core]\n\texcludesFile = {(home / '.gitignore').as_posix()}\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))


@RESOLVERS
@pytest.mark.parametrize("where", ["info/exclude", "global excludes"])
def test_only_a_gitignore_holds_back_a_tracked_file_never_this_machines_rules(
    git_repo: Path, caplog, resolve, where: str, monkeypatch: pytest.MonkeyPatch, tmp_path_factory
):
    _machine_rule(git_repo, "*.c", where, tmp_path_factory.mktemp("home"), monkeypatch)

    with caplog.at_level(logging.WARNING, logger="aristotle"):
        result = resolve(git_repo)
        scope.warn_held_back(git_repo, result)

    assert Path("rtl/driver.c") in result.upload_files
    assert not [message for message in caplog.messages if message.startswith("Not uploading")]


@RESOLVERS
@pytest.mark.parametrize("where", ["info/exclude", "global excludes"])
def test_a_machine_rule_on_a_folder_hides_no_gitignore_rule_inside_it(
    git_repo: Path, caplog, resolve, where: str, monkeypatch: pytest.MonkeyPatch, tmp_path_factory
):
    # git names the folder rule as the one deciding, though the repository's .gitignore matches the file too.
    (git_repo / "rtl" / "debug.log").write_text("x\n")
    git(git_repo, "add", "-f", "rtl/debug.log")
    (git_repo / ".gitignore").write_text("*.log\n")
    _machine_rule(git_repo, "rtl/", where, tmp_path_factory.mktemp("home"), monkeypatch)

    with caplog.at_level(logging.WARNING, logger="aristotle"):
        result = resolve(git_repo)
        scope.warn_held_back(git_repo, result)

    assert Path("rtl/debug.log") not in result.upload_files
    assert local_file_utils.held_back_warning({Path("rtl/debug.log"): ".gitignore:1: *.log"}) in caplog.messages


@RESOLVERS
def test_a_tracked_lean_build_file_an_ignore_rule_matches_is_uploaded_at_any_depth(git_repo: Path, caplog, resolve):
    for name in ("lake-manifest.json", "lean-toolchain", "pkg/lakefile.lean", "pkg/lakefile.toml"):
        (git_repo / name).parent.mkdir(parents=True, exist_ok=True)
        (git_repo / name).write_text("x\n")
    (git_repo / ".gitignore").write_text("*.json\nlean-toolchain\npkg/\n")
    git(git_repo, "add", "-f", "-A")
    git(git_repo, "commit", "-qm", "lean build files under ignore rules")

    with caplog.at_level(logging.WARNING, logger="aristotle"):
        result = resolve(git_repo)
        scope.warn_held_back(git_repo, result)

    for name in ("lake-manifest.json", "lean-toolchain", "pkg/lakefile.lean", "pkg/lakefile.toml"):
        assert Path(name) in result.upload_files
    assert "Not uploading" not in caplog.text


def test_every_tracked_file_held_back_is_named(git_repo: Path, caplog):
    names = [f"gen/out{i}.v" for i in range(7)]
    for name in names:
        (git_repo / name).parent.mkdir(exist_ok=True)
        (git_repo / name).write_text("module out;\nendmodule\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "generated")
    (git_repo / ".gitignore").write_text("gen/\n")

    with caplog.at_level(logging.WARNING, logger="aristotle"):
        scope.warn_held_back(git_repo, scope.resolve_checkout(git_repo))

    assert local_file_utils.held_back_warning(dict.fromkeys(map(Path, names), ".gitignore:1: gen/")) in caplog.messages


@pytest.mark.parametrize(
    ("ignored", "resolve", "error"),
    [
        ("rtl/\n", lambda root: scope.resolve(root, scope.Scope.PATHS, ["rtl/**"]), "matches --path"),
        ("*\n", scope.resolve_checkout, "No tracked files found"),
    ],
    ids=["--path on an ignored folder", "everything ignored"],
)
def test_an_error_after_listing_the_files_still_names_those_an_ignore_rule_held_back(
    git_repo: Path, caplog, ignored: str, resolve, error: str
):
    (git_repo / ".gitignore").write_text(ignored)

    with caplog.at_level(logging.WARNING, logger="aristotle"), pytest.raises(scope.ScopeError, match=error):
        resolve(git_repo)

    assert any(m.startswith("Not uploading") and "rtl/fifo.v" in m for m in caplog.messages)


def test_scope_changed_uses_the_diff_against_the_parent_commit(git_repo: Path):
    (git_repo / "rtl" / "alu.v").write_text("module alu;\nendmodule\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "add alu")

    result = scope.resolve(git_repo, scope.Scope.CHANGED)

    assert result.focus_files == [Path("rtl/alu.v")]
    # The whole repository is still uploaded, because verification needs context.
    assert Path("rtl/fifo.v") in result.upload_files


def test_scope_changed_counts_a_lean_change(git_repo: Path):
    (git_repo / "Main.lean").write_text("theorem t : 1 + 1 = 2 := by decide\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "add a theorem")

    result = scope.resolve(git_repo, scope.Scope.CHANGED)

    assert result.focus_files == [Path("Main.lean")]


def _lean_package(repo: Path, folder: str) -> None:
    """Commit a Lake package in ``folder``: its build files and two Lean files."""
    package = repo / folder
    (package / "Proofs").mkdir(parents=True)
    (package / "lean-toolchain").write_text("leanprover/lean4:v4.21.0\n")
    (package / "lakefile.lean").write_text("import Lake\nopen Lake DSL\npackage proofs\n")
    (package / "Proofs.lean").write_text("import Proofs.Basic\n")
    (package / "Proofs" / "Basic.lean").write_text("theorem two : 1 + 1 = 2 := rfl\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"add the Lean package in {folder}")


@pytest.mark.parametrize("build_file", ["lean-toolchain", "lakefile.lean"])
def test_a_change_to_a_lean_build_file_brings_in_every_lean_file_of_its_package(
    git_repo: Path, caplog, build_file: str
):
    _lean_package(git_repo, "lean")
    _lean_package(git_repo, "other")
    (git_repo / "lean" / build_file).write_text("leanprover/lean4:v4.22.0\n")
    git(git_repo, "commit", "-qam", f"change {build_file}")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        result = scope.resolve(git_repo, scope.Scope.CHANGED)

    assert result.focus_files == [Path("lean/Proofs/Basic.lean"), Path("lean/Proofs.lean")]
    assert f"lean/{build_file} changed, so the Lean files in its folder and below are verified too." in caplog.messages


def test_a_lean_build_file_at_the_top_brings_in_lean_files_at_any_depth(git_repo: Path):
    _lean_package(git_repo, ".")
    (git_repo / "lake-manifest.json").write_text('{"version": "1.1.0", "packages": []}\n')
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "pin the dependencies")

    result = scope.resolve(git_repo, scope.Scope.CHANGED)

    assert result.focus_files == [Path("Proofs/Basic.lean"), Path("Proofs.lean")]


def test_deleting_a_lean_build_file_brings_in_the_lean_files_it_built(git_repo: Path):
    _lean_package(git_repo, "lean")
    git(git_repo, "rm", "-q", "lean/lean-toolchain")
    git(git_repo, "commit", "-qm", "drop the toolchain pin")

    result = scope.resolve(git_repo, scope.Scope.CHANGED)

    assert result.focus_files == [Path("lean/Proofs/Basic.lean"), Path("lean/Proofs.lean")]


def test_moving_a_lean_build_file_brings_in_the_lean_files_of_both_folders(git_repo: Path):
    _lean_package(git_repo, "a")
    _lean_package(git_repo, "b")
    git(git_repo, "rm", "-q", "b/lean-toolchain")
    git(git_repo, "commit", "-qm", "b without a toolchain")
    git(git_repo, "mv", "a/lean-toolchain", "b/lean-toolchain")
    git(git_repo, "commit", "-qm", "move the toolchain")

    result = scope.resolve(git_repo, scope.Scope.CHANGED)

    assert result.focus_files == [
        Path("a/Proofs/Basic.lean"),
        Path("a/Proofs.lean"),
        Path("b/Proofs/Basic.lean"),
        Path("b/Proofs.lean"),
    ]


def test_a_lean_build_file_above_the_directory_brings_in_its_lean_files(git_repo: Path, caplog):
    _lean_package(git_repo, ".")
    _lean_package(git_repo, "other")
    (git_repo / "lean-toolchain").write_text("leanprover/lean4:v4.22.0\n")
    git(git_repo, "commit", "-qam", "bump Lean at the top")
    git(git_repo, "config", "diff.relative", "true")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        result = scope.resolve(git_repo / "Proofs", scope.Scope.CHANGED)

    assert result.focus_files == [Path("Basic.lean")]
    assert "../lean-toolchain changed, so the Lean files in its folder and below are verified too." in caplog.messages


def test_deleting_a_lean_build_file_above_the_directory_brings_in_its_lean_files(git_repo: Path):
    _lean_package(git_repo, ".")
    git(git_repo, "rm", "-q", "lakefile.lean")
    git(git_repo, "commit", "-qm", "drop the lakefile")

    result = scope.resolve(git_repo / "Proofs", scope.Scope.CHANGED)

    assert result.focus_files == [Path("Basic.lean")]


def test_a_lean_build_file_beside_the_directory_brings_in_nothing(git_repo: Path):
    _lean_package(git_repo, "lean")
    _lean_package(git_repo, "other")
    (git_repo / "other" / "lean-toolchain").write_text("leanprover/lean4:v4.22.0\n")
    git(git_repo, "commit", "-qam", "bump Lean in the other package")

    result = scope.resolve(git_repo / "lean", scope.Scope.CHANGED)

    assert result.is_empty


def test_a_folder_above_the_directory_is_named_literally_not_as_a_glob(git_repo: Path):
    _lean_package(git_repo, "x[ab]")
    _lean_package(git_repo, "xa")
    (git_repo / "xa" / "lean-toolchain").write_text("leanprover/lean4:v4.22.0\n")
    git(git_repo, "commit", "-qam", "bump Lean in xa")

    result = scope.resolve(git_repo / "x[ab]" / "Proofs", scope.Scope.CHANGED)

    assert result.is_empty


def test_a_renamed_source_file_is_in_focus_under_its_new_name(git_repo: Path):
    git(git_repo, "mv", "rtl/fifo.v", "rtl/queue.v")
    git(git_repo, "commit", "-qm", "rename")

    result = scope.resolve(git_repo, scope.Scope.CHANGED)

    assert result.focus_files == [Path("rtl/queue.v")]


def test_a_path_filter_narrows_the_lean_files_a_build_file_brings_in(git_repo: Path, caplog):
    _lean_package(git_repo, "lean")
    (git_repo / "lean" / "lean-toolchain").write_text("leanprover/lean4:v4.22.0\n")
    git(git_repo, "commit", "-qam", "bump Lean")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        narrowed = scope.resolve(git_repo, scope.Scope.CHANGED, paths=["lean/Proofs/**"])
        elsewhere = scope.resolve(git_repo, scope.Scope.CHANGED, paths=["rtl/**"])

    assert narrowed.focus_files == [Path("lean/Proofs/Basic.lean")]
    assert elsewhere.is_empty
    assert (
        caplog.messages.count(
            "lean/lean-toolchain changed, so the Lean files in its folder and below are verified too."
        )
        == 1
    )


def test_scope_changed_ignores_non_source_changes(git_repo: Path):
    (git_repo / "README.md").write_text("updated docs\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "docs only")

    result = scope.resolve(git_repo, scope.Scope.CHANGED)

    assert result.is_empty


def test_scope_changed_excludes_deleted_files(git_repo: Path):
    git(git_repo, "rm", "-q", "rtl/fifo.v")
    git(git_repo, "commit", "-qm", "remove fifo")

    result = scope.resolve(git_repo, scope.Scope.CHANGED)

    assert Path("rtl/fifo.v") not in result.focus_files


def test_scope_changed_against_a_base_ref(git_repo: Path):
    git(git_repo, "branch", "main")
    git(git_repo, "checkout", "-q", "-b", "feature")
    (git_repo / "rtl" / "alu.v").write_text("module alu;\nendmodule\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "first")
    (git_repo / "rtl" / "mul.v").write_text("module mul;\nendmodule\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "second")

    result = scope.resolve(git_repo, scope.Scope.CHANGED, base_ref="main")

    # Both commits on the branch are in scope, not just the most recent one.
    assert result.focus_files == [Path("rtl/alu.v"), Path("rtl/mul.v")]


def _runner_checkout(remote: Path, clone: Path, branch: str, depth: int | None) -> None:
    """Check out ``branch`` the way a GitLab runner does: fetch only the ref being built, detached."""
    clone.mkdir()
    git(clone, "init", "-q")
    git(clone, "remote", "add", "origin", remote.as_uri())
    depth_args = [f"--depth={depth}"] if depth else []
    git(clone, "fetch", "-q", "--no-tags", *depth_args, "origin", f"+refs/heads/{branch}:refs/pipelines/1")
    git(clone, "checkout", "-q", "refs/pipelines/1")


def _merge_request(remote: Path, fork_back: int = 0, feature_commits: int = 0) -> None:
    """Branch ``feature`` off ``main``, change two sources, then end on a docs-only commit; ``main`` moves on too."""
    git(remote, "branch", "-M", "main")
    for i in range(fork_back):
        git(remote, "commit", "-q", "--allow-empty", "-m", f"main {i}")
    git(remote, "checkout", "-q", "-b", "feature", f"HEAD~{fork_back}" if fork_back else "HEAD")
    (remote / "rtl" / "alu.v").write_text("module alu;\nendmodule\n")
    git(remote, "add", "-A")
    git(remote, "commit", "-qm", "add alu")
    (remote / "rtl" / "mul.v").write_text("module mul;\nendmodule\n")
    git(remote, "add", "-A")
    git(remote, "commit", "-qm", "add mul")
    for i in range(feature_commits):
        git(remote, "commit", "-q", "--allow-empty", "-m", f"feature {i}")
    (remote / "README.md").write_text("updated docs\n")
    git(remote, "add", "-A")
    git(remote, "commit", "-qm", "docs only")
    git(remote, "checkout", "-q", "main")
    git(remote, "commit", "-q", "--allow-empty", "-m", "main moves on")


@pytest.mark.parametrize("depth", [1, None], ids=["shallow", "GIT_DEPTH=0"])
def test_scope_changed_fetches_a_base_the_runner_did_not(git_repo: Path, tmp_path: Path, depth: int | None):
    _merge_request(git_repo)
    clone = tmp_path / "clone"
    _runner_checkout(git_repo, clone, "feature", depth)

    result = scope.resolve(clone, scope.Scope.CHANGED, base_ref="origin/main")

    # The last commit touches no source; the whole merge request is still in scope.
    assert result.focus_files == [Path("rtl/alu.v"), Path("rtl/mul.v")]


def test_scope_changed_accepts_a_bare_branch_name_the_checkout_lacks(git_repo: Path, tmp_path: Path):
    _merge_request(git_repo)
    clone = tmp_path / "clone"
    _runner_checkout(git_repo, clone, "feature", 1)

    result = scope.resolve(clone, scope.Scope.CHANGED, base_ref="main")

    assert result.focus_files == [Path("rtl/alu.v"), Path("rtl/mul.v")]


def test_scope_changed_deepens_a_shallow_clone_until_the_merge_base_is_reached(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(scope, "BASE_FETCH_DEPTH", 2)
    _merge_request(git_repo, fork_back=10, feature_commits=10)
    clone = tmp_path / "clone"
    _runner_checkout(git_repo, clone, "feature", 2)

    result = scope.resolve(clone, scope.Scope.CHANGED, base_ref="origin/main")

    assert result.focus_files == [Path("rtl/alu.v"), Path("rtl/mul.v")]


def test_scope_changed_unshallows_when_deepening_is_not_enough(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(scope, "BASE_FETCH_DEPTH", 1)
    monkeypatch.setattr(scope, "BASE_FETCH_DEEPEN_ATTEMPTS", 0)
    _merge_request(git_repo, fork_back=5, feature_commits=5)
    clone = tmp_path / "clone"
    _runner_checkout(git_repo, clone, "feature", 1)

    result = scope.resolve(clone, scope.Scope.CHANGED, base_ref="origin/main")

    assert result.focus_files == [Path("rtl/alu.v"), Path("rtl/mul.v")]


def test_scope_changed_errors_instead_of_guessing_when_the_base_cannot_be_fetched(git_repo: Path, tmp_path: Path):
    _merge_request(git_repo)
    clone = tmp_path / "clone"
    _runner_checkout(git_repo, clone, "feature", 1)

    # Diffing against HEAD~1 would see only the docs commit and report nothing to do. A deeper clone wouldn't help
    # either, so the error names the branch rather than the clone's depth.
    with pytest.raises(scope.ScopeError) as raised:
        scope.resolve(clone, scope.Scope.CHANGED, base_ref="origin/nonexistent")
    assert str(raised.value) == (
        "origin has no branch nonexistent to compare against. Check the name given with --base-ref or "
        "ARISTOTLE_BASE_REF, or use --scope all or --scope paths."
    )


def test_scope_changed_without_an_origin_gives_gits_error_not_the_depth_hint(
    git_repo: Path, caplog, monkeypatch: pytest.MonkeyPatch
):
    """git's "Please make sure you have the correct access rights" lines, and the fetch-depth hint, don't apply."""
    # Under GitHub Actions the hint would say fetch-depth, so its absence shows.
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with caplog.at_level(logging.INFO, logger="aristotle"), pytest.raises(scope.ScopeError) as raised:
        scope.resolve(git_repo, scope.Scope.CHANGED, base_ref="origin/main")
    message = str(raised.value)
    assert message.startswith("Cannot determine which files changed: main could not be fetched from origin: fatal: ")
    assert message.endswith("Or use --scope all or --scope paths.")
    assert "access rights" not in message and "fetch-depth" not in message
    assert "\n" not in message
    # Nor in the log, where main put git's four lines as a warning.
    assert "access rights" not in caplog.text


def _two_commit_push(repo: Path) -> str:
    """Push two commits to ``main``, the last touching only docs; return the commit before the push."""
    git(repo, "branch", "-M", "main")
    before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "rtl" / "alu.v").write_text("module alu;\nendmodule\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add alu")
    (repo / "README.md").write_text("updated docs\n")
    git(repo, "commit", "-qam", "docs only")
    return before


def test_scope_changed_covers_every_commit_of_a_push(git_repo: Path, caplog):
    before = _two_commit_push(git_repo)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        result = scope.resolve(git_repo, scope.Scope.CHANGED, push_before=before)

    # HEAD~1 would see only the docs commit.
    assert result.focus_files == [Path("rtl/alu.v")]
    assert f"{before[:12]}, the commit before this push" in caplog.text


def test_scope_changed_fetches_the_commit_before_a_push_into_a_shallow_clone(git_repo: Path, tmp_path: Path):
    before = _two_commit_push(git_repo)
    clone = tmp_path / "clone"
    _runner_checkout(git_repo, clone, "main", 1)

    result = scope.resolve(clone, scope.Scope.CHANGED, push_before=before)

    assert result.focus_files == [Path("rtl/alu.v")]


def test_scope_changed_errors_instead_of_guessing_when_the_push_base_cannot_be_fetched(git_repo: Path, caplog):
    _two_commit_push(git_repo)

    with caplog.at_level(logging.INFO, logger="aristotle"), pytest.raises(scope.ScopeError) as error:
        scope.resolve(git_repo, scope.Scope.CHANGED, push_before="ab" * 20)

    assert str(error.value).startswith(
        "Cannot determine which files changed: abababababab, the commit before this push, is not in the checkout "
        "and could not be fetched from origin."
    )
    assert str(error.value).endswith("Or set --base-ref to compare a branch, or use --scope all or --scope paths.")
    assert "Comparing against the previous commit" not in caplog.text


def test_a_base_branch_wins_over_the_push_base(git_repo: Path):
    before = _two_commit_push(git_repo)
    git(git_repo, "checkout", "-q", "-b", "feature")
    (git_repo / "rtl" / "mul.v").write_text("module mul;\nendmodule\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "add mul")

    result = scope.resolve(git_repo, scope.Scope.CHANGED, base_ref="main", push_before=before)

    assert result.focus_files == [Path("rtl/mul.v")]


def test_scope_changed_errors_on_a_missing_base_without_a_remote(git_repo: Path):
    (git_repo / "rtl" / "alu.v").write_text("module alu;\nendmodule\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "add alu")

    with pytest.raises(scope.ScopeError, match="Cannot determine which files changed"):
        scope.resolve(git_repo, scope.Scope.CHANGED, base_ref="origin/nonexistent")


def test_scope_changed_errors_on_an_initial_commit_with_no_parent(git_repo: Path):
    with pytest.raises(scope.ScopeError, match="no base branch and no parent commit"):
        scope.resolve(git_repo, scope.Scope.CHANGED)


def test_scope_paths_matches_globs(git_repo: Path):
    result = scope.resolve(git_repo, scope.Scope.PATHS, paths=["rtl/*.v"])

    assert result.focus_files == [Path("rtl/fifo.v")]


def test_scope_paths_treats_a_bare_prefix_as_a_directory(git_repo: Path):
    result = scope.resolve(git_repo, scope.Scope.PATHS, paths=["rtl"])

    assert result.focus_files == [Path("rtl/driver.c"), Path("rtl/fifo.v")]


def test_scope_paths_that_match_nothing_is_an_error_naming_the_patterns(git_repo: Path):
    with pytest.raises(scope.ScopeError, match="'rlt/\\*\\*'"):
        scope.resolve(git_repo, scope.Scope.PATHS, paths=["rlt/**"])


def test_scope_all_without_a_source_file_is_an_error(git_repo: Path):
    git(git_repo, "rm", "-q", "rtl/fifo.v", "rtl/driver.c")
    git(git_repo, "commit", "-qm", "docs only")

    with pytest.raises(scope.ScopeError, match="no tracked source files"):
        scope.resolve(git_repo, scope.Scope.ALL)


def test_scope_changed_that_touches_no_source_file_is_empty_not_an_error(git_repo: Path):
    (git_repo / "README.md").write_text("updated docs\n")
    git(git_repo, "commit", "-qam", "docs")

    assert scope.resolve(git_repo, scope.Scope.CHANGED, paths=["rtl/**"]).is_empty


def _held_back_change(repo: Path) -> None:
    """Commit gen/regs.v and gen/notes.txt with a rule that ignores gen/, so that both are held back."""
    (repo / "gen").mkdir()
    (repo / "gen" / "regs.v").write_text("module regs;\nendmodule\n")
    (repo / "gen" / "notes.txt").write_text("generated\n")
    (repo / ".gitignore").write_text("gen/\n")
    git(repo, "add", "-f", "-A")
    git(repo, "commit", "-qm", "generated files, committed though ignored")


@pytest.mark.parametrize(("paths", "held_back"), [(None, [Path("gen/regs.v")]), (["rtl"], [])])
def test_scope_changed_names_the_changed_source_files_an_ignore_rule_holds_back(
    git_repo: Path, paths: list[str] | None, held_back: list[Path]
):
    _held_back_change(git_repo)

    result = scope.resolve(git_repo, scope.Scope.CHANGED, paths=paths)

    assert result.is_empty
    assert result.held_back_focus == held_back


def test_a_lean_build_file_change_puts_the_lean_files_an_ignore_rule_holds_back_in_scope(git_repo: Path):
    _lean_package(git_repo, "proofs")
    (git_repo / ".gitignore").write_text("Basic.lean\n")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-qm", "ignore one of the package's Lean files")
    (git_repo / "proofs" / "lean-toolchain").write_text("leanprover/lean4:v4.22.0\n")
    git(git_repo, "commit", "-qam", "bump Lean")

    result = scope.resolve(git_repo, scope.Scope.CHANGED)

    assert result.focus_files == [Path("proofs/Proofs.lean")]
    assert result.held_back_focus == [Path("proofs/Proofs/Basic.lean")]


def test_scope_paths_names_the_source_files_it_matches_that_an_ignore_rule_holds_back(git_repo: Path):
    _held_back_change(git_repo)

    result = scope.resolve(git_repo, scope.Scope.PATHS, paths=["**/*.v"])

    assert result.focus_files == [Path("rtl/fifo.v")]
    assert result.held_back_focus == [Path("gen/regs.v")]


def test_scope_changed_with_a_glob_that_matches_nothing_is_an_error(git_repo: Path):
    (git_repo / "rtl" / "fifo.v").write_text("module fifo_v2;\nendmodule\n")
    git(git_repo, "commit", "-qam", "change the fifo")

    with pytest.raises(scope.ScopeError, match="'rtlx/\\*\\*'"):
        scope.resolve(git_repo, scope.Scope.CHANGED, paths=["rtlx/**"])


@pytest.mark.parametrize("pattern", ["rtl/**/*.v", "**/*.v", "**/fifo.v"])
def test_a_double_star_also_matches_no_directories(git_repo: Path, pattern: str):
    result = scope.resolve(git_repo, scope.Scope.PATHS, paths=[pattern])

    assert Path("rtl/fifo.v") in result.focus_files


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("**/rtl/**/*.sv", "blocks/uart/rtl/uart.sv", True),
        ("**/rtl/**/*.sv", "rtl/top.sv", True),
        ("**/core/**/*.sv", "blk/core/x.sv", True),
        ("rtl/**/**/*.sv", "rtl/x.sv", True),
        ("rtl/**/*.sv", "rtl/a/b/c.sv", True),
        ("rtl/**/*.sv", "rtlx/top.sv", False),
        ("rtl/*.sv", "rtl/sub/a.sv", True),
        ("rtl/[!t]*.sv", "rtl/top.sv", False),
        ("rtl/[!t]*.sv", "rtl/alu.sv", True),
        ("rtl/[^]x.sv", "rtl/^x.sv", True),
        ("rtl/a+b.sv", "rtl/a+b.sv", True),
    ],
)
def test_path_globs(pattern: str, path: str, matches: bool):
    assert scope._matching_files([Path(path)], [pattern]) == ([Path(path)] if matches else [])


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """``pattern`` as a regex: "**/" as "(?:.*/)?", "*" as ".*", "?" as ".", and a class as fnmatch reads one."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        char = pattern[i]
        close = -1
        if char == "[":
            start = i + 1 + (pattern[i + 1 : i + 2] == "!")
            close = pattern.find("]", start + (pattern[start : start + 1] == "]"))
        if char == "*":
            out.append(".*")
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
    return re.compile("".join(out) + r"\Z", re.DOTALL)


#: Pieces of generated --path globs, with what a path may hold in each one's place.
GLOB_PIECES = {
    "a": ["a"],
    "b": ["b"],
    "/": ["/"],
    ".": ["."],
    "\n": ["\n"],
    "\\": ["\\"],
    "*": ["", "a", "b/", "/\n"],
    "**/": ["", "/", "a/", "a/b/"],
    "?": ["a", "/", "\n"],
    "[ab]": ["a", "c"],
    "[!a]": ["a", "b", "\n"],
    "[^]": ["^", "a"],
    "[a-b]": ["b", "c"],
    "[]a]": ["]", "b"],
    "[!]]": ["]", "a"],
    "[b-a]": ["a"],
    "[": ["["],
    "]": ["]"],
}


@pytest.mark.filterwarnings("ignore::FutureWarning")
def test_a_glob_matches_the_paths_its_regex_matches():
    """On generated globs, each against a path filled in from it, then changed at one place half the time; a glob whose
    regex doesn't compile is a ScopeError."""
    rng = random.Random(0)
    differ = []
    for _ in range(20000):
        pieces = rng.choices(list(GLOB_PIECES), k=rng.randint(0, 6))
        glob = "".join(pieces)
        path = "".join(rng.choice(GLOB_PIECES[piece]) for piece in pieces)
        if rng.random() < 0.5:
            i = rng.randint(0, len(path))
            path = path[:i] + rng.choice(["", "a", "/"]) + path[i + 1 :]
        try:
            regex = _glob_regex(glob)
        except re.error as e:
            with pytest.raises(scope.ScopeError) as caught:
                scope._glob(glob)
            assert str(caught.value) == f"--path {glob!r} is not a valid glob: {e}"
            continue
        if wildcard.matches(scope._glob(glob), path) != bool(regex.match(path)):
            differ.append((glob, path))
    assert differ == []


def test_globs_that_re_warns_about_alike_warn_once():
    re.purge()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("default")
        scope._matching_files([Path("one/x.sv")], ["one/[[]x.sv", "two/[[]x.sv"])
    assert [str(w.message) for w in caught] == ["Possible nested set at position 5"]


@pytest.mark.parametrize(
    ("pattern", "path"),
    [("*a" * 30 + "b", "a" * 1000), ("**/a" * 30 + "b", "a/" * 499 + "a")],
    ids=["*", "**/"],
)
def test_a_glob_of_many_stars_matches_in_time_linear_in_the_path(pattern: str, path: str):
    started = time.perf_counter()
    assert scope._matching_files([Path(path)], [pattern]) == []
    assert time.perf_counter() - started < 1


def test_scope_changed_from_a_subdirectory_of_the_repository(git_repo: Path):
    (git_repo / "rtl" / "fifo.v").write_text("module fifo_v2;\nendmodule\n")
    git(git_repo, "commit", "-qam", "change the fifo")

    result = scope.resolve(git_repo / "rtl", scope.Scope.CHANGED)

    assert result.focus_files == [Path("fifo.v")]
    assert result.prefix == "rtl/"
    assert scope.resolve(git_repo, scope.Scope.ALL).prefix == ""


@pytest.mark.parametrize("scope_name", [scope.Scope.PATHS, scope.Scope.CHANGED])
def test_one_mistyped_pattern_among_several_is_an_error_naming_it(git_repo: Path, scope_name: scope.Scope):
    with pytest.raises(scope.ScopeError, match="--path 'rtlx/core'\\."):
        scope.resolve(git_repo, scope_name, paths=["rtl/**", "rtlx/core"])


def test_a_malformed_glob_is_a_scope_error_naming_it(git_repo: Path):
    with pytest.raises(scope.ScopeError, match="--path 'rtl/\\[z-a\\].sv' is not a valid glob"):
        scope.resolve(git_repo, scope.Scope.PATHS, paths=["rtl/[z-a].sv"])


def test_scope_all_rejects_a_path_rather_than_ignoring_it(git_repo: Path):
    with pytest.raises(scope.ScopeError, match="--scope all verifies everything"):
        scope.resolve(git_repo, scope.Scope.ALL, paths=["rtl/**"])


def test_scope_paths_requires_a_pattern(git_repo: Path):
    with pytest.raises(scope.ScopeError, match="requires at least one --path"):
        scope.resolve(git_repo, scope.Scope.PATHS)


@RESOLVERS
def test_scope_rejects_a_directory_that_is_not_a_repository(tmp_path: Path, resolve):
    with pytest.raises(scope.ScopeError, match="not a git repository"):
        resolve(tmp_path)


@RESOLVERS
@pytest.mark.parametrize("name", ["typo", "README.md"])
def test_scope_says_a_path_in_a_checkout_that_is_not_a_folder_is_not_a_directory(git_repo: Path, resolve, name: str):
    # Not that git refused the repository, which git's failure to change into it inside a checkout would read as.
    with pytest.raises(scope.ScopeError, match=r"is not a directory\.$"):
        resolve(git_repo / name)


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permissions, as a non-root user")
@RESOLVERS
@pytest.mark.parametrize("mode", [0o000, 0o600], ids=["none", "not enterable"])
def test_scope_says_a_folder_it_cannot_read_cannot_be_read(git_repo: Path, resolve, mode: int):
    locked = git_repo / "rtl"
    locked.chmod(mode)
    try:
        with pytest.raises(scope.ScopeError, match=r"can't be read\.$"):
            resolve(locked)
    finally:
        locked.chmod(0o755)


@RESOLVERS
def test_scope_says_git_is_not_installed_rather_than_that_there_is_no_repository(
    git_repo: Path, resolve, monkeypatch: pytest.MonkeyPatch
):
    without_git(monkeypatch, git_repo)

    with pytest.raises(scope.ScopeError, match="^git is not installed"):
        resolve(git_repo)


@RESOLVERS
def test_scope_says_git_is_not_installed_for_a_folder_without_a_git_folder(
    tmp_path: Path, resolve, monkeypatch: pytest.MonkeyPatch
):
    """As a checkout made without git leaves it."""
    monkeypatch.setenv("PATH", str(tmp_path / "no-git-here"))

    with pytest.raises(scope.ScopeError, match="^git is not installed"):
        resolve(tmp_path)


@RESOLVERS
def test_scope_passes_on_why_git_refuses_another_users_repository(
    git_repo: Path, resolve, monkeypatch: pytest.MonkeyPatch
):
    refusing_git(monkeypatch, git_repo)

    with pytest.raises(scope.ScopeError) as e:
        resolve(git_repo)

    assert str(e.value).startswith(f"git refused {git_repo}: fatal: ")
    assert "git config --global --add safe.directory " in str(e.value)


@RESOLVERS
def test_scope_rejects_a_repository_with_no_tracked_files(tmp_path: Path, resolve):
    git(tmp_path, "init", "-q")
    (tmp_path / "untracked.v").write_text("module untracked;\nendmodule\n")

    with pytest.raises(scope.ScopeError, match="No tracked files found"):
        resolve(tmp_path)


def test_a_checkout_uploads_every_tracked_file_with_every_source_file_in_focus(git_repo: Path):
    result = scope.resolve_checkout(git_repo)

    assert result.upload_files == [Path(".env"), Path("README.md"), Path("rtl/driver.c"), Path("rtl/fifo.v")]
    assert result.focus_files == [Path("rtl/driver.c"), Path("rtl/fifo.v")]
    assert result.prefix == ""


def test_a_checkout_without_a_source_file_is_not_an_error(git_repo: Path):
    git(git_repo, "rm", "-q", "rtl/fifo.v", "rtl/driver.c")
    git(git_repo, "commit", "-qm", "docs only")

    result = scope.resolve_checkout(git_repo)

    assert result.focus_files == []
    assert result.upload_files == [Path(".env"), Path("README.md")]


@pytest.mark.parametrize("verilog_tracked", [True, False], ids=["verilog", "c only"])
def test_a_checkout_collects_filelists_only_when_verilog_is_tracked(git_repo: Path, verilog_tracked: bool):
    (git_repo / "aristotle.f").write_text("rtl/fifo.v\n")
    git(git_repo, "add", "aristotle.f")
    if not verilog_tracked:
        git(git_repo, "rm", "-q", "rtl/fifo.v")
    git(git_repo, "commit", "-qm", "add filelist")

    assert scope.resolve_checkout(git_repo).filelists == ([Path("aristotle.f")] if verilog_tracked else [])


def test_a_checkout_from_a_subdirectory_is_relative_to_it(git_repo: Path):
    result = scope.resolve_checkout(git_repo / "rtl")

    assert result.upload_files == result.focus_files == [Path("driver.c"), Path("fifo.v")]
    assert result.prefix == "rtl/"


@pytest.mark.parametrize(
    "resolve",
    [
        lambda root: scope.resolve(root, scope.Scope.ALL),
        lambda root: scope.resolve(root, scope.Scope.CHANGED),
        scope.resolve_checkout,
    ],
    ids=["all", "changed", "checkout"],
)
def test_a_folder_shaped_like_a_bare_repository_is_listed_from_the_checkouts_repository(git_repo: Path, resolve):
    hw = commit_bare_shaped(git_repo)

    result = resolve(hw)

    # Sorted as the result is: Windows paths compare ignoring case.
    assert result.upload_files == sorted(map(Path, ["HEAD", "config", "objects/.keep", "refs/.keep", "top.v"]))
    assert result.focus_files == [Path("top.v")]
    assert result.prefix == "hw/"


def test_every_git_command_runs_from_the_checkouts_top_with_both_settings(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hw = commit_bare_shaped(git_repo)
    (git_repo / ".gitignore").write_text("*.log\n")
    (hw / "kept.log").write_text("tracked, then ignored\n")
    git(git_repo, "add", "-f", ".gitignore", "hw/kept.log")
    git(git_repo, "commit", "-qm", "a tracked file an ignore rule matches")
    branch = subprocess.run(
        ["git", "-C", str(git_repo), "symbolic-ref", "--short", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    git(tmp_path, "clone", "-q", "--bare", str(git_repo), "origin.git")
    git(git_repo, "remote", "add", "origin", str(tmp_path / "origin.git"))
    head = scope.head_commit(git_repo)
    calls = recording_git(monkeypatch)

    scope.resolve(hw, scope.Scope.CHANGED, base_ref=f"origin/{branch}")
    scope.resolve(hw, scope.Scope.CHANGED, push_before=head)
    with pytest.raises(scope.ScopeError, match="origin has no branch"):
        scope.resolve(hw, scope.Scope.CHANGED, base_ref="origin/no-such-branch")
    scope.warn_held_back(hw, scope.resolve_checkout(hw))
    naming.default_name(hw, context.CIContext(context.CISystem.LOCAL, hw), "hw/", "ci")
    local_file_utils.gitignored_files(hw, [Path("kept.log"), Path("top.v")])
    local_file_utils.left_out_rules(hw, [Path("kept.log")])

    expected = ["git", "-C", str(git_repo.resolve()), "-c", "safe.bareRepository=explicit", "-c", "core.fsmonitor="]
    assert [argv for argv in calls if argv[: len(expected)] != expected] == []
    words = {word for argv in calls for word in argv}
    assert {"ls-files", "diff", "merge-base", "fetch", "cat-file", "ls-remote", "check-ignore", "status"} <= words


def test_outside_a_repository_no_git_command_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "top.v").write_text("module top;\nendmodule\n")
    calls = recording_git(monkeypatch)

    with pytest.raises(scope.ScopeError, match="is not a git repository"):
        scope.resolve(tmp_path, scope.Scope.ALL)
    assert (scope.top_level(tmp_path), scope.head_commit(tmp_path)) == (None, None)
    assert local_file_utils.gitignored_files(tmp_path, [Path("top.v")]) == set()
    assert naming.default_name(tmp_path, context.CIContext(context.CISystem.LOCAL, tmp_path), "", "ci") == (
        f"{tmp_path.name}: ci"
    )
    assert calls == []


def test_a_linked_worktrees_folder_is_listed_from_the_worktree(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    git(git_repo, "worktree", "add", "-q", str(tmp_path / "wt"))
    calls = recording_git(monkeypatch)

    result = scope.resolve_checkout(tmp_path / "wt" / "rtl")

    assert result.upload_files == [Path("driver.c"), Path("fifo.v")]
    assert result.prefix == "rtl/"
    assert {argv[2] for argv in calls} == {str((tmp_path / "wt").resolve())}


def test_a_submodules_folder_is_listed_from_the_submodule(
    git_repo: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
):
    library = tmp_path_factory.mktemp("library")
    git(library, "init", "-q")
    (library / "rtl").mkdir()
    (library / "rtl" / "alu.v").write_text("module alu;\nendmodule\n")
    git(library, "add", "-A")
    git(library, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "alu")
    git(git_repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(library), "lib")
    git(git_repo, "commit", "-qm", "lib")
    calls = recording_git(monkeypatch)

    result = scope.resolve_checkout(git_repo / "lib" / "rtl")

    assert result.upload_files == [Path("alu.v")]
    assert result.prefix == "rtl/"
    assert {argv[2] for argv in calls} == {str((git_repo / "lib").resolve())}


def test_a_git_folder_git_passes_over_leaves_the_prefix_and_build_files_as_git_sees_them(git_repo: Path):
    """An empty .git, which git looks past to the repository above it."""
    (git_repo / "lakefile.lean").write_text("-- build\n")
    (git_repo / "sub" / "hw").mkdir(parents=True)
    (git_repo / "sub" / "hw" / "Spec.lean").write_text("theorem spec : True := trivial\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "a Lean project")
    (git_repo / "lakefile.lean").write_text("-- build, changed\n")
    git(git_repo, "commit", "-qam", "only the build file")
    (git_repo / "sub" / ".git").mkdir()

    result = scope.resolve(git_repo / "sub" / "hw", scope.Scope.CHANGED)

    assert result.focus_files == [Path("Spec.lean")]
    assert result.prefix == "sub/hw/"


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("core.v", True),
        ("core.SV", True),
        ("driver.c", True),
        ("lib.rs", True),
        ("Main.lean", True),
        ("Proofs/Basic.LEAN", True),
        ("lakefile.lean", False),
        ("pkg/lakefile.lean", False),
        ("lean-toolchain", False),
        ("notes.md", False),
        ("build.py", False),
    ],
)
def test_is_source_file(filename: str, expected: bool):
    assert scope.is_source_file(Path(filename)) is expected


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"rtl/fifo.v": "module fifo;\nendmodule\n", "sw/main.c": "", "lib.rs": ""}, ["C", "Rust", "Verilog"]),
        ({"rtl/top.sv": "module top; endmodule", "sim/harness.h": "", "sim/harness.cpp": ""}, ["Verilog"]),
        ({"Theories/Nat.v": "Theorem t : True. Proof. trivial. Qed.\n", "MAIN.C": ""}, []),
        ({"rtl/defs.svh": "`define W 8\n", "rtl/defs.vh": "endmodule"}, []),
    ],
    ids=["each language", "a header is not C", "Coq and an upper-case .C", "headers are not Verilog"],
)
def test_gated_languages_follows_aristotles_own_rules(tmp_path: Path, files: dict[str, str], expected: list[str]):
    for name, content in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(content)

    assert scope.gated_languages(tmp_path, [Path(name) for name in files]) == expected


def test_a_source_file_in_a_folder_that_holds_credentials_is_not_counted(tmp_path: Path):
    # It isn't uploaded, so Aristotle won't find its language.
    (tmp_path / ".env").mkdir()
    (tmp_path / ".env" / "driver.c").write_text("int main(void) { return 0; }\n")

    assert scope.gated_languages(tmp_path, [Path(".env/driver.c")]) == []


def test_a_verilog_file_that_cannot_be_read_is_not_counted(tmp_path: Path):
    assert scope.gated_languages(tmp_path, [Path("missing.v")]) == []


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
def test_a_symlink_counts_as_the_upload_takes_it_with_its_targets_content(tmp_path: Path):
    (tmp_path / "module.template").write_text("module m; endmodule\n")
    (tmp_path / "top.sv").symlink_to(tmp_path / "module.template")
    (tmp_path / "dangling.c").symlink_to(tmp_path / "missing.c")

    assert scope.gated_languages(tmp_path, [Path("top.sv"), Path("module.template")]) == ["Verilog"]
    assert scope.gated_languages(tmp_path, [Path("top.sv")]) == []
    assert scope.gated_languages(tmp_path, [Path("dangling.c")]) == []


def test_endmodule_across_a_read_boundary_is_found(tmp_path: Path):
    (tmp_path / "big.sv").write_bytes(b"x" * ((1 << 20) - 4) + b" endmodule\n")

    assert scope.gated_languages(tmp_path, [Path("big.sv")]) == ["Verilog"]


def test_a_root_aristotle_f_is_the_preferred_filelist():
    files = [Path("rtl/core.f"), Path("aristotle.f"), Path("build/top.f")]

    assert scope.find_filelists(files)[0] == Path("aristotle.f")


def test_a_nested_aristotle_f_beats_an_unrelated_filelist():
    files = [Path("build/top.f"), Path("rtl/aristotle.f")]

    assert scope.find_filelists(files)[0] == Path("rtl/aristotle.f")


def test_shallower_filelists_rank_first():
    files = [Path("a/b/c/deep.f"), Path("top.f")]

    assert scope.find_filelists(files) == [Path("top.f"), Path("a/b/c/deep.f")]


def test_every_filelist_is_offered():
    files = [Path(f"dir{i}/list.f") for i in range(15)]

    assert len(scope.find_filelists(files)) == 15


def test_non_filelists_are_ignored():
    assert scope.find_filelists([Path("main.c"), Path("README.md")]) == []


def test_a_filelist_is_found_for_a_verilog_repository(git_repo: Path):
    (git_repo / "aristotle.f").write_text("+incdir+rtl\nrtl/fifo.v\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "add filelist")

    result = scope.resolve(git_repo, scope=scope.Scope.ALL)

    assert result.has_verilog_focus
    assert result.filelists == [Path("aristotle.f")]


def test_filelists_are_not_collected_without_a_verilog_focus(tmp_path: Path):
    # `.f` is also a Fortran extension, so a C-only run must not claim to have found one.
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@e.com")
    git(tmp_path, "config", "user.name", "T")
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n")
    (tmp_path / "legacy.f").write_text("      PROGRAM MAIN\n      END\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "initial")

    result = scope.resolve(tmp_path, scope=scope.Scope.ALL)

    assert not result.has_verilog_focus
    assert result.filelists == []


def test_a_fetch_as_root_runs_as_the_checkout_owner(git_repo: Path, monkeypatch: pytest.MonkeyPatch):
    """Root-owned files left in a runner's .git break every later fetch and clean of that workspace."""
    calls = []
    real_run = scope.subprocess.run

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return real_run(argv, **{k: v for k, v in kwargs.items() if k not in ("user", "group", "extra_groups")})

    monkeypatch.setattr(scope.os, "geteuid", lambda: 0, raising=False)
    real_stat = scope.os.stat
    monkeypatch.setattr(scope.os, "stat", lambda p, **kwargs: os.stat_result((0o40755, 0, 0, 0, 1001, 121, 0, 0, 0, 0)))
    monkeypatch.setattr(scope.subprocess, "run", run)

    with pytest.raises(scope.ScopeError):
        scope._run_git(git_repo, "fetch", "origin", "main", writes=True)
    scope._run_git(git_repo, "rev-parse", "HEAD")
    monkeypatch.setattr(scope.os, "stat", real_stat)

    fetch = next(kwargs for argv, kwargs in calls if "fetch" in argv)
    assert (fetch["user"], fetch["group"], fetch["extra_groups"]) == (1001, 121, [])
    read = next(kwargs for argv, kwargs in calls if argv[-2:] == ["rev-parse", "HEAD"])
    assert "user" not in read


def test_a_tracked_name_that_is_not_utf8_is_left_out_of_the_upload_and_named(git_repo: Path, caplog):
    latin1_name(git_repo / "rtl")
    (git_repo / "rtl" / "naïve.v").write_text("module naive;\nendmodule\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "two names that aren't ASCII")

    with caplog.at_level(logging.WARNING, logger="aristotle"):
        checkout = scope.resolve_checkout(git_repo)

    assert checkout.focus_files == [Path("rtl/driver.c"), Path("rtl/fifo.v"), Path("rtl/naïve.v")]
    assert caplog.messages == [
        "Not uploading 1 tracked file(s) whose name is not UTF-8, which Aristotle can't take; rename one to upload "
        "it: rtl/caf\\xe9.v"
    ]


def test_a_tracked_credential_whose_name_is_not_utf8_is_named_as_a_credential(git_repo: Path, caplog):
    """No rename makes it go up, so the warning gives none for it."""
    if os.name == "nt":
        pytest.skip("Windows file names are Unicode")
    name = os.fsdecode(b"deploy\xe9.key")
    try:
        (git_repo / name).write_text("placeholder\n")
    except OSError:
        pytest.skip("this file system takes only UTF-8 names")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "a key whose name isn't UTF-8")

    with caplog.at_level(logging.WARNING, logger="aristotle"):
        scope.resolve_checkout(git_repo)

    assert caplog.messages == ["Excluded 1 file(s) matching secret patterns: deploy\\xe9.key"]


@pytest.mark.parametrize("ignored_head", [False, True], ids=["", "HEAD ignored"])
@pytest.mark.parametrize("checkout", ["working copy", "fresh clone"])
def test_a_bare_repository_committed_as_files_is_left_out_and_named(
    git_repo: Path, tmp_path: Path, checkout: str, ignored_head: bool, caplog
):
    """Its config may hold a remote's token. A clone has no empty objects or refs folder, but HEAD and config tell,
    whatever an ignore rule says of that HEAD."""
    git(git_repo, "init", "-q", "--bare", "vendor/lib.git")
    git(git_repo / "vendor" / "lib.git", "config", "remote.origin.url", "https://placeholder-token@example.com/x")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "a vendored bare repository")
    if ignored_head:
        (git_repo / ".gitignore").write_text("vendor/lib.git/HEAD\n")
        git(git_repo, "add", ".gitignore")
        git(git_repo, "commit", "-qm", "ignore its HEAD")
    root = git_repo
    if checkout == "fresh clone":
        git(tmp_path, "clone", "-q", str(git_repo), "clone")
        root = tmp_path / "clone"
        assert not (root / "vendor" / "lib.git" / "objects").exists()

    with caplog.at_level(logging.WARNING, logger="aristotle"):
        checkout_result = scope.resolve_checkout(root)

    assert not any(p.parts[:2] == ("vendor", "lib.git") for p in checkout_result.upload_files)
    warning = next(m for m in caplog.messages if "in a git directory" in m)
    assert "in a git directory, which hold git's own files:" in warning
    assert "vendor/lib.git/config" in warning


@pytest.mark.parametrize("checkout", ["working copy", "fresh clone"])
def test_a_bare_repository_committed_with_its_head_already_ignored_is_left_out(
    git_repo: Path, tmp_path: Path, checkout: str, caplog
):
    """git never tracks its HEAD, but its config names the repository's format."""
    (git_repo / ".gitignore").write_text("vendor/lib.git/HEAD\n")
    git(git_repo, "init", "-q", "--bare", "vendor/lib.git")
    git(git_repo / "vendor" / "lib.git", "config", "remote.origin.url", "https://placeholder-token@example.com/x")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "a vendored bare repository, HEAD ignored")
    assert "vendor/lib.git/HEAD" not in scope.tracked_paths(git_repo)
    root = git_repo
    if checkout == "fresh clone":
        git(tmp_path, "clone", "-q", str(git_repo), "clone")
        root = tmp_path / "clone"

    with caplog.at_level(logging.WARNING, logger="aristotle"):
        checkout_result = scope.resolve_checkout(root)

    assert not any(p.parts[:2] == ("vendor", "lib.git") for p in checkout_result.upload_files)
    assert any("vendor/lib.git/config" in m for m in caplog.messages)


def test_a_checked_folder_that_is_itself_a_committed_bare_repository_uploads_none_of_it(git_repo: Path, tmp_path: Path):
    git(git_repo, "init", "-q", "--bare", "vendor/lib.git")
    git(git_repo / "vendor" / "lib.git", "config", "remote.origin.url", "https://placeholder-token@example.com/x")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "a vendored bare repository")
    git(tmp_path, "clone", "-q", str(git_repo), "clone")

    with pytest.raises(scope.ScopeError, match=r"lib.git is a git directory, and nothing in one is uploaded\."):
        scope.resolve_checkout(tmp_path / "clone" / "vendor" / "lib.git")


def test_a_checked_folder_git_itself_reads_as_a_bare_repository_says_so(git_repo: Path, tmp_path: Path):
    """With history, its objects and refs are tracked, so git reads the folder as bare and ls-files has no work tree."""
    git(tmp_path, "clone", "-q", "--bare", str(git_repo), "lib.git")
    # A loose ref, so a clone has its refs folder: clone --bare packs them all.
    git(tmp_path / "lib.git", "update-ref", "refs/heads/kept", "HEAD")
    (git_repo / "vendor").mkdir()
    (tmp_path / "lib.git").rename(git_repo / "vendor" / "lib.git")
    git(git_repo, "add", "-f", "vendor/lib.git")
    git(git_repo, "commit", "-qm", "a vendored bare repository with history")
    git(tmp_path, "clone", "-q", str(git_repo), "clone")

    with pytest.raises(scope.ScopeError, match=r"lib.git is a git directory, and nothing in one is uploaded\."):
        scope.resolve_checkout(tmp_path / "clone" / "vendor" / "lib.git")
    with pytest.raises(scope.ScopeError, match=r"hooks is inside a git directory, and nothing in one is uploaded\."):
        scope.resolve_checkout(tmp_path / "clone" / "vendor" / "lib.git" / "hooks")


@pytest.mark.parametrize("through_a_link", [False, True], ids=["", "through a link"])
def test_a_checked_folder_inside_a_bare_repository_committed_without_history_says_so(
    git_repo: Path, tmp_path: Path, through_a_link: bool
):
    """git reads it as part of the work tree, with no objects or refs in a clone; its config marks it, as for submit,
    where the folder resolves."""
    if through_a_link and os.name == "nt":
        pytest.skip("symlinks need privileges on Windows")
    git(git_repo, "init", "-q", "--bare", "vendor/lib0.git")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "a vendored bare repository without history")
    git(tmp_path, "clone", "-q", str(git_repo), "clone")
    root = tmp_path / "clone" / "vendor" / "lib0.git" / "hooks"
    if through_a_link:
        (tmp_path / "link").symlink_to(root)
        root = tmp_path / "link"

    with pytest.raises(scope.ScopeError, match=r"is inside a git directory, and nothing in one is uploaded\."):
        scope.resolve_checkout(root)


def test_a_checked_folder_inside_the_checkouts_own_git_folder_says_so(git_repo: Path):
    with pytest.raises(scope.ScopeError, match=r"hooks is inside a git directory, and nothing in one is uploaded\."):
        scope.resolve_checkout(git_repo / ".git" / "hooks")


def test_a_work_tree_whose_top_tracks_a_git_style_config_still_goes_up(git_repo: Path):
    """Its .git says it is a work tree, whatever its own files look like."""
    (git_repo / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    git(git_repo, "add", "config")
    git(git_repo, "commit", "-qm", "a config that looks like git's")

    assert Path("config") in scope.resolve_checkout(git_repo).upload_files


def test_a_checkout_rooted_at_a_windows_credential_folder_leaves_its_files_out(git_repo: Path, tmp_path: Path):
    """--directory at AppData/Roaming/gcloud: the three names of the rule end at the root, so it holds back every file
    in it, as a root that is itself .docker/ does."""
    folder = git_repo / "AppData" / "Roaming" / "gcloud"
    folder.mkdir(parents=True)
    (folder / "access_tokens.db").write_text("placeholder\n")
    (folder / "top.v").write_text("module top;\nendmodule\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "a gcloud folder")

    from aristotlelib.ci import runner

    upload = runner.archive(folder, scope.resolve_checkout(folder), tmp_path / "upload.tar.gz")

    assert (upload.uploaded, sorted(upload.excluded)) == ([], [Path("access_tokens.db"), Path("top.v")])


def test_outside_ci_the_root_is_the_nearest_folder_up_to_the_top_level_that_holds_the_file(git_repo: Path):
    deep = git_repo / "rtl" / "deep"
    deep.mkdir()

    assert scope.local_root(deep, "aristotle.toml") == git_repo.resolve()
    (git_repo / "aristotle.toml").write_text("")
    assert scope.local_root(deep, "aristotle.toml") == git_repo.resolve()
    (git_repo / "rtl" / "aristotle.toml").write_text("")
    assert scope.local_root(deep, "aristotle.toml") == (git_repo / "rtl").resolve()
    # A file above the top level is no part of the repository.
    assert scope.local_root(deep) == git_repo.resolve()


def test_the_search_for_the_file_stops_at_the_top_level(tmp_path: Path):
    (tmp_path / "aristotle.toml").write_text("")
    repo = tmp_path / "repo"
    (repo / "rtl").mkdir(parents=True)
    git(repo, "init", "-q")

    assert scope.local_root(repo / "rtl", "aristotle.toml") == repo.resolve()


@pytest.mark.skipif(os.name == "nt", reason="Windows names can't end in a space")
def test_a_top_level_whose_name_ends_in_a_space_is_found_whole(tmp_path: Path):
    repo = tmp_path / "trail "
    (repo / "rtl").mkdir(parents=True)
    git(repo, "init", "-q")
    (repo / "aristotle.toml").write_text("")

    assert scope.local_root(repo / "rtl", "aristotle.toml") == repo.resolve()


@pytest.mark.parametrize("git_fails", [without_git, refusing_git], ids=["missing", "refusing"])
def test_when_git_cannot_answer_the_top_level_is_the_nearest_folder_that_holds_a_dot_git(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, git_fails
):
    # So that a run from a subfolder finds the manifest at the top, and fails on git rather than on its absence.
    deep = git_repo / "rtl" / "deep"
    deep.mkdir()
    (git_repo / "aristotle.toml").write_text("")
    git_fails(monkeypatch, git_repo)
    with pytest.raises(scope.ScopeError):
        scope._run_git(deep, "rev-parse", "--show-toplevel")

    assert scope.local_root(deep, "aristotle.toml") == git_repo.resolve()
    assert scope.local_root(deep) == git_repo.resolve()


def test_outside_a_repository_the_root_is_where_the_search_starts(tmp_path: Path):
    (tmp_path / "aristotle.toml").write_text("")
    start = tmp_path / "below"
    start.mkdir()

    assert scope.local_root(start, "aristotle.toml") == start.resolve()


def test_a_base_that_origin_wont_serve_keeps_the_depth_hint(git_repo: Path, monkeypatch: pytest.MonkeyPatch):
    """As without credentials: a full checkout holds the base already, so the hint is the fix."""
    git(git_repo, "remote", "add", "origin", str(git_repo.parent / "unreachable.git"))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    with pytest.raises(scope.ScopeError) as raised:
        scope.resolve(git_repo, scope.Scope.CHANGED, base_ref="origin/main")

    message = str(raised.value)
    assert message.startswith("Cannot determine which files changed: main could not be fetched from origin: fatal: ")
    assert message.endswith("Set fetch-depth: 0 on the actions/checkout step. Or use --scope all or --scope paths.")


def test_gits_reason_keeps_a_warning_before_its_error_and_drops_the_hints_after():
    error = scope.GitError(
        "git fetch failed",
        128,
        "Warning: Permanently added '127.0.0.1' (ED25519) to the list of known hosts.\n"
        "git@127.0.0.1: Permission denied (publickey).\n"
        "fatal: Could not read from remote repository.\n\n"
        "Please make sure you have the correct access rights\nand the repository exists.\n",
    )
    assert error.reason == (
        "Warning: Permanently added '127.0.0.1' (ED25519) to the list of known hosts. git@127.0.0.1: Permission denied "
        "(publickey). fatal: Could not read from remote repository."
    )
