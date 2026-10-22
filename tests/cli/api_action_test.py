"""Tests for how aristotlelib.cli.api_action.APIAction.run reports API errors and picks exit codes."""

import argparse
import asyncio
import errno
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from aristotlelib import api_request
from aristotlelib.agent_task import AgentTask, TaskStatus
from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli import controller
from aristotlelib.cli.api_action import (
    APIAction,
    _run_loop,
    add_prompt_arguments,
    download_hint,
    fetch_tasks,
    judge_properties,
    log_unjudged,
    read_prompt,
    resume_commands,
    resume_hint,
    task_exit_code,
)
from aristotlelib.cli.command.cancel import CancelAction
from aristotlelib.cli.command.continue_ import ContinueAction
from aristotlelib.cli.command.download import DownloadAction
from aristotlelib.cli.command.list import ListAction
from aristotlelib.cli.command.questions import QuestionsAction
from aristotlelib.cli.command.review import ReviewAction
from aristotlelib.cli.command.show import ShowAction
from aristotlelib.cli.command.submit import SubmitAction
from aristotlelib.cli.command.tasks import TasksAction
from aristotlelib.cli.login_flow import LoginError
from aristotlelib.declaration import DeclarationStatus
from aristotlelib.exit_codes import EXIT_INFRASTRUCTURE_ERROR, EXIT_RATE_LIMITED
from aristotlelib.project import Project, lookup_at_terminal
from aristotlelib.results import NO_SUMMARY
from aristotlelib.shell import shell_commands, shell_hint
from tests.ci.conftest import make_declaration
from tests.conftest import FAKE_KEY, PADDED_KEYS, REFUSED_KEYS, give_key, shown_key_runs


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setattr(api_request, "API_KEY", "test-key")


@pytest.fixture(autouse=True)
def no_aristotle_settings(monkeypatch):
    """Run as if no ARISTOTLE_* setting were exported: commands read them as defaults when their parser is built."""
    for name in [name for name in os.environ if name.startswith("ARISTOTLE_")]:
        monkeypatch.delenv(name)


UNKNOWN = "3f1002f7-0000-4000-8000-0000000000aa"


def _client_answering(status_code: int) -> AsyncMock:
    """A request client whose every GET fails with this status."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=AristotleAPIError("Forbidden", status_code=status_code))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _top_level() -> argparse.Namespace:
    """What main()'s parser sets before a subcommand parses: its own --api-key, unset."""
    return argparse.Namespace(api_key=None)


def _run(action_cls, argv: list[str], client: AsyncMock):
    parser = argparse.ArgumentParser()
    action = action_cls(parser.add_subparsers())
    action.add_arguments()
    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        return action.run(action.parser.parse_args(argv, _top_level()))


@pytest.mark.parametrize(
    ("action_cls", "argv", "message", "requested"),
    [
        (ShowAction, ["p-1"], "Project p-1 not found or not accessible.", ["/project/p-1"]),
        (ShowAction, ["p-1", "--task", "t-1"], "Task t-1 not found or not accessible.", ["/task/t-1"]),
        (TasksAction, ["p-1"], "Project p-1 not found or not accessible.", ["/project/p-1"]),
        (DownloadAction, ["p-1"], "Project p-1 not found or not accessible.", ["/project/p-1"]),
        (ContinueAction, ["p-1", "Prove it"], "Project p-1 not found or not accessible.", ["/project/p-1"]),
        (CancelAction, ["--project-id", "p-1"], "Project p-1 not found or not accessible.", ["/project/p-1"]),
        (CancelAction, ["--task-id", "t-1"], "Task t-1 not found or not accessible.", ["/task/t-1"]),
        (
            CancelAction,
            [UNKNOWN],
            f"Project or task {UNKNOWN} not found or not accessible.",
            [f"/project/{UNKNOWN}", f"/task/{UNKNOWN}"],
        ),
        (CancelAction, ["x-1"], "Project x-1 not found or not accessible.", ["/project/x-1"]),
        (ShowAction, ["p-1", "--task", ""], "Task '' not found or not accessible.", []),
        (CancelAction, ["--project-id", ""], "Project '' not found or not accessible.", []),
        (CancelAction, ["--task-id", ""], "Task '' not found or not accessible.", []),
        (QuestionsAction, ["p-1"], "Project p-1 not found or not accessible.", ["/project/p-1"]),
        (QuestionsAction, ["p-1", "--task", ""], "Task '' not found or not accessible.", []),
    ],
    ids=[
        "show",
        "show --task",
        "tasks",
        "download",
        "continue",
        "cancel --project-id",
        "cancel --task-id",
        "cancel",
        "cancel with a project reference",
        "show --task ''",
        "cancel --project-id ''",
        "cancel --task-id ''",
        "questions",
        "questions --task ''",
    ],
)
def test_a_missing_id_is_one_error_line_and_exit_status_4(action_cls, argv, message, requested, caplog, capsys):
    client = _client_answering(403)

    assert _run(action_cls, argv, client) == 4

    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [(logging.ERROR, message)]
    assert capsys.readouterr().out == ""
    assert [c.args[0] for c in client.get.await_args_list] == requested
    client.post.assert_not_awaited()


@pytest.mark.usefixtures("project_lookup")
def test_a_malformed_project_id_is_looked_up_as_a_name_and_reported_as_missing(caplog):
    client = _client_listing(_project_record(NAMED, "fifo"))

    assert _run(ShowAction, ["not-a-project"], client) == 4

    assert caplog.messages == ["No project is named 'not-a-project'."]
    assert [c.args[0] for c in client.get.await_args_list] == ["/project"]


def test_other_api_errors_are_one_error_line_and_exit_status_3(caplog, capsys):
    client = _client_answering(500)
    client.get.side_effect = AristotleAPIError("API request failed with status 500: Internal Server Error", 500)

    assert _run(ShowAction, ["p-1"], client) == 3

    assert caplog.messages == ["API request failed with status 500: Internal Server Error"]
    assert capsys.readouterr().out == ""


def test_a_network_error_exits_3(caplog):
    client = _client_answering(500)
    client.get.side_effect = AristotleAPIError("Request failed: [Errno -2] Name or service not known")

    assert _run(TasksAction, ["p-1"], client) == 3

    assert caplog.messages == ["Request failed: [Errno -2] Name or service not known"]


def test_a_rejected_key_exits_5(caplog):
    client = _client_answering(401)

    assert _run(TasksAction, ["p-1"], client) == 5

    assert caplog.messages == [
        "The API key passed with --api-key is not valid. Get one from https://aristotle.harmonic.fun/keys."
    ]


@pytest.mark.parametrize(
    "message",
    [
        "You have too many projects in progress. Cancel a running task or wait for one to complete before starting another.",
        "Too many requests to Aristotle at once. Wait a few seconds and try again.",
    ],
    ids=["projects", "requests"],
)
def test_a_rate_limit_exits_6_with_its_message(message, caplog):
    client = _client_answering(429)
    client.get.side_effect = AristotleAPIError(message, status_code=429)

    assert _run(TasksAction, ["p-1"], client) == 6

    assert caplog.messages == [message]


def test_a_missing_key_exits_5_without_a_request(monkeypatch, caplog):
    monkeypatch.setattr(api_request, "API_KEY", None)
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    monkeypatch.setattr("aristotlelib.credentials.read_api_key", lambda: None)
    client = _client_answering(500)

    with patch("sys.stdin.isatty", return_value=False):
        assert _run(TasksAction, ["p-1"], client) == 5

    assert caplog.messages[0].startswith("Aristotle API key not found.")
    client.get.assert_not_awaited()


NO_PROJECTS = {"projects": [], "pagination_key": None}


@pytest.mark.parametrize("source", ["--api-key", "environment", "stored"])
@pytest.mark.parametrize("key", PADDED_KEYS)
def test_a_key_with_whitespace_around_it_is_sent_without_it(source, key, api_server, monkeypatch, tmp_path, capsys):
    requests = api_server(httpx.Response(200, json=NO_PROJECTS))
    before = give_key(source, key, monkeypatch, tmp_path)
    monkeypatch.setattr("sys.argv", ["aristotle", *before, "list", "--json"])

    assert controller.main() == 0

    assert [request.headers["X-API-Key"] for request in requests] == [FAKE_KEY]
    assert json.loads(capsys.readouterr().out) == NO_PROJECTS


@pytest.mark.parametrize("command", [["list"], ["list", "--json"], ["submit", "Prove it", "--json"]], ids=" ".join)
@pytest.mark.parametrize(
    ("source", "where"),
    [
        ("--api-key", "given with --api-key"),
        ("environment", "in the ARISTOTLE_API_KEY environment variable"),
        ("stored", "in the credentials file"),
    ],
)
@pytest.mark.parametrize(("key", "problem"), REFUSED_KEYS)
def test_a_key_that_stripping_cannot_fix_exits_5_before_any_request_and_is_never_shown(
    command, source, where, key, problem, api_server, monkeypatch, tmp_path, capsys, caplog
):
    requests = api_server()
    before = give_key(source, key, monkeypatch, tmp_path)
    monkeypatch.setattr("sys.argv", ["aristotle", *before, *command])
    # At a terminal, where a missing key is offered a login.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr("sys.stdin.readline", Mock(side_effect=AssertionError("must not offer a login")))

    assert controller.main() == 5

    out, err = capsys.readouterr()
    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    assert caplog.messages[0].startswith(f"The API key {where}")
    assert f" {problem}." in caplog.messages[0]
    assert out == ""
    assert shown_key_runs(out + err + caplog.text, key) == []
    assert requests == []


@pytest.mark.parametrize(("returned", "code"), [(None, 0), (0, 0), (4, 4)])
def test_the_run_action_result_is_the_exit_code(returned, code):
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_arguments()

    with patch.object(ShowAction, "run_action", AsyncMock(return_value=returned)):
        assert action.run(action.parser.parse_args(["p-1"], _top_level())) == code


@pytest.mark.parametrize(
    ("returned", "code", "continued"), [(2, 2, False), (0, 0, False), (None, 0, True)], ids=["2", "0", "None"]
)
def test_a_preflight_exit_code_ends_the_command_before_the_key_is_checked(returned, code, continued):
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_arguments()
    args = action.parser.parse_args(["p-1"], _top_level())

    with (
        patch.object(ShowAction, "preflight", return_value=returned) as preflight,
        patch.object(ShowAction, "validate", return_value=True) as validate,
        patch.object(ShowAction, "run_action", AsyncMock(return_value=None)) as run_action,
    ):
        assert action.run(args) == code

    preflight.assert_called_once_with(args)
    assert validate.called == continued
    assert run_action.await_count == int(continued)


@pytest.mark.parametrize("action_cls", [ShowAction, ContinueAction, QuestionsAction])
def test_an_interrupt_exits_130_without_a_traceback(action_cls, capsys):
    parser = argparse.ArgumentParser()
    action = action_cls(parser.add_subparsers())
    action.add_arguments()
    argv = ["p-1", "Prove it"] if action_cls is ContinueAction else ["p-1"]

    with patch.object(action_cls, "run_action", side_effect=KeyboardInterrupt):
        assert action.run(action.parser.parse_args(argv, _top_level())) == 130

    assert capsys.readouterr().err == "\n"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (TaskStatus.COMPLETE, 0),
        (TaskStatus.COMPLETE_WITH_ERRORS, 1),
        (TaskStatus.OUT_OF_BUDGET, 1),
        (TaskStatus.FAILED, 3),
        (TaskStatus.CANCELED, 3),
        (TaskStatus.QUEUED, 3),
        (TaskStatus.IN_PROGRESS, 3),
        (TaskStatus.UNKNOWN, 3),
    ],
)
def test_a_waited_on_task_exits_on_its_final_status(status, code):
    assert task_exit_code(status) == code


@pytest.mark.parametrize(
    ("os_name", "words", "forms"),
    [
        ("posix", ["ci", "--directory", "out/"], [(None, ["ci --directory out/"])]),
        ("posix", ["ci", "--directory", "my out/"], [(None, ["ci --directory 'my out/'"])]),
        ("posix", ["rename", "--", "=v2", "a=b"], [(None, ["rename -- '=v2' a=b"])]),
        ("posix", ["rename", "--", "Q3\nfifo"], [(None, ["rename -- 'Q3\nfifo'"])]),
        ("nt", ["ci", "--directory", "C:/runs/out/"], [(None, ["ci --directory C:/runs/out/"])]),
        # Double quotes leave these alone in cmd.exe and PowerShell alike.
        ("nt", ["git", "commit", "-m", "Add aristotle.toml"], [(None, ['git commit -m "Add aristotle.toml"'])]),
        ("nt", ["ci", "--directory", "C:/Bob's runs/"], [(None, ['ci --directory "C:/Bob\'s runs/"'])]),
        ("nt", ["ci", "--directory", "C:/Bob\u2019s runs/"], [(None, ['ci --directory "C:/Bob\u2019s runs/"'])]),
        ("nt", ["ci", "--manifest", "R&D\\fifo"], [(None, ['ci --manifest "R&D\\fifo"'])]),
        ("nt", ["ci", "--manifest", "x^y", "", "a,b"], [(None, ['ci --manifest "x^y" "" "a,b"'])]),
        ("nt", ["git", "-C", "hw", "add", "--", "-ci.toml"], [(None, ['git -C hw add -- "-ci.toml"'])]),
        ("nt", ["ci", "--ci.toml"], [(None, ["ci --ci.toml"])]),
        ("nt", ["rename", "--", "-v2:rc1"], [(None, ['rename -- "-v2:rc1"'])]),
        ("nt", ["rename", "--", "-v2", "-"], [(None, ['rename -- -v2 "-"'])]),
        (
            "nt",
            ["ci", "--directory", "C:/runs/$work"],
            [("cmd.exe", ['ci --directory "C:/runs/$work"']), ("PowerShell", ["ci --directory 'C:/runs/$work'"])],
        ),
        (
            "nt",
            ["ci", "--directory", "C:/runs/`work"],
            [("cmd.exe", ['ci --directory "C:/runs/`work"']), ("PowerShell", ["ci --directory 'C:/runs/`work'"])],
        ),
        *(
            (
                "nt",
                ["ci", "--directory", f"C:/{quote}work"],
                [
                    ("cmd.exe", [f'ci --directory "C:/{quote}work"']),
                    ("PowerShell", [f"ci --directory 'C:/{quote}work'"]),
                ],
            )
            for quote in "\u201c\u201d\u201e"
        ),
        (
            "nt",
            ["ci", "--directory", "C:/Bob's \u2018$draft\u2019 \u201a\u201b"],
            [
                ("cmd.exe", ['ci --directory "C:/Bob\'s \u2018$draft\u2019 \u201a\u201b"']),
                ("PowerShell", ["ci --directory 'C:/Bob''s \u2018\u2018$draft\u2019\u2019 \u201a\u201a\u201b\u201b'"]),
            ],
        ),
        ("nt", ["ci", "--directory", "C:/runs/%work%"], [("PowerShell", ["ci --directory 'C:/runs/%work%'"])]),
        ("nt", ["ci", "--directory", "C:/runs/!work!"], [("PowerShell", ["ci --directory 'C:/runs/!work!'"])]),
        ("nt", ["rename", "--", 'say "hi"'], [("PowerShell", ["rename -- 'say \"hi\"'"])]),
        ("nt", ["rename", "--", "foo\\"], [("PowerShell", ["rename -- 'foo\\'"])]),
        ("nt", ["rename", "--", "--%"], [("PowerShell", ["rename -- @('--%')"])]),
        ("nt", ["rename", "--", "Q3\nfifo"], [("PowerShell", ["rename -- 'Q3\nfifo'"])]),
        ("nt", ["rename", "--", "Q3\rfifo"], [("PowerShell", ["rename -- 'Q3\rfifo'"])]),
    ],
    ids=[
        "posix plain",
        "posix space",
        "posix leading equals",
        "posix newline",
        "windows plain",
        "windows space",
        "windows apostrophe",
        "windows curly apostrophe",
        "windows ampersand",
        "windows caret, empty, comma",
        "windows dash and dot",
        "windows two dashes and dot",
        "windows dash and colon",
        "windows flag and lone dash",
        "windows dollar",
        "windows backtick",
        "windows left curly double quote",
        "windows right curly double quote",
        "windows low curly double quote",
        "windows quotes doubled for powershell",
        "windows percent",
        "windows exclamation mark",
        "windows straight double quote",
        "windows trailing backslash",
        "windows stop-parsing token",
        "windows newline",
        "windows carriage return",
    ],
)
def test_a_command_is_quoted_once_or_for_each_shell_that_reads_it_differently(monkeypatch, os_name, words, forms):
    monkeypatch.setattr("aristotlelib.shell.os.name", os_name)

    assert shell_commands(words) == forms


@pytest.mark.parametrize(
    ("words", "forms"),
    [
        (["rename", "p-1", "--", "v2"], [(None, ["rename p-1 -- v2"])]),
        (["ci", "--directory", "my $work dir/"], [(None, ["ci --directory 'my $work dir/'"])]),
        (
            ["rename", "p-1", "--", "Bob's proj"],
            [("a POSIX shell", ["rename p-1 -- 'Bob'\"'\"'s proj'"]), ("PowerShell", ["rename p-1 -- 'Bob''s proj'"])],
        ),
        (
            ["rename", "p-1", "--", "Bob\u2019s proj"],
            [
                ("a POSIX shell", ["rename p-1 -- 'Bob\u2019s proj'"]),
                ("PowerShell", ["rename p-1 -- 'Bob\u2019\u2019s proj'"]),
            ],
        ),
        (
            ["git", "add", "--", "-ci.toml", "a,b"],
            [("a POSIX shell", ["git add -- -ci.toml a,b"]), ("PowerShell", ["git add -- '-ci.toml' 'a,b'"])],
        ),
        (
            ["rename", "p-1", "--", "--%"],
            [("a POSIX shell", ["rename p-1 -- --%"]), ("PowerShell", ["rename p-1 -- @('--%')"])],
        ),
        (["rename", "p-1", "--", "Q3\nfifo"], [(None, ["rename p-1 -- 'Q3\nfifo'"])]),
    ],
    ids=[
        "plain",
        "space and dollar",
        "apostrophe",
        "curly apostrophe",
        "dash-dot and comma",
        "stop-parsing token",
        "newline",
    ],
)
def test_under_pwsh_outside_windows_powershells_form_follows_when_it_differs(monkeypatch, words, forms):
    monkeypatch.setattr("aristotlelib.shell.os.name", "posix")
    monkeypatch.setenv("PSModulePath", "/opt/microsoft/powershell/7/Modules")

    assert shell_commands(words) == forms


def test_under_pwsh_outside_windows_the_hint_labels_both_forms(monkeypatch):
    monkeypatch.setattr("aristotlelib.shell.os.name", "posix")
    monkeypatch.setenv("PSModulePath", "/opt/microsoft/powershell/7/Modules")

    assert shell_hint("Name it with", ["aristotle", "rename", "p-1", "--", "Bob's proj"]) == (
        "Name it with, in a POSIX shell:\n"
        "  aristotle rename p-1 -- 'Bob'\"'\"'s proj'\n"
        "or, in PowerShell:\n"
        "  aristotle rename p-1 -- 'Bob''s proj'"
    )


def test_commands_given_together_are_split_for_every_shell_if_one_of_them_needs_it(monkeypatch):
    monkeypatch.setattr("aristotlelib.shell.os.name", "nt")
    add = ["git", "-C", "C:/runs/$work", "add", "--", "aristotle.toml"]
    commit = ["git", "commit", "-m", "Add aristotle.toml"]

    assert shell_hint("and commit it", add, commit) == (
        "and commit it, in cmd.exe:\n"
        '  git -C "C:/runs/$work" add -- aristotle.toml\n'
        '  git commit -m "Add aristotle.toml"\n'
        "or, in PowerShell:\n"
        "  git -C 'C:/runs/$work' add -- aristotle.toml\n"
        "  git commit -m 'Add aristotle.toml'"
    )


_RESUME = "aristotle show p-1 --task t-1 --non-interactive"


@pytest.mark.parametrize(
    ("os_name", "destination", "hint"),
    [
        ("posix", None, f"then carry on waiting with:\n  {_RESUME}"),
        ("posix", "my results/", f"then carry on waiting with:\n  {_RESUME} '--destination=my results/'"),
        ("nt", "C:\\runs\\out\\", f"then carry on waiting with:\n  {_RESUME} --destination=C:/runs/out/"),
        (
            "nt",
            "C:\\my runs\\out\\",
            f'then carry on waiting with:\n  {_RESUME} "--destination=C:/my runs/out/"',
        ),
        (
            "nt",
            "C:\\runs\\$report\\result.tar.gz",
            f'then carry on waiting with, in cmd.exe:\n  {_RESUME} "--destination=C:/runs/$report/result.tar.gz"\n'
            f"or, in PowerShell:\n  {_RESUME} '--destination=C:/runs/$report/result.tar.gz'",
        ),
        (
            "nt",
            "C:\\runs\\%report%\\result.tar.gz",
            f"then carry on waiting with, in PowerShell:\n  {_RESUME} '--destination=C:/runs/%report%/result.tar.gz'",
        ),
    ],
    ids=[
        "no destination",
        "posix",
        "windows plain",
        "windows space",
        "windows needs quoting",
        "windows powershell only",
    ],
)
def test_the_resume_command_carries_the_destination_as_each_shell_reads_it(monkeypatch, os_name, destination, hint):
    monkeypatch.setattr("aristotlelib.cli.api_action.os.name", os_name)

    assert resume_hint(SimpleNamespace(project_id="p-1", agent_task_id="t-1"), destination) == hint


@pytest.mark.parametrize(
    ("os_name", "destination", "hint"),
    [
        ("posix", None, "Get them with:\n  aristotle download p-1"),
        (
            "posix",
            "my results/run 1.tar.gz",
            "Get them with:\n  aristotle download p-1 '--destination=my results/run 1.tar.gz'",
        ),
        (
            "nt",
            "C:\\my runs\\run 1.tar.gz",
            'Get them with:\n  aristotle download p-1 "--destination=C:/my runs/run 1.tar.gz"',
        ),
    ],
    ids=["no destination", "posix space", "windows space"],
)
def test_the_download_command_a_wait_names_keeps_the_destination_as_each_shell_reads_it(
    monkeypatch, os_name, destination, hint
):
    monkeypatch.setattr("aristotlelib.cli.api_action.os.name", os_name)

    assert download_hint("p-1", destination) == hint


@pytest.mark.parametrize(
    ("non_interactive", "json_output", "waits"),
    [(False, False, ["--wait"]), (False, True, ["--wait", "--json"]), (True, True, ["--non-interactive"])],
    ids=["--wait", "--wait --json", "--non-interactive"],
)
def test_the_commands_that_resume_a_project_wait_as_the_wait_did(monkeypatch, non_interactive, json_output, waits):
    monkeypatch.setattr("aristotlelib.cli.api_action.os.name", "posix")
    task = SimpleNamespace(project_id="p-1", agent_task_id="t-1")

    assert resume_commands(task, None, non_interactive=non_interactive, json=json_output) == [
        ["aristotle", "continue", "p-1", "Continue", *waits]
    ]
    # `continue` takes no --destination: `show`, with it, then follows the task `continue` started.
    assert resume_commands(task, "out/", non_interactive=non_interactive, json=json_output) == [
        ["aristotle", "continue", "p-1", "Continue"],
        ["aristotle", "show", "p-1", *waits, "--destination=out/"],
    ]


MISSING = "3f1002f7-0000-4000-8000-000000000000"


@pytest.mark.parametrize(
    "argv",
    [
        ["show", MISSING],
        ["show", MISSING, "--task", MISSING],
        ["tasks", MISSING],
        ["download", MISSING],
        ["continue", MISSING, "Prove it"],
        ["cancel", MISSING],
        ["cancel", "--project-id", MISSING],
        ["cancel", "--task-id", MISSING],
        ["questions", MISSING],
        ["questions", MISSING, "--task", MISSING],
        ["properties", MISSING],
        ["properties", MISSING, "--status", "PROVED"],
        ["property", MISSING, "Spec.Anything"],
        ["rename", MISSING, "New name"],
    ],
    ids=lambda argv: " ".join(a for a in argv if a != MISSING),
)
def test_every_command_exits_4_for_a_missing_project(argv, monkeypatch, caplog):
    client = _client_answering(403)
    client.post = AsyncMock(side_effect=AristotleAPIError("Not found", status_code=404))
    monkeypatch.setattr("sys.argv", ["aristotle", *argv])

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        try:
            code = controller.main()
        except SystemExit as exited:
            code = exited.code

    assert code == 4
    assert "not found" in caplog.text


@pytest.mark.parametrize(
    ("status", "code"),
    [(403, 4), (404, 4), (400, 2), (410, 2), (422, 2), (409, 3), (500, 3), (502, 3)],
)
def test_an_api_error_after_the_lookup_exits_by_status(status, code, caplog):
    """Cancelling a finished task answers 404; continuing a project of another organization, 403."""
    client = AsyncMock()
    task = {
        "project_id": "p-1",
        "agent_task_id": "t-1",
        "status": "COMPLETE",
        "created_at": "2024-01-01T00:00:00",
        "last_updated_at": "2024-01-01T00:00:00",
        "percent_complete": 100,
        "file_name": None,
        "description": None,
        "output_summary": None,
    }
    client.get = AsyncMock(return_value=Mock(json=Mock(return_value=task)))
    message = f"API request failed with status {status}: Agent task cannot be canceled"
    client.post = AsyncMock(side_effect=AristotleAPIError(message, status_code=status))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    assert _run(CancelAction, ["--task-id", "t-1"], client) == code

    # A 404 is checked against the task, which has finished.
    finished = "Task t-1 has already finished (COMPLETE), so it can't be cancelled."
    assert caplog.messages == [finished if status == 404 else message]


def _at_a_terminal_without_a_key(monkeypatch):
    monkeypatch.setattr(api_request, "API_KEY", None)
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    monkeypatch.setattr("aristotlelib.credentials.read_api_key", lambda: None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)


def test_ctrl_c_at_the_login_offer_exits_130(monkeypatch, capsys):
    _at_a_terminal_without_a_key(monkeypatch)
    monkeypatch.setattr("sys.stdin.readline", Mock(side_effect=KeyboardInterrupt))
    client = _client_answering(500)

    assert _run(TasksAction, ["p-1"], client) == 130

    assert capsys.readouterr().err.endswith("Log in now? [Y/n] \n")
    client.get.assert_not_awaited()


@pytest.mark.parametrize(("failure", "code"), [(KeyboardInterrupt(), 130), (LoginError("unreachable", exit_code=3), 3)])
def test_an_offered_login_that_is_interrupted_or_cannot_check_the_key_keeps_its_code(failure, code, monkeypatch):
    _at_a_terminal_without_a_key(monkeypatch)
    monkeypatch.setattr("sys.stdin.readline", lambda: "\n")
    monkeypatch.setattr("aristotlelib.cli.api_action.login", AsyncMock(side_effect=failure))

    assert _run(TasksAction, ["p-1"], _client_answering(500)) == code


def test_a_rejected_key_at_the_login_offer_exits_5(monkeypatch, caplog):
    _at_a_terminal_without_a_key(monkeypatch)
    monkeypatch.setattr("sys.stdin.readline", lambda: "\n")
    monkeypatch.setattr("aristotlelib.cli.api_action.login", AsyncMock(side_effect=LoginError("did not accept")))

    assert _run(TasksAction, ["p-1"], _client_answering(500)) == 5

    assert caplog.messages[0] == "did not accept"
    assert caplog.messages[1].startswith("Aristotle API key not found.")


def test_a_json_run_without_a_key_is_not_offered_a_login(monkeypatch, caplog):
    _at_a_terminal_without_a_key(monkeypatch)
    monkeypatch.setattr("sys.stdin.readline", Mock(side_effect=AssertionError("must not read stdin")))

    assert _run(TasksAction, ["p-1", "--json"], _client_answering(500)) == 5

    assert caplog.messages[0].startswith("Aristotle API key not found.")


def test_a_non_interactive_run_without_a_key_is_not_offered_a_login(monkeypatch, caplog):
    _at_a_terminal_without_a_key(monkeypatch)
    monkeypatch.setattr("sys.stdin.readline", Mock(side_effect=AssertionError("must not read stdin")))

    assert _run(ShowAction, ["p-1", "--non-interactive"], _client_answering(500)) == 5

    assert caplog.messages[0].startswith("Aristotle API key not found.")


def test_answering_from_a_script_without_a_key_is_not_offered_a_login(monkeypatch, caplog):
    _at_a_terminal_without_a_key(monkeypatch)
    monkeypatch.setattr("sys.stdin.readline", Mock(side_effect=AssertionError("must not read stdin")))

    assert _run(QuestionsAction, ["p-1", "--answer", "q-1=Yes"], _client_answering(500)) == 5

    assert caplog.messages[0].startswith("Aristotle API key not found.")


NAMED = "3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f"
SAME_NAME = "3f1002f7-0b6c-4c1e-9d8e-000000000001"


def _project_record(project_id: str, name: str) -> dict:
    return {
        "project_id": project_id,
        "status": 2,
        "created_at": "2026-09-01T00:00:00",
        "last_updated": "2026-09-01T00:00:00",
        "description": name,
        "has_input": False,
        "has_files": False,
    }


def _client_listing(*records: dict) -> AsyncMock:
    """A request client that lists these projects on GET /project and refuses every other request."""

    async def get(endpoint: str, params=None):
        if endpoint == "/project":
            return Mock(json=Mock(return_value={"projects": list(records), "pagination_key": None}))
        raise AristotleAPIError("Forbidden", status_code=403)

    client = AsyncMock()
    client.get = AsyncMock(side_effect=get)
    client.post = AsyncMock(side_effect=AristotleAPIError("Not found", status_code=404))
    client.rate_limit_refusals = 0
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


BY_NAME = [
    ["show", "fifo"],
    ["show", "fifo", "--json"],
    ["tasks", "fifo"],
    ["download", "fifo"],
    ["continue", "fifo", "Prove it"],
    ["cancel", "fifo"],
    ["cancel", "--project-id", "fifo"],
    ["questions", "fifo"],
    ["properties", "fifo"],
    ["property", "fifo", "Spec.Anything"],
    ["rename", "fifo", "New name"],
]


def _main(argv: list[str], client: AsyncMock, monkeypatch) -> int:
    monkeypatch.setattr("sys.argv", ["aristotle", *argv])
    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        try:
            return controller.main()
        except SystemExit as exited:
            return exited.code  # type: ignore[return-value]


@pytest.mark.usefixtures("project_lookup")
@pytest.mark.parametrize("argv", BY_NAME, ids=" ".join)
def test_every_command_looks_a_project_up_by_name_and_uses_its_id(argv, monkeypatch):
    client = _client_listing(_project_record(SAME_NAME, "other"), _project_record(NAMED, "fifo"))

    _main(argv, client, monkeypatch)

    requested = [c.args[0] for c in client.get.await_args_list + client.post.await_args_list]
    assert requested[0] == "/project"
    assert client.get.await_args_list[0].kwargs["params"] == {"pagination_key": None, "limit": 100}
    assert any(NAMED in endpoint for endpoint in requested[1:])


@pytest.mark.usefixtures("project_lookup")
@pytest.mark.parametrize("argv", BY_NAME, ids=" ".join)
def test_every_command_refuses_an_ambiguous_name_with_exit_2_and_lists_the_matches(argv, monkeypatch, capsys, caplog):
    client = _client_listing(_project_record(NAMED, "fifo"), _project_record(SAME_NAME, "fifo"))

    assert _main(argv, client, monkeypatch) == 2

    assert capsys.readouterr().out == ""
    [message] = caplog.messages
    assert message.startswith("'fifo' matches 2 projects. Give the id of one instead:\n")
    assert f"\n  {NAMED}  fifo (IDLE, created " in message
    assert f"\n  {SAME_NAME}  fifo (IDLE, created " in message
    assert [c.args[0] for c in client.get.await_args_list] == ["/project"]
    client.post.assert_not_awaited()


def _client_with(record: dict) -> AsyncMock:
    """A request client that lists ``record`` on GET /project, serves it by id with no tasks, and refuses the rest."""
    path = f"/project/{record['project_id']}"

    async def get(endpoint: str, params=None):
        if endpoint == "/project":
            return Mock(json=Mock(return_value={"projects": [record], "pagination_key": None}))
        if endpoint == path:
            return Mock(json=Mock(return_value=record))
        if endpoint == f"{path}/tasks":
            return Mock(json=Mock(return_value={"agent_tasks": [], "pagination_key": None}))
        raise AristotleAPIError("Forbidden", status_code=403)

    client = AsyncMock()
    client.get = AsyncMock(side_effect=get)
    client.post = AsyncMock(side_effect=AristotleAPIError("Not found", status_code=404))
    client.rate_limit_refusals = 0
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.mark.usefixtures("project_lookup")
@pytest.mark.parametrize(
    ("argv", "fetches"),
    [
        (["tasks", "fifo"], 0),
        (["cancel", "fifo"], 0),
        (["cancel", "--project-id", "fifo"], 0),
        (["questions", "fifo"], 0),
        # get_files refreshes the project before it downloads.
        (["download", "fifo"], 1),
        # Their output and checks read the project's state, as it stands after the lookup.
        (["show", "fifo"], 1),
        (["continue", "fifo", "Prove it"], 1),
    ],
    ids=["tasks", "cancel", "cancel --project-id", "questions", "download", "show", "continue"],
)
def test_a_command_that_needs_only_the_project_takes_it_as_the_lookup_listed_it(argv, fetches, monkeypatch):
    client = _client_with(_project_record(NAMED, "fifo"))

    _main(argv, client, monkeypatch)

    requested = [c.args[0] for c in client.get.await_args_list]
    assert requested[0] == "/project"
    assert requested.count(f"/project/{NAMED}") == fetches


@pytest.mark.parametrize(
    ("action_cls", "argv", "terminal", "at_terminal"),
    [
        (TasksAction, ["p-1"], True, True),
        (TasksAction, ["p-1"], False, False),
        (TasksAction, ["p-1", "--json"], True, False),
        (ShowAction, ["p-1", "--non-interactive"], True, False),
        (QuestionsAction, ["p-1", "--answer", "q-1=Yes"], True, False),
    ],
    ids=["at a terminal", "off a terminal", "--json", "--non-interactive", "--answer"],
)
def test_lookups_hold_their_note_back_only_for_someone_at_a_terminal(
    action_cls, argv, terminal, at_terminal, monkeypatch
):
    seen = []

    async def run_action(self, args):
        seen.append(lookup_at_terminal.get())

    monkeypatch.setattr("sys.stderr.isatty", lambda: terminal)
    with patch.object(action_cls, "run_action", run_action):
        assert _run(action_cls, argv, _client_answering(200)) == 0

    assert seen == [at_terminal]
    assert not lookup_at_terminal.get()


def test_sigterm_ends_a_command_as_ctrl_c_does_and_is_restored_after():
    seen = []

    async def run_action(self, args):
        seen.append(signal.getsignal(signal.SIGTERM))
        raise KeyboardInterrupt

    before = signal.getsignal(signal.SIGTERM)
    with patch.object(TasksAction, "run_action", run_action):
        code = _run(TasksAction, ["p-1"], _client_answering(200))

    assert code == 130
    assert seen == [signal.default_int_handler]
    assert signal.getsignal(signal.SIGTERM) is before


def test_the_loop_runs_a_name_lookup_in_a_daemon_thread_and_returns_what_it_returns_or_raises(monkeypatch):
    threads = []

    def lookup(host, *args, **kwargs):
        threads.append(threading.current_thread())
        if host == "missing.invalid":
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)

    async def resolve():
        loop = asyncio.get_running_loop()
        with pytest.raises(socket.gaierror):
            await asyncio.wait_for(loop.getaddrinfo("missing.invalid", 443), 10)
        return await asyncio.wait_for(loop.getaddrinfo("found.invalid", 443), 10)

    assert _run_loop(resolve) == [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))]
    assert [thread.daemon for thread in threads] == [True, True]


@pytest.mark.skipif(os.name == "nt", reason="asyncio's self-pipe on Windows is a TCP connection")
def test_the_loop_takes_a_threads_result_when_the_wake_up_the_thread_sends_is_refused():
    real_send = socket.socket.send

    # As a sandbox that blocks sendto refuses it on asyncio's self-pipe.
    def send(sock, *args):
        if sock.family == socket.AF_UNIX:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real_send(sock, *args)

    async def wait_for_a_thread() -> float:
        # Undone before the run ends, whose own wake-up would otherwise be refused too.
        with pytest.MonkeyPatch.context() as refusing:
            refusing.setattr(socket.socket, "send", send)
            started = time.monotonic()
            # The wait's own timeout would wake the loop at 10 s.
            await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, time.sleep, 0.2), 10)
            return time.monotonic() - started

    assert _run_loop(wait_for_a_thread) < 5


_STALLED_LOOKUP = """
import socket, threading
from aristotlelib import api_request
socket.getaddrinfo = lambda *args, **kwargs: threading.Event().wait()
api_request.CONNECTION_DEADLINE_SECONDS = 0.2
"""

_REFUSED_WAKE_UP = """
import errno, socket, time
real_send = socket.socket.send
def send(sock, *args):
    if sock.family == socket.AF_UNIX:
        raise PermissionError(errno.EPERM, "Operation not permitted")
    return real_send(sock, *args)
def getaddrinfo(*args, **kwargs):
    time.sleep(0.05)
    raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
socket.socket.send = send
socket.getaddrinfo = getaddrinfo
"""

_RUN_THE_CLI = """
import sys
from aristotlelib import api_request
from aristotlelib.cli.controller import main
api_request.RETRY_ATTEMPTS = 2
api_request.RETRY_BACKOFF_SECONDS = 0
sys.exit(main())
"""


@pytest.mark.parametrize(
    ("sandbox", "error", "detail"),
    [
        pytest.param(_STALLED_LOOKUP, "ConnectTimeout", r"No connection within 0\.2 s", id="stalled"),
        pytest.param(
            _REFUSED_WAKE_UP,
            "ConnectError",
            r"\[Errno -?\d+\] Temporary failure in name resolution",
            id="wake-up refused",
            marks=pytest.mark.skipif(os.name == "nt", reason="asyncio's self-pipe on Windows is a TCP connection"),
        ),
    ],
)
def test_a_command_whose_name_lookups_fail_in_a_sandbox_exits_3_with_the_error(sandbox, error, detail, tmp_path):
    # Out of the tests' network guard: no proxy, the system's included, and a host no resolver finds. No CA bundle
    # either, which is logged.
    unset = set(api_request.CA_BUNDLE_VARIABLES)
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy") and k not in unset}
    scratch = str(tmp_path)
    env.update(NO_PROXY="*", ARISTOTLE_WEB_URL="https://aristotle.invalid", ARISTOTLE_API_KEY="test-key")
    env.update(HOME=scratch, XDG_CONFIG_HOME=scratch, APPDATA=scratch)
    command = [sys.executable, "-c", sandbox + _RUN_THE_CLI, "list", "--limit", "1"]

    run = subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)

    assert (run.returncode, run.stdout) == (EXIT_INFRASTRUCTURE_ERROR, "")
    failure = f"{error} on GET /project"
    # Two attempts, each well inside the 10 s connect timeout.
    assert re.fullmatch(
        rf"Retrying in 0\.0 s after {failure}: {detail}\nERROR - {failure} after 2 attempts over [0-5] s: {detail}\n",
        run.stderr,
    ), run.stderr


def test_a_command_run_off_the_main_thread_leaves_sigterm_alone():
    finished = []

    def run() -> None:
        _run(TasksAction, ["p-1"], _client_answering(200))
        finished.append(True)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(30)

    # signal.signal raises ValueError outside the main thread.
    assert finished == [True]


def _read_prompt(argv: list[str]) -> str:
    parser = argparse.ArgumentParser(prog="aristotle submit")
    add_prompt_arguments(parser, "Instructions to Aristotle")
    return read_prompt(parser, parser.parse_args(argv))


def test_a_prompt_file_is_read_as_utf8_text_without_the_newlines_it_ends_with(tmp_path):
    path = tmp_path / "prompt.txt"
    path.write_bytes("\ufeffProve that é holds.\r\n\r\nKeep the lemma names.\r\n\r\n".encode())

    assert _read_prompt(["--prompt-file", str(path)]) == "Prove that é holds.\n\nKeep the lemma names."


def test_a_prompt_file_of_minus_is_refused_at_a_terminal_which_cuts_a_long_line_short(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Mock(isatty=Mock(return_value=True)))

    with pytest.raises(SystemExit) as exited:
        _read_prompt(["--prompt-file", "-"])

    assert exited.value.code == 2
    assert "--prompt-file -: stdin is a terminal, which cuts a long line short; pipe the prompt in" in (
        capsys.readouterr().err
    )


def test_a_prompt_file_in_utf16_with_a_bom_is_read_as_powershell_writes_it(tmp_path):
    path = tmp_path / "prompt.txt"
    path.write_bytes("Prove that ∀ x ≤ 3 holds.\r\n".encode("utf-16"))

    assert _read_prompt(["--prompt-file", str(path)]) == "Prove that ∀ x ≤ 3 holds."


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        ([], "a prompt is required, as an argument or with --prompt-file"),
        (["Prove it", "--prompt-file", "-"], "give the prompt as an argument or with --prompt-file, not both"),
    ],
    ids=["neither", "both"],
)
def test_the_prompt_comes_from_the_argument_or_the_file_and_not_both(argv, error, capsys):
    with pytest.raises(SystemExit) as exited:
        _read_prompt(argv)

    assert exited.value.code == 2
    assert f"aristotle submit: error: {error}" in capsys.readouterr().err


def test_a_prompt_file_that_cannot_be_read_is_a_usage_error(tmp_path, capsys):
    not_utf8 = tmp_path / "latin1.txt"
    not_utf8.write_bytes("Prouvez que é tient.".encode("latin-1"))
    utf32 = tmp_path / "utf32.txt"
    utf32.write_bytes("Prove it.".encode("utf-32"))

    for path, error in [
        (tmp_path / "missing.txt", "could not be read"),
        (tmp_path, "could not be read"),
        (not_utf8, "is not UTF-8 text"),
        (utf32, "is not UTF-8 text"),
    ]:
        with pytest.raises(SystemExit) as exited:
            _read_prompt(["--prompt-file", str(path)])

        assert exited.value.code == 2
        assert f"error: --prompt-file: {path} {error}" in capsys.readouterr().err


def test_a_prompt_file_of_minus_with_no_stdin_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", None)

    with pytest.raises(SystemExit) as exited:
        _read_prompt(["--prompt-file", "-"])

    assert exited.value.code == 2
    assert "error: --prompt-file -: there is no stdin to read" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("action_cls", "argv"),
    [
        (SubmitAction, [""]),
        (ListAction, ["--limit", "0"]),
        (TasksAction, ["p-1", "--limit", "0"]),
        (ShowAction, ["p-1", "--destination", "out/"]),
        (ContinueAction, ["p-1", "   "]),
        (QuestionsAction, ["p-1", "--answer", "q-1=yes", "--answer", "q-1=no"]),
        (CancelAction, []),
        (ReviewAction, ["--scope", "paths"]),
        (ReviewAction, ["--scope", "all", "--path", "rtl/*.sv"]),
    ],
    ids=["submit", "list", "tasks", "show", "continue", "questions", "cancel", "review paths", "review all"],
)
def test_bad_input_ends_the_command_before_a_key_is_looked_for_or_a_login_offered(action_cls, argv):
    parser = argparse.ArgumentParser()
    action = action_cls(parser.add_subparsers())
    action.add_arguments()

    with patch.object(APIAction, "validate") as validate:
        try:
            code = action.run(action.parser.parse_args(argv, _top_level()))
        except SystemExit as exited:
            code = exited.code

    assert code == 2
    validate.assert_not_called()


@pytest.mark.parametrize(
    "argv",
    [[], ["--scope", "paths", "--path", "rtl/*.sv"], ["--scope", "changed", "--path", "rtl/*.sv"], ["--scope", "all"]],
    ids=["default scope", "paths", "changed narrowed", "all"],
)
def test_review_takes_arguments_its_scope_agrees_with_before_the_key(argv):
    parser = argparse.ArgumentParser()
    action = ReviewAction(parser.add_subparsers())
    action.add_arguments()

    assert action.preflight(action.parser.parse_args(argv, _top_level())) is None


def _updated(day: int, status: DeclarationStatus = DeclarationStatus.PROVED):
    return make_declaration(f"Spec.Day{day}", status).model_copy(update={"last_updated": datetime(2026, 1, day)})


def _created(day: int) -> AgentTask:
    return Mock(spec=AgentTask, status=TaskStatus.COMPLETE, created_at=datetime(2026, 1, day))


async def test_fetch_tasks_lists_nothing_without_a_verdict_or_when_the_latest_task_set_every_one():
    latest = _created(2)

    with patch.object(Project, "from_id") as from_id:
        assert await fetch_tasks("project-1", None, latest) == [latest]
        assert await fetch_tasks("project-1", [_updated(1, DeclarationStatus.IN_PROGRESS)], latest) == [latest]
        assert await fetch_tasks("project-1", [_updated(2), _updated(3)], latest) == [latest]
        assert await fetch_tasks("project-1", [], None) == []

    from_id.assert_not_called()


async def test_fetch_tasks_pages_back_to_the_task_that_set_the_oldest_verdict():
    pages = {None: ([_created(9), _created(7)], "k1"), "k1": ([_created(5), _created(3)], "k2"), "k2": ([], None)}
    project = Mock(spec=Project)
    project.get_tasks = AsyncMock(side_effect=lambda pagination_key, limit: pages[pagination_key])

    with patch.object(Project, "from_id", AsyncMock(return_value=project)):
        tasks = await fetch_tasks("project-1", [_updated(8), _updated(4)], _created(9))

    assert [t.created_at.day for t in tasks] == [9, 7, 5, 3]
    assert [c.kwargs for c in project.get_tasks.await_args_list] == [
        {"pagination_key": None, "limit": 100},
        {"pagination_key": "k1", "limit": 100},
    ]


async def test_fetch_tasks_keeps_what_it_has_when_listing_fails(caplog):
    project = Mock(spec=Project)
    project.get_tasks = AsyncMock(side_effect=[([_created(9)], "k1"), AristotleAPIError("down", status_code=500)])
    latest = _created(9)

    with patch.object(Project, "from_id", AsyncMock(return_value=project)):
        assert [t.created_at.day for t in await fetch_tasks("project-1", [_updated(4)], latest)] == [9]
    # A verdict older than every task listed may then count as checked, which the warning says.
    assert any("couldn't all be listed" in m and "may count as checked" in m for m in caplog.messages)
    with patch.object(Project, "from_id", AsyncMock(side_effect=AristotleAPIError("down", status_code=500))):
        assert await fetch_tasks("project-1", [_updated(4)], latest) == [latest]
        assert await fetch_tasks("project-1", [_updated(4)]) == []


async def test_fetch_tasks_raises_when_strict_and_listing_fails_on_any_page():
    project = Mock(spec=Project)
    project.get_tasks = AsyncMock(side_effect=[([_created(9)], "k1"), AristotleAPIError("down", status_code=500)])

    with patch.object(Project, "from_id", AsyncMock(return_value=project)), pytest.raises(AristotleAPIError):
        await fetch_tasks("project-1", [_updated(4)], strict=True)
    with (
        patch.object(Project, "from_id", AsyncMock(side_effect=AristotleAPIError("down", status_code=500))),
        pytest.raises(AristotleAPIError),
    ):
        await fetch_tasks("project-1", [_updated(4)], strict=True)


async def test_judge_properties_counts_by_the_tasks_or_nulls_all_with_the_error_when_they_cannot_be_listed():
    latest = _created(9)
    verdict = _updated(4)
    project = Mock(spec=Project)
    down = AristotleAPIError("down", status_code=503)

    with (
        patch("aristotlelib.cli.api_action.fetch_properties", AsyncMock(return_value=[verdict])),
        patch.object(Project, "from_id", AsyncMock(return_value=project)),
    ):
        project.get_tasks = AsyncMock(return_value=([latest, _created(3)], None))
        summary, error = await judge_properties("project-1", latest)
        assert summary.property_counts is not None and summary.requested_counts is not None
        assert ([s.name for s in summary.properties or []], error) == ([verdict.name], None)

        project.get_tasks = AsyncMock(side_effect=down)
        assert await judge_properties("project-1", latest) == (NO_SUMMARY, down)


async def test_judge_properties_takes_any_failure_to_list_as_unjudged_and_the_exit_follows_the_error():
    latest = _created(9)
    broken = ValueError("not JSON")

    with (
        patch("aristotlelib.cli.api_action.fetch_properties", AsyncMock(return_value=[_updated(4)])),
        patch.object(Project, "from_id", AsyncMock(side_effect=broken)),
    ):
        assert await judge_properties("project-1", latest) == (NO_SUMMARY, broken)

    assert log_unjudged("project-1", broken) == EXIT_INFRASTRUCTURE_ERROR
    assert log_unjudged("project-1", AristotleAPIError("slow down", status_code=429)) == EXIT_RATE_LIMITED
