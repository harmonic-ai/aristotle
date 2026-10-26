"""Tests for aristotlelib.cli.command.review."""

import argparse
import logging
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aristotlelib import local_file_utils
from aristotlelib.agent_task import TaskStatus
from aristotlelib.ci import results, runner
from aristotlelib.cli.command.review import ReviewAction
from aristotlelib.declaration import DeclarationEvidence, DeclarationStatus
from aristotlelib.exit_codes import (
    EXIT_INFRASTRUCTURE_ERROR,
    EXIT_RATE_LIMITED,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    EXIT_VERIFICATION_FAILED,
)
from aristotlelib.project import Project, ProjectStatus
from aristotlelib.verilog import filelist
from tests.ci.conftest import commit_bare_shaped, git, latin1_name, make_declaration
from tests.conftest import NEVER_READ, UNREACHABLE_HOST, WILDCARD_FOLDERS

CI_VARIABLE_PREFIXES = ("GITHUB_", "GITLAB_", "CI_", "BUILDKITE", "CIRCLE", "BUILD_", "SYSTEM_", "JENKINS_")

UNTRACKED = "aristotle.f isn't tracked by git, so `aristotle review` doesn't read it or upload it. Commit it to use it."
"""The warning for a root aristotle.f git doesn't track."""


@pytest.fixture(autouse=True)
def _outside_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as if on a laptop, with no CI system's variables and no ARISTOTLE_* configuration."""
    for name in list(os.environ):
        if name.startswith((*CI_VARIABLE_PREFIXES, "ARISTOTLE_")) or name in (
            "CIRCLECI",
            "TF_BUILD",
            "CHANGE_ID",
            "JOB_NAME",
        ):
            monkeypatch.delenv(name)


def _action() -> ReviewAction:
    parser = argparse.ArgumentParser()
    action = ReviewAction(parser.add_subparsers())
    action.add_action_arguments()
    return action


@pytest.fixture
def action() -> ReviewAction:
    return _action()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "fifo.v").write_text("module fifo;\nendmodule\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def _commit(repo: Path, name: str, content: str) -> None:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(content)
    git(repo, "add", "-f", name)
    git(repo, "commit", "-qm", name)


def _revision(repo: Path) -> str:
    """The branch and short commit the default project name ends with."""

    def output(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout

    return f"{output('symbolic-ref', '--short', 'HEAD').strip()} {output('rev-parse', '--short', 'HEAD').strip()}"


def _project() -> Project:
    return Project(
        project_id="test-123",
        status=ProjectStatus.RUNNING,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=None,
        has_input=True,
        has_files=False,
    )


async def _run(
    action: ReviewAction,
    argv: list[str],
    declarations,
    final_status: TaskStatus | None = TaskStatus.COMPLETE,
    stopped_summary: str | None = None,
    access: dict[str, bool | None] | None = None,
    summary: str | None = None,
    preflight: bool = False,
) -> tuple[int, AsyncMock]:
    """Run the command, after its preflight if ``preflight``, with everything that would talk to the network patched
    out.

    ``access`` is what the account's language check answers; by default every language it asks about is enabled.
    """
    args = action.parser.parse_args(argv)
    if preflight and (code := action.preflight(args)) is not None:
        return code, AsyncMock()
    language_access = AsyncMock(
        side_effect=lambda languages: dict.fromkeys(languages, True) if access is None else access
    )
    with (
        patch("aristotlelib.ci.runner.submit", AsyncMock(return_value=_project())) as submit,
        patch("aristotlelib.ci.runner.wait_for_completion", AsyncMock(return_value=final_status)),
        patch("aristotlelib.ci.runner.fetch_stopped_summary", AsyncMock(return_value=stopped_summary)),
        patch("aristotlelib.ci.runner.fetch_summary", AsyncMock(return_value=summary)),
        patch("aristotlelib.ci.runner.language_access", language_access),
        patch.object(Project, "get_declarations", AsyncMock(return_value=declarations)),
    ):
        code = await action.run_action(args)
    return code, submit


async def test_from_a_subfolder_review_checks_the_whole_repository(
    action: ReviewAction, repo: Path, monkeypatch: pytest.MonkeyPatch
):
    _commit(repo, "rtl/arbiter.v", "module arbiter;\nendmodule\n")
    monkeypatch.chdir(repo / "rtl")

    code, submit = await _run(action, ["--scope", "all"], [make_declaration("Spec.A", DeclarationStatus.PROVED)])

    assert code == EXIT_SUCCESS
    assert submit.await_args.args[0] == repo.resolve()


async def test_a_run_that_registered_no_properties_prints_its_summary(
    action: ReviewAction, repo: Path, tmp_path_factory, caplog
):
    report = tmp_path_factory.mktemp("junit") / "out.xml"
    with caplog.at_level(logging.INFO, logger="aristotle"):
        await _run(
            action,
            ["--scope", "all", "--directory", str(repo), "--junit", str(report)],
            [],
            summary="No property fits a FIFO this small.",
        )

    assert "    No property fits a FIFO this small." in caplog.text
    assert "Its summary is above; the run is at" in caplog.text


async def test_a_clean_run_succeeds(action: ReviewAction, repo: Path, tmp_path: Path):
    code, submit = await _run(
        action,
        ["--scope", "all", "--directory", str(repo), "--junit", str(tmp_path / "out.xml")],
        [make_declaration("Spec.A", DeclarationStatus.PROVED)],
    )

    assert code == EXIT_SUCCESS
    submit.assert_awaited_once()


@pytest.mark.parametrize(
    ("declaration", "final_status"),
    [
        (
            make_declaration("Spec.A", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE),
            TaskStatus.COMPLETE,
        ),
        (make_declaration("Spec.A", DeclarationStatus.PROVED), TaskStatus.FAILED),
    ],
    ids=["a counterexample", "a failed run"],
)
async def test_a_failure_does_not_fail_the_build_by_default(
    action: ReviewAction, repo: Path, tmp_path: Path, caplog, declaration, final_status: TaskStatus
):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(
            action,
            ["--scope", "all", "--directory", str(repo), "--junit", str(tmp_path / "out.xml")],
            [declaration],
            final_status=final_status,
        )

    assert code == EXIT_SUCCESS
    assert "--fail-on never is set, so the build is not being failed." in caplog.text


async def test_a_review_still_queued_when_the_budget_ran_out_is_rate_limited_though_review_is_advisory(
    action: ReviewAction, repo: Path, tmp_path: Path, caplog
):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(
            action,
            ["--scope", "all", "--directory", str(repo), "--junit", str(tmp_path / "out.xml")],
            [],
            final_status=TaskStatus.QUEUED,
        )

    assert code == EXIT_RATE_LIMITED
    assert "Nothing was verified. The run never started" in caplog.text
    assert "The run registered no properties" not in caplog.text


@pytest.mark.parametrize("final_status", [TaskStatus.OUT_OF_BUDGET, None], ids=["out of budget", "out of time"])
@pytest.mark.parametrize(
    ("fail_on", "expected"),
    [
        ([], EXIT_SUCCESS),
        (["--fail-on", "failure"], EXIT_INFRASTRUCTURE_ERROR),
        (["--fail-on", "no-verdict"], EXIT_INFRASTRUCTURE_ERROR),
    ],
    ids=["default", "failure", "no-verdict"],
)
async def test_a_cut_short_review_reports_its_proofs_as_unchecked_and_gates_only_when_asked(
    action: ReviewAction,
    repo: Path,
    tmp_path: Path,
    caplog,
    final_status: TaskStatus | None,
    fail_on: list[str],
    expected: int,
):
    report = tmp_path / "out.xml"

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(
            action,
            ["--scope", "all", "--directory", str(repo), "--junit", str(report), *fail_on],
            [make_declaration("Spec.A", DeclarationStatus.PROVED, description="Holds.")],
            final_status=final_status,
        )

    # A review the time budget cut hasn't ended, so it exits 3 whatever --fail-on is.
    ended = final_status is not None
    assert code == (expected if ended else EXIT_INFRASTRUCTURE_ERROR)
    assert ("--fail-on never is set, so the build is not being failed." in caplog.messages) == (not fail_on and ended)
    assert "  SKIP  [proved, unchecked] Spec.A\n" in caplog.text
    skipped = ET.parse(report).getroot().find("testsuite/testcase/skipped")
    assert skipped is not None and skipped.get("message").startswith("Reported proved, unchecked.")


@pytest.mark.parametrize("final_status", [TaskStatus.OUT_OF_BUDGET, None], ids=["out of budget", "out of time"])
async def test_a_cut_short_review_with_no_properties_does_not_blame_the_accounts_languages(
    action: ReviewAction, repo: Path, caplog, final_status: TaskStatus | None
):
    with caplog.at_level(logging.WARNING, logger="aristotle"):
        code, _ = await _run(
            action, ["--scope", "all", "--directory", str(repo), "--no-junit"], [], final_status=final_status
        )

    # A review the time budget cut has no results yet, so it doesn't pass as one that found nothing.
    assert code == (EXIT_SUCCESS if final_status is not None else EXIT_INFRASTRUCTURE_ERROR)
    assert len(caplog.messages) == 1
    assert "with no property in the run." in caplog.messages[0]


@pytest.mark.parametrize(("flags", "env"), [(["--fail-on", "failure"], None), ([], "failure")], ids=["flag", "env"])
async def test_fail_on_failure_makes_a_counterexample_fail_the_build(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog, flags: list[str], env: str | None
):
    if env is not None:
        monkeypatch.setenv("ARISTOTLE_FAIL_ON", env)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(
            _action(),
            ["--scope", "all", "--directory", str(repo), "--junit", str(tmp_path / "out.xml"), *flags],
            [make_declaration("Spec.A", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE)],
        )

    assert code == EXIT_VERIFICATION_FAILED
    assert "--fail-on never" not in caplog.text


async def test_the_junit_report_is_written(action: ReviewAction, repo: Path, tmp_path: Path):
    report = tmp_path / "reports" / "out.xml"

    await _run(
        action,
        ["--scope", "all", "--directory", str(repo), "--junit", str(report)],
        [make_declaration("Spec.A", DeclarationStatus.PROVED)],
    )

    assert "Spec.A" in report.read_text()


async def test_no_report_is_written_with_no_junit(action: ReviewAction, repo: Path, tmp_path: Path):
    report = tmp_path / "out.xml"

    await _run(
        action,
        ["--scope", "all", "--directory", str(repo), "--no-junit", "--junit", str(report)],
        [make_declaration("Spec.A")],
    )

    assert not report.exists()


async def test_paths_that_match_no_source_file_are_a_usage_error(
    action: ReviewAction, repo: Path, tmp_path: Path, caplog
):
    with caplog.at_level(logging.ERROR, logger="aristotle"):
        code, submit = await _run(
            action,
            ["--scope", "paths", "--path", "rlt/**", "--directory", str(repo), "--junit", str(tmp_path / "out.xml")],
            [],
        )

    # A typo in a glob must not leave the gate permanently green, whatever --fail-on says.
    assert code == EXIT_USAGE_ERROR
    assert "'rlt/**'" in caplog.text
    submit.assert_not_awaited()
    assert not (tmp_path / "out.xml").exists()


async def test_a_review_run_out_of_budget_with_no_property_prints_its_summary(action: ReviewAction, repo: Path, caplog):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        await _run(
            action,
            ["--scope", "all", "--directory", str(repo), "--no-junit"],
            [],
            final_status=TaskStatus.OUT_OF_BUDGET,
            summary="Aristotle ran out of budget while reading fifo.v.",
        )

    assert "Aristotle ran out of budget while reading fifo.v." in caplog.text


async def test_scope_all_without_a_source_file_is_a_usage_error(action: ReviewAction, tmp_path: Path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@e.com")
    git(tmp_path, "config", "user.name", "T")
    _commit(tmp_path, "README.md", "docs\n")

    code, submit = await _run(action, ["--scope", "all", "--directory", str(tmp_path)], [])

    assert code == EXIT_USAGE_ERROR
    submit.assert_not_awaited()


async def test_a_directory_that_is_not_a_repository_is_a_usage_error(action: ReviewAction, tmp_path: Path, caplog):
    (tmp_path / "aristotle.f").write_text("fifo.v\n")

    code, submit = await _run(action, ["--scope", "all", "--directory", str(tmp_path)], [])

    assert code == EXIT_USAGE_ERROR
    assert "is not a git repository" in caplog.text
    assert "isn't tracked by git" not in caplog.text
    submit.assert_not_awaited()


async def test_a_change_with_no_source_file_passes_with_an_empty_report(
    action: ReviewAction, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    _commit(repo, "README.md", "docs\n")
    report = tmp_path / "reports" / "out.xml"

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, submit = await _run(
            action,
            ["--scope", "changed", "--fail-on", "no-verdict", "--directory", str(repo), "--junit", str(report)],
            [],
        )

    assert code == EXIT_SUCCESS
    submit.assert_not_awaited()
    suite = ET.parse(report).getroot().find("testsuite")
    assert suite is not None and suite.get("tests") == "0"
    assert output.read_text() == f"junit={report}\n"
    assert "This change touched no verifiable source file." in caplog.text
    assert "nothing was verified" in caplog.text


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
async def test_a_change_with_no_source_file_writes_its_empty_report_over_a_link_it_commits_not_through_it(
    action: ReviewAction, repo: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, caplog
):
    target = tmp_path_factory.mktemp("outside") / "config.xml"
    target.write_text("<hudson/>\n")
    (repo / "aristotle-results.xml").symlink_to(target)
    git(repo, "add", "aristotle-results.xml")
    _commit(repo, "README.md", "docs\n")
    monkeypatch.chdir(repo)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, submit = await _run(action, ["--scope", "changed", "--directory", str(repo)], [])

    assert code == EXIT_SUCCESS
    submit.assert_not_awaited()
    assert target.read_text() == "<hudson/>\n"
    report = repo / "aristotle-results.xml"
    assert not report.is_symlink() and ET.parse(report).getroot().find("testsuite").get("tests") == "0"
    assert (
        "Replaced the symbolic link at aristotle-results.xml with the report, rather than write through it."
        in caplog.text
    )
    assert "This change touched no verifiable source file." in caplog.text


async def test_a_lean_change_is_verified_rather_than_passed_as_touching_no_source_file(
    action: ReviewAction, repo: Path, caplog
):
    _commit(repo, "Main.lean", "theorem t : 1 + 1 = 2 := rfl\n")
    _commit(repo, "Main.lean", "theorem t : 1 + 1 = 2 := by decide\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, submit = await _run(
            action, ["--directory", str(repo), "--fail-on", "no-verdict", "--no-junit", "--dry-run"], []
        )

    assert code == EXIT_SUCCESS
    assert "Nothing to do" not in caplog.text
    assert caplog.messages[2:4] == ["Verifying 1 file(s):", "  Main.lean"]
    submit.assert_not_awaited()


async def test_a_lean_build_file_above_the_directory_is_a_change_to_its_lean_files(
    action: ReviewAction, repo: Path, caplog
):
    _commit(repo, "lean-toolchain", "leanprover/lean4:v4.21.0\n")
    _commit(repo, "Proofs/Basic.lean", "theorem two : 1 + 1 = 2 := rfl\n")
    _commit(repo, "lean-toolchain", "leanprover/lean4:v4.22.0\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, submit = await _run(
            action,
            ["--directory", str(repo / "Proofs"), "--fail-on", "no-verdict", "--no-junit", "--dry-run"],
            [],
        )

    assert code == EXIT_SUCCESS
    assert "Nothing to do" not in caplog.text
    assert "  Basic.lean" in caplog.messages
    submit.assert_not_awaited()


async def test_a_github_push_is_compared_against_the_commit_before_it(
    action: ReviewAction, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    _commit(repo, "alu.v", "module alu;\nendmodule\n")
    _commit(repo, "README.md", "docs\n")
    event = tmp_path / "event.json"
    event.write_text(f'{{"before": "{before}"}}')
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--scope", "changed", "--dry-run", "--directory", str(repo)], [])

    assert code == EXIT_SUCCESS
    assert "Verifying 1 file(s)" in caplog.text
    assert "  alu.v" in caplog.text


@pytest.mark.parametrize("fail_on", ["never", "no-verdict"])
async def test_a_github_push_whose_previous_commit_cannot_be_fetched_is_a_usage_error_not_a_guess(
    action: ReviewAction, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog, fail_on: str
):
    _commit(repo, "alu.v", "module alu;\nendmodule\n")
    git(repo, "remote", "add", "origin", (tmp_path / "unreachable").as_uri())
    event = tmp_path / "event.json"
    event.write_text(f'{{"before": "{"ab" * 20}"}}')
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, submit = await _run(action, ["--fail-on", fail_on, "--directory", str(repo), "--no-junit"], [])

    assert code == EXIT_USAGE_ERROR
    assert "abababababab, the commit before this push, is not in the checkout" in caplog.text
    assert "Comparing against the previous commit" not in caplog.text
    submit.assert_not_awaited()


@pytest.mark.parametrize(("fail_on", "expected"), [("never", EXIT_SUCCESS), ("no-verdict", EXIT_VERIFICATION_FAILED)])
async def test_a_change_only_to_a_name_that_is_not_utf8_says_why_nothing_was_verified(
    action: ReviewAction, repo: Path, tmp_path: Path, caplog, fail_on: str, expected: int
):
    latin1_name(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "a Latin-1 name")
    report = tmp_path / "out.xml"

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, submit = await _run(action, ["--directory", str(repo), "--fail-on", fail_on, "--junit", str(report)], [])

    assert code == expected
    assert any(m.startswith("Not uploading 1 tracked file(s) whose name is not UTF-8") for m in caplog.messages)
    assert (
        "This change touched only source files whose names aren't UTF-8, so nothing was verified: caf\\xe9.v. "
        "Rename them to verify them." in caplog.messages
    )
    assert "Nothing to do" not in caplog.text
    assert report.exists()
    submit.assert_not_awaited()


async def test_a_dry_run_with_no_changed_source_file_writes_no_report(action: ReviewAction, repo: Path, tmp_path: Path):
    _commit(repo, "README.md", "docs\n")
    report = tmp_path / "out.xml"

    code, _ = await _run(
        action, ["--scope", "changed", "--dry-run", "--directory", str(repo), "--junit", str(report)], []
    )

    assert code == EXIT_SUCCESS
    assert not report.exists()


async def test_a_dry_run_does_not_submit(action: ReviewAction, repo: Path, caplog):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, submit = await _run(action, ["--scope", "all", "--directory", str(repo), "--dry-run"], [])

    assert code == EXIT_SUCCESS
    submit.assert_not_awaited()
    assert caplog.messages[-1] == f'Dry run: would name the project "{repo.name}: review all @ {_revision(repo)}".'


@pytest.mark.parametrize(
    ("argv", "run"),
    [
        (["--scope", "all"], "review all"),
        (["--scope", "paths", "--path", "*.v"], "review paths *.v"),
        (["--scope", "all", "--name", "audit"], None),
    ],
)
async def test_the_project_is_named_after_the_repository_the_scope_and_the_commit(
    action: ReviewAction, repo: Path, argv: list[str], run: str | None
):
    _, submit = await _run(action, [*argv, "--directory", str(repo), "--no-junit"], [make_declaration("Spec.A")])

    expected = f"{repo.name}: {run} @ {_revision(repo)}" if run else "audit"
    assert submit.await_args.kwargs["name"] == expected


@pytest.mark.parametrize(
    ("filelist", "announced", "not_announced"),
    [
        (None, "infer the build configuration", "Using the Verilog filelist"),
        ("sim/files.f", "Found candidate Verilog filelists: sim/files.f", "Using the Verilog filelist"),
        ("aristotle.f", "Using the Verilog filelist committed at aristotle.f", "infer the build configuration"),
    ],
    ids=["none", "a candidate", "the root aristotle.f"],
)
async def test_the_verilog_filelist_is_announced_at_info(
    action: ReviewAction, repo: Path, caplog, filelist: str | None, announced: str, not_announced: str
):
    if filelist is not None:
        _commit(repo, filelist, "fifo.v\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--scope", "all", "--directory", str(repo), "--dry-run"], [])

    assert code == EXIT_SUCCESS
    assert announced in caplog.text
    assert not_announced not in caplog.text
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_only_the_held_back_files_the_block_leaves_out_are_warned_of(action: ReviewAction, repo: Path, caplog):
    """The block takes a file it needs whatever an ignore rule says, so the warning names only the others."""
    _commit(repo, "gen/defs.vh", "`define W 8\n")
    _commit(repo, "gen/unused.v", "module unused;\nendmodule\n")
    _commit(repo, "fifo.v", '`include "gen/defs.vh"\nmodule fifo;\nendmodule\n')
    _commit(repo, ".gitignore", "gen/\n")
    _commit(repo, "aristotle.f", "fifo.v\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--scope", "all", "--directory", str(repo), "--dry-run"], [])

    assert code == EXIT_SUCCESS
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == [
        local_file_utils.held_back_warning({Path("gen/unused.v"): ".gitignore:1: gen/"})
    ]


async def test_a_review_of_everything_verifies_the_block_sources_an_ignore_rule_holds_back(
    action: ReviewAction, repo: Path, caplog
):
    """The upload set has no source here, but the block a root aristotle.f describes brings one."""
    _commit(repo, ".gitignore", "*.v\n")
    _commit(repo, "aristotle.f", "fifo.v\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--scope", "all", "--directory", str(repo), "--dry-run"], [])

    assert code == EXIT_SUCCESS
    assert "Using the Verilog filelist committed at aristotle.f" in caplog.messages
    i = caplog.messages.index("Verifying 1 file(s):")
    assert caplog.messages[i + 1] == "  fifo.v"


def _ignoring(repo: Path, rule: str, source: str, filelist: bool = True) -> None:
    """Commit an ignore rule, with the repository's one source at ``source`` and, with ``filelist``, a root aristotle.f
    naming it."""
    if source != "fifo.v":
        (repo / source).parent.mkdir(parents=True, exist_ok=True)
        git(repo, "mv", "fifo.v", source)
        git(repo, "commit", "-qm", "move")
    if filelist:
        _commit(repo, "aristotle.f", f"{source}\n")
    _commit(repo, ".gitignore", f"{rule}\n")


@pytest.mark.parametrize(
    ("rule", "source"), [("*.v", "fifo.v"), ("*", "fifo.v"), ("rtl/", "rtl/fifo.v")], ids=["*.v", "*", "dir/"]
)
async def test_a_review_of_everything_reads_the_block_past_any_ignore_rule(
    action: ReviewAction, repo: Path, rule: str, source: str, caplog
):
    """An ignore rule may leave the upload set no source, or no file at all; the block brings its own."""
    _ignoring(repo, rule, source)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--scope", "all", "--directory", str(repo), "--dry-run"], [])

    assert code == EXIT_SUCCESS
    i = caplog.messages.index("Verifying 1 file(s):")
    assert caplog.messages[i + 1] == f"  {source}"


@pytest.mark.parametrize("untracked", [False, True], ids=["no aristotle.f", "an untracked aristotle.f"])
@pytest.mark.parametrize(
    ("rule", "error"),
    [("*", "No tracked files found in {root}. Has the repository been checked out?"), ("*.v", "--scope all found")],
    ids=["*", "*.v"],
)
async def test_without_a_root_filelist_an_ignore_rule_still_stops_a_review_of_everything(
    action: ReviewAction, repo: Path, rule: str, error: str, untracked: bool, caplog
):
    _ignoring(repo, rule, "fifo.v", filelist=False)
    if untracked:
        (repo / "aristotle.f").write_text("fifo.v\n")

    code, _ = await _run(action, ["--scope", "all", "--directory", str(repo), "--dry-run"], [])

    assert code == EXIT_USAGE_ERROR
    assert caplog.messages[-1].startswith(error.format(root=repo))
    assert (caplog.messages[-2] == UNTRACKED) is untracked


@pytest.mark.parametrize("from_folder", [False, True], ids=["--directory", "current folder"])
async def test_a_folder_shaped_like_a_bare_repository_is_reviewed_from_the_checkouts_repository(
    action: ReviewAction, repo: Path, from_folder: bool, monkeypatch: pytest.MonkeyPatch, caplog
):
    hw = commit_bare_shaped(repo)
    monkeypatch.setenv("ARISTOTLE_WEB_URL", "http://127.0.0.1:9")
    if from_folder:
        monkeypatch.chdir(hw)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(
            action, ["--scope", "all", "--dry-run", *([] if from_folder else ["--directory", str(hw)])], []
        )

    assert code == EXIT_SUCCESS
    # From the folder, review checks the whole checkout.
    verified = ["fifo.v", "hw/top.v"] if from_folder else ["top.v"]
    start = caplog.messages.index(f"Verifying {len(verified)} file(s):")
    assert caplog.messages[start + 1 : start + 1 + len(verified)] == [f"  {name}" for name in verified]


async def test_a_worktree_inside_a_bare_repository_is_reviewed(tmp_path: Path, repo: Path, caplog):
    """git clone --bare and worktree add lay a checkout out inside the bare repository, which isn't a .git folder."""
    _commit(repo, "aristotle.f", "fifo.v\n")
    git(tmp_path, "clone", "-q", "--bare", str(repo), "proj.git")
    git(tmp_path / "proj.git", "worktree", "add", "-q", "main")
    action = _action()

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(
            action, ["--scope", "all", "--directory", str(tmp_path / "proj.git" / "main"), "--dry-run"], []
        )

    assert code == EXIT_SUCCESS
    assert "Verifying 1 file(s):" in caplog.messages


async def test_a_subprojects_block_file_outside_its_folder_stops_the_review(action: ReviewAction, repo: Path, caplog):
    """Tracked elsewhere in the repository isn't enough: review uploads only what git tracks in its folder."""
    _commit(repo, "shared/core.v", "module core;\nendmodule\n")
    _commit(repo, "hw/aristotle.f", "../shared/core.v\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--scope", "all", "--directory", str(repo / "hw"), "--dry-run"], [])

    assert code == EXIT_USAGE_ERROR
    assert caplog.messages[-1].endswith(
        "is outside the folder the step uploads, and `aristotle review` uploads only files git tracks there. Commit "
        "the file inside that folder and name it from there, name a folder that holds it with --allow-untracked, or "
        "submit the block with `aristotle submit`."
    )


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
async def test_a_review_of_everything_verifies_a_block_file_reached_through_a_linked_folder_once(
    action: ReviewAction, repo: Path, caplog
):
    (repo / "fifo.v").unlink()
    (repo / "rtl").mkdir()
    _commit(repo, "rtl/top.v", "module top;\nendmodule\n")
    (repo / "lib").symlink_to("rtl")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "link")
    _commit(repo, "aristotle.f", "lib/top.v\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--scope", "all", "--directory", str(repo), "--dry-run"], [])

    assert code == EXIT_SUCCESS
    i = caplog.messages.index("Verifying 1 file(s):")
    assert caplog.messages[i + 1] == "  rtl/top.v"


async def test_a_review_of_everything_with_no_source_still_names_what_an_ignore_rule_holds_back(
    action: ReviewAction, repo: Path, caplog
):
    (repo / "fifo.v").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "no verilog")
    _commit(repo, "x.c", "int main(void) { return 0; }\n")
    _commit(repo, ".gitignore", "*.c\n")
    _commit(repo, "aristotle.f", "+define+FOO\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--scope", "all", "--directory", str(repo), "--dry-run"], [])

    assert code == EXIT_USAGE_ERROR
    assert caplog.messages[-1].startswith("--scope all found no tracked source files")
    assert caplog.messages[-2] == local_file_utils.held_back_warning({Path("x.c"): ".gitignore:1: *.c"})


def _blackboxed(repo: Path, named: bool = True) -> None:
    """A tracked vendor model, vendor/sram_1p.v, which the root aristotle.f black-boxes, and names when ``named``."""
    _commit(repo, "vendor/sram_1p.v", "module sram_1p;\nendmodule\n")
    _commit(repo, "aristotle.f", "--blackbox sram_1p\nfifo.v\n" + "vendor/sram_1p.v\n" * named)


NAMED = pytest.mark.parametrize("named", [True, False], ids=["named", "in no filelist"])


@NAMED
async def test_a_review_of_everything_verifies_no_file_a_black_box_leaves_out(
    action: ReviewAction, repo: Path, caplog, named: bool
):
    _blackboxed(repo, named)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--scope", "all", "--directory", str(repo), "--dry-run"], [])

    assert code == EXIT_SUCCESS
    i = caplog.messages.index("Verifying 1 file(s):")
    assert caplog.messages[i + 1] == "  fifo.v"
    assert any(
        m.startswith("Dry run: would upload 2 files (") and m.endswith("; verifying 1.") for m in caplog.messages
    )
    assert any(
        m.startswith("Dry run: would ask in the prompt: Treat this module as a black box") for m in caplog.messages
    )


@NAMED
async def test_a_change_to_only_a_file_a_black_box_leaves_out_has_nothing_to_verify(
    action: ReviewAction, repo: Path, caplog, named: bool
):
    """As a change to no source file, the step passes."""
    _blackboxed(repo, named)
    _commit(repo, "vendor/sram_1p.v", "module sram_1p;\n  // edited\nendmodule\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, submit = await _run(
            action, ["--directory", str(repo), "--base-ref", "HEAD~1", "--fail-on", "no-verdict", "--no-junit"], []
        )

    assert code == EXIT_SUCCESS
    submit.assert_not_awaited()
    assert (
        "This change touched only source files that a --blackbox line in aristotle.f leaves out of the upload: "
        "vendor/sram_1p.v. Nothing to do."
    ) in caplog.messages
    assert caplog.messages[-1] == (
        "--fail-on no-verdict is set, but nothing was verified: this change touched only source files a --blackbox "
        "line in aristotle.f leaves out."
    )


@NAMED
async def test_paths_that_match_only_files_a_black_box_leaves_out_stop(
    action: ReviewAction, repo: Path, caplog, named: bool
):
    _blackboxed(repo, named)

    code, submit = await _run(
        action, ["--scope", "paths", "--path", "vendor/*", "--directory", str(repo), "--dry-run"], []
    )

    assert code == EXIT_USAGE_ERROR
    submit.assert_not_awaited()
    assert caplog.messages[-1] == (
        "Each file --path matches is one a --blackbox line in aristotle.f leaves out of the upload, so nothing would "
        "be verified: vendor/sram_1p.v."
    )


HELD_BACK_STOP = (
    "Nothing this change touched would be verified: an ignore rule holds back gen/regs.v. To verify one, upload it as "
    "the warning above says, or, if it is Verilog, name it in a root aristotle.f that git tracks. To keep one out of "
    "the review on purpose, give --path globs that don't match it."
)


def _regenerated(repo: Path, filelist: str | None = None) -> None:
    """Commit gen/regs.v, which an ignore rule holds back though git tracks it, with ``filelist`` as the root aristotle.f
    if given, then a change to gen/regs.v alone."""
    _commit(repo, "gen/regs.v", "module regs;\nendmodule\n")
    _commit(repo, ".gitignore", "gen/\n")
    if filelist is not None:
        _commit(repo, "aristotle.f", filelist)
    _commit(repo, "gen/regs.v", "module regs;\n  // regenerated\nendmodule\n")


def _commit_all(repo: Path, edits: dict[str, str]) -> None:
    """Commit ``edits``, files by path and their contents, in one commit, ignore rules aside."""
    for name, content in edits.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    git(repo, "add", "-f", *edits)
    git(repo, "commit", "-qm", "edits")


@pytest.mark.parametrize("flags", [[], ["--fail-on", "no-verdict"], ["--dry-run"]], ids=["never", "no-verdict", "dry"])
async def test_a_change_only_to_a_file_an_ignore_rule_holds_back_stops_naming_it(
    action: ReviewAction, repo: Path, tmp_path: Path, caplog, flags: list[str]
):
    _regenerated(repo)
    report = tmp_path / "out.xml"

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, submit = await _run(action, ["--directory", str(repo), "--junit", str(report), *flags], [])

    assert code == EXIT_USAGE_ERROR
    submit.assert_not_awaited()
    assert not report.exists()
    assert caplog.messages[-2:] == [
        local_file_utils.held_back_warning({Path("gen/regs.v"): ".gitignore:1: gen/"}),
        HELD_BACK_STOP,
    ]


async def test_a_change_to_a_held_back_file_the_root_filelist_names_verifies_it(
    action: ReviewAction, repo: Path, caplog
):
    _regenerated(repo, "fifo.v\ngen/regs.v\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--directory", str(repo), "--dry-run"], [])

    assert code == EXIT_SUCCESS
    i = caplog.messages.index("Verifying 1 file(s):")
    assert caplog.messages[i + 1] == "  gen/regs.v"
    assert any(
        m.startswith("Dry run: would upload 4 files (") and m.endswith("; verifying 1.") for m in caplog.messages
    )
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize("beside", ["fifo.v", "driver.c"])
async def test_a_held_back_file_the_root_filelist_names_is_verified_beside_the_rest_of_the_change(
    action: ReviewAction, repo: Path, caplog, beside: str
):
    """Beside C alone too, which wouldn't have the block read."""
    _commit(repo, "driver.c", "int main(void) { return 0; }\n")
    _regenerated(repo, "fifo.v\ngen/regs.v\n")
    _commit_all(repo, {beside: "// edited\n", "gen/regs.v": "module regs;\n  // edited\nendmodule\n"})

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--directory", str(repo), "--dry-run"], [])

    assert code == EXIT_SUCCESS
    i = caplog.messages.index("Verifying 2 file(s):")
    assert caplog.messages[i + 1 : i + 3] == [f"  {beside}", "  gen/regs.v"]


async def test_a_held_back_file_the_root_filelist_does_not_name_still_stops(action: ReviewAction, repo: Path, caplog):
    _regenerated(repo, "fifo.v\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, submit = await _run(action, ["--directory", str(repo), "--no-junit"], [])

    assert code == EXIT_USAGE_ERROR
    submit.assert_not_awaited()
    assert "Using the Verilog filelist committed at aristotle.f" in caplog.messages
    assert caplog.messages[-1] == HELD_BACK_STOP


async def test_a_change_with_another_source_to_verify_goes_on_naming_the_held_back_one(
    action: ReviewAction, repo: Path, caplog
):
    _commit(repo, "driver.c", "int main(void) { return 0; }\n")
    _regenerated(repo)
    _commit_all(repo, {"driver.c": "// edited\n", "gen/regs.v": "module regs;\n  // edited\nendmodule\n"})

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--directory", str(repo), "--dry-run"], [])

    assert code == EXIT_SUCCESS
    i = caplog.messages.index("Verifying 1 file(s):")
    assert caplog.messages[i + 1] == "  driver.c"
    assert local_file_utils.held_back_warning({Path("gen/regs.v"): ".gitignore:1: gen/"}) in caplog.messages


@pytest.mark.parametrize(("pattern", "stops"), [("**/*.v", True), ("fifo.v", False)])
async def test_a_path_filter_decides_whether_a_held_back_change_stops_the_review(
    action: ReviewAction, repo: Path, caplog, pattern: str, stops: bool
):
    _regenerated(repo)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--directory", str(repo), "--path", pattern, "--no-junit"], [])

    if stops:
        assert code == EXIT_USAGE_ERROR
        assert caplog.messages[-1] == HELD_BACK_STOP.replace("touched", "touched matching **/*.v")
    else:
        assert code == EXIT_SUCCESS
        assert caplog.messages[-1] == "This change touched no verifiable source file matching fifo.v. Nothing to do."


async def test_a_change_to_a_lean_build_file_whose_lean_files_an_ignore_rule_holds_back_stops(
    action: ReviewAction, repo: Path, caplog
):
    _commit(repo, "Proofs/lean-toolchain", "leanprover/lean4:v4.21.0\n")
    _commit(repo, "Proofs/Basic.lean", "theorem two : 1 + 1 = 2 := rfl\n")
    _commit(repo, ".gitignore", "*.lean\n")
    _commit(repo, "Proofs/lean-toolchain", "leanprover/lean4:v4.22.0\n")

    code, _ = await _run(action, ["--directory", str(repo), "--no-junit"], [])

    assert code == EXIT_USAGE_ERROR
    assert caplog.messages[-1] == HELD_BACK_STOP.replace("gen/regs.v", "Proofs/Basic.lean")


@NAMED
async def test_a_change_only_to_a_held_back_file_a_black_box_leaves_out_has_nothing_to_verify(
    action: ReviewAction, repo: Path, caplog, named: bool
):
    """As docs/verilog.md advises for a model that mustn't be sent: an ignore rule beside its --blackbox."""
    _blackboxed(repo, named)
    _commit(repo, ".gitignore", "vendor/\n")
    _commit(repo, "vendor/sram_1p.v", "module sram_1p;\n  // edited\nendmodule\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, submit = await _run(action, ["--directory", str(repo), "--no-junit"], [])

    assert code == EXIT_SUCCESS
    submit.assert_not_awaited()
    assert caplog.messages[-1] == (
        "This change touched only source files that a --blackbox line in aristotle.f leaves out of the upload: "
        "vendor/sram_1p.v. Nothing to do."
    )
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_a_held_back_file_a_black_box_leaves_out_does_not_cover_one_it_does_not(
    action: ReviewAction, repo: Path, caplog
):
    _blackboxed(repo, named=False)
    _commit(repo, "vendor/regs.v", "module regs;\nendmodule\n")
    _commit(repo, ".gitignore", "vendor/\n")
    _commit_all(repo, {"vendor/sram_1p.v": "module sram_1p;\nendmodule\n// edited\n", "vendor/regs.v": "// edited\n"})

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--directory", str(repo), "--no-junit"], [])

    assert code == EXIT_USAGE_ERROR
    assert caplog.messages[-1] == HELD_BACK_STOP.replace("gen/regs.v", "vendor/regs.v")


@pytest.mark.parametrize("beside", [False, True], ids=["alone", "beside fifo.v"])
async def test_a_held_back_file_sharing_a_black_box_with_another_module_is_held_back_not_black_boxed(
    action: ReviewAction, repo: Path, caplog, beside: bool
):
    """It goes up neither way, so it stops the review only when nothing else would be verified."""
    _blackboxed(repo, named=False)
    _commit(repo, ".gitignore", "vendor/\n")
    edits = {"vendor/sram_1p.v": "module sram_1p;\nendmodule\nmodule sram_ctrl;\nendmodule\n"}
    _commit_all(repo, {**edits, **({"fifo.v": "module fifo;\n  // edited\nendmodule\n"} if beside else {})})

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--directory", str(repo), "--dry-run"], [])

    if beside:
        assert code == EXIT_SUCCESS
        i = caplog.messages.index("Verifying 1 file(s):")
        assert caplog.messages[i + 1] == "  fifo.v"
        assert (
            local_file_utils.held_back_warning({Path("vendor/sram_1p.v"): ".gitignore:1: vendor/"}) in caplog.messages
        )
    else:
        assert code == EXIT_USAGE_ERROR
        assert caplog.messages[-1] == HELD_BACK_STOP.replace("gen/regs.v", "vendor/sram_1p.v")


async def test_paths_matching_a_held_back_file_sharing_a_black_box_with_another_module_go_on(
    action: ReviewAction, repo: Path, caplog
):
    _blackboxed(repo, named=False)
    _commit(repo, "vendor/models.v", "module sram_1p;\nendmodule\nmodule sram_2p;\nendmodule\n")
    _commit(repo, ".gitignore", "vendor/models.v\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(
            action, ["--scope", "paths", "--path", "**/*.v", "--directory", str(repo), "--dry-run"], []
        )

    assert code == EXIT_SUCCESS
    i = caplog.messages.index("Verifying 1 file(s):")
    assert caplog.messages[i + 1] == "  fifo.v"


async def test_paths_verify_a_held_back_file_the_root_filelist_names_with_the_rest_they_match(
    action: ReviewAction, repo: Path, caplog
):
    _regenerated(repo, "fifo.v\ngen/regs.v\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(
            action, ["--scope", "paths", "--path", "**/*.v", "--directory", str(repo), "--dry-run"], []
        )

    assert code == EXIT_SUCCESS
    i = caplog.messages.index("Verifying 2 file(s):")
    assert caplog.messages[i + 1 : i + 3] == ["  fifo.v", "  gen/regs.v"]


async def test_paths_whose_only_file_to_verify_an_ignore_rule_holds_back_stop_naming_it(
    action: ReviewAction, repo: Path, caplog
):
    """The other file --path matches is black-boxed."""
    _blackboxed(repo)
    _commit(repo, "vendor/gen/regs.v", "module regs;\nendmodule\n")
    _commit(repo, ".gitignore", "vendor/gen/\n")

    code, submit = await _run(
        action, ["--scope", "paths", "--path", "vendor", "--directory", str(repo), "--dry-run"], []
    )

    assert code == EXIT_USAGE_ERROR
    submit.assert_not_awaited()
    assert caplog.messages[-1] == (
        "Nothing --path matches would be verified: an ignore rule holds back vendor/gen/regs.v. To verify one, upload "
        "it as the warning above says, or, if it is Verilog, name it in a root aristotle.f that git tracks. To keep one "
        "out of the review on purpose, give --path globs that don't match it."
    )


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
async def test_a_held_back_link_to_a_changed_file_verifies_that_file_once(action: ReviewAction, repo: Path, caplog):
    _commit(repo, ".gitignore", "gen/\n")
    (repo / "gen").mkdir()
    (repo / "gen" / "fifo.v").symlink_to(Path("..", "fifo.v"))
    _commit_all(repo, {"fifo.v": "module fifo;\n  // edited\nendmodule\n", "aristotle.f": "gen/fifo.v\n"})
    git(repo, "add", "-f", "gen/fifo.v")
    git(repo, "commit", "--amend", "-qm", "a link to fifo.v the root aristotle.f names")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--directory", str(repo), "--dry-run"], [])

    assert code == EXIT_SUCCESS
    i = caplog.messages.index("Verifying 1 file(s):")
    assert caplog.messages[i + 1] == "  fifo.v"


async def test_a_review_of_no_verilog_reads_no_block(action: ReviewAction, repo: Path, caplog):
    """Only a review of the whole checkout, or of Verilog, sends the design aristotle.f describes."""
    _commit(repo, "aristotle.f", "fifo.v\n")
    _commit(repo, "driver.c", "int main(void) { return 0; }\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(
            action, ["--scope", "paths", "--path", "driver.c", "--directory", str(repo), "--dry-run"], []
        )

    assert code == EXIT_SUCCESS
    assert "Using the Verilog filelist" not in caplog.text


@pytest.mark.parametrize(
    ("argv", "warned"),
    [(["--scope", "all"], True), (["--scope", "paths", "--path", "driver.c"], False)],
    ids=["the whole checkout", "no verilog"],
)
async def test_a_root_aristotle_f_git_doesnt_track_is_named_where_a_tracked_one_would_be_read(
    action: ReviewAction, repo: Path, caplog, argv: list[str], warned: bool
):
    _commit(repo, "driver.c", "int main(void) { return 0; }\n")
    (repo / "aristotle.f").write_text("fifo.v\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, [*argv, "--directory", str(repo), "--dry-run"], [])

    assert code == EXIT_SUCCESS
    assert (UNTRACKED in caplog.messages) is warned
    assert "Using the Verilog filelist" not in caplog.text


async def test_a_root_filelist_the_checkout_lost_stops_the_step(action: ReviewAction, repo: Path, caplog):
    """It describes the block, so the run doesn't go on without it."""
    _commit(repo, "aristotle.f", "fifo.v\n")
    (repo / "aristotle.f").unlink()

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--scope", "all", "--directory", str(repo), "--dry-run"], [])

    assert code == EXIT_USAGE_ERROR
    assert caplog.messages[-1] == (
        "aristotle.f is tracked, but the checkout has no file there, so the block it describes can't be read."
    )
    assert "Using the Verilog filelist" not in caplog.text


async def test_a_manifest_variable_is_ignored_with_a_warning(action: ReviewAction, repo: Path, monkeypatch, caplog):
    monkeypatch.setenv("ARISTOTLE_MANIFEST", "hw/aristotle.toml")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(action, ["--scope", "all", "--directory", str(repo), "--dry-run"], [])

    assert code == EXIT_SUCCESS
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == [
        "ARISTOTLE_MANIFEST configures `aristotle ci`; `aristotle review` reads no manifest and ignores it."
    ]


async def test_a_dry_run_counts_only_what_would_be_uploaded(action: ReviewAction, repo: Path, caplog):
    _commit(repo, ".env", "SECRET=hunter2\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        await _run(action, ["--scope", "all", "--directory", str(repo), "--dry-run"], [])

    assert "Excluded 1 file(s) matching secret patterns: .env" in caplog.text
    assert re.search(r"Dry run: would upload 1 file \(\d+ bytes\); verifying 1\.", caplog.text)


async def test_a_dry_run_leaves_out_tracked_aws_credentials(action: ReviewAction, repo: Path, caplog):
    _commit(repo, ".aws/credentials", "[default]\naws_access_key_id = EXAMPLEINVALID\n")
    _commit(repo, "Proofs/credentials_test.lean", "theorem t : True := trivial\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        await _run(action, ["--scope", "all", "--directory", str(repo), "--dry-run"], [])

    assert "Excluded 1 file(s) matching secret patterns: .aws/credentials" in caplog.text
    assert re.search(r"Dry run: would upload 2 files \(\d+ bytes\); verifying 2\.", caplog.text)


async def test_a_dry_run_leaves_out_database_cloud_cluster_and_terraform_credentials_at_any_depth(
    action: ReviewAction, repo: Path, caplog
):
    names = [".pgpass", ".my.cnf", ".htpasswd", "_netrc", ".vault-token", ".s3cfg", ".boto", "kubeconfig"]
    names += ["terraform.tfstate", "terraform.tfstate.backup", "terraform.tfstate.1760000000.backup", "prod.tfstate"]
    names += ["prod.tfvars", "terraform.tfvars.json"]
    paths = [path for name in names for path in (name, f"deploy/prod/{name}")]
    _commit_all(repo, dict.fromkeys(paths, "dummy, not a credential\n"))

    with caplog.at_level(logging.INFO, logger="aristotle"):
        await _run(action, ["--scope", "all", "--directory", str(repo), "--dry-run"], [])

    [message] = [m for m in caplog.messages if "matching secret patterns" in m]
    header, *listed = message.split("\n  ")
    assert header == f"Excluded {len(paths)} file(s) matching secret patterns:"
    assert sorted(listed) == sorted(paths)
    assert re.search(r"Dry run: would upload 1 file \(\d+ bytes\); verifying 1\.", caplog.text)


@pytest.mark.parametrize(
    ("access", "causes"),
    [
        ({"Verilog": True}, ""),
        ({"Verilog": False}, " Likely cause: Verilog is not enabled for this account, so Aristotle left it out."),
        ({"Verilog": None}, ""),
    ],
    ids=["on", "off", "unknown"],
)
async def test_a_run_with_no_properties_warns_naming_a_language_only_where_aristotle_says_it_is_off(
    action: ReviewAction, repo: Path, caplog, access: dict[str, bool | None], causes: str
):
    with caplog.at_level(logging.WARNING, logger="aristotle"):
        code, _ = await _run(action, ["--scope", "all", "--directory", str(repo), "--no-junit"], [], access=access)

    assert code == EXIT_SUCCESS
    assert caplog.messages == [
        f"The run registered no properties, so nothing was verified.{causes} --fail-on no-verdict fails a run with "
        f"no properties. See the run's summary at {_project().web_url()}"
    ]


async def test_submit_receives_the_instructions_and_no_manifest(action: ReviewAction, repo: Path):
    _, submit = await _run(
        action,
        ["--scope", "all", "--directory", str(repo), "--instructions", "Skip the legacy bus.", "--no-junit"],
        [make_declaration("Spec.A")],
    )

    assert submit.await_args.kwargs["extra_instructions"] == "Skip the legacy bus."
    assert submit.await_args.kwargs["manifest"] is None


async def test_the_time_budget_is_sixty_minutes_when_unset(action: ReviewAction, repo: Path, monkeypatch):
    monkeypatch.setattr("aristotlelib.cli.command.ci.time.monotonic", lambda: 0.0)
    args = action.parser.parse_args(["--scope", "all", "--directory", str(repo), "--no-junit"])

    with (
        patch("aristotlelib.ci.runner.submit", AsyncMock(return_value=_project())),
        patch("aristotlelib.ci.runner.wait_for_completion", AsyncMock(return_value=TaskStatus.COMPLETE)) as wait,
        patch(
            "aristotlelib.ci.runner.language_access",
            AsyncMock(side_effect=lambda languages: dict.fromkeys(languages, True)),
        ),
        patch.object(Project, "get_declarations", AsyncMock(return_value=[])),
    ):
        await action.run_action(args)

    assert args.timeout is None
    assert wait.await_args.kwargs["timeout_seconds"] == 60 * 60


async def test_no_run_slot_is_rate_limited_even_though_review_is_advisory(
    action: ReviewAction, repo: Path, tmp_path: Path, caplog
):
    report = tmp_path / "out.xml"
    args = action.parser.parse_args(["--scope", "all", "--directory", str(repo), "--junit", str(report)])

    with (
        patch("aristotlelib.ci.runner.submit", AsyncMock(side_effect=runner.SlotTimeoutError("No run slot."))),
        patch("aristotlelib.ci.runner.wait_for_completion", AsyncMock()) as wait,
    ):
        code = await action.run_action(args)

    assert code == EXIT_RATE_LIMITED
    assert "No run slot." in caplog.text
    assert not report.exists()
    wait.assert_not_awaited()


async def test_a_reporter_that_raises_does_not_change_the_exit_code(
    action: ReviewAction, repo: Path, tmp_path: Path, caplog
):
    with (
        patch("aristotlelib.cli.command.ci.console.report", side_effect=ValueError("broken reporter")),
        caplog.at_level(logging.WARNING),
    ):
        code, _ = await _run(
            action,
            ["--scope", "all", "--directory", str(repo), "--fail-on", "failure", "--junit", str(tmp_path / "out.xml")],
            [make_declaration("Spec.A", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE)],
        )

    assert code == EXIT_VERIFICATION_FAILED
    assert "broken reporter" in caplog.text
    assert "Spec.A" in (tmp_path / "out.xml").read_text()


def test_environment_variables_supply_the_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARISTOTLE_SCOPE", "all")
    monkeypatch.setenv("ARISTOTLE_FAIL_ON", "no-verdict")
    monkeypatch.setenv("ARISTOTLE_TIMEOUT", "45")
    monkeypatch.setenv("ARISTOTLE_JUNIT", "reports/aristotle.xml")
    monkeypatch.setenv("ARISTOTLE_PATHS", "rtl/**\nsrc/*.c, lib/*.c")
    monkeypatch.setenv("ARISTOTLE_INSTRUCTIONS", "Skip the legacy bus.")

    args = _action().parser.parse_args([])

    assert str(args.scope) == "all"
    assert str(args.fail_on) == "no-verdict"
    assert args.timeout == 45
    assert args.junit == Path("reports/aristotle.xml")
    assert args.paths == ["rtl/**", "src/*.c", "lib/*.c"]
    assert args.instructions == "Skip the legacy bus."


def test_aristotle_fail_on_takes_unresolved_as_the_old_name_of_no_verdict(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARISTOTLE_FAIL_ON", "unresolved")

    assert _action().parser.parse_args([]).fail_on is results.FailOn.NO_VERDICT


def test_a_path_flag_replaces_the_environment_globs_rather_than_adding_to_them(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARISTOTLE_PATHS", "rtl/**")

    assert _action().parser.parse_args(["--path", "c/**", "--path", "d/**"]).paths == ["c/**", "d/**"]


def test_an_empty_environment_variable_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch):
    # CI systems substitute unset inputs as the empty string.
    for name in ("ARISTOTLE_SCOPE", "ARISTOTLE_PATHS", "ARISTOTLE_DIRECTORY", "ARISTOTLE_FAIL_ON", "ARISTOTLE_TIMEOUT"):
        monkeypatch.setenv(name, "")

    args = _action().parser.parse_args([])

    assert str(args.scope) == "changed"
    assert args.paths is None
    assert args.directory is None
    assert str(args.fail_on) == "never"
    assert args.timeout is None


def test_command_line_flags_override_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARISTOTLE_SCOPE", "all")
    monkeypatch.setenv("ARISTOTLE_FAIL_ON", "failure")

    args = _action().parser.parse_args(["--scope", "changed", "--fail-on", "never"])

    assert str(args.scope) == "changed"
    assert str(args.fail_on) == "never"


def test_on_timeouts_help_says_it_cancels_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COLUMNS", "2000")

    help_text = " ".join(_action().parser.format_help().split())

    assert "or the job is cancelled (default: cancel)." in help_text


async def test_a_stopped_run_with_no_properties_prints_its_questions_instead_of_the_warning(
    action: ReviewAction, repo: Path, tmp_path: Path, caplog
):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(
            action,
            ["--scope", "all", "--directory", str(repo), "--junit", str(tmp_path / "out.xml")],
            [],
            final_status=TaskStatus.COMPLETE_WITH_ERRORS,
            stopped_summary="Open questions:\n1. Which reset polarity is intended?",
        )

    # review's default --fail-on never leaves the build alone; the questions are still printed.
    assert code == EXIT_SUCCESS
    assert "    1. Which reset polarity is intended?" in caplog.text
    assert "registered no properties" not in caplog.text


async def test_a_held_back_file_whose_name_is_not_utf8_stops_naming_the_rename_too(
    action: ReviewAction, repo: Path, caplog
):
    (repo / ".gitignore").write_text("caf*.v\n")
    _commit(repo, ".gitignore", "caf*.v\n")
    latin1_name(repo)
    git(repo, "add", "-f", "-A")
    git(repo, "commit", "-qm", "a held-back Latin-1 name")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, submit = await _run(action, ["--directory", str(repo), "--fail-on", "no-verdict", "--no-junit"], [])

    assert code == EXIT_USAGE_ERROR
    submit.assert_not_awaited()
    assert caplog.messages[-1] == (
        "Nothing this change touched would be verified: an ignore rule holds back caf\\xe9.v. To verify one, upload it "
        "as the warning above says, or, if it is Verilog, name it in a root aristotle.f that git tracks, and rename "
        "caf\\xe9.v: its name isn't UTF-8. To keep one out of the review on purpose, give --path globs that don't "
        "match it."
    )
    assert "Rename them" not in caplog.text


async def test_the_files_left_out_for_their_names_are_reported_within_the_path_filter(
    action: ReviewAction, repo: Path, caplog
):
    _commit(repo, "rtl/alu.v", "module alu;\nendmodule\n")
    latin1_name(repo / "rtl")
    _commit(repo, "main.c", "int main(void) { return 0; }\n")
    git(repo, "add", "-A")
    git(repo, "commit", "--amend", "-qm", "a Latin-1 name in rtl, and a C file elsewhere")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(
            action, ["--directory", str(repo), "--path", "rtl", "--fail-on", "no-verdict", "--no-junit"], []
        )

    assert code == EXIT_VERIFICATION_FAILED
    assert (
        "This change touched only source files matching rtl whose names aren't UTF-8, so nothing was verified: "
        "rtl/caf\\xe9.v. Rename them to verify them." in caplog.messages
    )


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
async def test_a_dry_run_whose_files_to_verify_cannot_be_uploaded_is_a_usage_error(
    action: ReviewAction, repo: Path, caplog
):
    (repo / "dangling.v").symlink_to(repo / "missing.v")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "a dangling link")

    with caplog.at_level(logging.ERROR, logger="aristotle"):
        code, _ = await _run(
            action, ["--scope", "paths", "--path", "dangling.v", "--directory", str(repo), "--dry-run"], []
        )

    assert code == EXIT_USAGE_ERROR
    assert caplog.messages == ["None of the files to verify could be uploaded; the warnings above say why."]


async def test_a_review_of_headers_asks_about_the_language_aristotle_finds_in_the_rest_of_the_upload(
    action: ReviewAction, repo: Path
):
    _commit(repo, "fifo.c", "int push(void) { return 0; }\n")
    _commit(repo, "fifo.h", "int push(void);\n")
    language_access = AsyncMock(return_value={"C": False})
    args = action.parser.parse_args(["--directory", str(repo), "--no-junit"])

    with (
        patch("aristotlelib.ci.runner.submit", AsyncMock(return_value=_project())),
        patch("aristotlelib.ci.runner.wait_for_completion", AsyncMock(return_value=TaskStatus.COMPLETE)),
        patch("aristotlelib.ci.runner.fetch_stopped_summary", AsyncMock(return_value=None)),
        patch("aristotlelib.ci.runner.language_access", language_access),
        patch.object(Project, "get_declarations", AsyncMock(return_value=[])),
    ):
        await action.run_action(args)

    language_access.assert_awaited_once_with(["C"])


def _subproject(repo: Path) -> None:
    """A subproject in hw/ whose aristotle.f names gen/regs.v, a source the build writes, which an ignore rule keeps
    untracked, and whose -I folder is ../shared, outside the subproject, with the header regs.v includes."""
    _commit(repo, "shared/defs.vh", "`define W 8\n")
    (repo / "hw" / "gen").mkdir(parents=True)
    (repo / "hw" / "gen" / "regs.v").write_text('`include "defs.vh"\nmodule regs;\nendmodule\n')
    _commit(repo, "hw/.gitignore", "gen/\n")
    _commit(repo, "hw/aristotle.f", "+incdir+../shared\ngen/regs.v\n")


async def test_a_review_of_everything_verifies_the_files_allow_untracked_lets_through(
    action: ReviewAction, repo: Path, caplog
):
    _subproject(repo)
    argv = ["--scope", "all", "--directory", str(repo / "hw"), "--dry-run"]

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(
            action,
            [*argv, "--allow-untracked", str(repo / "hw" / "gen"), "--allow-untracked", str(repo / "shared")],
            [],
            preflight=True,
        )

    assert code == EXIT_SUCCESS
    i = caplog.messages.index("Verifying 2 file(s):")
    assert caplog.messages[i + 1 : i + 3] == ["  _ext/shared/defs.vh", "  gen/regs.v"]
    assert any(
        m.startswith("Uploading 2 files git doesn't track in the folder the step uploads") for m in caplog.messages
    )


async def test_without_allow_untracked_a_review_stops_on_a_generated_file_naming_it(
    action: ReviewAction, repo: Path, caplog
):
    _subproject(repo)

    code, _ = await _run(action, ["--scope", "all", "--directory", str(repo / "hw"), "--dry-run"], [], preflight=True)

    assert code == EXIT_USAGE_ERROR
    assert caplog.messages[-1].endswith(
        "isn't tracked by git, and `aristotle review` uploads only files git tracks there. Commit it, name a folder "
        "that holds it with --allow-untracked, or submit the block with `aristotle submit`."
    )


@pytest.mark.parametrize("allowed", [False, True], ids=["", "--allow-untracked"])
@pytest.mark.parametrize(("folder", "sibling"), WILDCARD_FOLDERS)
async def test_a_folder_whose_path_holds_a_wildcard_has_only_its_own_block_reviewed(
    action: ReviewAction, repo: Path, caplog, folder: str, sibling: str, allowed: bool
):
    """Not the block of a sibling folder that the path matches as a pattern, even one --allow-untracked names."""
    for name in (folder, sibling):
        _commit(repo, f"{name}/core.v", "module core;\nendmodule\n")
        _commit(repo, f"{name}/aristotle.f", "core.v\n")
    argv = ["--scope", "all", "--directory", str(repo / folder), "--dry-run"]

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _run(
            action, [*argv, *(["--allow-untracked", str(repo / sibling)] if allowed else [])], [], preflight=True
        )

    assert code == EXIT_SUCCESS
    assert (
        f"The Verilog block's 1 file:\n  core.v  (named at {repo / folder / 'aristotle.f'}:1)\n"
        "aristotle.f goes up as it is."
    ) in caplog.messages
    i = caplog.messages.index("Verifying 1 file(s):")
    assert caplog.messages[i + 1] == "  core.v"


async def test_a_review_naming_the_folder_it_uploads_for_allow_untracked_stops_before_resolving_the_scope(
    action: ReviewAction, repo: Path, caplog
):
    _subproject(repo)
    argv = ["--scope", "all", "--directory", str(repo / "hw"), "--allow-untracked", str(repo / "hw"), "--dry-run"]

    with patch("aristotlelib.ci.scope.resolve") as resolve:
        code, _ = await _run(action, argv, [], preflight=True)

    assert code == EXIT_USAGE_ERROR
    resolve.assert_not_called()
    assert caplog.messages[-1].startswith(
        f"--allow-untracked {repo / 'hw'} is the folder the step uploads, {repo / 'hw'}, "
    )


@pytest.mark.parametrize("value", NEVER_READ.values(), ids=NEVER_READ.keys())
@pytest.mark.parametrize("route", ["aristotle.f", "--allow-untracked"])
async def test_a_path_never_read_refuses_stops_the_review_before_anything_looks_it_up(
    action: ReviewAction, repo: Path, no_lookup_of, value: str, route: str, caplog
):
    argv = ["--scope", "all", "--directory", str(repo), "--dry-run"]
    if route == "aristotle.f":
        _commit(repo, "aristotle.f", f"fifo.v\n'{value}'\n")
        expected = f"{repo / 'aristotle.f'}:2: the file named is {filelist.never_read(value)}"
    else:
        _commit(repo, "aristotle.f", "fifo.v\n")
        argv += ["--allow-untracked", value]
        expected = f"--allow-untracked names {filelist.never_read(value)}."
    no_lookup_of(UNREACHABLE_HOST)

    code, submit = await _run(action, argv, [], preflight=True)

    assert code == EXIT_USAGE_ERROR
    submit.assert_not_awaited()
    assert caplog.messages[-1] == expected


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["--scope", "paths"], "--scope paths requires at least one --path pattern."),
        (
            ["--scope", "all", "--path", "rtl/**"],
            "--path narrows --scope changed or --scope paths; --scope all verifies everything.",
        ),
    ],
    ids=["paths without --path", "all with --path"],
)
def test_paths_the_scope_contradicts_are_a_usage_error_before_anything_else(action: ReviewAction, argv, error, capsys):
    with patch("aristotlelib.ci.scope.resolve") as resolve, pytest.raises(SystemExit) as exited:
        action.preflight(action.parser.parse_args(argv))

    assert exited.value.code == EXIT_USAGE_ERROR
    resolve.assert_not_called()
    assert capsys.readouterr().err.endswith(f"error: {error}\n")
