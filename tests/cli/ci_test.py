"""Tests for aristotlelib.cli.command.ci."""

import argparse
import asyncio
import contextlib
import dataclasses
import logging
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import NamedTuple
from unittest.mock import AsyncMock, Mock, patch

import pytest

from aristotlelib import local_file_utils
from aristotlelib.agent_task import AgentTask, TaskStatus
from aristotlelib.api_request import AristotleAPIError, AristotleNotFoundError, AristotleOutcomeUnknownError
from aristotlelib.ci import github, manifest, results, runner, scope
from aristotlelib.ci.record import RunRecord
from aristotlelib.cli.api_action import APIAction
from aristotlelib.cli.command.ci import (
    DEFAULT_JUNIT_PATH,
    DEFAULT_TIMEOUT_MINUTES,
    MAX_TIMEOUT_MINUTES,
    CIAction,
    _block_problem,
    _guarded,
    _holds,
    _identity,
    _likely_causes,
    _system_folder,
    _temporary_folders,
    allowed_folders,
)
from aristotlelib.cli.controller import main
from aristotlelib.declaration import Declaration, DeclarationEvidence, DeclarationFailureReason, DeclarationStatus
from aristotlelib.exit_codes import (
    EXIT_AUTH_ERROR,
    EXIT_INFRASTRUCTURE_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OUTCOME_UNKNOWN,
    EXIT_RATE_LIMITED,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    EXIT_VERIFICATION_FAILED,
)
from aristotlelib.printable import escape_logged_text
from aristotlelib.project import Project, ProjectStatus
from aristotlelib.verilog import bundle, filelist
from tests.ci.conftest import commit_bare_shaped, git, latin1_name, make_declaration
from tests.conftest import NEEDS_INT_DIGIT_LIMIT, NEVER_READ, UNREACHABLE_HOST, WILDCARD_FOLDERS

CI_VARIABLE_PREFIXES = ("GITHUB_", "GITLAB_", "CI_", "BUILDKITE", "CIRCLE", "BUILD_", "SYSTEM_", "JENKINS_")

MANIFEST = """\
[requested_properties.write_when_full]
kind = "natural_language"
property = "A write to a full FIFO is dropped."

[requested_properties.read_when_empty]
kind = "natural_language"
property = "A read from an empty FIFO returns no data."
"""
IDS = ["write_when_full", "read_when_empty"]
"""MANIFEST's ids, in file order, which is not sorted order."""

UNTRACKED = "aristotle.f isn't tracked by git, so `aristotle ci` doesn't read it or upload it. Commit it to use it."
"""The warning for a root aristotle.f git doesn't track."""


@pytest.fixture(autouse=True)
def _outside_ci(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run as if on a laptop in an empty directory, with no CI system's variables and no ARISTOTLE_* configuration."""
    for name in list(os.environ):
        if name.startswith((*CI_VARIABLE_PREFIXES, "ARISTOTLE_")) or name in (
            "CIRCLECI",
            "TF_BUILD",
            "CHANGE_ID",
            "JOB_NAME",
            "RUNNER_TEMP",
            "AGENT_TEMPDIRECTORY",
            "WORKSPACE",
        ):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


def _action() -> CIAction:
    """A ci action; its defaults are read from the environment as it is when this is called."""
    parser = argparse.ArgumentParser()
    action = CIAction(parser.add_subparsers())
    action.add_arguments()
    return action


@pytest.fixture
def action() -> CIAction:
    return _action()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with one Verilog file and MANIFEST committed as aristotle.toml."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "fifo.v").write_text("module fifo;\nendmodule\n")
    (root / manifest.MANIFEST_NAME).write_text(MANIFEST)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "initial")
    return root


def _commit(repo: Path, name: str, content: str) -> None:
    (repo / name).write_text(content)
    git(repo, "add", "-f", name)
    git(repo, "commit", "-qm", name)


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


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


def _declaration(
    property_id: str,
    status: DeclarationStatus = DeclarationStatus.PROVED,
    evidence: DeclarationEvidence | None = None,
) -> Declaration:
    """A property the run registered under the manifest id ``property_id``, with a name that differs from it."""
    return make_declaration(f"Fifo.{property_id}", status, evidence=evidence).model_copy(
        update={"declaration_id": property_id}
    )


def _testcases(report: Path) -> dict[str, ET.Element]:
    return {case.get("name", ""): case for case in ET.parse(report).getroot().iter("testcase")}


class Run(NamedTuple):
    code: int
    submit: AsyncMock
    wait: AsyncMock
    fetch_summary: AsyncMock
    language_access: AsyncMock


async def _run(
    action: CIAction,
    argv: list[str],
    declarations: list[Declaration] | None = None,
    final_status: TaskStatus | None = TaskStatus.COMPLETE,
    stopped_summary: str | None = None,
    summary: str | Exception | None = None,
    access: dict[str, bool | None] | None = None,
) -> Run:
    """Preflight then run, as run() does, with the network patched out; a final status of None is a timeout.

    ``summary`` is what fetching the run's summary returns, or raises. ``access`` is what the account's language
    check answers; by default every language it asks about is enabled.
    """
    args = action.parser.parse_args(argv)
    fetch_summary = (
        AsyncMock(side_effect=summary) if isinstance(summary, Exception) else AsyncMock(return_value=summary)
    )
    language_access = AsyncMock(
        side_effect=lambda languages: dict.fromkeys(languages, True) if access is None else access
    )
    with (
        patch("aristotlelib.cli.command.ci.runner.submit", AsyncMock(return_value=_project())) as submit,
        patch("aristotlelib.cli.command.ci.runner.wait_for_completion", AsyncMock(return_value=final_status)) as wait,
        patch("aristotlelib.cli.command.ci.runner.fetch_stopped_summary", AsyncMock(return_value=stopped_summary)),
        patch("aristotlelib.cli.command.ci.runner.fetch_summary", fetch_summary),
        patch("aristotlelib.cli.command.ci.runner.language_access", language_access),
        patch.object(Project, "get_declarations", AsyncMock(return_value=declarations or [])),
    ):
        code = action.preflight(args)
        if code is None:
            code = await action.run_action(args)
    return Run(code, submit, wait, fetch_summary, language_access)


async def test_without_a_manifest_ci_exits_2_before_resolving_the_checkout(action: CIAction, tmp_path: Path, caplog):
    """It names both ways to make one, as `ci setup` alone needs a terminal."""
    git(tmp_path, "init", "-q")
    with (
        patch("aristotlelib.cli.command.ci.scope.resolve_checkout") as resolve,
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        run = await _run(action, [])

    assert run.code == EXIT_USAGE_ERROR
    assert caplog.messages[1:] == [
        "No aristotle.toml found. Write one, or have `aristotle ci setup` propose one: at a terminal, or from a "
        "finished project with --from <project>.",
        f"Looked for {tmp_path.resolve() / manifest.MANIFEST_NAME}; pass --manifest to read another file.",
    ]
    resolve.assert_not_called()
    run.submit.assert_not_awaited()


async def test_outside_a_repository_ci_says_so_rather_than_that_it_has_no_manifest(
    action: CIAction, tmp_path: Path, caplog
):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(action, [])

    assert run.code == EXIT_USAGE_ERROR
    assert caplog.messages[-1].startswith(f"{tmp_path.resolve()} is not a git repository.")
    assert "No aristotle.toml found" not in caplog.text
    run.submit.assert_not_awaited()


async def test_a_manifest_flag_naming_a_missing_file_exits_2(action: CIAction, repo: Path, tmp_path: Path, caplog):
    missing = tmp_path / "missing.toml"

    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(action, ["--directory", str(repo), "--manifest", str(missing)])

    assert run.code == EXIT_USAGE_ERROR
    assert caplog.messages[1:] == [f"Manifest {missing} not found."]
    run.submit.assert_not_awaited()


@pytest.mark.parametrize("manifest_text", [None, "[requested_properties.x]\nkind = 'nl'\n"], ids=["missing", "invalid"])
async def test_review_variables_are_named_even_when_the_manifest_is_unusable(
    action: CIAction, repo: Path, monkeypatch: pytest.MonkeyPatch, caplog, manifest_text: str | None
):
    if manifest_text is None:
        (repo / manifest.MANIFEST_NAME).unlink()
    else:
        (repo / manifest.MANIFEST_NAME).write_text(manifest_text)
    monkeypatch.setenv("ARISTOTLE_INSTRUCTIONS", "Check the FIFO.")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(action, ["--directory", str(repo)])

    assert run.code == EXIT_USAGE_ERROR
    assert (
        "ARISTOTLE_INSTRUCTIONS configures `aristotle review`; `aristotle ci` checks the properties in the manifest "
        "and ignores it." in caplog.messages
    )


async def test_from_a_subfolder_ci_reads_the_manifest_at_the_repositorys_top(
    action: CIAction, repo: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    (repo / "rtl").mkdir()
    monkeypatch.chdir(repo / "rtl")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        assert action.preflight(action.parser.parse_args([])) is None

    assert f"Checking 2 properties from {repo.resolve() / manifest.MANIFEST_NAME}:" in caplog.messages


async def test_from_inside_a_subproject_ci_reads_its_own_manifest(
    action: CIAction, repo: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    (repo / "fifo").mkdir()
    (repo / "fifo" / manifest.MANIFEST_NAME).write_text(MANIFEST)
    (repo / "fifo" / "rtl").mkdir()
    monkeypatch.chdir(repo / "fifo" / "rtl")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        assert action.preflight(action.parser.parse_args([])) is None

    assert f"Checking 2 properties from {(repo / 'fifo').resolve() / manifest.MANIFEST_NAME}:" in caplog.messages


async def test_with_manifest_and_no_directory_ci_checks_the_repository_from_its_top(
    action: CIAction, repo: Path, monkeypatch: pytest.MonkeyPatch
):
    # No search: a subproject's manifest nearer the working directory doesn't make it the root.
    (repo / "fifo" / "rtl").mkdir(parents=True)
    (repo / "fifo" / manifest.MANIFEST_NAME).write_text(MANIFEST)
    monkeypatch.chdir(repo / "fifo" / "rtl")

    assert action.preflight(action.parser.parse_args(["--manifest", str(repo / manifest.MANIFEST_NAME)])) is None
    assert action._root == repo.resolve()


async def test_without_a_manifest_up_to_the_top_ci_says_where_it_looked(
    action: CIAction, repo: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    (repo / manifest.MANIFEST_NAME).unlink()
    (repo / "rtl").mkdir()
    monkeypatch.chdir(repo / "rtl")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        assert action.preflight(action.parser.parse_args([])) == EXIT_USAGE_ERROR

    assert caplog.messages[-1] == (
        f"Looked in {(repo / 'rtl').resolve()} and each folder above it up to {repo.resolve()}; "
        "pass --manifest to read another file."
    )


@pytest.mark.parametrize(
    ("text", "given", "hinted"),
    [
        ('[tool]\nname = "a different aristotle"\n', False, True),
        ('[tool]\nname = "a different aristotle"\n', True, False),
        (MANIFEST + "severity = 1\n", False, False),
    ],
    ids=["another tool's file", "--manifest", "a manifest with a mistake"],
)
async def test_a_file_that_is_no_manifest_suggests_manifest_unless_one_was_given(
    action: CIAction, repo: Path, caplog, text: str, given: bool, hinted: bool
):
    (repo / manifest.MANIFEST_NAME).write_text(text)
    argv = ["--directory", str(repo)] + (["--manifest", str(repo / manifest.MANIFEST_NAME)] if given else [])

    with caplog.at_level(logging.INFO, logger="aristotle"):
        assert action.preflight(action.parser.parse_args(argv)) == EXIT_USAGE_ERROR

    hint = f"If {repo.resolve() / manifest.MANIFEST_NAME} is not Aristotle's manifest, pass --manifest to read the one that is."
    assert (hint in caplog.messages) is hinted


async def test_an_invalid_manifest_exits_2_naming_each_bad_key(action: CIAction, repo: Path, caplog):
    (repo / manifest.MANIFEST_NAME).write_text(
        '[requested_properties.write_when_full]\nkind = "natural_language"\nproperty = "Dropped."\nseverity = 1\n\n'
        '[requested_properties.read_when_empty]\nkind = "lean"\nproperty = "No data."\n'
    )

    with (
        patch("aristotlelib.cli.command.ci.scope.resolve_checkout") as resolve,
        caplog.at_level(logging.ERROR, logger="aristotle"),
    ):
        run = await _run(action, ["--directory", str(repo)])

    assert run.code == EXIT_USAGE_ERROR
    [message] = caplog.messages
    assert message.startswith(f"{repo.resolve() / manifest.MANIFEST_NAME} is not valid:\n")
    assert "\n  requested_properties.write_when_full.severity: unknown key" in message
    assert "\n  requested_properties.read_when_empty.kind: 'lean' is not one of" in message
    resolve.assert_not_called()
    run.submit.assert_not_awaited()


@pytest.mark.parametrize(
    "first_line",
    [
        pytest.param(f"repo_id = {'9' * 5000}", id="integer-past-the-digit-limit", marks=NEEDS_INT_DIGIT_LIMIT),
        pytest.param("repo_id = " + "[" * 5000 + "]" * 5000, id="nesting-past-the-stack"),
    ],
)
def test_a_manifest_too_large_for_the_toml_reader_exits_2_naming_it(
    action: CIAction, repo: Path, caplog, first_line: str
):
    (repo / manifest.MANIFEST_NAME).write_text(f"{first_line}\n\n{MANIFEST}")
    args = action.parser.parse_args(["--directory", str(repo)], argparse.Namespace(api_key=None))

    with patch("aristotlelib.cli.api_action.get_api_key", side_effect=ValueError("no key")) as get_api_key:
        code = action.run(args)

    assert code == EXIT_USAGE_ERROR
    assert caplog.messages[-1].startswith(f"{repo.resolve() / manifest.MANIFEST_NAME} is not valid TOML: ")
    get_api_key.assert_not_called()


@pytest.mark.parametrize("from_environment", [False, True], ids=["--manifest", "ARISTOTLE_MANIFEST"])
async def test_a_manifest_named_elsewhere_replaces_the_repositorys(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog, from_environment: bool
):
    other = tmp_path / "checks.toml"
    other.write_text('[requested_properties.no_x]\nkind = "sva"\nidentifier = "fifo.no_x"\n')
    argv = ["--directory", str(repo)]
    if from_environment:
        monkeypatch.setenv("ARISTOTLE_MANIFEST", str(other))
    else:
        argv += ["--manifest", str(other)]

    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(_action(), argv, [_declaration("no_x")])

    assert run.code == EXIT_SUCCESS
    assert run.submit.await_args.kwargs["manifest"] == manifest.load(other)
    assert caplog.messages[1:3] == [f"Checking 1 property from {other}:", "  no_x  (sva)"]


async def test_submit_checks_the_loaded_manifest_without_instructions(
    action: CIAction, repo: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    monkeypatch.setenv("ARISTOTLE_INSTRUCTIONS", "Skip the legacy bus.")
    path = repo.resolve() / manifest.MANIFEST_NAME

    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(action, ["--directory", str(repo)], [_declaration(i) for i in IDS])

    assert run.code == EXIT_SUCCESS
    run.submit.assert_awaited_once()
    assert run.submit.await_args.kwargs["manifest"] == manifest.load(path)
    assert run.submit.await_args.kwargs["extra_instructions"] is None
    assert caplog.messages[2:5] == [
        f"Checking 2 properties from {path}:",
        "  write_when_full  (natural_language)",
        "  read_when_empty  (natural_language)",
    ]
    assert "not yet enforced" not in caplog.text


def test_keys_accepted_but_not_yet_enforced_are_logged(action: CIAction, repo: Path, caplog):
    # The appended key lands in the last table, read_when_empty.
    (repo / manifest.MANIFEST_NAME).write_text(
        "[check]\nmax_cost_dollars = 5\n\n" + MANIFEST + 'property_result_id = "result-1"\n'
    )

    with caplog.at_level(logging.INFO, logger="aristotle"):
        assert action.preflight(action.parser.parse_args(["--directory", str(repo)])) is None

    assert "Accepted but not yet enforced: check.max_cost_dollars, property_result_id (1 property)." in caplog.messages


@pytest.mark.parametrize(
    ("variables", "warnings"),
    [
        (
            {"ARISTOTLE_INSTRUCTIONS": "Skip the legacy bus."},
            [
                "ARISTOTLE_INSTRUCTIONS configures `aristotle review`; "
                "`aristotle ci` checks the properties in the manifest and ignores it."
            ],
        ),
        (
            {"ARISTOTLE_BASE_REF": "main", "ARISTOTLE_SCOPE": "all"},
            [
                "ARISTOTLE_SCOPE, ARISTOTLE_BASE_REF configure `aristotle review`; "
                "`aristotle ci` checks the properties in the manifest and ignores them."
            ],
        ),
        ({}, []),
        ({"ARISTOTLE_PATHS": ""}, []),
    ],
    ids=["one", "two", "none", "empty is unset"],
)
def test_review_variables_are_named_in_a_warning(
    action: CIAction,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog,
    variables: dict[str, str],
    warnings: list[str],
):
    for name, value in variables.items():
        monkeypatch.setenv(name, value)

    with caplog.at_level(logging.WARNING, logger="aristotle"):
        assert action.preflight(action.parser.parse_args(["--directory", str(repo)])) is None

    assert caplog.messages == warnings


@pytest.mark.parametrize(
    ("argv", "environment", "check", "budget_seconds"),
    [
        (["--timeout", "10"], "20", "max_wall_time_seconds = 1800", 10 * 60),
        ([], "20", "max_wall_time_seconds = 1800", 20 * 60),
        ([], None, "max_wall_time_seconds = 1800", 30 * 60),
        ([], None, "", 60 * 60),
    ],
    ids=["--timeout", "ARISTOTLE_TIMEOUT", "check.max_wall_time_seconds", "default"],
)
async def test_the_time_budget_is_the_flag_else_the_environment_else_the_manifest_else_an_hour(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog,
    argv: list[str],
    environment: str | None,
    check: str,
    budget_seconds: int,
):
    (repo / manifest.MANIFEST_NAME).write_text(f"[check]\n{check}\n\n{MANIFEST}")
    if environment is not None:
        monkeypatch.setenv("ARISTOTLE_TIMEOUT", environment)
    monkeypatch.setattr("aristotlelib.cli.command.ci.time.monotonic", lambda: 0.0)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(_action(), ["--directory", str(repo), *argv])

    assert run.wait.await_args.kwargs["timeout_seconds"] == budget_seconds
    from_manifest = "Time budget: 30 min, from check.max_wall_time_seconds." in caplog.messages
    assert from_manifest == (budget_seconds == 30 * 60)


async def test_every_manifest_property_proved_passes(action: CIAction, repo: Path):
    run = await _run(action, ["--directory", str(repo)], [_declaration(i) for i in IDS])

    assert run.code == EXIT_SUCCESS


async def test_a_counterexample_to_a_manifest_property_fails_the_build(action: CIAction, repo: Path):
    run = await _run(
        action,
        ["--directory", str(repo)],
        [
            _declaration("write_when_full"),
            _declaration("read_when_empty", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE),
        ],
    )

    assert run.code == EXIT_VERIFICATION_FAILED


@pytest.mark.parametrize(
    ("fail_on", "expected"),
    # unresolved, no-verdict's old name, still fails the build the same way.
    [("failure", EXIT_SUCCESS), ("no-verdict", EXIT_VERIFICATION_FAILED), ("unresolved", EXIT_VERIFICATION_FAILED)],
)
async def test_a_manifest_id_the_run_registered_nothing_under_has_no_verdict(
    action: CIAction, repo: Path, caplog, fail_on: str, expected: int
):
    with caplog.at_level(logging.WARNING, logger="aristotle"):
        run = await _run(action, ["--directory", str(repo), "--fail-on", fail_on], [_declaration("write_when_full")])

    assert run.code == expected
    assert caplog.messages == [
        "Aristotle registered no property under the manifest id read_when_empty, or withdrew it, "
        "so it is reported without a verdict.",
        (
            "1 of the manifest's 2 properties have no verdict: read_when_empty. The build still passes: "
            "--fail-on failure doesn't fail it on a property without a verdict, and --fail-on no-verdict does."
            if fail_on == "failure"
            # The summary line counts it as in progress, but the error says why the build fails either way.
            else "1 of the manifest's 2 properties have no verdict: read_when_empty. --fail-on no-verdict fails the "
            "build on a property without one."
        ),
    ]


@pytest.mark.parametrize(
    ("final_status", "stopped_summary", "stop"),
    [
        (TaskStatus.OUT_OF_BUDGET, None, "it ran out of budget"),
        (TaskStatus.FAILED, None, "it failed due to an internal error"),
        (TaskStatus.CANCELED, None, "it was cancelled outside this job"),
        (TaskStatus.COMPLETE_WITH_ERRORS, "Which reset?", "Aristotle stopped it for decisions only you can make"),
    ],
    ids=["out of budget", "failed", "cancelled", "stopped for questions"],
)
async def test_a_manifest_id_a_stopped_run_has_nothing_under_is_reported_with_when_it_stopped(
    action: CIAction, repo: Path, tmp_path: Path, caplog, final_status, stopped_summary, stop
):
    with caplog.at_level(logging.WARNING, logger="aristotle"):
        await _run(
            action,
            ["--directory", str(repo)],
            [_declaration("write_when_full")],
            final_status=final_status,
            stopped_summary=stopped_summary,
        )

    warning = (
        f"The run had no property under the manifest id read_when_empty when {stop}, "
        "so it is reported without a verdict."
    )
    assert warning in caplog.messages
    skipped = _testcases(tmp_path / DEFAULT_JUNIT_PATH)["read_when_empty"].find("skipped")
    assert skipped is not None and skipped.get("message") == f"The run had no property under this id when {stop}."


@pytest.mark.parametrize(
    "declarations",
    [[], [_declaration("write_when_full", DeclarationStatus.IN_PROGRESS), _declaration("elsewhere")]],
    ids=["nothing registered", "no verdict, a suggestion proved"],
)
async def test_a_run_that_verified_no_manifest_property_fails_and_names_the_likely_causes(
    action: CIAction, repo: Path, tmp_path: Path, caplog, declarations: list[Declaration]
):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(action, ["--directory", str(repo), "--junit", str(tmp_path / "out.xml")], declarations)

    assert run.code == EXIT_VERIFICATION_FAILED
    error = next(r.message for r in caplog.records if r.message.startswith("No property in the manifest was verified"))
    assert "enabled" not in error
    assert error.endswith(_project().web_url())
    run.language_access.assert_awaited_once_with(["Verilog"])


@pytest.mark.parametrize(
    ("access", "causes"),
    [
        (
            {"Verilog": False},
            "Likely causes: Verilog is not enabled for this account, so Aristotle left it out; or Aristotle "
            "registered nothing under the manifest's ids.",
        ),
        ({"Verilog": None}, "Likely cause: Aristotle registered nothing under the manifest's ids."),
        ({"Verilog": True}, "Likely cause: Aristotle registered nothing under the manifest's ids."),
    ],
    ids=["off", "unknown", "on"],
)
async def test_a_language_is_a_cause_only_where_aristotle_says_it_is_off(
    action: CIAction, repo: Path, caplog, access: dict[str, bool | None], causes: str
):
    with caplog.at_level(logging.ERROR, logger="aristotle"):
        await _run(action, ["--directory", str(repo), "--no-junit"], [], access=access)

    assert caplog.messages == [
        f"No property in the manifest was verified. {causes} See the run's summary at {_project().web_url()}"
    ]


DECISION = "a property needs a decision only you can make, which its description names"


def _failed(property_id: str, reason: DeclarationFailureReason) -> Declaration:
    return make_declaration(f"Fifo.{property_id}", DeclarationStatus.FAILED, failure_reason=reason).model_copy(
        update={"declaration_id": property_id}
    )


@pytest.mark.parametrize(
    ("declarations", "expected"),
    [
        ([], ["Aristotle registered nothing under the manifest's ids"]),
        ([_declaration(i, DeclarationStatus.IN_PROGRESS) for i in IDS], [DECISION]),
        (
            [_declaration("write_when_full", DeclarationStatus.IN_PROGRESS)],
            ["Aristotle registered nothing under some of the manifest's ids", DECISION],
        ),
        ([_failed(i, DeclarationFailureReason.OUT_OF_BUDGET) for i in IDS], ["Aristotle ran out of budget first"]),
        (
            [_failed(i, DeclarationFailureReason.SYSTEM_ERROR) for i in IDS],
            ["Aristotle stopped short of a verdict, for the reason the property's result names"],
        ),
        (
            [_failed(i, DeclarationFailureReason.PROOF_CHECK_FAILED) for i in IDS],
            ["a proof Aristotle had no longer checks, so a re-run should re-prove it"],
        ),
    ],
    ids=[
        "nothing registered",
        "all registered, none settled",
        "some registered",
        "out of budget",
        "system error",
        "proof no longer checks",
    ],
)
def test_the_likely_causes_leave_out_what_the_results_rule_out(declarations, expected: list[str]):
    requested = manifest.parse(manifest.tomllib.loads(MANIFEST), Path("aristotle.toml")).properties
    run_results = results.RunResults.from_declarations(declarations, duration_seconds=1.0, requested=requested)

    assert run_results.nothing_verified
    assert _likely_causes(run_results, {}) == expected


def test_a_language_that_is_off_comes_first_and_one_aristotle_cannot_vouch_for_last():
    requested = manifest.parse(manifest.tomllib.loads(MANIFEST), Path("aristotle.toml")).properties
    run_results = results.RunResults.from_declarations(
        [_declaration("write_when_full", DeclarationStatus.IN_PROGRESS)], duration_seconds=1.0, requested=requested
    )

    assert _likely_causes(run_results, {"C": False, "Rust": None, "Verilog": True}) == [
        "C is not enabled for this account, so Aristotle left it out",
        "Aristotle registered nothing under some of the manifest's ids",
        DECISION,
        "Rust is left out if this account can't verify it, which Aristotle gives no way to check",
    ]


async def test_a_run_that_verified_something_does_not_ask_about_the_accounts_languages(action: CIAction, repo: Path):
    run = await _run(action, ["--directory", str(repo), "--no-junit"], [_declaration("write_when_full")])

    assert run.code == EXIT_SUCCESS
    run.language_access.assert_not_awaited()


async def test_fail_on_never_logs_a_run_that_verified_nothing_without_failing(action: CIAction, repo: Path, caplog):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(action, ["--directory", str(repo), "--no-junit", "--fail-on", "never"], [])

    assert run.code == EXIT_SUCCESS
    assert "No property in the manifest was verified" in caplog.text
    assert "--fail-on never is set, so the build is not being failed." in caplog.text


async def test_a_refuted_suggestion_does_not_fail_the_build(action: CIAction, repo: Path, tmp_path: Path, caplog):
    suggestion = make_declaration(
        "Fifo.PointerWraps", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE
    )

    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(action, ["--directory", str(repo)], [*(_declaration(i) for i in IDS), suggestion])

    assert run.code == EXIT_SUCCESS
    assert "Suggested by Aristotle, not part of the verdict:" in caplog.text
    assert "Fifo.PointerWraps" not in (tmp_path / DEFAULT_JUNIT_PATH).read_text()


async def test_the_junit_report_has_a_testcase_per_manifest_id_in_manifest_order(
    action: CIAction, repo: Path, tmp_path: Path
):
    run = await _run(
        action,
        ["--directory", str(repo)],
        [_declaration("read_when_empty", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE)],
    )

    assert run.code == EXIT_VERIFICATION_FAILED
    cases = _testcases(tmp_path / DEFAULT_JUNIT_PATH)
    assert list(cases) == IDS
    skipped = cases["write_when_full"].find("skipped")
    assert (
        skipped is not None
        and skipped.get("message") == "Aristotle registered no property under this id, or withdrew it."
    )
    failure = cases["read_when_empty"].find("failure")
    assert failure is not None and "Requested: A read from an empty FIFO returns no data." in (failure.text or "")


async def test_fail_on_never_reports_a_counterexample_without_failing(action: CIAction, repo: Path, caplog):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(
            action,
            ["--directory", str(repo), "--fail-on", "never"],
            [
                _declaration("write_when_full", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE),
                _declaration("read_when_empty"),
            ],
        )

    assert run.code == EXIT_SUCCESS
    assert "--fail-on never is set, so the build is not being failed." in caplog.messages


@pytest.mark.parametrize(
    ("fail_on", "final_status", "status", "evidence", "expected", "logged"),
    [
        ("failure", TaskStatus.FAILED, DeclarationStatus.PROVED, None, EXIT_INFRASTRUCTURE_ERROR, "internal error"),
        (
            "failure",
            TaskStatus.FAILED,
            DeclarationStatus.REFUTED,
            DeclarationEvidence.COUNTEREXAMPLE,
            EXIT_VERIFICATION_FAILED,
            "internal error",
        ),
        ("failure", TaskStatus.CANCELED, DeclarationStatus.PROVED, None, EXIT_INFRASTRUCTURE_ERROR, "outside this job"),
        ("never", TaskStatus.FAILED, DeclarationStatus.PROVED, None, EXIT_SUCCESS, "internal error"),
    ],
    ids=["failed", "failed with a refutation", "cancelled elsewhere", "failed under --fail-on never"],
)
async def test_a_failed_or_cancelled_run_is_an_infrastructure_error_unless_a_property_is_refuted(
    action: CIAction,
    repo: Path,
    tmp_path: Path,
    caplog,
    fail_on: str,
    final_status: TaskStatus,
    status: DeclarationStatus,
    evidence: DeclarationEvidence | None,
    expected: int,
    logged: str,
):
    run = await _run(
        action,
        ["--directory", str(repo), "--fail-on", fail_on],
        [_declaration("write_when_full", status, evidence=evidence), _declaration("read_when_empty")],
        final_status=final_status,
    )

    assert run.code == expected
    assert logged in caplog.text
    cases = _testcases(tmp_path / DEFAULT_JUNIT_PATH)
    assert list(cases) == ["Aristotle run", *IDS]
    error = cases["Aristotle run"].find("error")
    assert error is not None and error.get("type") == ("failed" if final_status == TaskStatus.FAILED else "cancelled")


OUT_OF_BUDGET_ERROR = (
    "The run ran out of budget before Aristotle checked its results. None of them counts as proved or refuted."
)
TIME_BUDGET_ERROR = (
    "The time budget ran out before Aristotle checked the run's results. None of them counts as proved or refuted. "
    "Raise --timeout. Waiting for a free slot counts toward --timeout."
)
CUT_SHORT = pytest.mark.parametrize(
    ("final_status", "error"),
    [(TaskStatus.OUT_OF_BUDGET, OUT_OF_BUDGET_ERROR), (None, TIME_BUDGET_ERROR)],
    ids=["out of budget", "out of time"],
)


@CUT_SHORT
async def test_a_cut_short_run_with_every_property_proved_exits_3_and_reports_them_unchecked(
    action: CIAction,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog,
    final_status: TaskStatus | None,
    error: str,
):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(action, ["--directory", str(repo)], [_declaration(i) for i in IDS], final_status=final_status)

    assert run.code == EXIT_INFRASTRUCTURE_ERROR
    assert "0 proved, 0 refuted, 0 in progress, 2 unchecked (2 proved)" in caplog.text
    assert "  SKIP  [proved, unchecked] write_when_full\n" in caplog.text
    assert "PASS" not in caplog.text
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == [error]
    cases = _testcases(tmp_path / DEFAULT_JUNIT_PATH)
    assert all(cases[i].find("skipped").get("message").startswith("Reported proved, unchecked.") for i in IDS)
    run_error = cases["Aristotle run"].find("error")
    reason = results.OUT_OF_BUDGET_REASON if final_status == TaskStatus.OUT_OF_BUDGET else results.TIME_BUDGET_REASON
    assert run_error is not None and run_error.get("message") == reason
    assert summary.read_text().count("| proved, unchecked |") == 2


@CUT_SHORT
@pytest.mark.parametrize(
    ("fail_on", "expected"),
    [("failure", EXIT_INFRASTRUCTURE_ERROR), ("no-verdict", EXIT_INFRASTRUCTURE_ERROR), ("never", EXIT_SUCCESS)],
)
@pytest.mark.parametrize(
    ("status", "evidence"),
    [(DeclarationStatus.PROVED, None), (DeclarationStatus.REFUTED, DeclarationEvidence.COUNTEREXAMPLE)],
    ids=["proved", "counterexample"],
)
async def test_a_cut_short_run_fails_the_build_unless_advisory_whatever_it_reported(
    action: CIAction,
    repo: Path,
    caplog,
    final_status: TaskStatus | None,
    error: str,
    fail_on: str,
    expected: int,
    status: DeclarationStatus,
    evidence: DeclarationEvidence | None,
):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(
            action,
            ["--directory", str(repo), "--no-junit", "--fail-on", fail_on],
            [
                _declaration("write_when_full", status, evidence=evidence),
                _declaration("read_when_empty", DeclarationStatus.IN_PROGRESS),
            ],
            final_status=final_status,
        )

    # The time budget's cut hasn't ended the run, so --fail-on never doesn't pass it either.
    ended = final_status is not None
    assert run.code == (expected if ended else EXIT_INFRASTRUCTURE_ERROR)
    assert error in caplog.messages
    assert ("--fail-on never is set, so the build is not being failed." in caplog.messages) == (
        fail_on == "never" and ended
    )
    assert "No property in the manifest was verified" not in caplog.text


@pytest.mark.parametrize(("on_timeout", "cancel"), [("cancel", True), ("leave", False)])
async def test_on_timeout_also_decides_whether_an_interrupt_before_the_wait_cancels_the_run(
    action: CIAction, repo: Path, on_timeout: str, cancel: bool
):
    run = await _run(action, ["--directory", str(repo), "--no-junit", "--on-timeout", on_timeout])

    assert run.submit.await_args.kwargs["cancel_on_interrupt"] is cancel
    assert run.wait.await_args.kwargs["cancel_on_timeout"] is cancel


async def test_a_run_left_running_at_the_time_budget_is_cut_short_too(action: CIAction, repo: Path):
    run = await _run(
        action,
        ["--directory", str(repo), "--no-junit", "--on-timeout", "leave"],
        [_declaration(i) for i in IDS],
        final_status=None,
    )

    assert run.wait.await_args.kwargs["cancel_on_timeout"] is False
    assert run.code == EXIT_INFRASTRUCTURE_ERROR


URL = _project().web_url()


@pytest.mark.parametrize(
    ("final_status", "access", "error"),
    [
        (
            TaskStatus.OUT_OF_BUDGET,
            {"Verilog": True},
            "The run ran out of budget with no property under any of the manifest's ids. See the run's summary at "
            f"{URL}",
        ),
        (
            None,
            {"Verilog": True},
            "The time budget ran out with no property under any of the manifest's ids. See how far it got at "
            f"{URL}, and raise --timeout if it needs longer. Waiting for a free slot counts toward --timeout.",
        ),
        (
            None,
            {"Verilog": False},
            "The time budget ran out with no property under any of the manifest's ids. Likely causes: Verilog is not "
            "enabled for this account, so Aristotle left it out; or the run needs longer than the time budget, so "
            f"raise --timeout. Waiting for a free slot counts toward --timeout. See how far it got at {URL}",
        ),
        (
            None,
            {"Verilog": None},
            "The time budget ran out with no property under any of the manifest's ids. See how far it got at "
            f"{URL}, and raise --timeout if it needs longer. Waiting for a free slot counts toward --timeout.",
        ),
        (
            TaskStatus.OUT_OF_BUDGET,
            {"Verilog": False},
            "The run ran out of budget with no property under any of the manifest's ids. Likely cause: Verilog is not "
            f"enabled for this account, so Aristotle left it out. See the run's summary at {URL}",
        ),
    ],
    ids=["out of budget", "out of time", "out of time, language off", "out of time, unknown", "out of budget, off"],
)
async def test_a_cut_short_run_with_nothing_registered_says_so_and_names_a_language_only_where_aristotle_does(
    action: CIAction,
    repo: Path,
    caplog,
    final_status: TaskStatus | None,
    access: dict[str, bool | None],
    error: str,
):
    with caplog.at_level(logging.WARNING, logger="aristotle"):
        run = await _run(action, ["--directory", str(repo), "--no-junit"], [], final_status=final_status, access=access)

    assert run.code == EXIT_INFRASTRUCTURE_ERROR
    assert caplog.messages == [error]
    run.language_access.assert_awaited_once_with(["Verilog"])


async def test_a_cut_short_run_that_reached_a_property_does_not_ask_about_the_accounts_languages(
    action: CIAction, repo: Path
):
    run = await _run(
        action, ["--directory", str(repo), "--no-junit"], [_declaration("write_when_full")], final_status=None
    )

    run.language_access.assert_not_awaited()


@pytest.mark.parametrize(
    ("manifest_text", "asked"),
    [
        ('[requested_properties.no_x]\nkind = "sva"\nidentifier = "fifo.no_x"\n', ["Verilog"]),
        ('[requested_properties.no_ub]\nkind = "c_function_no_ub"\nidentifier = "push"\n', ["C"]),
        (MANIFEST, ["C", "Verilog"]),
    ],
    ids=["sva only", "c only", "natural language"],
)
async def test_the_languages_asked_about_are_those_in_the_checkout_that_the_manifest_is_about(
    action: CIAction, repo: Path, manifest_text: str, asked: list[str]
):
    _commit(repo, "fifo.c", "int push(void) { return 0; }\n")
    _commit(repo, "harness.h", "int harness(void);\n")
    (repo / manifest.MANIFEST_NAME).write_text(manifest_text)

    run = await _run(_action(), ["--directory", str(repo), "--no-junit"], [])

    run.language_access.assert_awaited_once_with(asked)


async def test_a_run_cut_short_in_time_names_the_ids_it_had_not_registered_yet(action: CIAction, repo: Path, caplog):
    with caplog.at_level(logging.WARNING, logger="aristotle"):
        await _run(
            action, ["--directory", str(repo), "--no-junit"], [_declaration("write_when_full")], final_status=None
        )

    assert caplog.messages == [
        TIME_BUDGET_ERROR,
        "The run had no property under the manifest id read_when_empty when the time budget ran out, so it is "
        "reported without a verdict.",
    ]


@pytest.mark.parametrize(
    ("final_status", "state"),
    [(None, "goes on"), (TaskStatus.QUEUED, "is still queued, and starts later")],
    ids=["cut short", "never started"],
)
async def test_with_on_timeout_leave_a_run_the_budget_ran_out_on_says_it_goes_on_and_how_to_follow_it(
    action: CIAction,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog,
    final_status: TaskStatus | None,
    state: str,
):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    with caplog.at_level(logging.INFO, logger="aristotle"):
        await _run(
            action, ["--directory", str(repo), "--no-junit", "--on-timeout", "leave"], [], final_status=final_status
        )

    assert caplog.messages[-1] == (
        f"The run {state}, as --on-timeout leave is set.\n"
        "Report on it later by running the same command with --project test-123.\n"
        "Follow it with: aristotle show test-123\nCancel it with: aristotle cancel test-123"
    )
    assert (
        f"> [!NOTE]\n> The run {state}, as --on-timeout leave is set. Report on it later by running the same command "
        "with --project test-123.\n"
    ) in summary.read_text()


def _record(root: Path, manifest_path: Path | None = None) -> RunRecord:
    """The record of a run `aristotle ci` would start from ``root`` as it stands, with its manifest or another."""
    checked = manifest.load(manifest_path or root / manifest.MANIFEST_NAME)
    return RunRecord.of_checkout(root, scope.resolve_checkout(root, warn=False).upload_files, checked)


def _started_here(action: CIAction) -> str:
    """The end of a prompt `aristotle ci` would start a run with from the action's checkout and manifest as they are."""
    return _prompt_ending(_record(action._root, action._manifest.path))


async def _follow(
    action: CIAction,
    argv: list[str],
    prompt: str | None = None,
    tasks: int = 1,
    status: TaskStatus = TaskStatus.COMPLETE,
    **kwargs,
) -> tuple[Run, AsyncMock]:
    """_run with --project 1a2b, which resolves to _project()'s run, the first of its ``tasks``, started with ``prompt``,
    by default one from this checkout and manifest, the latest in ``status``; returns the run and the resolve mock."""
    fetch_prompt = AsyncMock(side_effect=lambda task: _started_here(action) if prompt is None else prompt)
    listed = [Mock(spec=AgentTask, status=status)] * tasks
    with (
        patch.object(Project, "_resolve", AsyncMock(return_value=("test-123", None))) as resolve,
        patch.object(Project, "from_id", AsyncMock(return_value=_project())),
        patch.object(Project, "get_tasks", AsyncMock(return_value=(listed, None))),
        patch("aristotlelib.cli.command.ci.runner.fetch_prompt", fetch_prompt),
    ):
        return await _run(action, ["--project", "1a2b", *argv], **kwargs), resolve


async def test_project_reports_on_a_run_already_started_without_uploading(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        (run, resolve) = await _follow(
            action,
            ["--directory", str(repo)],
            declarations=[
                _declaration("write_when_full"),
                _declaration(
                    "read_when_empty", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.NEGATION_PROVED
                ),
            ],
            summary="A write to a full FIFO overwrote the oldest entry.",
        )

    resolve.assert_awaited_once_with("1a2b")
    run.submit.assert_not_awaited()
    assert run.wait.await_args.args[0].project_id == "test-123"
    assert run.wait.await_args.kwargs["timeout_seconds"] == pytest.approx(DEFAULT_TIMEOUT_MINUTES * 60, abs=5)
    assert run.code == EXIT_VERIFICATION_FAILED
    assert f"Following the run: {URL}" in caplog.messages
    assert "Run submitted" not in caplog.text
    cases = _testcases(tmp_path / DEFAULT_JUNIT_PATH)
    assert list(cases) == IDS
    assert cases["read_when_empty"].find("failure") is not None


FRESH = "start a fresh run with plain `aristotle ci`"


def _prompt_ending(record: RunRecord) -> str:
    return f"Check each of the following properties.\n\n{record.prompt_ending}"


async def _refused(
    action: CIAction,
    repo: Path,
    caplog,
    prompt: str | None,
    argv: tuple[str, ...] = (),
    tasks: int = 1,
    status: TaskStatus = TaskStatus.COMPLETE,
) -> str:
    """Follow a run started with ``prompt``; assert --project exits 2 before waiting or reporting, and return why."""
    with caplog.at_level(logging.INFO, logger="aristotle"):
        (run, _) = await _follow(action, ["--directory", str(repo), *argv], prompt=prompt, tasks=tasks, status=status)

    assert run.code == EXIT_USAGE_ERROR
    run.wait.assert_not_awaited()
    # The report goes where the command runs, as for every other test here.
    assert not Path(DEFAULT_JUNIT_PATH).exists()
    return caplog.messages[-1]


async def test_project_refuses_a_project_with_a_follow_up(action: CIAction, repo: Path, caplog):
    assert await _refused(action, repo, caplog, None, tasks=2) == (
        "Project test-123 has more than the one run `aristotle ci` starts, such as a follow-up, so its results aren't "
        f"only what that run checked. To check this checkout, {FRESH}."
    )


@pytest.mark.parametrize(
    ("prompt", "tasks"),
    [("Prove that the FIFO never overflows.", 1), ("", 1), ("", 0)],
    ids=["submitted", "no message", "no task"],
)
async def test_project_refuses_a_run_ci_did_not_start(action: CIAction, repo: Path, caplog, prompt: str, tasks: int):
    assert await _refused(action, repo, caplog, prompt, tasks=tasks) == (
        "Project test-123 wasn't started by `aristotle ci`, or was started by an older version that doesn't record "
        f"which files and manifest its run checks. To check this checkout, {FRESH}."
    )


PLANTED_BUG = "module fifo;\n  // planted bug\nendmodule\n"


async def test_project_refuses_a_run_of_other_files_at_another_commit_naming_both(action: CIAction, repo: Path, caplog):
    started, run_commit = _record(repo), _head(repo)[:12]
    _commit(repo, "fifo.v", PLANTED_BUG)

    assert await _refused(action, repo, caplog, _prompt_ending(started)) == (
        f"Project test-123's run checks other files than this checkout's: it started at commit {run_commit}, and this "
        f"checkout is at commit {_head(repo)[:12]}. If it started from a clean checkout, check out commit {run_commit} "
        f"to report on it; to check this one, {FRESH}."
    )


async def test_project_refuses_a_run_of_other_files_at_the_same_commit(action: CIAction, repo: Path, caplog):
    started = _record(repo)
    (repo / "fifo.v").write_text(PLANTED_BUG)

    assert await _refused(action, repo, caplog, _prompt_ending(started)) == (
        "Project test-123's run checks other files than this checkout's, though both are at commit "
        f"{_head(repo)[:12]}: other uncommitted changes, other files --allow-untracked lets through, another "
        "--directory or --manifest, another folder to run from, or other line-ending or LFS settings can do that. "
        f"Report on that run from the files it started with, or {FRESH} to check these."
    )


async def test_project_counts_an_edit_to_a_manifest_it_uploads_as_other_files(action: CIAction, repo: Path, caplog):
    started, run_commit = _record(repo), _head(repo)[:12]
    _commit(repo, manifest.MANIFEST_NAME, f"# Only a comment more.\n{MANIFEST}")

    assert (await _refused(action, repo, caplog, _prompt_ending(started))).startswith(
        f"Project test-123's run checks other files than this checkout's: it started at commit {run_commit}, and this "
        f"checkout is at commit {_head(repo)[:12]}."
    )


async def test_project_names_the_manifest_when_a_command_names_another_with_other_properties(
    action: CIAction, repo: Path, caplog
):
    (repo / "ci").mkdir()
    _commit(repo, "ci/nightly.toml", MANIFEST.replace("is dropped", "overwrites the oldest entry"))
    started = _record(repo, repo / "ci" / "nightly.toml")

    assert await _refused(action, repo, caplog, _prompt_ending(started)) == (
        f"{repo / manifest.MANIFEST_NAME} doesn't list the properties project test-123's run checks: an id, kind, "
        f"statement or assumption differs. Pass the manifest the run was started with, or {FRESH}."
    )


async def test_project_gives_no_commit_to_check_out_for_a_run_started_before_the_first_commit(
    action: CIAction, repo: Path, caplog
):
    started = dataclasses.replace(_record(repo), commit=None, files="0" * 64)

    assert await _refused(action, repo, caplog, _prompt_ending(started)) == (
        "Project test-123's run checks other files than this checkout's: it started at no commit, and this checkout "
        f"is at commit {_head(repo)[:12]}. To check this checkout, {FRESH}."
    )


async def test_project_checks_the_files_first_and_refuses_before_the_time_budget_is_logged(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    started = _record(repo)
    (repo / "fifo.v").write_text(PLANTED_BUG)
    other = tmp_path / "other.toml"
    other.write_text(f"[check]\nmax_wall_time_seconds = 600\n\n{MANIFEST.replace('is dropped', 'is kept')}")

    refusal = await _refused(action, repo, caplog, _prompt_ending(started), ("--manifest", str(other)))

    assert refusal.startswith("Project test-123's run checks other files than this checkout's")
    assert not [m for m in caplog.messages if m.startswith(("Time budget", "Following the run"))]


async def test_project_refuses_a_manifest_with_other_properties(action: CIAction, repo: Path, tmp_path: Path, caplog):
    started = _record(repo)
    other = tmp_path / "other.toml"
    other.write_text(MANIFEST.replace("is dropped", "overwrites the oldest entry"))

    assert await _refused(action, repo, caplog, _prompt_ending(started), ("--manifest", str(other))) == (
        f"{other} doesn't list the properties project test-123's run checks: an id, kind, statement or assumption "
        f"differs. Pass the manifest the run was started with, or {FRESH}."
    )


GOING = " That run hasn't ended: if a fresh run replaces it, stop it first with `aristotle cancel test-123`."


@pytest.mark.parametrize("status", [TaskStatus.QUEUED, TaskStatus.IN_PROGRESS])
async def test_project_says_how_to_stop_a_refused_run_that_has_not_ended(
    action: CIAction, repo: Path, caplog, status: TaskStatus
):
    started = _record(repo)
    (repo / "fifo.v").write_text(PLANTED_BUG)

    assert (await _refused(action, repo, caplog, _prompt_ending(started), status=status)).endswith(
        f"Report on that run from the files it started with, or {FRESH} to check these.{GOING}"
    )


async def test_project_says_how_to_stop_a_run_at_another_commit_that_has_not_ended(
    action: CIAction, repo: Path, caplog
):
    started = _record(repo)
    _commit(repo, "fifo.v", PLANTED_BUG)

    refusal = await _refused(action, repo, caplog, _prompt_ending(started), status=TaskStatus.IN_PROGRESS)

    assert refusal.endswith(f"to check this one, {FRESH}.{GOING}")


async def test_project_says_how_to_stop_a_run_of_other_properties_that_has_not_ended(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    started = _record(repo)
    other = tmp_path / "other.toml"
    other.write_text(MANIFEST.replace("is dropped", "overwrites the oldest entry"))

    refusal = await _refused(
        action, repo, caplog, _prompt_ending(started), ("--manifest", str(other)), status=TaskStatus.IN_PROGRESS
    )

    assert refusal.endswith(f"Pass the manifest the run was started with, or {FRESH}.{GOING}")


@pytest.mark.parametrize(("prompt", "tasks"), [(None, 2), ("Prove that the FIFO never overflows.", 1)])
async def test_project_suggests_no_cancel_for_a_run_ci_may_not_have_started(
    action: CIAction, repo: Path, caplog, prompt: str | None, tasks: int
):
    # A follow-up, or a project `aristotle ci` didn't start, can be someone else's work going on.
    refusal = await _refused(action, repo, caplog, prompt, tasks=tasks, status=TaskStatus.IN_PROGRESS)

    assert refusal.endswith(f"To check this checkout, {FRESH}.")


async def test_a_refusal_of_the_run_aristotle_project_names_says_to_unset_it_for_a_fresh_run(
    repo: Path, monkeypatch, caplog
):
    # Plain `aristotle ci` would follow the same project again.
    monkeypatch.setenv("ARISTOTLE_PROJECT", "1a2b")
    action = _action()
    started = _record(repo)
    (repo / "fifo.v").write_text(PLANTED_BUG)
    fetch_prompt = AsyncMock(return_value=_prompt_ending(started))

    with (
        patch.object(Project, "_resolve", AsyncMock(return_value=("test-123", None))),
        patch.object(Project, "from_id", AsyncMock(return_value=_project())),
        patch.object(
            Project, "get_tasks", AsyncMock(return_value=([Mock(spec=AgentTask, status=TaskStatus.COMPLETE)], None))
        ),
        patch("aristotlelib.cli.command.ci.runner.fetch_prompt", fetch_prompt),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        run = await _run(action, ["--directory", str(repo), "--no-junit"])

    assert run.code == EXIT_USAGE_ERROR
    assert caplog.messages[-1].endswith(
        "Report on that run from the files it started with, or unset ARISTOTLE_PROJECT and start a fresh run with "
        "plain `aristotle ci` to check these."
    )


def _uncommitted(repo: Path) -> None:
    (repo / "fifo.v").write_text(PLANTED_BUG)


def _same_tree_new_commit(repo: Path) -> None:
    git(repo, "commit", "-q", "--allow-empty", "-m", "a merge commit with the same tree, as Jenkins makes per build")


def _not_uploaded(repo: Path) -> None:
    _commit(repo, ".env", "SECRET=v1\n")


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (_uncommitted, None),
        (None, _same_tree_new_commit),
        (_not_uploaded, lambda repo: (repo / ".env").write_text("SECRET=v2\n")),
    ],
    ids=[
        "from the same uncommitted changes",
        "at another commit with the same files",
        "after a change it doesn't upload",
    ],
)
async def test_project_reports_a_run_of_the_files_this_checkout_would_upload(
    action: CIAction, repo: Path, before, after
):
    if before is not None:
        before(repo)
    started = _record(repo)
    if after is not None:
        after(repo)

    (run, _) = await _follow(
        action,
        ["--directory", str(repo), "--no-junit"],
        prompt=_prompt_ending(started),
        declarations=[_declaration(i) for i in IDS],
    )

    assert run.code == EXIT_SUCCESS


async def test_project_reports_a_run_whose_manifest_differs_only_in_order_comments_and_budget(
    action: CIAction, repo: Path, tmp_path: Path
):
    started = _record(repo)
    reordered = tmp_path / "reordered.toml"
    write, read = MANIFEST.split("\n\n")
    reordered.write_text(f"# The same properties.\n[check]\nmax_wall_time_seconds = 600\n\n{read}\n{write}\n")

    (run, _) = await _follow(
        action,
        ["--directory", str(repo), "--no-junit", "--manifest", str(reordered)],
        prompt=_prompt_ending(started),
        declarations=[_declaration(i) for i in IDS],
    )

    assert run.code == EXIT_SUCCESS


async def test_project_takes_the_runs_time_budget_and_on_timeout(action: CIAction, repo: Path):
    _commit(repo, manifest.MANIFEST_NAME, f"[check]\nmax_wall_time_seconds = 600\n\n{MANIFEST}")

    (run, _) = await _follow(_action(), ["--directory", str(repo), "--no-junit", "--on-timeout", "leave"])

    assert run.wait.await_args.kwargs["timeout_seconds"] == pytest.approx(600, abs=5)
    assert run.wait.await_args.kwargs["cancel_on_timeout"] is False


async def test_project_left_going_again_says_how_to_report_on_it_later(action: CIAction, repo: Path, caplog):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        (run, _) = await _follow(
            action,
            ["--directory", str(repo), "--no-junit", "--timeout", "9", "--on-timeout", "leave"],
            declarations=[_declaration(i) for i in IDS],
            final_status=None,
        )

    assert run.code == EXIT_INFRASTRUCTURE_ERROR
    assert caplog.messages[-1].startswith(
        "The run goes on, as --on-timeout leave is set.\n"
        "Report on it later by running the same command with --project test-123.\n"
    )


async def test_project_warns_of_nothing_left_out_of_an_upload_it_does_not_make(action: CIAction, repo: Path, caplog):
    _commit(repo, "notes.v", "module notes;\nendmodule\n")
    (repo / ".gitignore").write_text("notes.v\n")
    (repo / "aristotle.f").write_text("fifo.v\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        await _follow(action, ["--directory", str(repo), "--no-junit"], declarations=[_declaration(i) for i in IDS])

    assert "Not uploading" not in caplog.text
    assert "Verilog filelist" not in caplog.text
    assert "isn't tracked by git" not in caplog.text


async def test_project_refuses_a_dry_run(action: CIAction, repo: Path, capsys):
    with pytest.raises(SystemExit) as exited:
        await _follow(action, ["--directory", str(repo), "--dry-run"])

    assert exited.value.code == EXIT_USAGE_ERROR
    assert capsys.readouterr().err.endswith(
        "error: --dry-run starts no run, and --project reports on a run already started.\n"
    )


async def test_project_is_read_from_aristotle_project(repo: Path, monkeypatch, caplog):
    monkeypatch.setenv("ARISTOTLE_PROJECT", "1a2b")
    action = _action()

    with (
        patch.object(Project, "_resolve", AsyncMock(return_value=("test-123", None))) as resolve,
        patch.object(Project, "from_id", AsyncMock(return_value=_project())),
        patch.object(
            Project, "get_tasks", AsyncMock(return_value=([Mock(spec=AgentTask, status=TaskStatus.IN_PROGRESS)], None))
        ),
        patch(
            "aristotlelib.cli.command.ci.runner.fetch_prompt", AsyncMock(side_effect=lambda _: _started_here(action))
        ),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        run = await _run(
            action, ["--directory", str(repo), "--no-junit"], declarations=[_declaration("write_when_full")]
        )

    resolve.assert_awaited_once_with("1a2b")
    run.submit.assert_not_awaited()
    assert run.wait.await_args.kwargs["cancel_on_timeout"] is False
    assert "Reporting on the run ARISTOTLE_PROJECT names: 1a2b" in caplog.messages


async def test_project_follows_the_run_of_the_project_its_lookup_listed_without_fetching_it_again(
    action: CIAction, repo: Path
):
    with (
        patch.object(Project, "_resolve", AsyncMock(return_value=("test-123", _project()))) as resolve,
        patch.object(Project, "from_id", AsyncMock(return_value=_project())) as from_id,
        patch.object(
            Project, "get_tasks", AsyncMock(return_value=([Mock(spec=AgentTask, status=TaskStatus.COMPLETE)], None))
        ),
        patch(
            "aristotlelib.cli.command.ci.runner.fetch_prompt", AsyncMock(side_effect=lambda _: _started_here(action))
        ),
    ):
        run = await _run(
            action,
            ["--directory", str(repo), "--project", "FIFO run", "--no-junit"],
            declarations=[_declaration("write_when_full")],
        )

    resolve.assert_awaited_once_with("FIFO run")
    from_id.assert_not_awaited()
    run.submit.assert_not_awaited()
    assert run.wait.await_args.args[0].project_id == "test-123"


def test_project_wins_over_aristotle_project(monkeypatch, caplog, capsys):
    monkeypatch.setenv("ARISTOTLE_PROJECT", "1a2b")
    action = _action()
    args = action.parser.parse_args(["--project", "9z9z", "--dry-run"])

    with caplog.at_level(logging.INFO, logger="aristotle"), pytest.raises(SystemExit):
        action.preflight(args)

    err = capsys.readouterr().err
    assert args.project == "9z9z"
    assert "ARISTOTLE_PROJECT" not in caplog.text + err
    assert err.endswith("error: --dry-run starts no run, and --project reports on a run already started.\n")


async def test_a_dry_run_with_aristotle_project_set_names_the_variable(repo: Path, monkeypatch, capsys):
    monkeypatch.setenv("ARISTOTLE_PROJECT", "1a2b")

    with pytest.raises(SystemExit) as exited:
        await _run(_action(), ["--directory", str(repo), "--dry-run"])

    assert exited.value.code == EXIT_USAGE_ERROR
    assert capsys.readouterr().err.endswith(
        "error: --dry-run starts no run, and ARISTOTLE_PROJECT reports on a run already started.\n"
    )


def test_an_empty_aristotle_project_is_unset(monkeypatch):
    monkeypatch.setenv("ARISTOTLE_PROJECT", "")

    assert _action().parser.parse_args([]).project is None


async def test_project_ignores_a_name_given_for_the_run_it_follows(action: CIAction, repo: Path, caplog):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        (run, _) = await _follow(
            action,
            ["--directory", str(repo), "--no-junit", "--name", "fifo"],
            declarations=[_declaration(i) for i in IDS],
        )

    assert run.code == EXIT_SUCCESS
    assert "--name names the project a new run creates, so --project ignores it." in caplog.messages


@pytest.mark.parametrize(
    ("argv", "environment", "cancel"),
    [
        ([], {}, False),
        ([], {"ARISTOTLE_ON_TIMEOUT": ""}, False),
        (["--on-timeout", "cancel"], {}, True),
        ([], {"ARISTOTLE_ON_TIMEOUT": "cancel"}, True),
    ],
    ids=["by default", "the Action's empty on-timeout", "given cancel", "cancel from the environment"],
)
async def test_project_leaves_the_run_going_unless_told_to_cancel_it(
    repo: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], environment: dict[str, str], cancel: bool
):
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    (run, _) = await _follow(_action(), ["--directory", str(repo), "--no-junit", *argv])

    assert run.wait.await_args.kwargs["cancel_on_timeout"] is cancel


async def test_project_says_it_left_the_run_going_by_default(action: CIAction, repo: Path, caplog):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        await _follow(action, ["--directory", str(repo), "--no-junit"], final_status=None)

    assert caplog.messages[-1].startswith("The run goes on, as --project leaves it going.\n")


@pytest.mark.parametrize(("argv", "hinted"), [([], True), (["--on-timeout", "cancel"], False)])
async def test_an_interrupted_project_says_how_to_report_on_the_run_unless_it_cancels_it(
    action: CIAction, repo: Path, caplog, argv: list[str], hinted: bool
):
    args = action.parser.parse_args(["--directory", str(repo), "--no-junit", "--project", "1a2b", *argv])

    with (
        patch.object(Project, "_resolve", AsyncMock(return_value=("test-123", None))),
        patch.object(Project, "from_id", AsyncMock(return_value=_project())),
        patch.object(
            Project, "get_tasks", AsyncMock(return_value=([Mock(spec=AgentTask, status=TaskStatus.IN_PROGRESS)], None))
        ),
        patch(
            "aristotlelib.cli.command.ci.runner.fetch_prompt", AsyncMock(side_effect=lambda _: _started_here(action))
        ),
        patch("aristotlelib.cli.command.ci.runner.wait_for_completion", AsyncMock(side_effect=KeyboardInterrupt)),
        caplog.at_level(logging.INFO, logger="aristotle"),
        pytest.raises(KeyboardInterrupt),
    ):
        assert action.preflight(args) is None
        await action.run_action(args)

    hint = "Report on it later by running the same command with --project test-123."
    assert (hint in caplog.messages) is hinted


async def test_project_ignores_a_name_from_the_environment(repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARISTOTLE_NAME", "fifo")

    (run, _) = await _follow(
        _action(), ["--directory", str(repo), "--no-junit"], declarations=[_declaration(i) for i in IDS]
    )

    assert run.code == EXIT_SUCCESS


def test_project_that_is_not_found_exits_4(action: CIAction, repo: Path, caplog):
    args = action.parser.parse_args(["--directory", str(repo), "--project", "1a2b"], argparse.Namespace(api_key=None))
    missing = AristotleNotFoundError("project", "1a2b")

    with (
        patch("aristotlelib.cli.api_action.get_api_key", return_value="test-key"),
        patch.object(Project, "_resolve", AsyncMock(side_effect=missing)),
        patch("aristotlelib.cli.command.ci.runner.wait_for_completion", AsyncMock()) as wait,
    ):
        code = action.run(args)

    assert code == EXIT_NOT_FOUND
    assert caplog.messages[-1] == str(missing)
    wait.assert_not_awaited()


@pytest.mark.parametrize(
    ("on_timeout", "final_status"),
    [
        ("cancel", None),
        ("cancel", TaskStatus.QUEUED),
        ("leave", TaskStatus.OUT_OF_BUDGET),
        ("leave", TaskStatus.COMPLETE),
    ],
    ids=["cancelled when cut short", "cancelled when never started", "out of budget", "finished"],
)
async def test_a_run_that_is_not_left_going_says_nothing_of_following_it(
    action: CIAction, repo: Path, caplog, on_timeout: str, final_status: TaskStatus | None
):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        await _run(
            action, ["--directory", str(repo), "--no-junit", "--on-timeout", on_timeout], [], final_status=final_status
        )

    assert "Follow it with" not in caplog.text


@pytest.mark.parametrize("fail_on", [[], ["--fail-on", "never"]], ids=["failure", "never"])
async def test_a_run_still_queued_when_the_budget_ran_out_says_it_never_started_and_is_rate_limited(
    action: CIAction, repo: Path, tmp_path: Path, caplog, fail_on: list[str]
):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(action, ["--directory", str(repo), *fail_on], [], final_status=TaskStatus.QUEUED)

    assert run.code == EXIT_RATE_LIMITED
    assert "--fail-on never is set" not in caplog.text
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert errors[0] == (
        "Nothing was verified. The run never started: it was still queued, behind your other runs in progress or "
        "waiting for capacity, when the time budget ran out. Waiting for a free slot counts toward --timeout. Raise "
        "it, or, if your other runs in progress held the run up, cancel one you don't need, or run CI as a dedicated "
        "account with its own key."
    )
    assert not any("registered no property" in e or "Likely causes" in e for e in errors)
    cases = _testcases(tmp_path / DEFAULT_JUNIT_PATH)
    assert cases["write_when_full"].find("skipped").get("message").startswith("The run never started")


async def test_no_run_slot_within_the_budget_exits_rate_limited_without_a_report(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    args = action.parser.parse_args(["--directory", str(repo)])
    timeout = runner.SlotTimeoutError("No run slot freed up.")

    with (
        patch("aristotlelib.cli.command.ci.runner.submit", AsyncMock(side_effect=timeout)),
        patch("aristotlelib.cli.command.ci.runner.wait_for_completion", AsyncMock()) as wait,
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        assert action.preflight(args) is None
        code = await action.run_action(args)

    assert code == EXIT_RATE_LIMITED
    assert caplog.messages[-2:] == [
        "No run slot freed up.",
        "Waiting for a free slot counts toward --timeout. Cancel a project in progress, raise --timeout, or run CI "
        "as a dedicated account with its own key.",
    ]
    assert not (tmp_path / DEFAULT_JUNIT_PATH).exists()
    wait.assert_not_awaited()


@pytest.mark.parametrize(("timeout", "resubmit_by"), [("60", 45 * 60), ("2", 60)], ids=["a quarter", "a minute"])
async def test_a_run_submitted_after_a_slot_wait_keeps_a_quarter_of_the_budget(
    action: CIAction, repo: Path, monkeypatch: pytest.MonkeyPatch, timeout: str, resubmit_by: float
):
    monkeypatch.setattr("aristotlelib.cli.command.ci.time.monotonic", lambda: 0.0)

    run = await _run(action, ["--directory", str(repo), "--timeout", timeout])

    assert run.submit.await_args.kwargs["resubmit_by"] == resubmit_by


async def test_waiting_for_a_slot_comes_out_of_the_runs_budget(
    action: CIAction, repo: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    clock = [0.0]
    monkeypatch.setattr("aristotlelib.cli.command.ci.time.monotonic", lambda: clock[0])

    async def submit(*_args, **_kwargs):
        clock[0] += 25 * 60
        return _project()

    args = action.parser.parse_args(["--directory", str(repo)])
    with (
        patch("aristotlelib.cli.command.ci.runner.submit", side_effect=submit),
        patch("aristotlelib.cli.command.ci.runner.wait_for_completion", AsyncMock(return_value=None)) as wait,
        patch(
            "aristotlelib.cli.command.ci.runner.language_access",
            AsyncMock(side_effect=lambda languages: dict.fromkeys(languages, True)),
        ),
        patch.object(Project, "get_declarations", AsyncMock(return_value=[])),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        assert action.preflight(args) is None
        await action.run_action(args)

    assert wait.await_args.kwargs["timeout_seconds"] == 35 * 60
    assert "35 min of the time budget is left for the run." in caplog.text


@pytest.mark.parametrize(
    "reporter",
    [
        "aristotlelib.cli.command.ci.console.report",
        "aristotlelib.cli.command.ci.junit.write",
        "aristotlelib.cli.command.ci.github.report",
        "aristotlelib.cli.command.ci.github.write_output",
    ],
)
@pytest.mark.parametrize(("fail_on", "expected"), [("never", EXIT_SUCCESS), ("failure", EXIT_VERIFICATION_FAILED)])
async def test_a_reporter_that_raises_does_not_change_the_exit_code(
    action: CIAction,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog,
    reporter: str,
    fail_on: str,
    expected: int,
):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "github_output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))

    with patch(reporter, side_effect=ValueError("broken reporter")), caplog.at_level(logging.WARNING):
        run = await _run(
            action,
            ["--directory", str(repo), "--fail-on", fail_on],
            [
                _declaration("write_when_full", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE),
                _declaration("read_when_empty"),
            ],
        )

    assert run.code == expected
    assert "broken reporter" in caplog.text


@pytest.mark.parametrize("no_junit", [False, True], ids=["--junit", "--no-junit"])
async def test_the_junit_path_is_a_github_step_output_when_a_report_is_written(
    action: CIAction, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_junit: bool
):
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    report = tmp_path / "reports" / "out.xml"

    await _run(
        action,
        ["--directory", str(repo), "--junit", str(report), *(["--no-junit"] if no_junit else [])],
        [_declaration(i) for i in IDS],
    )

    assert report.exists() != no_junit
    assert (output.read_text() if output.exists() else None) == (None if no_junit else f"junit={report}\n")


def _commit_link(repo: Path, name: str, target: Path) -> None:
    """Commit a symbolic link at ``name`` in ``repo`` to ``target``, as a pull request can."""
    (repo / name).symlink_to(target, target_is_directory=target.is_dir())
    git(repo, "add", name)
    git(repo, "commit", "-qm", f"link {name}")


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
async def test_a_link_a_checkout_commits_at_the_junit_path_is_replaced_by_the_report_and_the_exit_code_stands(
    action: CIAction, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    target = tmp_path / "config.xml"
    target.write_text("<hudson/>\n")
    _commit_link(repo, "aristotle-results.xml", target)
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.chdir(repo)

    with caplog.at_level(logging.WARNING):
        run = await _run(action, ["--directory", str(repo)], [_declaration(i) for i in IDS])

    assert run.code == EXIT_SUCCESS
    assert target.read_text() == "<hudson/>\n"
    assert not (repo / "aristotle-results.xml").is_symlink() and set(_testcases(repo / "aristotle-results.xml")) == set(
        IDS
    )
    assert output.read_text() == "junit=aristotle-results.xml\n"
    assert (
        "Replaced the symbolic link at aristotle-results.xml with the report, rather than write through it."
        in caplog.messages
    )


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
@pytest.mark.parametrize(
    ("variables", "argv"),
    [
        ({"GITHUB_ACTIONS": "true", "GITHUB_WORKSPACE": "{repo}"}, ["--directory", "."]),
        ({"JENKINS_URL": "http://jenkins.invalid/", "WORKSPACE": "{repo}"}, ["--directory", "."]),
        ({"CIRCLECI": "true"}, []),
    ],
    ids=["GitHub working-directory", "Jenkins dir()", "CircleCI working_directory"],
)
@pytest.mark.parametrize("linked", ["report", "folder"])
async def test_from_a_subproject_folder_a_link_at_the_checkouts_top_is_not_written_through(
    action: CIAction,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog,
    variables: dict[str, str],
    argv: list[str],
    linked: str,
):
    (repo / "hw").mkdir()
    git(repo, "mv", "fifo.v", manifest.MANIFEST_NAME, "hw")
    git(repo, "commit", "-qm", "subproject")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "config.xml").write_text("<hudson/>\n")
    if linked == "report":
        _commit_link(repo, "aristotle-results.xml", outside / "config.xml")
    else:
        _commit_link(repo, "test-results", outside)
    for name, value in variables.items():
        monkeypatch.setenv(name, value.format(repo=repo))
    monkeypatch.chdir(repo / "hw")
    junit = "../aristotle-results.xml" if linked == "report" else "../test-results/aristotle.xml"

    with caplog.at_level(logging.WARNING):
        run = await _run(action, [*argv, "--junit", junit], [_declaration(i) for i in IDS])

    assert run.code == EXIT_SUCCESS
    assert {p.name: p.read_text() for p in outside.iterdir()} == {"config.xml": "<hudson/>\n"}
    if linked == "report":
        assert set(_testcases(repo / "aristotle-results.xml")) == set(IDS)
    else:
        assert f"Could not write the JUnit report to {junit}: {repo / 'test-results'} is a symbolic link" in caplog.text


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
async def test_a_dotdot_after_a_folder_the_checkout_lacks_hides_no_link_after_it(
    action: CIAction, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    outside = tmp_path / "outside"
    outside.mkdir()
    _commit_link(repo, "reports", outside)
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(repo))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.chdir(repo)

    with caplog.at_level(logging.WARNING):
        run = await _run(action, ["--junit", f"{repo}/out/../reports/aristotle.xml"], [_declaration(i) for i in IDS])

    assert run.code == EXIT_SUCCESS
    assert os.listdir(outside) == []
    assert not (repo / "out").exists()
    assert not output.exists()
    assert f"{repo / 'reports'} is a symbolic link, which could lead the report out of the checkout." in caplog.text


async def test_a_dotdot_after_a_folder_the_checkout_lacks_still_names_the_report_in_the_step_output(
    action: CIAction, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.chdir(repo)

    run = await _run(action, ["--junit", "build/../aristotle-results.xml"], [_declaration(i) for i in IDS])

    assert run.code == EXIT_SUCCESS
    assert output.read_text() == f"junit={Path('build', '..', 'aristotle-results.xml')}\n"
    assert set(_testcases(repo / "build" / ".." / "aristotle-results.xml")) == set(IDS)


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
async def test_from_the_file_systems_root_a_link_outside_the_checkout_still_takes_the_report(
    action: CIAction, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """As /dev/stdout does, from a container with no working directory."""
    real = tmp_path / "real.xml"
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "latest.xml").symlink_to(real)
    monkeypatch.chdir(os.path.abspath(os.sep))

    run = await _run(
        action,
        ["--directory", str(repo), "--junit", str(tmp_path / "shared" / "latest.xml")],
        [_declaration(i) for i in IDS],
    )

    assert run.code == EXIT_SUCCESS
    assert set(_testcases(real)) == set(IDS)


@pytest.mark.parametrize(
    ("filelist", "announced", "not_announced"),
    [
        (None, "infer the build configuration", "Using the Verilog filelist"),
        ("aristotle.f", "Using the Verilog filelist committed at aristotle.f", "infer the build configuration"),
    ],
    ids=["none", "the root aristotle.f"],
)
async def test_the_verilog_filelist_is_announced_at_info(
    action: CIAction, repo: Path, caplog, filelist: str | None, announced: str, not_announced: str
):
    if filelist is not None:
        _commit(repo, filelist, "fifo.v\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(action, ["--directory", str(repo), "--dry-run"])

    assert run.code == EXIT_SUCCESS
    assert announced in caplog.text
    assert not_announced not in caplog.text
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_the_project_is_named_after_the_repository_the_run_and_the_commit(action: CIAction, repo: Path):
    run = await _run(action, ["--directory", str(repo)], [_declaration(i) for i in IDS])

    assert run.submit.await_args.kwargs["name"] == f"repo: ci @ {_revision(repo)}"


@pytest.mark.parametrize("from_folder", [False, True], ids=["--directory", "current folder"])
async def test_a_folder_shaped_like_a_bare_repository_is_checked_from_the_checkouts_repository(
    action: CIAction, repo: Path, from_folder: bool, monkeypatch: pytest.MonkeyPatch, caplog
):
    hw = commit_bare_shaped(repo)
    _commit(repo, f"hw/{manifest.MANIFEST_NAME}", MANIFEST)
    monkeypatch.setenv("ARISTOTLE_WEB_URL", "http://127.0.0.1:9")
    if from_folder:
        monkeypatch.chdir(hw)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(action, ["--dry-run", *([] if from_folder else ["--directory", str(hw)])])

    assert run.code == EXIT_SUCCESS
    # HEAD, config, objects/.keep, refs/.keep, top.v and the manifest.
    assert any(
        re.fullmatch(r"Dry run: would upload 6 files \(\d+ bytes\); checking 2 properties\.", m)
        for m in caplog.messages
    )
    assert f'Dry run: would name the project "repo/hw: ci @ {_revision(repo)}".' in caplog.messages


async def test_a_subprojects_run_is_named_by_its_directory_and_manifest(repo: Path, tmp_path: Path):
    hw = repo / "evals" / "01_add" / "project"
    hw.mkdir(parents=True)
    (hw / "add.c").write_text("int add(int a, int b) { return a + b; }\n")
    (hw / "gate.toml").write_text(MANIFEST)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "case")

    run = await _run(_action(), ["--directory", str(hw), "--manifest", str(hw / "gate.toml")])

    assert run.submit.await_args.kwargs["name"] == f"repo/evals/01_add/project: ci gate.toml @ {_revision(repo)}"


@pytest.mark.parametrize("from_environment", [False, True], ids=["--name", "ARISTOTLE_NAME"])
async def test_a_given_name_replaces_the_default(repo: Path, monkeypatch: pytest.MonkeyPatch, from_environment: bool):
    argv = ["--directory", str(repo)]
    if from_environment:
        monkeypatch.setenv("ARISTOTLE_NAME", " nightly gate ")
    else:
        argv += ["--name", " nightly gate "]

    run = await _run(_action(), argv)

    assert run.submit.await_args.kwargs["name"] == "nightly gate"


@pytest.mark.parametrize("from_environment", [False, True], ids=["--name", "ARISTOTLE_NAME"])
def test_a_blank_name_is_a_usage_error(monkeypatch: pytest.MonkeyPatch, capsys, from_environment: bool):
    argv = []
    if from_environment:
        monkeypatch.setenv("ARISTOTLE_NAME", "  ")
    else:
        argv = ["--name", "  "]

    with pytest.raises(SystemExit) as exit_info:
        _action().parser.parse_args(argv)

    assert exit_info.value.code == EXIT_USAGE_ERROR
    assert (
        "'  ' is not valid (from the flag or ARISTOTLE_NAME): Project name cannot be empty" in capsys.readouterr().err
    )


@pytest.mark.parametrize("value", ["0", "-5", "99999", "nan", "inf", "five"])
@pytest.mark.parametrize("from_environment", [False, True], ids=["--timeout", "ARISTOTLE_TIMEOUT"])
def test_an_invalid_timeout_says_why(monkeypatch: pytest.MonkeyPatch, capsys, value: str, from_environment: bool):
    argv = []
    if from_environment:
        monkeypatch.setenv("ARISTOTLE_TIMEOUT", value)
    else:
        argv = [f"--timeout={value}"]

    with pytest.raises(SystemExit) as exit_info:
        _action().parser.parse_args(argv)

    assert exit_info.value.code == EXIT_USAGE_ERROR
    assert (
        f"argument --timeout: '{value}' is not valid (from the flag or ARISTOTLE_TIMEOUT): the timeout must be a "
        f"positive number of minutes, at most {MAX_TIMEOUT_MINUTES}"
    ) in capsys.readouterr().err


def test_a_dry_run_counts_the_upload_and_needs_no_api_key(action: CIAction, repo: Path, tmp_path: Path, caplog):
    _commit(repo, ".env", "SECRET=hunter2\n")
    args = action.parser.parse_args(["--directory", str(repo), "--dry-run"], argparse.Namespace(api_key=None))

    with (
        patch("aristotlelib.cli.api_action.get_api_key", side_effect=ValueError("no key")) as get_api_key,
        patch("aristotlelib.cli.command.ci.runner.submit", AsyncMock()) as submit,
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        code = action.run(args)

    assert code == EXIT_SUCCESS
    get_api_key.assert_not_called()
    submit.assert_not_awaited()
    assert "Excluded 1 file(s) matching secret patterns: .env" in caplog.messages
    assert any(
        re.fullmatch(r"Dry run: would upload 2 files \(\d+ bytes\); checking 2 properties\.", m)
        for m in caplog.messages
    )
    assert f'Dry run: would name the project "repo: ci @ {_revision(repo)}".' in caplog.messages
    record = _record(repo)
    assert f"Dry run: would end the prompt with the run's record: {record.line}" in caplog.messages
    assert record.line.startswith(f"aristotle-ci-run: commit={_head(repo)} files=sha256:")
    assert not (tmp_path / DEFAULT_JUNIT_PATH).exists()


def test_a_run_whose_create_was_lost_and_not_found_exits_7(action: CIAction, repo: Path, caplog):
    args = action.parser.parse_args(["--directory", str(repo)], argparse.Namespace(api_key=None))
    lost = AristotleOutcomeUnknownError(
        "None of your newest projects is named repo 0123456789ab, so Aristotle either did not create it or has not "
        "yet. Check `aristotle list` before running this again."
    )

    with (
        patch("aristotlelib.cli.api_action.get_api_key", return_value="test-key"),
        patch("aristotlelib.cli.command.ci.runner.submit", AsyncMock(side_effect=lost)),
    ):
        code = action.run(args)

    assert code == EXIT_OUTCOME_UNKNOWN
    assert caplog.messages[-1] == str(lost)


def test_a_missing_manifest_exits_2_before_the_api_key_is_looked_for(
    action: CIAction, repo: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    (repo / manifest.MANIFEST_NAME).unlink()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    args = action.parser.parse_args(["--directory", str(repo)], argparse.Namespace(api_key=None))

    with patch("aristotlelib.cli.api_action.get_api_key", side_effect=ValueError("no key")) as get_api_key:
        code = action.run(args)

    assert code == EXIT_USAGE_ERROR
    get_api_key.assert_not_called()
    assert any(m.startswith("No aristotle.toml found.") for m in caplog.messages)
    assert "API key not found" not in caplog.text


async def test_a_directory_that_is_not_a_repository_exits_2_after_the_manifest_loads(
    action: CIAction, tmp_path: Path, caplog
):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / manifest.MANIFEST_NAME).write_text(MANIFEST)
    (plain / "aristotle.f").write_text("fifo.v\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(action, ["--directory", str(plain)])

    assert run.code == EXIT_USAGE_ERROR
    assert f"Checking 2 properties from {plain.resolve() / manifest.MANIFEST_NAME}:" in caplog.messages
    assert "is not a git repository" in caplog.messages[-1]
    assert "isn't tracked by git" not in caplog.text
    run.submit.assert_not_awaited()


def test_environment_variables_supply_the_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARISTOTLE_MANIFEST", "ci/aristotle.toml")
    monkeypatch.setenv("ARISTOTLE_FAIL_ON", "no-verdict")
    monkeypatch.setenv("ARISTOTLE_TIMEOUT", "45")
    monkeypatch.setenv("ARISTOTLE_JUNIT", "reports/aristotle.xml")
    monkeypatch.setenv("ARISTOTLE_DIRECTORY", "checkout")
    monkeypatch.setenv("ARISTOTLE_NAME", "nightly gate")

    args = _action().parser.parse_args([])

    assert args.manifest == Path("ci/aristotle.toml")
    assert str(args.fail_on) == "no-verdict"
    assert args.timeout == 45
    assert args.junit == Path("reports/aristotle.xml")
    assert args.directory == Path("checkout")
    assert args.name == "nightly gate"


@pytest.mark.parametrize("env", [True, False], ids=["ARISTOTLE_FAIL_ON", "flag"])
def test_fail_on_takes_unresolved_as_the_old_name_of_no_verdict(env: bool, monkeypatch: pytest.MonkeyPatch):
    if env:
        monkeypatch.setenv("ARISTOTLE_FAIL_ON", "unresolved")

    args = _action().parser.parse_args([] if env else ["--fail-on", "unresolved"])

    assert args.fail_on is results.FailOn.NO_VERDICT


def test_fail_on_lists_only_the_current_names(capsys):
    with pytest.raises(SystemExit):
        _action().parser.parse_args(["--fail-on", "bogus"])

    assert "'bogus' is not one of failure, no-verdict, never (from the flag or ARISTOTLE_FAIL_ON)" in (
        capsys.readouterr().err
    )
    assert "[--fail-on {failure,no-verdict,never}]" in _action().parser.format_usage()


REVIEW_OPTION_MESSAGE = (
    "{} is an option of `aristotle review`. `aristotle ci` checks the properties in aristotle.toml instead; "
    "`aristotle ci setup` writes one."
)


REVIEW_OPTIONS = [
    (["--scope", "changed"], "--scope"),
    (["--scope=changed"], "--scope"),
    (["--scope"], "--scope"),
    (["--path", "rtl/**"], "--path"),
    (["--path=rtl/**"], "--path"),
    (["--paths", "rtl/**"], "--path"),
    (["--base-ref", "main"], "--base-ref"),
    (["--base-ref=origin/main"], "--base-ref"),
    (["--instructions", "Skip the legacy bus."], "--instructions"),
    (["--instructions=Skip the legacy bus."], "--instructions"),
    (["--dry-run", "--scope", "all"], "--scope"),
    (["--scope", "all", "--dry-run"], "--scope"),
    (["--path", "rtl/**", "setup"], "--path"),
]
"""`aristotle ci` arguments that give a review option, and the option its message names."""


@pytest.mark.parametrize(("argv", "option"), REVIEW_OPTIONS)
def test_a_review_option_exits_2_saying_where_it_belongs(argv: list[str], option: str, capsys):
    with pytest.raises(SystemExit) as exit_info:
        _action().parser.parse_args(argv)

    assert exit_info.value.code == EXIT_USAGE_ERROR
    assert capsys.readouterr().err.endswith(f" error: {REVIEW_OPTION_MESSAGE.format(option)}\n")


@pytest.mark.parametrize(
    ("argv", "option"),
    [
        *REVIEW_OPTIONS,
        (["--base_ref", "main"], "--base-ref"),
        (["--base_ref=origin/main"], "--base-ref"),
        (["--dry_run", "--sco", "all"], "--scope"),
    ],
)
def test_a_review_option_in_any_spelling_exits_2_saying_where_it_belongs(argv, option, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["aristotle", "ci", *argv])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_USAGE_ERROR
    assert capsys.readouterr().err.endswith(f"\naristotle ci: error: {REVIEW_OPTION_MESSAGE.format(option)}\n")


def test_a_review_option_reaches_the_terminal_as_a_usage_error_said_once():
    command = [sys.executable, "-m", "aristotlelib.cli.controller", "ci", "--path", "rtl/**"]

    run = subprocess.run(command, capture_output=True, text=True, timeout=60)

    assert (run.returncode, run.stdout) == (EXIT_USAGE_ERROR, "")
    assert run.stderr.startswith("usage: aristotle ci ")
    assert run.stderr.endswith(f"\naristotle ci: error: {REVIEW_OPTION_MESSAGE.format('--path')}\n")
    assert run.stderr.count("is an option of") == 1


@pytest.mark.skipif(os.name == "nt", reason="Windows file names cannot hold a newline or an escape")
def test_on_github_a_file_name_cannot_put_a_workflow_command_in_the_log(repo: Path, monkeypatch: pytest.MonkeyPatch):
    for name in ("k\n::error file=fifo.v,line=1::forged by a file name\nz.pem", "e\x1b[8m.key"):
        (repo / name).write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "names")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    command = [sys.executable, "-m", "aristotlelib.cli.controller", "ci", "--dry-run", "--directory", str(repo)]

    run = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=60)

    assert run.returncode == EXIT_SUCCESS
    assert not [line for line in (run.stdout + run.stderr).splitlines() if line.lstrip().startswith("::")]
    assert (
        "Excluded 2 file(s) matching secret patterns: e\\x1b[8m.key, "
        "k\n\\::error file=fifo.v,line=1::forged by a file name\nz.pem\n"
    ) in run.stderr


async def test_on_github_the_runs_text_is_escaped_and_the_clis_own_annotations_still_print(
    action: CIAction, repo: Path, monkeypatch: pytest.MonkeyPatch, caplog, capsys
):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    escape_logged_text()

    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(
            action,
            ["--directory", str(repo), "--no-junit"],
            [
                _declaration("write_when_full", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE),
                _declaration("read_when_empty"),
            ],
            final_status=TaskStatus.COMPLETE_WITH_ERRORS,
            stopped_summary="Which depth?\n::stop-commands::x\n::error file=fifo.v,line=1::forged by the run",
        )

    assert run.code == EXIT_VERIFICATION_FAILED
    assert not [line for line in caplog.text.splitlines() if line.lstrip().startswith("::")]
    assert "    \\::stop-commands::x\n    \\::error file=fifo.v,line=1::forged by the run\n" in caplog.text
    assert [line.partition("::")[2].partition("::")[0] for line in capsys.readouterr().out.splitlines()] == [
        "error title=Aristotle stopped before finishing",
        "error title=Aristotle%3A write_when_full",
    ]


def test_review_options_are_not_in_cis_help():
    help_text = _action().parser.format_help()

    assert not any(option in help_text for option in ("--scope", "--path", "--base-ref", "--instructions"))


def test_on_timeouts_help_says_project_leaves_the_run_going(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COLUMNS", "2000")

    help_text = " ".join(_action().parser.format_help().split())

    assert "or the job is cancelled (default: cancel, or leave with --project)." in help_text


def test_an_empty_environment_variable_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch):
    # CI systems substitute unset inputs as the empty string.
    for name in (
        "ARISTOTLE_MANIFEST",
        "ARISTOTLE_FAIL_ON",
        "ARISTOTLE_TIMEOUT",
        "ARISTOTLE_DIRECTORY",
        "ARISTOTLE_NAME",
    ):
        monkeypatch.setenv(name, "")

    args = _action().parser.parse_args([])

    assert args.manifest is None
    assert str(args.fail_on) == "failure"
    assert args.timeout is None
    assert args.directory is None
    assert args.name is None


def test_an_unexpected_failure_is_an_infrastructure_exit_not_a_traceback(action, caplog, monkeypatch):
    """A build log should say what went wrong, not print a stack trace."""

    def boom(self, args):
        raise AristotleAPIError("Internal Server Error", status_code=500)

    monkeypatch.setattr(APIAction, "run", boom)

    with caplog.at_level(logging.ERROR):
        exit_code = action.run(argparse.Namespace(dry_run=False))

    assert exit_code == EXIT_INFRASTRUCTURE_ERROR
    assert "could not complete the run" in caplog.text
    assert "Traceback" not in caplog.text


def _run_whole(action: CIAction, argv: list[str], declarations: list[Declaration] | None = None) -> int:
    """run(), as the CLI calls it, with the network patched out."""
    args = action.parser.parse_args(argv, argparse.Namespace(api_key=None))
    with (
        patch("aristotlelib.cli.api_action.get_api_key", return_value="test-key"),
        patch("aristotlelib.cli.command.ci.runner.submit", AsyncMock(return_value=_project())),
        patch("aristotlelib.cli.command.ci.runner.wait_for_completion", AsyncMock(return_value=TaskStatus.COMPLETE)),
        patch("aristotlelib.cli.command.ci.runner.fetch_summary", AsyncMock(return_value=None)),
        patch("aristotlelib.cli.command.ci.runner.language_access", AsyncMock(return_value={})),
        patch.object(Project, "get_declarations", AsyncMock(return_value=declarations or [])),
    ):
        return action.run(args)


def test_on_github_an_error_that_ends_ci_before_a_run_is_an_annotation(
    action: CIAction, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """One line, the error as the log words it."""
    git(tmp_path, "init", "-q")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))

    assert _run_whole(action, []) == EXIT_USAGE_ERROR

    annotations = capsys.readouterr().out.splitlines()
    assert len(annotations) == 1
    assert annotations[0].startswith("::error title=aristotle ci::No aristotle.toml found. ")


def test_on_github_a_refused_project_is_an_annotation(
    action: CIAction, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """--project's refusal ends ci before it reports a run, so it is one too."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(repo))
    listed = [Mock(spec=AgentTask, status=TaskStatus.COMPLETE)] * 2
    with (
        patch.object(Project, "_resolve", AsyncMock(return_value=("test-123", None))),
        patch.object(Project, "from_id", AsyncMock(return_value=_project())),
        patch.object(Project, "get_tasks", AsyncMock(return_value=(listed, None))),
    ):
        assert _run_whole(action, ["--directory", str(repo), "--no-junit", "--project", "1a2b"]) == EXIT_USAGE_ERROR

    assert capsys.readouterr().out.splitlines() == [
        "::error title=aristotle ci::Project test-123 has more than the one run `aristotle ci` starts, such as a "
        f"follow-up, so its results aren't only what that run checked. To check this checkout, {FRESH}."
    ]


def test_on_github_an_unexpected_failure_is_an_annotation(action, monkeypatch: pytest.MonkeyPatch, capsys):
    def boom(self, args):
        raise RuntimeError("disk full")

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(APIAction, "run", boom)

    assert action.run(argparse.Namespace(dry_run=False)) == EXIT_INFRASTRUCTURE_ERROR

    assert capsys.readouterr().out == "::error title=aristotle ci::Aristotle could not complete the run: disk full\n"


@pytest.mark.parametrize(
    ("patches", "code", "message"),
    [
        (
            {"aristotlelib.cli.api_action.get_api_key": {"side_effect": ValueError("no key")}},
            EXIT_AUTH_ERROR,
            "Aristotle API key not found.",
        ),
        (
            {"aristotlelib.cli.command.ci.runner.submit": {"side_effect": AristotleAPIError("Unauthorized", 401)}},
            EXIT_AUTH_ERROR,
            "is not valid",
        ),
        (
            {"aristotlelib.cli.command.ci.runner.submit": {"side_effect": AristotleOutcomeUnknownError("lost")}},
            EXIT_OUTCOME_UNKNOWN,
            "lost",
        ),
    ],
    ids=["no key", "key rejected", "outcome unknown"],
)
def test_on_github_an_error_the_command_code_logs_is_an_annotation(
    action: CIAction, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys, patches, code: int, message: str
):
    """These are logged through the root logger by the code every command shares."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(repo))
    args = action.parser.parse_args(["--directory", str(repo), "--no-junit"], argparse.Namespace(api_key=None))
    with contextlib.ExitStack() as stack:
        if "aristotlelib.cli.api_action.get_api_key" not in patches:
            stack.enter_context(patch("aristotlelib.cli.api_action.get_api_key", return_value="test-key"))
        for target, kwargs in patches.items():
            stack.enter_context(patch(target, **kwargs))
        assert action.run(args) == code

    errors = [line for line in capsys.readouterr().out.splitlines() if line.startswith("::error")]
    assert len(errors) == 1 and errors[0].startswith("::error title=aristotle ci::") and message in errors[0]


def test_on_github_a_project_that_is_not_found_is_an_annotation(
    action: CIAction, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(repo))
    args = action.parser.parse_args(["--directory", str(repo), "--project", "1a2b"], argparse.Namespace(api_key=None))
    missing = AristotleNotFoundError("project", "1a2b")

    with (
        patch("aristotlelib.cli.api_action.get_api_key", return_value="test-key"),
        patch.object(Project, "_resolve", AsyncMock(side_effect=missing)),
    ):
        assert action.run(args) == EXIT_NOT_FOUND

    assert capsys.readouterr().out == f"::error title=aristotle ci::{missing}\n"


def test_on_github_an_invalid_option_is_an_annotation(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("ARISTOTLE_TIMEOUT", "99999")
    monkeypatch.setattr("sys.argv", ["aristotle", "ci"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_USAGE_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith(
        "::error title=aristotle ci::argument --timeout: '99999' is not valid (from the flag or ARISTOTLE_TIMEOUT): "
    )


def test_on_github_a_review_option_given_to_ci_is_an_annotation(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr("sys.argv", ["aristotle", "ci", "--scope", "changed"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_USAGE_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith(f"::error title=aristotle ci::{REVIEW_OPTION_MESSAGE.format('--scope')}\nusage: ")
    assert err.endswith(f"\naristotle ci: error: {REVIEW_OPTION_MESSAGE.format('--scope')}\n")


def test_a_working_folder_that_is_gone_ends_ci_without_a_traceback(action, monkeypatch: pytest.MonkeyPatch, caplog):
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    args = action.parser.parse_args(["--dry-run"], argparse.Namespace(api_key=None))
    monkeypatch.setattr("os.getcwd", Mock(side_effect=FileNotFoundError(2, "No such file or directory")))

    assert action.run(args) == EXIT_INFRASTRUCTURE_ERROR

    assert caplog.messages[-1] == "Aristotle could not complete the run: [Errno 2] No such file or directory"


def test_outside_github_an_error_that_ends_ci_before_a_run_is_only_logged(action: CIAction, tmp_path: Path, capsys):
    assert _run_whole(action, ["--directory", str(tmp_path)]) == EXIT_USAGE_ERROR

    assert "::error" not in capsys.readouterr().out


def test_on_github_a_reported_run_adds_no_annotation_for_its_errors(
    action: CIAction, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    refuted = [
        _declaration("write_when_full", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE),
        _declaration("read_when_empty"),
    ]

    assert _run_whole(action, ["--directory", str(repo), "--no-junit"], refuted) == EXIT_VERIFICATION_FAILED

    errors = [line for line in capsys.readouterr().out.splitlines() if line.startswith("::error")]
    assert len(errors) == 1 and "write_when_full" in errors[0] and "title=aristotle ci" not in errors[0]


def test_a_closed_pipe_on_stdout_is_left_for_the_cli_to_end_quietly(action, caplog, monkeypatch):
    def broken(self, args):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(APIAction, "run", broken)

    with pytest.raises(BrokenPipeError):
        action.run(argparse.Namespace(dry_run=False))

    assert caplog.text == ""


def test_a_report_on_a_closed_pipe_is_not_turned_into_a_warning(caplog):
    def annotate() -> None:
        raise BrokenPipeError(32, "Broken pipe")

    with pytest.raises(BrokenPipeError):
        _guarded("the GitHub annotations", annotate)

    assert caplog.text == ""


@pytest.mark.parametrize(
    ("fail_on", "expected"), [([], EXIT_VERIFICATION_FAILED), (["--fail-on", "never"], EXIT_SUCCESS)]
)
async def test_a_run_that_stopped_with_open_questions_fails_and_prints_them(
    action: CIAction, repo: Path, caplog, fail_on: list[str], expected: int
):
    questions = "Which reset polarity is intended?\n- active high\n- active low"
    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(
            action,
            ["--directory", str(repo), *fail_on],
            [_declaration("no_overflow", DeclarationStatus.IN_PROGRESS)],
            final_status=TaskStatus.COMPLETE_WITH_ERRORS,
            stopped_summary=questions,
        )

    assert run.code == expected
    assert "  Aristotle stopped before finishing:\n\n    Which reset polarity is intended?\n" in caplog.text
    assert "No property in the manifest was verified" not in caplog.text
    assert ("--fail-on never is set" in caplog.text) is (expected == EXIT_SUCCESS)


async def test_a_dry_run_leaves_out_a_tracked_name_that_is_not_utf8(action: CIAction, repo: Path, caplog):
    latin1_name(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "a Latin-1 name")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(action, ["--directory", str(repo), "--dry-run"])

    assert run.code == EXIT_SUCCESS
    assert any(
        re.fullmatch(r"Dry run: would upload 2 files \(\d+ bytes\); checking 2 properties\.", m)
        for m in caplog.messages
    )
    assert any(m.startswith("Not uploading 1 tracked file(s) whose name is not UTF-8") for m in caplog.messages)


COUNTEREXAMPLE_SUMMARY = "write_when_full fails: at cycle 12 a write to the full FIFO overwrites the oldest entry."


async def test_a_refuted_property_prints_the_runs_summary_which_names_the_counterexample(
    action: CIAction, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(
            action,
            ["--directory", str(repo)],
            [
                _declaration(
                    "write_when_full", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.NEGATION_PROVED
                ),
                _declaration("read_when_empty"),
            ],
            summary=COUNTEREXAMPLE_SUMMARY,
        )

    assert run.code == EXIT_VERIFICATION_FAILED
    assert f"  Aristotle's summary of the run:\n\n    {COUNTEREXAMPLE_SUMMARY}\n" in caplog.text
    assert (
        "The run's files, with any reproducer its summary names, are in its project. Get them with:\n"
        "  aristotle download test-123"
    ) in caplog.messages
    assert f"> {COUNTEREXAMPLE_SUMMARY}" in summary_file.read_text(encoding="utf-8")
    failure = _testcases(tmp_path / DEFAULT_JUNIT_PATH)["write_when_full"].find("failure")
    assert failure is not None and COUNTEREXAMPLE_SUMMARY in (failure.text or "")


async def test_a_stopped_run_with_a_refuted_property_reports_the_summary_it_stopped_with(
    action: CIAction, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog, capsys
):
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(
            action,
            ["--directory", str(repo)],
            [
                _declaration(
                    "write_when_full", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.NEGATION_PROVED
                ),
                _declaration("read_when_empty"),
            ],
            final_status=TaskStatus.COMPLETE_WITH_ERRORS,
            stopped_summary=f"{COUNTEREXAMPLE_SUMMARY}\nWhich reset polarity is intended?",
        )

    assert run.code == EXIT_VERIFICATION_FAILED
    run.fetch_summary.assert_not_awaited()
    assert caplog.text.count(COUNTEREXAMPLE_SUMMARY) == 1
    assert "Aristotle's summary of the run:" not in caplog.text
    assert f"> {COUNTEREXAMPLE_SUMMARY}" in summary_file.read_text(encoding="utf-8")
    failure = _testcases(tmp_path / DEFAULT_JUNIT_PATH)["write_when_full"].find("failure")
    assert failure is not None and COUNTEREXAMPLE_SUMMARY in (failure.text or "")
    assert github.SUMMARY_POINTER in capsys.readouterr().out


async def test_a_stopped_run_whose_summary_is_the_marker_alone_reports_a_refuted_property_without_one(
    action: CIAction, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    run = await _run(
        action,
        ["--directory", str(repo)],
        [_declaration("write_when_full", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.NEGATION_PROVED)],
        final_status=TaskStatus.COMPLETE_WITH_ERRORS,
        stopped_summary=runner.NO_STOPPED_SUMMARY,
    )

    assert run.code == EXIT_VERIFICATION_FAILED
    run.fetch_summary.assert_not_awaited()
    assert "Aristotle's summary of the run" not in summary_file.read_text(encoding="utf-8")
    failure = _testcases(tmp_path / DEFAULT_JUNIT_PATH)["write_when_full"].find("failure")
    assert failure is not None and "Aristotle's summary of the run" not in (failure.text or "")
    assert github.SUMMARY_POINTER not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("declarations", "final_status", "stopped_summary"),
    [
        ([_declaration(i) for i in IDS], TaskStatus.COMPLETE, None),
        (
            [_declaration("write_when_full", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.NEGATION_PROVED)],
            TaskStatus.COMPLETE_WITH_ERRORS,
            "Open questions:\n1. Which reset?",
        ),
    ],
    ids=["all proved", "stopped"],
)
async def test_the_runs_summary_is_fetched_unless_every_property_passed_or_the_run_stopped(
    action: CIAction,
    repo: Path,
    declarations: list[Declaration],
    final_status: TaskStatus | None,
    stopped_summary: str | None,
):
    run = await _run(
        action,
        ["--directory", str(repo), "--no-junit"],
        declarations,
        final_status=final_status,
        stopped_summary=stopped_summary,
        summary=COUNTEREXAMPLE_SUMMARY,
    )

    run.fetch_summary.assert_not_awaited()


UNCHECKED_SUMMARY = "write_when_full was never checked: the formal model drops the assertion."


async def test_a_finished_run_with_an_unresolved_property_prints_its_summary_and_says_the_build_still_passes(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(
            action,
            ["--directory", str(repo)],
            [_declaration("write_when_full", DeclarationStatus.IN_PROGRESS), _declaration("read_when_empty")],
            summary=UNCHECKED_SUMMARY,
        )

    assert run.code == EXIT_SUCCESS
    run.fetch_summary.assert_awaited_once()
    assert f"  Aristotle's summary of the run:\n\n    {UNCHECKED_SUMMARY}" in caplog.text
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == [
        "1 of the manifest's 2 properties have no verdict: write_when_full. Aristotle's summary of the run, above, "
        "may say why. The build still passes: --fail-on failure doesn't fail it on a property without a verdict, and "
        "--fail-on no-verdict does."
    ]
    skipped = _testcases(tmp_path / DEFAULT_JUNIT_PATH)["write_when_full"].find("skipped")
    assert skipped is not None and skipped.get("message").startswith(
        "In progress. The run finished without checking it."
    )


@pytest.mark.parametrize("fail_on", ["no-verdict", "never"])
async def test_only_fail_on_failure_warns_that_the_build_passes_with_a_property_in_progress(
    action: CIAction, repo: Path, caplog, fail_on: str
):
    with caplog.at_level(logging.WARNING, logger="aristotle"):
        await _run(
            action,
            ["--directory", str(repo), "--fail-on", fail_on, "--no-junit"],
            [_declaration("write_when_full", DeclarationStatus.IN_PROGRESS), _declaration("read_when_empty")],
        )

    assert "The build still passes" not in caplog.text


async def test_a_build_fail_on_no_verdict_fails_says_which_properties_have_no_verdict(
    action: CIAction, repo: Path, caplog
):
    """The summary line counts one unresolved apart from those in progress, which could read as "0 in progress"."""
    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(
            action,
            ["--directory", str(repo), "--fail-on", "no-verdict", "--no-junit"],
            [_declaration("write_when_full", DeclarationStatus.FAILED), _declaration("read_when_empty")],
        )

    assert run.code == EXIT_VERIFICATION_FAILED
    assert "1 proved, 0 refuted, 0 in progress, 1 unresolved" in caplog.text
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == [
        "1 of the manifest's 2 properties have no verdict: write_when_full. --fail-on no-verdict fails the build on a "
        "property without one."
    ]


@pytest.mark.parametrize(
    ("declarations", "final_status", "error"),
    [
        (
            [_declaration("write_when_full", DeclarationStatus.IN_PROGRESS)],
            TaskStatus.COMPLETE,
            "No property in the manifest was verified.",
        ),
        ([], TaskStatus.OUT_OF_BUDGET, "The run ran out of budget with no property under any of the manifest's ids."),
    ],
    ids=["nothing verified", "out of budget, nothing registered"],
)
async def test_an_error_after_the_printed_summary_points_up_to_it(
    action: CIAction, repo: Path, caplog, declarations, final_status: TaskStatus, error: str
):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        await _run(
            action,
            ["--directory", str(repo), "--no-junit"],
            declarations,
            final_status=final_status,
            summary=UNCHECKED_SUMMARY,
        )

    assert f"  Aristotle's summary of the run:\n\n    {UNCHECKED_SUMMARY}" in caplog.text
    [logged] = [m for m in caplog.messages if m.startswith(error)]
    assert logged.endswith(f"Its summary is above; the run is at {_project().web_url()}")


async def test_a_run_that_never_started_has_no_summary_to_fetch(action: CIAction, repo: Path):
    run = await _run(action, ["--directory", str(repo), "--no-junit"], [], final_status=TaskStatus.QUEUED)

    run.fetch_summary.assert_not_awaited()


@pytest.mark.parametrize(
    ("final_status", "fetched"), [(TaskStatus.OUT_OF_BUDGET, True), (None, False)], ids=["out of budget", "out of time"]
)
async def test_a_cut_short_runs_summary_is_fetched_once_the_run_has_ended(
    action: CIAction, repo: Path, tmp_path: Path, final_status: TaskStatus | None, fetched: bool
):
    run = await _run(
        action,
        ["--directory", str(repo)],
        [_declaration(i) for i in IDS],
        final_status=final_status,
        summary=UNCHECKED_SUMMARY,
    )

    assert run.fetch_summary.await_count == fetched
    run_error = _testcases(tmp_path / DEFAULT_JUNIT_PATH)["Aristotle run"].find("error")
    assert run_error is not None and (UNCHECKED_SUMMARY in (run_error.text or "")) == fetched


@pytest.mark.parametrize(
    "error",
    [
        AristotleAPIError("Service unavailable", status_code=503),
        runner.RunnerError("no task to wait on"),
        ValueError("Expecting value: line 1 column 1"),
        KeyError("agent_tasks"),
    ],
    ids=["api error", "no task", "not json", "no tasks key"],
)
async def test_a_summary_that_cannot_be_fetched_leaves_the_verdict_and_says_so(
    action: CIAction, repo: Path, caplog, error: Exception
):
    with caplog.at_level(logging.WARNING, logger="aristotle"):
        run = await _run(
            action,
            ["--directory", str(repo), "--no-junit"],
            [
                _declaration("write_when_full", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE),
                _declaration("read_when_empty"),
            ],
            summary=error,
        )

    assert run.code == EXIT_VERIFICATION_FAILED
    assert caplog.messages == [f"Could not fetch the run's summary: {error}"]


async def test_a_summary_that_takes_too_long_is_left_out(
    action: CIAction, repo: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    monkeypatch.setattr("aristotlelib.cli.command.ci.SUMMARY_FETCH_SECONDS", 0.05)

    async def stall(_project):
        await asyncio.sleep(1)

    with (
        patch("aristotlelib.cli.command.ci.runner.fetch_summary", side_effect=stall),
        caplog.at_level(logging.WARNING, logger="aristotle"),
    ):
        args = action.parser.parse_args(["--directory", str(repo), "--no-junit"])
        with (
            patch("aristotlelib.cli.command.ci.runner.submit", AsyncMock(return_value=_project())),
            patch(
                "aristotlelib.cli.command.ci.runner.wait_for_completion", AsyncMock(return_value=TaskStatus.COMPLETE)
            ),
            patch("aristotlelib.cli.command.ci.runner.fetch_stopped_summary", AsyncMock(return_value=None)),
            patch.object(
                Project,
                "get_declarations",
                AsyncMock(
                    return_value=[
                        _declaration(
                            "write_when_full", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.NEGATION_PROVED
                        )
                    ]
                ),
            ),
        ):
            assert action.preflight(args) is None
            code = await action.run_action(args)

    assert code == EXIT_VERIFICATION_FAILED
    assert caplog.messages[0] == "Could not fetch the run's summary: no answer in 0.05 s"


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
async def test_a_dry_run_counts_only_what_the_run_would_upload(action: CIAction, repo: Path, caplog):
    (repo / "outside.v").symlink_to("/etc/hostname")
    (repo / "dangling.v").symlink_to(repo / "missing.v")
    _commit(repo, "gone.v", "module gone;\nendmodule\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "links")
    (repo / "gone.v").unlink()

    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(action, ["--directory", str(repo), "--dry-run"])

    assert run.code == EXIT_SUCCESS
    assert any(m.startswith("Left out 2 symbolic link") for m in caplog.messages)
    assert any(
        m.startswith("Not uploading 1 tracked path(s) that are not files in the checkout") for m in caplog.messages
    )
    assert any(
        re.fullmatch(r"Dry run: would upload 2 files \(\d+ bytes\); checking 2 properties\.", m)
        for m in caplog.messages
    )


@pytest.mark.parametrize("dry_run", [["--dry-run"], []], ids=["dry run", "run"])
async def test_an_upload_over_the_size_limit_is_a_usage_error_naming_the_repository(
    action: CIAction, repo: Path, monkeypatch: pytest.MonkeyPatch, caplog, dry_run: list[str]
):
    monkeypatch.setattr("aristotlelib.ci.runner.local_file_utils.MAX_FILE_SIZE_BYTES", 100)
    args = action.parser.parse_args(["--directory", str(repo), "--no-junit", *dry_run])

    with (
        patch.object(Project, "create", AsyncMock()) as create,
        caplog.at_level(logging.ERROR, logger="aristotle"),
    ):
        assert action.preflight(args) is None
        code = await action.run_action(args)

    assert code == EXIT_USAGE_ERROR
    [message] = caplog.messages
    assert message.startswith(f"The upload of {repo.resolve()} is too large: 2 files (")
    assert "and Aristotle takes at most 0 MB (100 bytes)" in message
    create.assert_not_awaited()


def test_a_dry_run_lists_the_block_the_committed_aristotle_f_describes(action: CIAction, repo: Path, caplog):
    """An aristotle.f in simulator syntax is read as submit reads it, and a generated one goes up in its place."""
    (repo / "inc").mkdir()
    _commit(repo, "inc/defs.vh", "`define W 8\n")
    _commit(repo, "aristotle.f", "-sv\n+incdir+inc\nfifo.v\n")
    args = action.parser.parse_args(["--directory", str(repo), "--dry-run"], argparse.Namespace(api_key=None))

    with (
        patch("aristotlelib.cli.command.ci.runner.submit", AsyncMock()) as submit,
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        code = action.run(args)

    assert code == EXIT_SUCCESS
    submit.assert_not_awaited()
    assert "Using the Verilog filelist committed at aristotle.f" in caplog.messages
    assert any(m.startswith("A generated aristotle.f goes up in place of the folder's own") for m in caplog.messages)
    assert "aristotle.f, as it goes up:" in caplog.text
    assert "  --single-unit\n  -I inc\n  fifo.v" in caplog.text


def test_only_the_held_back_files_the_block_leaves_out_are_warned_of(action: CIAction, repo: Path, caplog):
    """The block takes a file it needs whatever an ignore rule says, so the warning names only the others."""
    (repo / "gen").mkdir()
    _commit(repo, "gen/defs.vh", "`define W 8\n")
    _commit(repo, "gen/unused.v", "module unused;\nendmodule\n")
    _commit(repo, "fifo.v", '`include "gen/defs.vh"\nmodule fifo;\nendmodule\n')
    _commit(repo, ".gitignore", "gen/\n")
    _commit(repo, "aristotle.f", "fifo.v\n")
    args = action.parser.parse_args(["--directory", str(repo), "--dry-run"], argparse.Namespace(api_key=None))

    with caplog.at_level(logging.INFO, logger="aristotle"):
        assert action.run(args) == EXIT_SUCCESS

    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == [
        local_file_utils.held_back_warning({Path("gen/unused.v"): ".gitignore:1: gen/"})
    ]


def test_a_block_whose_sources_an_ignore_rule_holds_back_still_goes_up(action: CIAction, repo: Path, caplog):
    """The upload set leaves out every Verilog file here, so without the block nothing would name the design."""
    _commit(repo, ".gitignore", "*.v\n")
    _commit(repo, "aristotle.f", "fifo.v\n")
    args = action.parser.parse_args(["--directory", str(repo), "--dry-run"], argparse.Namespace(api_key=None))

    with caplog.at_level(logging.INFO, logger="aristotle"):
        assert action.run(args) == EXIT_SUCCESS

    assert "Using the Verilog filelist committed at aristotle.f" in caplog.messages
    assert any(m.startswith("Uploading 1 tracked file") and m.endswith(": fifo.v") for m in caplog.messages)


def test_a_root_aristotle_f_an_ignore_rule_matches_is_read_and_named_in_the_prompt(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    _commit(repo, "aristotle.f", "fifo.v\n")
    _commit(repo, ".gitignore", "aristotle.f\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        prompt = _started_block_run(action, repo, tmp_path)

    assert "Using the Verilog filelist committed at aristotle.f" in caplog.messages
    assert "Use aristotle.f as the commands file." in prompt


def _blackboxed_checkout(repo: Path, named: bool = True) -> None:
    """A tracked vendor model, vendor/sram_1p.v, which the root aristotle.f black-boxes, and names when ``named``, with
    a vendor cell no file declares."""
    (repo / "vendor").mkdir()
    _commit(repo, "vendor/sram_1p.v", "module sram_1p;\nendmodule\n")
    _commit(repo, "aristotle.f", "--blackbox sram_1p\n--blackbox rom_8k\nfifo.v\n" + "vendor/sram_1p.v\n" * named)


NAMED = pytest.mark.parametrize("named", [True, False], ids=["named", "in no filelist"])


@NAMED
def test_a_black_box_leaves_its_tracked_file_out_and_is_named_in_the_prompt(
    action: CIAction, repo: Path, tmp_path, named: bool
):
    """The prompt still ends with the run's record."""
    _blackboxed_checkout(repo, named)
    scope_result = action._read_block(repo, scope.resolve_checkout(repo, warn=False))
    checked = manifest.load(repo / manifest.MANIFEST_NAME)

    packaged = runner.package(repo, scope_result, tmp_path / "upload.tar.gz", checked)

    with tarfile.open(tmp_path / "upload.tar.gz", encoding="utf-8") as tar:
        assert sorted(tar.getnames()) == ["aristotle.f", "aristotle.toml", "fifo.v"]
    assert (
        "\n\nUse aristotle.f as the commands file.\nTreat these modules as black boxes with unconstrained outputs; their "
        "sources aren't in the upload: sram_1p, rom_8k.\n"
    ) in packaged.prompt
    assert RunRecord.ending(packaged.prompt) == packaged.record


@NAMED
def test_a_dry_run_says_what_a_black_box_leaves_out_and_asks_in_the_prompt(
    action: CIAction, repo: Path, caplog, named: bool
):
    _blackboxed_checkout(repo, named)
    args = action.parser.parse_args(["--directory", str(repo), "--dry-run"], argparse.Namespace(api_key=None))

    with caplog.at_level(logging.INFO, logger="aristotle"):
        assert action.run(args) == EXIT_SUCCESS

    assert (
        "Black boxes, which the prompt names, and the files left out for them:\n"
        f"  sram_1p ({Path('repo', 'aristotle.f')}:1): {Path('repo', 'vendor', 'sram_1p.v')}\n"
        f"  rom_8k ({Path('repo', 'aristotle.f')}:2): no file in the upload declares it"
    ) in caplog.messages
    assert any(m.startswith("Dry run: would upload 3 files (") for m in caplog.messages)
    assert (
        "Dry run: would ask in the prompt: Treat these modules as black boxes with unconstrained outputs; their "
        "sources aren't in the upload: sram_1p, rom_8k."
    ) in caplog.messages


@pytest.mark.parametrize(
    ("named", "reason", "fix"),
    [
        (True, "named at {f}:4", "or black-box those too"),
        (
            False,
            "tracked, so it goes up",
            f"black-box those too, or keep the file out of the upload with a rule in {Path('repo', '.gitignore')}",
        ),
    ],
    ids=["named", "in no filelist"],
)
def test_a_tracked_file_sharing_a_black_box_with_another_module_stops(
    action: CIAction, repo: Path, caplog, named: bool, reason: str, fix: str
):
    _blackboxed_checkout(repo, named)
    _commit(repo, "vendor/sram_1p.v", "module sram_1p;\nendmodule\nmodule sram_ctrl;\nendmodule\n")

    assert _dry_run(action, repo, caplog) == EXIT_USAGE_ERROR

    f = Path("repo", "aristotle.f")
    assert caplog.messages[-1] == (
        f"{Path('repo', 'vendor', 'sram_1p.v')} ({reason.format(f=f)}) can't be left out for --blackbox sram_1p "
        f"({f}:1): it also declares sram_ctrl. Move sram_1p to a file of its own, {fix}."
    )


@pytest.mark.parametrize("blackbox", ["", "--blackbox sram_1p\n"], ids=["without a black box", "with one"])
def test_only_a_tracked_verilog_file_a_black_box_could_leave_out_is_checked_for_a_credentials_name(
    action: CIAction, repo: Path, monkeypatch, blackbox: str
):
    (repo / "vendor").mkdir()
    _commit(repo, "vendor/sram_1p.v", "module sram_1p;\nendmodule\n")
    _commit(repo, "vendor/notes.txt", "datasheet\n")
    _commit(repo, "aristotle.f", f"{blackbox}fifo.v\n")
    scope_result = scope.resolve_checkout(repo, warn=False)
    checked: list[Path] = []
    is_secret_path = local_file_utils.is_secret_path
    monkeypatch.setattr(
        local_file_utils, "is_secret_path", lambda path, root=None: checked.append(path) or is_secret_path(path, root)
    )

    action._read_block(repo, scope_result)

    assert checked == ([Path("vendor", "sram_1p.v")] if blackbox else [])


def test_a_file_a_black_box_leaves_out_that_an_ignore_rule_holds_back_is_not_warned_of(
    action: CIAction, repo: Path, caplog
):
    """It stays out either way, so the warning's advice on uploading it would mislead."""
    _blackboxed_checkout(repo)
    _commit(repo, "vendor/notes.txt", "datasheet\n")
    _commit(repo, ".gitignore", "vendor/\n")
    args = action.parser.parse_args(["--directory", str(repo), "--dry-run"], argparse.Namespace(api_key=None))

    with caplog.at_level(logging.INFO, logger="aristotle"):
        assert action.run(args) == EXIT_SUCCESS

    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == [
        local_file_utils.held_back_warning({Path("vendor/notes.txt"): ".gitignore:1: vendor/"})
    ]


async def test_project_reports_a_run_of_a_block_with_a_black_box(action: CIAction, repo: Path, tmp_path: Path):
    """Its record is of the upload without the file the black box leaves out, as this checkout's is."""
    _blackboxed_checkout(repo)
    started = _started_block_run(action, repo, tmp_path)

    (run, _) = await _follow(
        action, ["--directory", str(repo), "--no-junit"], prompt=started, declarations=[_declaration(i) for i in IDS]
    )

    assert run.code == EXIT_SUCCESS


async def test_a_block_whose_sources_an_ignore_rule_holds_back_counts_their_language(action: CIAction, repo: Path):
    """So a run that verifies nothing still says when Verilog isn't enabled for the account."""
    _commit(repo, ".gitignore", "*.v\n")
    _commit(repo, "aristotle.f", "fifo.v\n")

    run = await _run(action, ["--directory", str(repo), "--no-junit"], [])

    run.language_access.assert_awaited_once_with(["Verilog"])


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
async def test_a_block_file_linked_to_one_an_ignore_rule_holds_back_counts_its_language(action: CIAction, repo: Path):
    """The block uploads what the link leads to, though the upload set would leave the link out."""
    (repo / "fifo.v").unlink()
    (repo / "gen").mkdir()
    _commit(repo, "gen/impl.v", "module fifo;\nendmodule\n")
    (repo / "fifo.v").symlink_to(Path("gen", "impl.v"))
    git(repo, "add", "fifo.v")
    git(repo, "commit", "-qm", "link")
    _commit(repo, ".gitignore", "gen/\n")
    _commit(repo, "aristotle.f", "fifo.v\n")

    run = await _run(action, ["--directory", str(repo), "--no-junit"], [])

    run.language_access.assert_awaited_once_with(["Verilog"])


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
async def test_a_link_in_the_upload_to_a_held_back_block_file_counts_its_language(action: CIAction, repo: Path):
    """The link goes up, since the block uploads what it leads to, so its name counts as it would for any file."""
    (repo / "fifo.v").unlink()
    (repo / "gen").mkdir()
    _commit(repo, "gen/core.svh", "module fifo;\nendmodule\n")
    (repo / "core.sv").symlink_to(Path("gen", "core.svh"))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "link")
    _commit(repo, ".gitignore", "gen/\n")
    _commit(repo, "aristotle.f", "gen/core.svh\n")

    run = await _run(action, ["--directory", str(repo), "--no-junit"], [])

    run.language_access.assert_awaited_once_with(["Verilog"])


def test_an_ignored_root_aristotle_f_is_not_warned_of_as_held_back(action: CIAction, repo: Path, caplog):
    """It goes up with the block."""
    _commit(repo, "aristotle.f", "fifo.v\n")
    _commit(repo, ".gitignore", "aristotle.f\n")
    args = action.parser.parse_args(["--directory", str(repo), "--dry-run"], argparse.Namespace(api_key=None))

    with caplog.at_level(logging.INFO, logger="aristotle"):
        assert action.run(args) == EXIT_SUCCESS

    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize("dry_run", [True, False], ids=["--dry-run", "run"])
def test_a_block_file_that_changes_mid_run_ends_ci_with_its_reason(action: CIAction, repo: Path, caplog, dry_run: bool):
    """Exit 3, as a run that could not be completed, with no traceback."""
    _commit(repo, "aristotle.f", "fifo.v\n")
    argv = ["--directory", str(repo), "--no-junit", *(["--dry-run"] if dry_run else [])]
    args = action.parser.parse_args(argv, argparse.Namespace(api_key=None))
    changed = bundle.BundleError("fifo.v changed while it was being uploaded. Try again.")

    with (
        patch("aristotlelib.cli.api_action.get_api_key", return_value="test-key"),
        patch("aristotlelib.ci.runner.build_tarball", side_effect=changed),
    ):
        assert action.run(args) == EXIT_INFRASTRUCTURE_ERROR

    assert caplog.messages[-1] == str(changed)


def _block_checkout(repo: Path) -> None:
    """A block whose upload isn't the checkout's files: its aristotle.f is generated, fifo.v's include of a header by
    absolute path is rewritten, and that header is one an ignore rule holds back."""
    (repo / "gen").mkdir()
    _commit(repo, "gen/defs.vh", "`define W 8\n")
    _commit(repo, ".gitignore", "gen/\n")
    _commit(repo, "fifo.v", f'`include "{(repo / "gen" / "defs.vh").as_posix()}"\nmodule fifo;\nendmodule\n')
    _commit(repo, "aristotle.f", "-sv\n+incdir+gen\nfifo.v\n")


def _started_block_run(action: CIAction, repo: Path, tmp_path: Path, allowed: tuple[str, ...] = ()) -> str:
    """The prompt of a run `aristotle ci` starts on ``repo``'s block, with the folders --allow-untracked names
    ``allowed``, whose record is of the archive it uploads."""
    scope_result = action._read_block(
        repo, scope.resolve_checkout(repo, warn=False), allowed=allowed_folders(repo, allowed)
    )
    checked = manifest.load(repo / manifest.MANIFEST_NAME)
    return runner.package(repo, scope_result, tmp_path / "started.tar.gz", checked).prompt


async def test_project_reports_a_run_of_the_block_this_checkout_would_upload(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    """Without a word of the upload it doesn't make."""
    _block_checkout(repo)
    started = _started_block_run(action, repo, tmp_path)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        (run, _) = await _follow(
            action,
            ["--directory", str(repo), "--no-junit"],
            prompt=started,
            declarations=[_declaration(i) for i in IDS],
        )

    assert run.code == EXIT_SUCCESS
    assert not any("Uploading" in m or "aristotle.f" in m for m in caplog.messages)


async def test_project_refuses_a_run_of_the_block_after_a_change_to_a_file_only_the_block_uploads(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    _block_checkout(repo)
    started = _started_block_run(action, repo, tmp_path)
    (repo / "gen" / "defs.vh").write_text("`define W 16\n")

    assert (await _refused(action, repo, caplog, started)).startswith(
        "Project test-123's run checks other files than this checkout's, though both are at commit "
    )


async def test_project_refuses_a_block_a_fresh_run_couldnt_upload_before_any_request(
    action: CIAction, repo: Path, caplog
):
    """The checkout can't be the run's then, as a fresh run would stop on it."""
    (repo / "extra.v").write_text("module extra;\nendmodule\n")
    _commit(repo, "aristotle.f", "fifo.v\nextra.v\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        (run, resolve) = await _follow(action, ["--directory", str(repo), "--no-junit"])

    assert run.code == EXIT_USAGE_ERROR
    resolve.assert_not_awaited()
    assert "extra.v (named at " in caplog.messages[-1]
    assert "isn't tracked by git, and `aristotle ci` uploads only files git tracks there" in caplog.messages[-1]


async def test_project_ends_with_its_reason_when_a_block_file_changes_as_it_is_compared(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    """Exit 3, as a run that could not be completed, with no traceback."""
    _block_checkout(repo)
    started = _started_block_run(action, repo, tmp_path)
    changed = bundle.BundleError("fifo.v changed while it was being uploaded. Try again.")

    with patch("aristotlelib.ci.runner.build_tarball", side_effect=changed):
        (run, _) = await _follow(action, ["--directory", str(repo), "--no-junit"], prompt=started)

    assert run.code == EXIT_INFRASTRUCTURE_ERROR
    run.wait.assert_not_awaited()
    assert caplog.messages[-1] == str(changed)


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
def test_a_root_filelist_brings_its_block_past_any_ignore_rule(
    action: CIAction, repo: Path, rule: str, source: str, caplog
):
    """An ignore rule may leave the upload set no source, or no file at all; the block brings its own."""
    _ignoring(repo, rule, source)
    args = action.parser.parse_args(["--directory", str(repo), "--dry-run"], argparse.Namespace(api_key=None))
    with caplog.at_level(logging.INFO, logger="aristotle"):
        code = action.run(args)
    assert code == EXIT_SUCCESS
    assert "Using the Verilog filelist committed at aristotle.f" in caplog.messages
    assert f"The Verilog block's 1 file:\n  {source}  (named at " in caplog.text
    assert any(m.startswith("Dry run: would upload") for m in caplog.messages)


@pytest.mark.parametrize("untracked", [False, True], ids=["no aristotle.f", "an untracked aristotle.f"])
def test_an_ignore_rule_holding_back_everything_still_stops_without_a_root_filelist(
    action: CIAction, repo: Path, caplog, untracked: bool
):
    _ignoring(repo, "*", "fifo.v", filelist=False)
    if untracked:
        (repo / "aristotle.f").write_text("fifo.v\n")
    args = action.parser.parse_args(["--directory", str(repo), "--dry-run"], argparse.Namespace(api_key=None))
    assert action.run(args) == EXIT_USAGE_ERROR
    assert caplog.messages[-1] == f"No tracked files found in {repo}. Has the repository been checked out?"
    assert (caplog.messages[-2] == UNTRACKED) is untracked


def _blocked(action: CIAction, repo: Path, caplog, *argv: str) -> str:
    """Run a dry run, with ``argv``, that should stop on the block, and return its last message."""
    args = action.parser.parse_args(["--directory", str(repo), "--dry-run", *argv], argparse.Namespace(api_key=None))
    with patch("aristotlelib.cli.command.ci.runner.submit", AsyncMock()) as submit:
        code = action.run(args)
    assert code == EXIT_USAGE_ERROR
    submit.assert_not_awaited()
    return caplog.messages[-1]


def test_a_block_file_outside_the_checkout_stops_the_run(action: CIAction, repo: Path, tmp_path: Path, caplog):
    """ci uploads only files git tracks, so a pull request can't make it send one from the build machine."""
    (tmp_path / "wa").mkdir()
    (tmp_path / "wa" / "core.v").write_text("module core;\nendmodule\n")
    _commit(repo, "aristotle.f", f"{(tmp_path / 'wa' / 'core.v').as_posix()}\nfifo.v\n")
    message = _blocked(action, repo, caplog)
    assert message.endswith(
        f"(named at {Path('repo', 'aristotle.f')}:1) is outside the folder the step uploads, and `aristotle ci` uploads "
        "only files git tracks there. Commit the file inside that folder and name it from there, name a folder that "
        "holds it with --allow-untracked, or submit the block with `aristotle submit`."
    )


def test_ci_expands_no_environment_variable(action: CIAction, repo: Path, monkeypatch, caplog):
    """A define could otherwise carry a secret from the job's environment into the upload."""
    monkeypatch.setenv("JOB_SECRET", "placeholder-not-a-secret")
    _commit(repo, "aristotle.f", "+define+K=$JOB_SECRET\nfifo.v\n")
    message = _blocked(action, repo, caplog)
    assert message == (
        f"{Path('repo', 'aristotle.f')}:1: $JOB_SECRET isn't expanded, since `aristotle ci` expands no environment "
        "variable in aristotle.f, so a change can't send the job's environment"
    )
    assert "placeholder-not-a-secret" not in caplog.text


@pytest.mark.parametrize("where", ["outside", "untracked", "git", "credential"])
def test_a_filelist_ci_cant_upload_from_is_refused_before_it_is_read(
    action: CIAction, repo: Path, tmp_path: Path, where: str, caplog
):
    """A nested filelist is checked like a block file before it is read, so its content never reaches the upload or
    the log."""
    content = "+define+LEAK=placeholder-filelist-value\n"
    if where == "outside":
        (tmp_path / "defs.f").write_text(content)
        line, problem = f"-f {(tmp_path / 'defs.f').as_posix()}", "is outside the folder the step uploads"
    elif where == "untracked":
        (repo / "workspace.f").write_text(content)
        line, problem = "-F workspace.f", "isn't tracked by git"
    elif where == "git":
        line, problem = "-f .git/info/exclude", "is inside a .git folder"
    else:
        (repo / "gha-creds-0a1b2c.json").write_text('{"token": "placeholder-not-a-token"}\n')
        line, problem = "-f gha-creds-*.json", "isn't tracked by git"
    _commit(repo, "aristotle.f", f"{line}\nfifo.v\n")
    message = _blocked(action, repo, caplog)
    if where == "git":
        assert message.endswith("is inside a .git folder, which is never uploaded.")
    else:
        assert f"{problem}, and `aristotle ci` reads only filelists git tracks there" in message
        advice = "Commit the file inside that folder and name it from there," if where == "outside" else "Commit it,"
        # No folder --allow-untracked can take holds the folder the step uploads, or one above it.
        assert message.endswith(f"there. {advice} or submit the block with `aristotle submit`.")
    assert "placeholder" not in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_root_filelist_committed_as_a_link_out_of_the_checkout_is_refused(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    (tmp_path / "outside.f").write_text("+define+LEAK=placeholder-root-value\nfifo.v\n")
    (repo / "aristotle.f").symlink_to(tmp_path / "outside.f")
    git(repo, "add", "aristotle.f")
    git(repo, "commit", "-qm", "link")
    message = _blocked(action, repo, caplog)
    assert message.startswith(
        f"{Path('repo', 'aristotle.f')} is a link that leads outside the folder the step uploads, and `aristotle ci` "
        "reads only filelists git tracks there."
    )
    assert message.endswith("Commit what the link leads to in its place, or submit the block with `aristotle submit`.")
    assert "placeholder" not in caplog.text


def test_an_untracked_credential_in_the_workspace_stops_the_run(action: CIAction, repo: Path, caplog):
    """A credential file an earlier step wrote into the checkout, named by a glob, isn't tracked, so it stops."""
    (repo / "gha-creds-0a1b2c.json").write_text('{"type": "service_account"}\n')
    _commit(repo, "aristotle.f", "fifo.v\ngha-creds-*.json\n")
    message = _blocked(action, repo, caplog)
    assert "gha-creds-0a1b2c.json (named at" in message
    assert "isn't tracked by git, and `aristotle ci` uploads only files git tracks there" in message


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("target", ["a .git folder", "a bare repository"])
def test_a_tracked_link_into_a_git_directory_outside_is_refused_for_it(
    action: CIAction, repo: Path, tmp_path: Path, target: str, caplog
):
    """Whatever the git directory is named, so the refusal never advises committing what the link leads to."""
    if target == "a .git folder":
        (tmp_path / "other").mkdir()
        git(tmp_path / "other", "init", "-q")
        destination = tmp_path / "other" / ".git" / "config"
    else:
        git(tmp_path, "init", "-q", "--bare", "other.git")
        destination = tmp_path / "other.git" / "config"
    (repo / "cfg.vh").symlink_to(destination)
    git(repo, "add", "cfg.vh")
    _commit(repo, "fifo.v", '`include "cfg.vh"\nmodule fifo;\nendmodule\n')
    _commit(repo, "aristotle.f", "fifo.v\n")
    message = _blocked(action, repo, caplog)
    assert message.endswith(
        "cfg.vh (included at " + str(Path("repo", "fifo.v")) + ":1) is inside a .git folder, which is never uploaded."
    )


def test_an_untracked_credential_in_the_folder_is_refused_as_one(action: CIAction, repo: Path, caplog):
    """Named before the tracked-only rule, so the refusal never advises committing it."""
    (repo / ".env").write_text("TOKEN=placeholder-inside\n")
    _commit(repo, "aristotle.f", "fifo.v\n.env\n")
    message = _blocked(action, repo, caplog)
    assert "is named like a credential, and such files are never uploaded." in message
    assert "Commit" not in message
    assert "placeholder" not in caplog.text


@pytest.mark.parametrize("how", ["included", "upper case", "linked", "named link", "outside", "outside link"])
def test_a_credential_inside_a_git_folder_is_refused_for_the_folder(
    action: CIAction, repo: Path, tmp_path: Path, how: str, caplog
):
    """As submit does: renaming it wouldn't help, so the .git folder, in any case, is the reason given, as named or
    where a link leads, inside the folder or outside it."""
    if "link" in how and os.name == "nt":
        pytest.skip("symlinks need privileges on Windows")
    (tmp_path / "gitdir").mkdir()
    (tmp_path / "gitdir" / "deploy.key").write_text("placeholder-key\n")
    if how.startswith("outside"):
        if how == "outside link":
            (tmp_path / "bar").mkdir()
            (tmp_path / "bar" / ".git").symlink_to(tmp_path / "gitdir")
            outside = tmp_path / "bar" / ".git" / "deploy.key"
        else:
            (tmp_path / ".git").mkdir()
            outside = tmp_path / ".git" / "deploy.key"
            outside.write_text("placeholder-key\n")
        _commit(repo, "aristotle.f", f"fifo.v\n{outside.as_posix()}\n")
    else:
        if how == "named link":
            (repo / "vendor").mkdir()
            (repo / "vendor" / ".git").symlink_to(tmp_path / "gitdir")
            name = "vendor/.git/deploy.key"
        elif how == "upper case":
            (repo / ".GIT").mkdir(exist_ok=True)
            (repo / ".GIT" / "deploy.key").write_text("placeholder-key\n")
            name = ".GIT/deploy.key"
        else:
            (repo / ".git" / "deploy.key").write_text("placeholder-key\n")
            name = ".git/deploy.key"
            if how == "linked":
                (repo / "cfg.vh").symlink_to(repo / ".git" / "deploy.key")
                git(repo, "add", "cfg.vh")
                name = "cfg.vh"
        _commit(repo, "fifo.v", f'`include "{name}"\nmodule fifo;\nendmodule\n')
        _commit(repo, "aristotle.f", "fifo.v\n")
    assert _blocked(action, repo, caplog).endswith("is inside a .git folder, which is never uploaded.")
    assert "placeholder" not in caplog.text


def test_an_include_of_git_config_stops_the_run(action: CIAction, repo: Path, caplog):
    """.git/config can hold the job's token, so an include reaching it stops the run."""
    _commit(repo, "fifo.v", '`include ".git/config"\nmodule fifo;\nendmodule\n')
    _commit(repo, "aristotle.f", "fifo.v\n")
    assert "is inside a .git folder, which is never uploaded" in _blocked(action, repo, caplog)


@pytest.mark.skipif(not os.path.isfile("/proc/self/environ"), reason="needs Linux's /proc")
def test_an_include_of_the_process_environment_stops_the_run(action: CIAction, repo: Path, caplog):
    """The job's environment holds its keys, so a file in /proc never goes up, and no advice leads to it."""
    _commit(repo, "fifo.v", '`include "/proc/self/environ"\nmodule fifo;\nendmodule\n')
    _commit(repo, "aristotle.f", "fifo.v\n")
    assert _blocked(action, repo, caplog).endswith(
        f"environ (included at {Path('repo', 'fifo.v')}:1) is in /proc, which is never uploaded."
    )


def test_a_committed_aristotle_f_naming_a_missing_file_stops_the_run(action: CIAction, repo: Path, caplog):
    _commit(repo, "aristotle.f", "fifo.v\nmissing.v\n")

    assert (
        _blocked(action, repo, caplog) == f"{Path('repo', 'aristotle.f')}:2: {Path('repo', 'missing.v')}: no such file"
    )


DRY_RUN = pytest.mark.parametrize("dry_run", [["--dry-run"], []], ids=["dry run", "run"])


@DRY_RUN
async def test_the_first_line_of_aristotle_f_ci_cant_read_stops_it_before_anything_goes_up(
    action: CIAction, repo: Path, caplog, dry_run: list[str]
):
    _commit(repo, "aristotle.f", "fifo.v\n--frobnicate\nmissing.v\n")

    run = await _run(action, ["--directory", str(repo), *dry_run])

    assert run.code == EXIT_USAGE_ERROR
    run.submit.assert_not_awaited()
    assert caplog.messages[-1] == f"{Path('repo', 'aristotle.f')}:2: --frobnicate is not an option Aristotle knows"


@DRY_RUN
async def test_an_aristotle_f_that_cant_be_read_stops_ci_naming_it_alone(
    action: CIAction, repo: Path, caplog, dry_run: list[str]
):
    """It is the repository's, not the command line's."""
    _commit(repo, "aristotle.f", "fifo.v\n")
    (repo / "aristotle.f").chmod(0)
    try:
        if os.access(repo / "aristotle.f", os.R_OK):
            pytest.skip("running as a user who can read any file")
        run = await _run(action, ["--directory", str(repo), *dry_run])
    finally:
        (repo / "aristotle.f").chmod(0o644)

    assert run.code == EXIT_USAGE_ERROR
    run.submit.assert_not_awaited()
    assert caplog.messages[-1] == f"Can't read {Path('repo', 'aristotle.f')}: Permission denied"


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permission bits, which root bypasses")
async def test_an_untracked_root_aristotle_f_linked_into_a_folder_it_cant_enter_is_named_too(
    action: CIAction, repo: Path, caplog
):
    target = repo.parent / "locked" / "top.f"
    target.parent.mkdir()
    target.write_text("fifo.v\n")
    (repo / "aristotle.f").symlink_to(target)
    target.parent.chmod(0o600)
    try:
        with caplog.at_level(logging.INFO, logger="aristotle"):
            run = await _run(action, ["--directory", str(repo), "--dry-run"])
    finally:
        target.parent.chmod(0o700)

    assert run.code == EXIT_SUCCESS
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [UNTRACKED]


@DRY_RUN
@pytest.mark.parametrize("ignored", [False, True], ids=["untracked", "ignored"])
async def test_a_root_aristotle_f_git_doesnt_track_is_named_and_stays_out(
    action: CIAction, repo: Path, caplog, dry_run: list[str], ignored: bool
):
    if ignored:
        _commit(repo, ".gitignore", "aristotle.f\n")
    (repo / "aristotle.f").write_text("fifo.v\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        run = await _run(action, ["--directory", str(repo), *dry_run], [_declaration(i) for i in IDS])

    assert run.code == EXIT_SUCCESS
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [UNTRACKED]
    if dry_run:
        assert "No Verilog filelist is uploaded. Aristotle will infer the build configuration from the sources." in (
            caplog.messages
        )
        run.submit.assert_not_awaited()
    else:
        scope_result = run.submit.await_args.args[1]
        assert scope_result.block is None
        assert Path("aristotle.f") not in scope_result.upload_files


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize(
    ("target", "problem"),
    [
        ("outside", "is a link that leads outside the folder the step uploads"),
        ("untracked", "leads to a file git doesn't track"),
        ("secret", "leads to a file named like a credential"),
    ],
)
def test_a_tracked_link_cant_carry_a_file_past_the_tracked_only_rule(
    action: CIAction, repo: Path, tmp_path: Path, target: str, problem: str, caplog
):
    """A link git tracks still has to lead to a tracked file in the folder, not named like a credential."""
    if target == "outside":
        (tmp_path / "secret.txt").write_text("TOKEN\n")
        destination = tmp_path / "secret.txt"
    elif target == "untracked":
        (repo / "workspace-creds.json").write_text("{}\n")
        destination = repo / "workspace-creds.json"
    else:
        (repo / ".env").write_text("TOKEN=x\n")
        git(repo, "add", "-f", ".env")
        destination = repo / ".env"
    (repo / "cfg.vh").symlink_to(destination)
    git(repo, "add", "cfg.vh")
    _commit(repo, "fifo.v", '`include "cfg.vh"\nmodule fifo;\nendmodule\n')
    _commit(repo, "aristotle.f", "fifo.v\n")
    message = _blocked(action, repo, caplog)
    assert f"cfg.vh (included at {Path('repo', 'fifo.v')}:1) {problem}" in message
    if target == "outside":
        assert message.endswith(
            "Commit what the link leads to in its place, or submit the block with `aristotle submit`."
        )


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_file_reached_through_a_linked_folder_names_the_link(action: CIAction, repo: Path, tmp_path: Path, caplog):
    """The file itself isn't the link here, so the refusal says which folder on its path leads out."""
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "core.v").write_text("module core;\nendmodule\n")
    (repo / "rtl").symlink_to(tmp_path / "shared")
    git(repo, "add", "rtl")
    _commit(repo, "aristotle.f", "fifo.v\nrtl/core.v\n")
    message = _blocked(action, repo, caplog)
    assert message.endswith(
        f"{Path('repo', 'rtl', 'core.v')} (named at {Path('repo', 'aristotle.f')}:2) is reached through "
        f"{Path('repo', 'rtl')}, a link that leads outside the folder the step uploads, and `aristotle ci` uploads only "
        "files git tracks there. Commit what the link leads to in its place, name a folder that holds that with "
        "--allow-untracked, or submit the block with `aristotle submit`."
    )


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize(
    ("how", "problem"),
    [("named", "is named like a credential"), ("linked", "leads to a file named like a credential")],
)
def test_a_credential_outside_the_folder_is_refused_as_one(
    action: CIAction, repo: Path, tmp_path: Path, how: str, problem: str, caplog
):
    """Named first, so the refusal never advises committing it, under its own name or a link's."""
    (tmp_path / ".env").write_text("TOKEN=placeholder-outside\n")
    if how == "named":
        _commit(repo, "aristotle.f", f"fifo.v\n{(tmp_path / '.env').as_posix()}\n")
    else:
        (repo / "cfg.vh").symlink_to(tmp_path / ".env")
        git(repo, "add", "cfg.vh")
        _commit(repo, "fifo.v", '`include "cfg.vh"\nmodule fifo;\nendmodule\n')
        _commit(repo, "aristotle.f", "fifo.v\n")
    message = _blocked(action, repo, caplog)
    assert f"{problem}, and such files are never uploaded." in message
    assert "Commit" not in message
    assert "placeholder" not in caplog.text


def test_an_untracked_block_file_is_refused_before_its_includes_are_read(action: CIAction, repo: Path, caplog):
    """An untracked file's own includes never reach the log: the file is refused before it is read."""
    (repo / "build").mkdir()
    (repo / "build" / "gen.vh").write_text('`include "../.git/placeholder-from-untracked"\n')
    (repo / ".git" / "placeholder-from-untracked").write_text("x\n")
    _commit(repo, "fifo.v", '`include "build/gen.vh"\nmodule fifo;\nendmodule\n')
    _commit(repo, "aristotle.f", "fifo.v\n")
    message = _blocked(action, repo, caplog)
    assert f"gen.vh (included at {Path('repo', 'fifo.v')}:1) isn't tracked by git" in message
    assert "placeholder" not in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_tracked_file_through_a_folder_link_goes_up(action: CIAction, repo: Path, caplog):
    """git lists no path through a linked folder, so a file reached through one counts as tracked by its target."""
    (repo / "rtl").mkdir()
    _commit(repo, "rtl/core.v", "module core;\nendmodule\n")
    (repo / "ip").symlink_to(repo / "rtl")
    git(repo, "add", "ip")
    _commit(repo, "aristotle.f", "ip/core.v\nfifo.v\n")
    args = action.parser.parse_args(["--directory", str(repo), "--dry-run"], argparse.Namespace(api_key=None))

    with (
        patch("aristotlelib.cli.command.ci.runner.submit", AsyncMock()),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        code = action.run(args)

    assert code == EXIT_SUCCESS
    assert "  ip/core.v  (named at" in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_link_into_a_credential_folder_is_refused_with_advice_to_move(action: CIAction, repo: Path, caplog):
    """A tracked file in a folder that holds credentials doesn't go up through a link either."""
    (repo / ".env").mkdir()
    _commit(repo, ".env/x.vh", "`define X 1\n")
    (repo / "cfg.vh").symlink_to(repo / ".env" / "x.vh")
    git(repo, "add", "cfg.vh")
    _commit(repo, "fifo.v", '`include "cfg.vh"\nmodule fifo;\nendmodule\n')
    _commit(repo, "aristotle.f", "fifo.v\n")
    assert _blocked(action, repo, caplog).endswith(
        "leads into a folder that holds credentials, and such files are never uploaded. Leave it out of the block."
    )


@pytest.mark.parametrize("folder", [".docker", ".env"])
def test_a_block_file_in_a_folder_that_holds_credentials_is_refused(action: CIAction, repo: Path, caplog, folder: str):
    """As the upload leaves such a folder out, whatever its files are called."""
    (repo / folder).mkdir()
    _commit(repo, f"{folder}/regs.v", "module regs;\nendmodule\n")
    _commit(repo, "aristotle.f", f"fifo.v\n{folder}/regs.v\n")
    assert _blocked(action, repo, caplog).endswith(
        "is in a folder that holds credentials, and such files are never uploaded. Leave it out of the block."
    )


@pytest.mark.parametrize("parent", [".env.d", ".git"])
def test_a_checkout_under_a_folder_named_like_a_credential_still_runs(
    action: CIAction, tmp_path: Path, parent: str, caplog
):
    """Only folders inside the checkout count against a file, not the ones it sits under."""
    root = tmp_path / parent / "repo"
    root.mkdir(parents=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "fifo.v").write_text("module fifo;\nendmodule\n")
    (root / manifest.MANIFEST_NAME).write_text(MANIFEST)
    (root / "aristotle.f").write_text("-sv\nfifo.v\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "initial")
    args = action.parser.parse_args(["--directory", str(root), "--dry-run"], argparse.Namespace(api_key=None))

    with (
        patch("aristotlelib.cli.command.ci.runner.submit", AsyncMock()),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        code = action.run(args)

    assert code == EXIT_SUCCESS
    assert "aristotle.f, as it goes up:" in caplog.text


def _generated(repo: Path, tmp_path: Path) -> Path:
    """A block whose aristotle.f names fifo.v and gen/regs.v, a source the build writes, which an ignore rule keeps
    untracked, and whose -I folder is ../shared, outside the checkout, with the header regs.v includes; that folder."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "defs.vh").write_text("`define W 8\n")
    (repo / "gen").mkdir()
    (repo / "gen" / "regs.v").write_text('`include "defs.vh"\nmodule regs;\nendmodule\n')
    _commit(repo, ".gitignore", "gen/\n")
    _commit(repo, "aristotle.f", "+incdir+../shared\nfifo.v\ngen/regs.v\n")
    return shared


ALLOWED = ("--allow-untracked", "repo/gen", "--allow-untracked", "shared")
"""The folders _generated()'s block needs named, from the current directory, which holds the repository."""


def _dry_run(action: CIAction, repo: Path, caplog, *argv: str) -> int:
    args = action.parser.parse_args(["--directory", str(repo), "--dry-run", *argv], argparse.Namespace(api_key=None))
    with (
        patch("aristotlelib.cli.command.ci.runner.submit", AsyncMock()) as submit,
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        code = action.run(args)
    submit.assert_not_awaited()
    return code


def test_allow_untracked_lists_each_file_it_lets_through_with_its_folder(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    """A generated source and a shared header go up, each named with the folder that lets it through, and not as a
    tracked file an ignore rule holds back."""
    shared = _generated(repo, tmp_path)

    assert _dry_run(action, repo, caplog, *ALLOWED) == EXIT_SUCCESS

    width = len("_ext/shared/defs.vh")
    assert (
        "Uploading 2 files git doesn't track in the folder the step uploads, which --allow-untracked lets through:\n"
        f"  {'gen/regs.v'.ljust(width)}  (--allow-untracked repo/gen)\n"
        f"  _ext/shared/defs.vh <- {shared / 'defs.vh'}  (--allow-untracked shared)"
    ) in caplog.messages
    assert f"  _ext/shared/defs.vh <- {shared / 'defs.vh'}  (included at " in caplog.text
    assert not any("tracked file" in m for m in caplog.messages)


def test_allow_untracked_uploads_a_shared_file_under_ext(action: CIAction, repo: Path, tmp_path: Path):
    """As submit uploads a file from outside its folder, with the generated aristotle.f naming its -I folder there."""
    _generated(repo, tmp_path)
    scope_result = action._read_block(
        repo, scope.resolve_checkout(repo, warn=False), allowed=allowed_folders(repo, ["repo/gen", "shared"])
    )

    runner.package(repo, scope_result, tmp_path / "upload.tar.gz", manifest.load(repo / manifest.MANIFEST_NAME))

    with tarfile.open(tmp_path / "upload.tar.gz") as tar:
        names = sorted(tar.getnames())
        generated = tar.extractfile("aristotle.f")
        assert generated is not None
        lines = generated.read().decode().splitlines()
    assert names == [".gitignore", "_ext/shared/defs.vh", "aristotle.f", "aristotle.toml", "fifo.v", "gen/regs.v"]
    assert "-I _ext/shared" in lines
    assert lines[-2:] == ["fifo.v", "gen/regs.v"]
    assert scope_result.allowed == {"gen/regs.v", "_ext/shared/defs.vh"}


@pytest.mark.parametrize("allowed", [False, True], ids=["", "--allow-untracked"])
@pytest.mark.parametrize(("folder", "sibling"), WILDCARD_FOLDERS)
def test_a_checkout_whose_path_holds_a_wildcard_uploads_only_its_own_block(
    action: CIAction, repo: Path, tmp_path: Path, caplog, folder: str, sibling: str, allowed: bool
):
    """Not the block of a sibling folder that the path matches as a pattern, even one --allow-untracked names."""
    checkout = repo.rename(tmp_path / folder)
    _commit(checkout, "aristotle.f", "fifo.v\n")
    (tmp_path / sibling).mkdir()
    (tmp_path / sibling / "core.v").write_text("module core;\nendmodule\n")
    (tmp_path / sibling / "aristotle.f").write_text("core.v\n")

    assert _dry_run(action, checkout, caplog, *(["--allow-untracked", sibling] if allowed else [])) == EXIT_SUCCESS

    assert (
        f"The Verilog block's 1 file:\n  fifo.v  (named at {Path(folder, 'aristotle.f')}:1)\n"
        "aristotle.f goes up as it is."
    ) in caplog.messages
    assert any(m.startswith("Dry run: would upload 3 files (") for m in caplog.messages)


@pytest.mark.skipif(os.name == "nt", reason="Windows names can't hold ?")
def test_a_block_file_whose_path_holds_a_wildcard_stops_the_step_naming_it(action: CIAction, repo: Path, caplog):
    """The generated aristotle.f would name it by a pattern, which the server could match to other files."""
    (repo / "a?b").mkdir()
    _commit(repo, "a?b/fifo.v", "module fifo;\nendmodule\n")
    _commit(repo, "aristotle.f", "a?b/fifo.v\n")

    assert _dry_run(action, repo, caplog) == EXIT_USAGE_ERROR

    named = filelist.shown(repo / "aristotle.f")
    assert any(m.startswith(f'a?b/fifo.v ({named}:1) has "?" in its path') for m in caplog.messages)


def test_without_allow_untracked_a_generated_file_stops_the_run_naming_it(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    _generated(repo, tmp_path)

    assert _blocked(action, repo, caplog) == (
        f"{Path('repo', 'gen', 'regs.v')} (named at {Path('repo', 'aristotle.f')}:3) isn't tracked by git, and "
        "`aristotle ci` uploads only files git tracks there. Commit it, name a folder that holds it with "
        "--allow-untracked, or submit the block with `aristotle submit`."
    )


def test_a_file_no_folder_allow_untracked_names_holds_still_stops_the_run(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    _generated(repo, tmp_path)

    assert _blocked(action, repo, caplog, "--allow-untracked", "repo/gen") == (
        f"{Path('shared', 'defs.vh')} (included at {Path('repo', 'gen', 'regs.v')}:1) is outside the folder the step "
        "uploads, and no folder --allow-untracked names holds it. Commit the file inside that folder and name it from "
        "there, name a folder that holds it with --allow-untracked, or submit the block with `aristotle submit`."
    )


def test_allow_untracked_reads_a_filelist_in_a_named_folder_and_says_so(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    """Once, though it is named twice."""
    shared = _generated(repo, tmp_path)
    (shared / "shared.f").write_text("+define+SHARED\n")
    _commit(
        repo, "aristotle.f", "+incdir+../shared\n-f ../shared/shared.f\n-f ../shared/shared.f\nfifo.v\ngen/regs.v\n"
    )

    assert _dry_run(action, repo, caplog, *ALLOWED) == EXIT_SUCCESS

    assert (
        "Reading 1 filelist git doesn't track in the folder the step uploads, which --allow-untracked lets through:\n"
        f"  {Path('shared', 'shared.f')}  (--allow-untracked shared)"
    ) in caplog.text
    assert "  -D SHARED\n" in caplog.text


@pytest.mark.parametrize(
    ("folder", "problem"),
    [
        ("repo/missing", "--allow-untracked repo/missing: {repo}{sep}missing doesn't exist."),
        ("repo/fifo.v", "--allow-untracked repo/fifo.v: {repo}{sep}fifo.v is not a folder."),
        (
            "repo",
            "--allow-untracked repo is the folder the step uploads, {repo}, which may hold files git doesn't track",
        ),
        (".", "--allow-untracked . holds the folder the step uploads, {repo}, which may hold files git doesn't track"),
    ],
    ids=["missing", "a file", "the folder", "above it"],
)
def test_a_folder_allow_untracked_cant_take_stops_the_run_before_the_block_is_read(
    action: CIAction, repo: Path, folder: str, problem: str, caplog
):
    """Naming the folder the step uploads, or one that holds it, would let any untracked file in it go up, such as a
    credential an earlier step wrote there."""
    _commit(repo, "aristotle.f", "fifo.v\n")

    message = _blocked(action, repo, caplog, "--allow-untracked", folder)

    assert message.startswith(problem.format(repo=repo, sep=os.sep))
    assert "Using the Verilog filelist" not in caplog.text


def test_allow_untracked_names_no_empty_folder(action: CIAction, capsys):
    with pytest.raises(SystemExit) as exited:
        action.parser.parse_args(["--allow-untracked", ""])

    assert exited.value.code == EXIT_USAGE_ERROR
    assert "argument --allow-untracked: names no folder" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("target", ["elsewhere", "root"])
def test_a_folder_allow_untracked_reaches_through_a_link_stops_the_run(
    action: CIAction, repo: Path, tmp_path: Path, target: str, caplog
):
    """A change could commit the link and point it anywhere, as at the file system's root."""
    destination = tmp_path / "elsewhere" if target == "elsewhere" else Path(os.path.abspath(os.sep))
    destination.mkdir(exist_ok=True)
    (repo / "gen").symlink_to(destination)
    _commit(repo, "aristotle.f", "fifo.v\n")

    assert _blocked(action, repo, caplog, "--allow-untracked", "repo/gen") == (
        f"--allow-untracked repo/gen: {repo / 'gen'} is reached through a link, to {destination}. Name the folder by the "
        "path it leads to instead."
    )


@pytest.mark.parametrize("where", ["aristotle.f", "aristotle.toml", "environment"])
def test_nothing_but_the_command_line_names_a_folder_for_allow_untracked(
    action: CIAction, repo: Path, tmp_path: Path, monkeypatch, where: str, caplog
):
    """A change can edit the repository's files, and a variable could come from elsewhere than the job's command."""
    _generated(repo, tmp_path)
    if where == "aristotle.f":
        _commit(repo, "aristotle.f", "--allow-untracked gen\n+incdir+../shared\nfifo.v\ngen/regs.v\n")
        problem = f"{Path('repo', 'aristotle.f')}:1: --allow-untracked is not an option Aristotle knows"
    elif where == "aristotle.toml":
        _commit(repo, manifest.MANIFEST_NAME, f'{MANIFEST}\n[check]\nallow_untracked = ["gen", "../shared"]\n')
        problem = "check.allow_untracked: unknown key"
    else:
        monkeypatch.setenv("ARISTOTLE_ALLOW_UNTRACKED", "gen")
        problem = "isn't tracked by git, and `aristotle ci` uploads only files git tracks there."
    action = _action()

    assert problem in _blocked(action, repo, caplog)


@pytest.mark.parametrize(
    "folders", [("repo/gen",), ("repo/gen", "repo/.git")], ids=["a generated folder", "the .git folder"]
)
def test_allow_untracked_never_lets_git_config_through(action: CIAction, repo: Path, folders: tuple[str, ...], caplog):
    """.git/config can hold the job's token, whichever folder --allow-untracked names."""
    (repo / "gen").mkdir()
    (repo / "gen" / "regs.v").write_text('`include "../.git/config"\nmodule regs;\nendmodule\n')
    _commit(repo, ".gitignore", "gen/\n")
    _commit(repo, "aristotle.f", "fifo.v\ngen/regs.v\n")

    message = _blocked(action, repo, caplog, *[arg for folder in folders for arg in ("--allow-untracked", folder)])

    assert message == (
        f"{Path('repo', '.git', 'config')} (included at {Path('repo', 'gen', 'regs.v')}:1) is inside a .git folder, "
        "which is never uploaded."
    )


@pytest.mark.parametrize(
    ("name", "problem"),
    [
        (".git/config", "is inside a .git folder"),
        ("gen/.env", "is named like a credential"),
        ("gen/.ssh/id_work", "is in a folder that holds credentials"),
    ],
)
def test_a_folder_allow_untracked_names_lifts_only_gits_not_tracking_a_file(repo: Path, name: str, problem: str):
    """Before the file is read, whatever folder holds it."""
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).touch()
    (repo / "gen").mkdir(exist_ok=True)
    allowed = allowed_folders(repo, ["repo/gen", "repo/.git"])

    assert _block_problem(repo, scope.tracked_paths(repo), allowed, repo / name) == (problem, None)


def test_allow_untracked_never_lets_through_a_credential_an_earlier_step_wrote_in_the_workspace(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    """It isn't in a folder --allow-untracked names, which can't be the folder the step uploads."""
    _generated(repo, tmp_path)
    (repo / "gha-creds-0a1b2c.json").write_text('{"token": "placeholder-not-a-token"}\n')
    _commit(repo, "aristotle.f", "+incdir+../shared\nfifo.v\ngen/regs.v\ngha-creds-*.json\n")

    message = _blocked(action, repo, caplog, *ALLOWED)

    assert message == (
        f"{Path('repo', 'gha-creds-0a1b2c.json')} (named at {Path('repo', 'aristotle.f')}:4) isn't tracked by git, and "
        "no folder --allow-untracked names holds it. Commit it, or submit the block with `aristotle submit`."
    )
    assert "placeholder" not in caplog.text


@pytest.mark.parametrize(
    ("name", "problem"),
    [
        ("credentials.json", "is named like a credential"),
        (".docker/config.json", "is in a folder that holds credentials"),
    ],
)
@pytest.mark.parametrize("folder", ["gen", "../shared"])
def test_allow_untracked_never_lets_a_credential_through_from_a_folder_it_names(
    action: CIAction, repo: Path, tmp_path: Path, name: str, problem: str, folder: str, caplog
):
    shared = _generated(repo, tmp_path)
    credential = (repo / "gen" if folder == "gen" else shared) / name
    credential.parent.mkdir(exist_ok=True)
    credential.write_text('{"auths": "placeholder-not-a-token"}\n')
    _commit(repo, "aristotle.f", f"+incdir+../shared\nfifo.v\ngen/regs.v\n{folder}/{name}\n")

    message = _blocked(action, repo, caplog, *ALLOWED)

    assert message.endswith(f"{problem}, and such files are never uploaded. Leave it out of the block.")
    assert "placeholder" not in caplog.text


@pytest.mark.parametrize("value", NEVER_READ.values(), ids=NEVER_READ.keys())
@pytest.mark.parametrize("route", ["aristotle.f", "include", "aristotle.f with --allow-untracked"])
def test_a_path_never_read_refuses_stops_the_run_before_anything_looks_it_up(
    action: CIAction, repo: Path, tmp_path: Path, no_lookup_of, value: str, route: str, caplog
):
    """A change can name one in aristotle.f or an include; on a Windows runner, a lookup would reach the computer."""
    allowed: tuple[str, ...] = ()
    if route == "include":
        _commit(repo, "fifo.v", f'`include "{value}"\nmodule fifo;\nendmodule\n')
        _commit(repo, "aristotle.f", "fifo.v\n")
        expected = f"{Path('repo', 'fifo.v')}:1 includes {filelist.never_read(value)}."
    else:
        if route == "aristotle.f with --allow-untracked":
            _generated(repo, tmp_path)
            allowed = ALLOWED
        _commit(repo, "aristotle.f", f"+incdir+../shared\nfifo.v\n'{value}'\n")
        expected = f"{Path('repo', 'aristotle.f')}:3: the file named is {filelist.never_read(value)}"
    no_lookup_of(UNREACHABLE_HOST)

    assert _blocked(action, repo, caplog, *allowed) == expected


@pytest.mark.parametrize("value", NEVER_READ.values(), ids=NEVER_READ.keys())
def test_a_folder_allow_untracked_names_by_a_path_never_read_refuses_stops_the_run_before_it_is_looked_up(
    action: CIAction, repo: Path, no_lookup_of, value: str, caplog
):
    """The command line is the job's own, but no file in such a folder could pass the filelist reader."""
    _commit(repo, "aristotle.f", "fifo.v\n")
    no_lookup_of(UNREACHABLE_HOST)

    assert _blocked(action, repo, caplog, "--allow-untracked", value) == (
        f"--allow-untracked names {filelist.never_read(value)}."
    )
    assert "Using the Verilog filelist" not in caplog.text


@pytest.mark.skipif(not os.path.isfile("/proc/self/environ"), reason="needs Linux's /proc")
@pytest.mark.parametrize("how", ["named", "linked", "a magic link", "a magic link with two slashes"])
def test_allow_untracked_never_lets_the_process_environment_through(
    action: CIAction, repo: Path, monkeypatch, how: str, caplog
):
    """Not from /proc named as a folder, nor through a link in a named folder, nor by a /proc link that leads into
    one, which spelled with two slashes is a network path's form: the job's environment holds its keys."""
    monkeypatch.setenv("JOB_SECRET", "placeholder-environ-value")
    (repo / "gen").mkdir()
    if how == "named":
        include, folders = "/proc/self/environ", ("repo/gen", "/proc")
        problem = f"environ (included at {Path('repo', 'gen', 'regs.v')}:1) is in /proc, which is never uploaded."
    elif how.startswith("a magic link"):
        (repo / "gen" / "token.vh").write_text("`define TOKEN placeholder-in-gen\n")
        include, folders = "/proc/self/cwd/repo/gen/token.vh", ("repo/gen",)
        problem = f"token.vh (included at {Path('repo', 'gen', 'regs.v')}:1) is in /proc, which is never uploaded."
        if how.endswith("two slashes"):
            include = f"/{include}"
            problem = f"{Path('repo', 'gen', 'regs.v')}:1 includes {filelist.never_read(include)}."
    else:
        (repo / "gen" / "env.vh").symlink_to("/proc/self/environ")
        include, folders = "env.vh", ("repo/gen",)
        problem = (
            f"{Path('repo', 'gen', 'env.vh')} (included at {Path('repo', 'gen', 'regs.v')}:1) leads into /proc, which "
            "is never uploaded."
        )
    (repo / "gen" / "regs.v").write_text(f'`include "{include}"\nmodule regs;\nendmodule\n')
    _commit(repo, ".gitignore", "gen/\n")
    _commit(repo, "aristotle.f", "fifo.v\ngen/regs.v\n")

    message = _blocked(action, repo, caplog, *[arg for folder in folders for arg in ("--allow-untracked", folder)])

    assert message.endswith(problem)
    assert "placeholder" not in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="Windows has no such folders")
@pytest.mark.parametrize(
    ("path", "folder"),
    [("//proc/self/environ", "/proc"), ("///sys/kernel", "/sys"), ("/dev/shm/x", "/dev"), ("/devx", None)],
)
def test_a_system_folder_is_found_past_extra_leading_slashes(path: str, folder: str | None):
    assert _system_folder(path) == folder


@pytest.mark.skipif(not os.access("/dev/shm", os.W_OK), reason="needs a writable /dev/shm")
def test_allow_untracked_never_lets_a_file_in_dev_through(action: CIAction, repo: Path, caplog):
    with tempfile.TemporaryDirectory(dir="/dev/shm") as shm:
        (Path(shm) / "token.vh").write_text("`define TOKEN placeholder-in-dev\n")
        _commit(repo, "fifo.v", f'`include "{shm}/token.vh"\nmodule fifo;\nendmodule\n')
        _commit(repo, "aristotle.f", "fifo.v\n")

        message = _blocked(action, repo, caplog, "--allow-untracked", shm)

    assert message.endswith(
        "token.vh (included at " + str(Path("repo", "fifo.v")) + ":1) is in /dev, which is never uploaded."
    )
    assert "placeholder" not in caplog.text


@pytest.mark.skipif(not os.access("/dev/shm", os.W_OK), reason="needs a writable /dev/shm")
def test_a_checkout_in_dev_still_uploads_its_own_files(action: CIAction, caplog):
    """/dev counts against a file from outside the folder the step uploads, not against the folder it sits in."""
    with tempfile.TemporaryDirectory(dir="/dev/shm") as shm:
        root = Path(shm)
        git(root, "init", "-q")
        (root / "fifo.v").write_text("module fifo;\nendmodule\n")
        (root / manifest.MANIFEST_NAME).write_text(MANIFEST)
        (root / "aristotle.f").write_text("fifo.v\n")
        git(root, "add", "-A")
        git(root, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-qm", "initial")

        assert _dry_run(action, root, caplog) == EXIT_SUCCESS


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("how", ["a file", "a folder", "outside"])
def test_allow_untracked_never_follows_a_link_out_of_every_folder_it_names(
    action: CIAction, repo: Path, tmp_path: Path, how: str, caplog
):
    shared = _generated(repo, tmp_path)
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "token.vh").write_text("`define TOKEN placeholder-escaped\n")
    if how == "a file":
        (repo / "gen" / "token.vh").symlink_to(secret / "token.vh")
        include, named, folder = "token.vh", Path("repo", "gen", "token.vh"), "repo/gen"
    elif how == "a folder":
        (repo / "gen" / "sub").symlink_to(secret)
        include, named, folder = "sub/token.vh", Path("repo", "gen", "sub", "token.vh"), "repo/gen"
    else:
        (shared / "token.vh").symlink_to(secret / "token.vh")
        include, named, folder = "token.vh", Path("shared", "token.vh"), "shared"
    (repo / "gen" / "regs.v").write_text(f'`include "defs.vh"\n`include "{include}"\nmodule regs;\nendmodule\n')

    message = _blocked(action, repo, caplog, *ALLOWED)

    assert message == (
        f"{named} (included at {Path('repo', 'gen', 'regs.v')}:2) is in {folder}, but a link leads it to "
        f"{secret / 'token.vh'}, outside every folder --allow-untracked names."
    )
    assert "placeholder" not in caplog.text


def test_allow_untracked_without_a_root_filelist_says_it_changes_nothing(action: CIAction, repo: Path, caplog):
    (repo / "gen").mkdir()

    assert _dry_run(action, repo, caplog, "--allow-untracked", "repo/gen") == EXIT_SUCCESS

    assert (
        f"--allow-untracked lets through only files a root aristotle.f reaches, and git tracks none in {repo}, so it "
        "changes nothing."
    ) in caplog.messages


async def test_a_block_from_outside_the_folder_counts_its_language(action: CIAction, repo: Path, tmp_path: Path):
    """Read from where it is, as its place in the upload, under _ext, isn't in the checkout."""
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "top.v").write_text("module top;\nendmodule\n")
    git(repo, "rm", "-q", "fifo.v")
    _commit(repo, "aristotle.f", "../shared/top.v\n")

    run = await _run(action, ["--directory", str(repo), "--no-junit", "--allow-untracked", "shared"], [])

    run.language_access.assert_awaited_once_with(["Verilog"])


async def test_project_reports_a_run_of_files_allow_untracked_let_through(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    _generated(repo, tmp_path)
    started = _started_block_run(action, repo, tmp_path, ("repo/gen", "shared"))

    (run, _) = await _follow(
        action,
        ["--directory", str(repo), "--no-junit", *ALLOWED],
        prompt=started,
        declarations=[_declaration(i) for i in IDS],
    )

    assert run.code == EXIT_SUCCESS


@pytest.mark.parametrize("changed", ["gen/regs.v", "shared/defs.vh"])
async def test_project_refuses_a_run_after_a_file_allow_untracked_let_through_changes(
    action: CIAction, repo: Path, tmp_path: Path, changed: str, caplog
):
    """git sees no change to either, so only the record of the upload tells the run's files from these."""
    _generated(repo, tmp_path)
    started = _started_block_run(action, repo, tmp_path, ("repo/gen", "shared"))
    (tmp_path / "repo" / changed if changed.startswith("gen") else tmp_path / changed).write_text("`define W 16\n")

    assert (await _refused(action, repo, caplog, started, ALLOWED)).startswith(
        "Project test-123's run checks other files than this checkout's, though both are at commit "
    )


async def test_project_without_allow_untracked_refuses_a_run_that_needed_it_before_any_request(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    _generated(repo, tmp_path)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        (run, resolve) = await _follow(action, ["--directory", str(repo), "--no-junit"])

    assert run.code == EXIT_USAGE_ERROR
    resolve.assert_not_awaited()
    assert caplog.messages[-1].endswith(
        "isn't tracked by git, and `aristotle ci` uploads only files git tracks there. Commit it, name a folder that "
        "holds it with --allow-untracked, or submit the block with `aristotle submit`."
    )


@pytest.mark.parametrize(
    ("detected", "variable", "suffix"),
    [
        ({"GITHUB_ACTIONS": "true"}, "RUNNER_TEMP", ""),
        ({"TF_BUILD": "True"}, "AGENT_TEMPDIRECTORY", ""),
        ({"GITLAB_CI": "true"}, "CI_PROJECT_DIR", ".tmp"),
        ({"JENKINS_URL": "https://jenkins.example.com/"}, "WORKSPACE", "@tmp"),
    ],
    ids=["GitHub", "Azure", "GitLab", "Jenkins"],
)
@pytest.mark.parametrize("named", ["it", "above it"])
def test_allow_untracked_cant_name_the_folder_of_the_jobs_credential_files(
    repo: Path,
    tmp_path: Path,
    monkeypatch,
    detected: dict[str, str],
    variable: str,
    suffix: str,
    named: str,
    caplog,
):
    """actions/checkout keeps the job's token in RUNNER_TEMP, and the other systems keep secret files beside the
    checkout; a folder in one, which the build may write into, can still be named."""
    credentials = tmp_path / "ci" / f"job{suffix}"
    (credentials / "gen").mkdir(parents=True)
    for name, value in detected.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(variable, str(tmp_path / "ci" / "job"))
    _commit(repo, "aristotle.f", "fifo.v\n")
    folder = credentials if named == "it" else credentials.parent

    message = _blocked(_action(), repo, caplog, "--allow-untracked", str(folder))

    assert message == (
        f"--allow-untracked {folder} {'is' if named == 'it' else 'holds'} {credentials}, where the CI system keeps the "
        "job's credentials, such as its token or secret files, so a change could send them. Name the folders that hold "
        "the generated files, or the shared ones, instead."
    )
    assert _dry_run(_action(), repo, caplog, "--allow-untracked", str(credentials / "gen")) == EXIT_SUCCESS


@pytest.mark.parametrize("kept", ["home", "temporary"])
@pytest.mark.parametrize("named", ["it", "above it"])
def test_allow_untracked_cant_name_the_home_or_temporary_folder(
    repo: Path, tmp_path: Path, monkeypatch, kept: str, named: str, caplog
):
    """Tools keep credentials there under names no rule matches; a folder in one can still be named."""
    folder = tmp_path / "elsewhere" / kept
    (folder / "gen").mkdir(parents=True)
    if kept == "home":
        monkeypatch.setenv("HOME", str(folder))
        monkeypatch.setenv("USERPROFILE", str(folder))
    else:
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(folder))
    _commit(repo, "aristotle.f", "fifo.v\n")
    given = folder if named == "it" else folder.parent

    message = _blocked(_action(), repo, caplog, "--allow-untracked", str(given))

    what = "the home folder" if kept == "home" else "a temporary folder"
    assert message.startswith(f"--allow-untracked {given} {'is' if named == 'it' else 'holds'} {what}, {folder}, ")
    assert _dry_run(_action(), repo, caplog, "--allow-untracked", str(folder / "gen")) == EXIT_SUCCESS


@pytest.mark.skipif(os.name == "nt" or not os.path.isdir("/var/tmp"), reason="needs /var/tmp")
def test_the_systems_temporary_folders_stay_out_whatever_tmpdir_says(repo: Path, tmp_path: Path, monkeypatch, caplog):
    """A CI agent keeps a job's environment file in its own temporary folder, whichever one the job's TMPDIR names."""
    (tmp_path / "job-tmp").mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path / "job-tmp"))
    _commit(repo, "aristotle.f", "fifo.v\n")
    given = os.path.realpath("/var/tmp")

    assert _blocked(_action(), repo, caplog, "--allow-untracked", given).startswith(
        f"--allow-untracked {given} is a temporary folder, "
    )


def test_the_temporary_folders_are_the_ones_the_variables_name_too(tmp_path: Path, monkeypatch):
    for name in ("TMPDIR", "TEMP", "TMP"):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv("TMP", str(tmp_path / "missing"))

    folders = _temporary_folders()

    assert tmp_path / "TMPDIR" in folders and tmp_path / "TEMP" in folders
    assert tmp_path / "missing" not in folders


@pytest.mark.parametrize("kept", ["root", "home", "temporary", "CI"])
@pytest.mark.parametrize("depth", ["in it", "below it"])
def test_a_file_reached_by_another_path_to_a_folder_the_option_cant_take_stops_the_run(
    repo: Path, tmp_path: Path, monkeypatch, kept: str, depth: str
):
    """As a bind mount of one inside a named folder is: its files are that folder's, which never go up."""
    folder = repo if kept == "root" else tmp_path / kept
    folder.mkdir(exist_ok=True)
    if kept == "home":
        monkeypatch.setenv("HOME", str(folder))
        monkeypatch.setenv("USERPROFILE", str(folder))
    elif kept == "temporary":
        monkeypatch.setenv("TMPDIR", str(folder))
    elif kept == "CI":
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("RUNNER_TEMP", str(folder))
    inner = tmp_path / "shared" / "inner"
    file = inner / "gha-creds-0a1b2c.json" if depth == "in it" else inner / "sub" / "gha-creds-0a1b2c.json"
    file.parent.mkdir(parents=True)
    file.write_text("{}\n")
    allowed = allowed_folders(repo, ["shared"])
    monkeypatch.setattr(
        "aristotlelib.cli.command.ci._identity",
        lambda path: (
            _identity(str(folder)) if os.path.normcase(path) == os.path.normcase(str(inner)) else _identity(path)
        ),
    )

    problem, admitted = _block_problem(repo, scope.tracked_paths(repo), allowed, file)

    assert admitted is None
    assert problem is not None
    assert problem.startswith(f"is in shared, but {inner} is ")
    assert problem.endswith(", by another path")
    assert str(folder) in problem


def test_a_relative_folder_is_from_the_current_directory_not_where_the_manifest_is(
    action: CIAction, tmp_path: Path, monkeypatch, caplog
):
    """Outside CI the folder the step uploads is the nearest one up that holds aristotle.toml, which a change can move,
    as here to the repository's top, to name a folder of the same name higher up."""
    top = tmp_path / "top"
    (top / "hw" / "fifo").mkdir(parents=True)
    (top / "hw" / "shared").mkdir()
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "notes.v").write_text("module notes;\nendmodule\n")
    git(top, "init", "-q")
    (top / manifest.MANIFEST_NAME).write_text(MANIFEST)
    (top / "fifo.v").write_text("module fifo;\nendmodule\n")
    (top / "aristotle.f").write_text("fifo.v\n../shared/notes.v\n")
    git(top, "add", "-A")
    git(top, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-qm", "initial")
    monkeypatch.chdir(top / "hw" / "fifo")
    args = action.parser.parse_args(["--dry-run", "--allow-untracked", "../shared"], argparse.Namespace(api_key=None))

    assert action.run(args) == EXIT_USAGE_ERROR

    assert caplog.messages[-1] == (
        f"{tmp_path / 'shared' / 'notes.v'} (named at {top / 'aristotle.f'}:2) is outside the folder the step uploads, "
        "and no folder --allow-untracked names holds it. Commit the file inside that folder and name it from there, "
        "name a folder that holds it with --allow-untracked, or submit the block with `aristotle submit`."
    )


@pytest.mark.parametrize("kept", ["home", "temporary", "CI"])
def test_a_refusal_names_no_option_for_a_file_directly_in_a_folder_it_cant_take(
    action: CIAction, repo: Path, tmp_path: Path, monkeypatch, kept: str, caplog
):
    folder = tmp_path / "kept"
    folder.mkdir()
    (folder / "job-env").write_text("TOKEN=placeholder-in-kept\n")
    if kept == "home":
        monkeypatch.setenv("HOME", str(folder))
        monkeypatch.setenv("USERPROFILE", str(folder))
    elif kept == "temporary":
        monkeypatch.setenv("TMPDIR", str(folder))
    else:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("RUNNER_TEMP", str(folder))
    _commit(repo, "aristotle.f", f"fifo.v\n{(folder / 'job-env').as_posix()}\n")

    message = _blocked(_action(), repo, caplog)

    assert message.endswith(
        "is outside the folder the step uploads, and `aristotle ci` uploads only files git tracks there. Commit the "
        "file inside that folder and name it from there, or submit the block with `aristotle submit`."
    )
    assert "placeholder" not in caplog.text


def test_a_folder_holding_the_root_counts_by_identity_however_it_is_spelled(repo: Path):
    """As a case-insensitive disk, or a bind mount, can spell it."""
    (repo / "gen").mkdir()
    assert _holds(str(repo / "gen" / ".."), repo) == "is"
    assert _holds(f"{repo}{os.sep}..", repo) == "holds"
    assert _holds(str(repo / ".git"), repo) is None


def test_the_folder_the_step_uploads_named_in_another_case_stops_the_run(action: CIAction, tmp_path: Path, caplog):
    root = tmp_path / "Repo"
    root.mkdir()
    if not (tmp_path / "REPO").is_dir():
        pytest.skip("needs a case-insensitive disk")
    git(root, "init", "-q")
    (root / "fifo.v").write_text("module fifo;\nendmodule\n")
    (root / manifest.MANIFEST_NAME).write_text(MANIFEST)
    (root / "aristotle.f").write_text("fifo.v\n")
    git(root, "add", "-A")
    git(root, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-qm", "initial")

    message = _blocked(action, root, caplog, "--allow-untracked", str(tmp_path / "REPO"))

    assert " is the folder the step uploads, " in message


def test_a_shared_file_that_would_go_up_where_the_folder_has_a_file_stops_the_run(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    """As submit does: the tracked file at that path would be lost from the upload."""
    _generated(repo, tmp_path)
    (repo / "_ext" / "shared").mkdir(parents=True)
    _commit(repo, "_ext/shared/defs.vh", "`define FROM_TRACKED\n")

    assert _blocked(action, repo, caplog, *ALLOWED) == (
        f"{repo} has a file at _ext/shared/defs.vh, where a file its aristotle.f names from outside it would go up. "
        "Rename that file or its folder."
    )


def test_a_shared_file_that_would_go_up_where_the_upload_needs_a_folder_stops_the_run(
    action: CIAction, repo: Path, tmp_path: Path, caplog
):
    _generated(repo, tmp_path)
    (repo / "_ext" / "shared" / "defs.vh").mkdir(parents=True)
    _commit(repo, "_ext/shared/defs.vh/readme.txt", "notes\n")

    assert _blocked(action, repo, caplog, *ALLOWED) == (
        f"{repo}'s upload can't hold both _ext/shared/defs.vh and _ext/shared/defs.vh/readme.txt, which needs "
        "_ext/shared/defs.vh to be a folder. Rename one of them or its folder."
    )


@pytest.mark.parametrize("how", ["tracked", "held back", "let through"])
def test_a_shared_file_that_would_go_up_below_a_file_of_the_upload_stops_the_run(
    action: CIAction, repo: Path, tmp_path: Path, how: str, caplog
):
    """An archive can't hold both: _ext/shared would have to be a folder, whether the folder's upload or the block
    takes the file there."""
    _generated(repo, tmp_path)
    (repo / "_ext").mkdir()
    (repo / "_ext" / "shared").write_text("module not_a_folder;\nendmodule\n")
    folders = ALLOWED
    if how != "tracked":
        _commit(repo, ".gitignore", "gen/\n_ext/\n")
        _commit(repo, "aristotle.f", "+incdir+../shared\nfifo.v\ngen/regs.v\n_ext/shared\n")
    if how == "let through":
        folders = (*ALLOWED, "--allow-untracked", "repo/_ext")
    else:
        git(repo, "add", "-f", "_ext/shared")
        git(repo, "commit", "-qm", "_ext/shared")

    assert _blocked(action, repo, caplog, *folders) == (
        f"{repo}'s upload can't hold both _ext/shared and _ext/shared/defs.vh, which needs _ext/shared to be a folder. "
        "Rename one of them or its folder."
    )


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_shared_file_goes_up_beside_a_tracked_folder_link(action: CIAction, repo: Path, tmp_path: Path, caplog):
    """The link is a path of the folder's upload, and a block file below it isn't one of a file."""
    _generated(repo, tmp_path)
    (repo / "rtl").mkdir()
    _commit(repo, "rtl/top.v", "module top;\nendmodule\n")
    (repo / "ip").symlink_to(repo / "rtl")
    git(repo, "add", "ip")
    _commit(repo, "aristotle.f", "+incdir+../shared\nfifo.v\ngen/regs.v\nip/top.v\n")

    assert _dry_run(action, repo, caplog, *ALLOWED) == EXIT_SUCCESS


@pytest.mark.parametrize("where", ["gen", "../shared"])
def test_a_black_box_leaves_out_a_file_allow_untracked_would_let_through(
    action: CIAction, repo: Path, tmp_path: Path, where: str, caplog
):
    """It never goes up, and the option's lines don't name it."""
    shared = _generated(repo, tmp_path)
    ((repo / "gen") if where == "gen" else shared).joinpath("sram_1p.v").write_text("module sram_1p;\nendmodule\n")
    _commit(repo, "aristotle.f", f"--blackbox sram_1p\n+incdir+../shared\nfifo.v\ngen/regs.v\n{where}/sram_1p.v\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        scope_result = action._read_block(
            repo, scope.resolve_checkout(repo, warn=False), allowed=allowed_folders(repo, ["repo/gen", "shared"])
        )
    runner.package(repo, scope_result, tmp_path / "upload.tar.gz", manifest.load(repo / manifest.MANIFEST_NAME))

    with tarfile.open(tmp_path / "upload.tar.gz", encoding="utf-8") as tar:
        names = tar.getnames()
    assert "gen/regs.v" in names and "_ext/shared/defs.vh" in names
    assert not any(name.endswith("sram_1p.v") for name in names)
    assert scope_result.allowed == {"gen/regs.v", "_ext/shared/defs.vh"}
    assert not any("sram_1p.v" in m for m in caplog.messages if "--allow-untracked lets through" in m)
