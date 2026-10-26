"""Tests for aristotlelib.cli.controller module."""

import argparse
import contextlib
import io
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from aristotlelib import api_request
from aristotlelib.cli import controller
from aristotlelib.cli.api_action import APIAction
from aristotlelib.cli.ci_setup import SetupAction
from aristotlelib.cli.command import ci as ci_command
from aristotlelib.cli.command.cancel import CancelAction
from aristotlelib.cli.command.ci import CIAction
from aristotlelib.cli.command.continue_ import ContinueAction
from aristotlelib.cli.command.download import DownloadAction
from aristotlelib.cli.command.list import ListAction
from aristotlelib.cli.command.login import LoginAction
from aristotlelib.cli.command.logout import LogoutAction
from aristotlelib.cli.command.properties import PropertiesAction
from aristotlelib.cli.command.property import PropertyAction
from aristotlelib.cli.command.questions import QuestionsAction
from aristotlelib.cli.command.rename import RenameAction
from aristotlelib.cli.command.review import ReviewAction
from aristotlelib.cli.command.show import ShowAction
from aristotlelib.cli.command.submit import SubmitAction
from aristotlelib.cli.command.tasks import TasksAction
from aristotlelib.cli.controller import (
    ASK_REPLACED_MESSAGE,
    FORMALIZE_REMOVED_MESSAGE,
    VERILOG_REMOVED_MESSAGE,
    _AfterStdoutHandler,
    action_classes,
    main,
)
from aristotlelib.cli.login_flow import LoginError
from tests.conftest import user_agent_of


def test_action_classes_returns_all_actions_in_priority_order():
    classes = action_classes()
    assert classes == [
        LoginAction,  # 5
        SubmitAction,  # 10
        CIAction,  # 15
        ReviewAction,  # 16
        ContinueAction,  # 20
        DownloadAction,  # 40
        ListAction,  # 50
        ShowAction,  # 60
        PropertiesAction,  # 62
        PropertyAction,  # 64
        TasksAction,  # 70
        QuestionsAction,  # 75
        CancelAction,  # 80
        RenameAction,  # 85
        LogoutAction,  # 90
    ]


def test_ask_command_prints_replacement_message(capsys):
    with patch("sys.argv", ["aristotle", "ask", "project-123", "old prompt", "--mode", "ask"]):
        assert main() == 2

    captured = capsys.readouterr()
    assert ASK_REPLACED_MESSAGE in captured.err


def test_verilog_command_points_at_project_dir(capsys):
    with patch("sys.argv", ["aristotle", "verilog", "--top", "top", "rtl/top.sv"]):
        assert main() == 2

    captured = capsys.readouterr()
    assert VERILOG_REMOVED_MESSAGE in captured.err


def test_formalize_command_points_at_submit(capsys):
    with patch("sys.argv", ["aristotle", "formalize", "paper.tex", "--wait"]):
        assert main() == 2

    captured = capsys.readouterr()
    assert FORMALIZE_REMOVED_MESSAGE in captured.err


@pytest.mark.parametrize("api_key", [["--api-key", "k"], ["--api_key", "k"], ["--api-key=k"]])
def test_a_retired_command_after_the_api_key_still_prints_its_replacement(api_key, capsys):
    with patch("sys.argv", ["aristotle", *api_key, "formalize", "paper.tex"]):
        assert main() == 2

    assert FORMALIZE_REMOVED_MESSAGE in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "used"),
    [
        (["--api-key", "top-key", "list"], "top-key"),
        (["list", "--api-key", "sub-key"], "sub-key"),
        (["--api-key", "top-key", "list", "--api-key", "sub-key"], "sub-key"),
        (["list"], "env-key"),
    ],
    ids=["before the command", "after the command", "both: the command's wins", "neither"],
)
def test_api_key_is_used_before_or_after_the_command(argv, used, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("ARISTOTLE_API_KEY", "env-key")
    monkeypatch.setattr(api_request, "API_KEY", None)
    monkeypatch.setattr("sys.argv", ["aristotle", *argv])
    seen: list[str] = []

    async def run_action(self, args):
        seen.append(api_request.get_api_key())

    with patch.object(ListAction, "run_action", run_action):
        main()

    assert seen == [used]


def test_a_rejected_api_key_before_the_command_is_reported_as_the_argument(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("ARISTOTLE_API_KEY", "env-key")
    monkeypatch.setattr(api_request, "API_KEY", None)
    monkeypatch.setattr("sys.argv", ["aristotle", "--api-key", "bad-key", "list"])
    rejected = api_request.AristotleAPIError("Unauthorized", status_code=401)

    with patch("aristotlelib.cli.command.list.Project.list_projects", side_effect=rejected):
        main()

    assert "The API key passed with --api-key is not valid." in caplog.text


@pytest.mark.parametrize(
    ("argv", "environment", "surface"),
    [
        (["list"], {}, "cli"),
        (["ci", "setup"], {}, "cli"),
        (["ci"], {}, "ci"),
        (["review"], {}, "ci"),
        (["ci"], {"GITHUB_ACTIONS": "true", "GITHUB_ACTION_REPOSITORY": ""}, "ci"),
        (["ci"], {"GITHUB_ACTIONS": "true", "GITHUB_ACTION_REPOSITORY": "harmonic-ai/aristotle"}, "action"),
        (["review"], {"GITHUB_ACTIONS": "true", "GITHUB_ACTION_REPOSITORY": "someone/aristotle-fork"}, "action"),
        (["list"], {"GITHUB_ACTIONS": "true", "GITHUB_ACTION_REPOSITORY": "harmonic-ai/aristotle"}, "cli"),
    ],
    ids=["list", "ci setup", "ci", "review", "ci in a run step", "ci in action", "review in a fork", "list in action"],
)
def test_a_commands_requests_name_its_surface(argv, environment, surface, api_server, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTION_REPOSITORY", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("sys.argv", ["aristotle", *argv])
    requests = api_server(httpx.Response(200, json={}))

    async def send_a_request(self, args):
        async with api_request.AristotleRequestClient() as client:
            await client.get("/project")

    with contextlib.ExitStack() as stack:
        for action in (ListAction, SetupAction, CIAction, ReviewAction):
            stack.enter_context(patch.object(action, "preflight", return_value=None))
            stack.enter_context(patch.object(action, "run_action", send_a_request))
        assert main() == 0

    assert [request.headers["User-Agent"] for request in requests] == [user_agent_of(surface)]


def test_output_that_the_console_encoding_cannot_represent_is_escaped_not_a_crash(monkeypatch):
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="cp1252", newline="\n")
    monkeypatch.setattr(sys, "stdout", stdout)

    controller._tolerate_unencodable_output()
    print("∀ n, n ≤ n → True")
    stdout.flush()

    assert raw.getvalue().decode("cp1252") == "\\u2200 n, n \\u2264 n \\u2192 True\n"


def test_an_unexpected_error_keeps_its_traceback_and_exits_3(monkeypatch, capsys):
    monkeypatch.setattr(api_request, "API_KEY", "test-key")
    monkeypatch.setattr("sys.argv", ["aristotle", "list"])

    with patch("aristotlelib.cli.command.list.Project.list_projects", side_effect=RuntimeError("boom")):
        assert main() == 3

    err = capsys.readouterr().err
    assert err.startswith("Traceback (most recent call last):")
    assert err.rstrip().endswith("RuntimeError: boom")


PROJECT_ID = "3f1002f7-0000-4000-8000-000000000000"


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["list"], 3),
        (["show", PROJECT_ID], 3),
        (["show", PROJECT_ID, "--wait", "--json"], 3),
        (["tasks", PROJECT_ID], 3),
        (["download", PROJECT_ID], 3),
        (["continue", PROJECT_ID, "Prove it"], 3),
        (["cancel", PROJECT_ID], 3),
        (["questions", PROJECT_ID], 3),
        (["properties", PROJECT_ID], 3),
        (["property", PROJECT_ID, "Spec.Anything"], 3),
        (["rename", PROJECT_ID, "New name"], 3),
        (["submit", "Prove it"], 7),
        (["login", "--api-key", "fake-key"], 3),
    ],
    ids=lambda value: " ".join(word for word in value if word != PROJECT_ID) if isinstance(value, list) else None,
)
@pytest.mark.usefixtures("frozen_api_clock")
def test_a_page_in_place_of_aristotles_answers_ends_every_command_with_its_error_not_a_traceback(
    argv, code, api_server, monkeypatch, tmp_path, capsys, caplog
):
    monkeypatch.setattr(api_request, "getproxies", dict)
    monkeypatch.delenv("ARISTOTLE_WEB_URL", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    page = httpx.Response(200, html="<title>Hotel WiFi - Sign in</title>")
    api_server(*[page] * api_request.RETRY_ATTEMPTS)
    monkeypatch.setattr("sys.argv", ["aristotle", *argv])

    with patch("sys.stdin", io.StringIO("")):
        try:
            exit_code = main()
        except SystemExit as raised:
            exit_code = raised.code

    err = capsys.readouterr().err
    assert exit_code == code
    assert (
        "the answer from https://aristotle.harmonic.fun was 200 OK with text/html, not Aristotle's: a captive portal "
        "or a proxy may have answered instead." in (err or caplog.messages[-1])
    )
    assert "Traceback" not in err
    assert list(tmp_path.iterdir()) == []


def test_on_github_an_unexpected_errors_traceback_cannot_start_a_workflow_command(monkeypatch, capsys):
    monkeypatch.setattr(api_request, "API_KEY", "test-key")
    monkeypatch.setattr("sys.argv", ["aristotle", "list"])
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    with patch("aristotlelib.cli.command.list.Project.list_projects", side_effect=RuntimeError("boom\n::error::x")):
        assert main() == 3

    assert capsys.readouterr().err.rstrip().endswith("RuntimeError: boom\n\\::error::x")


def test_on_github_a_usage_errors_message_is_escaped_but_not_what_argparse_adds(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    with pytest.raises(SystemExit) as exited:
        controller.CLIParser(prog="aristotle").error("bad \x1b[31m\n::error::x")
    with pytest.raises(SystemExit):
        controller.CLIParser(prog="aristotle").exit(2, "\x1b[35merror:\x1b[0m as argparse 3.15 colours it\n")

    assert exited.value.code == 2
    err = capsys.readouterr().err
    assert "aristotle: error: bad \\x1b[31m\n\\::error::x\n" in err
    assert err.endswith("\x1b[35merror:\x1b[0m as argparse 3.15 colours it\n")


def test_on_github_a_failed_login_cannot_start_a_workflow_command(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr("sys.argv", ["aristotle", "login"])
    failure = LoginError("Aristotle answered 400: \x1b[31m\n::error::forged", 3)

    with (
        patch("aristotlelib.cli.command.login.login", AsyncMock(side_effect=failure)),
        pytest.raises(SystemExit) as exited,
    ):
        main()

    assert exited.value.code == 3
    assert capsys.readouterr().err == "Aristotle answered 400: \\x1b[31m\n\\::error::forged\n"


def test_a_usage_error_from_a_command_is_not_caught(monkeypatch):
    monkeypatch.setattr("sys.argv", ["aristotle", "list", "--limit", "0"])
    monkeypatch.setattr(api_request, "API_KEY", "test-key")

    with pytest.raises(SystemExit) as exited:
        main()

    assert exited.value.code == 2


def test_a_bad_ci_environment_variable_does_not_break_other_commands(monkeypatch, capsys):
    monkeypatch.setenv("ARISTOTLE_FAIL_ON", "bogus")
    monkeypatch.setenv("ARISTOTLE_SCOPE", "bogus")
    monkeypatch.setenv("ARISTOTLE_TIMEOUT", "soon")

    with patch("sys.argv", ["aristotle", "login", "--help"]), pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    assert "usage: aristotle login" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("command", "variable", "value"),
    [
        ("ci", "ARISTOTLE_FAIL_ON", "bogus"),
        ("review", "ARISTOTLE_SCOPE", "bogus"),
        ("ci", "ARISTOTLE_TIMEOUT", "soon"),
        ("ci", "ARISTOTLE_ON_TIMEOUT", "bogus"),
    ],
)
def test_a_bad_ci_environment_variable_is_a_usage_error_for_the_command_reading_it(
    monkeypatch, capsys, command, variable, value
):
    monkeypatch.setenv(variable, value)

    with patch("sys.argv", ["aristotle", command, "--dry-run"]), pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert f"'{value}'" in err
    assert variable in err


# 1e307 is finite in minutes but overflows to inf once converted to seconds.
@pytest.mark.parametrize("value", ["0", "-5", "nan", "inf", "1e307", "10081"])
def test_a_non_positive_non_finite_or_oversized_timeout_is_a_usage_error(monkeypatch, capsys, value):
    monkeypatch.setenv("ARISTOTLE_TIMEOUT", value)
    monkeypatch.setattr("sys.argv", ["aristotle", "ci", "--dry-run"])

    with pytest.raises(SystemExit) as exited:
        main()

    assert exited.value.code == 2
    assert "ARISTOTLE_TIMEOUT" in capsys.readouterr().err


def test_the_maximum_timeout_is_accepted():
    assert ci_command._positive_minutes(str(ci_command.MAX_TIMEOUT_MINUTES)) == ci_command.MAX_TIMEOUT_MINUTES


def _parsed(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> argparse.Namespace:
    """The arguments `aristotle <argv>` hands its command, which does not run."""
    seen: list[argparse.Namespace] = []

    def run(self, args):
        seen.append(args)
        return 0

    monkeypatch.setattr(APIAction, "run", run)
    monkeypatch.setattr("sys.argv", ["aristotle", *argv])
    assert main() == 0
    return seen[0]


def _usage_error(argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys) -> str:
    """What `aristotle <argv>` prints on stderr, failing with exit 2 while parsing."""
    monkeypatch.setattr("sys.argv", ["aristotle", *argv])
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 2
    return capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--verbose", "ci", "setup"],
        ["ci", "--verbose", "setup"],
        ["ci", "setup", "--verbose"],
        ["--verbose", "ci", "setup", "--verbose"],
    ],
)
def test_verbose_is_accepted_before_or_after_the_nested_setup_command(argv, monkeypatch):
    assert _parsed(argv, monkeypatch).verbose is True


@pytest.mark.parametrize(
    ("argv", "dest", "value"),
    [
        (["submit", "--project_dir", "rtl", "Prove it"], "project_dir", Path("rtl")),
        (["submit", "--project_dir=rtl", "Prove it"], "project_dir", Path("rtl")),
        (["submit", "--project_dir=my rtl", "Prove it"], "project_dir", Path("my rtl")),
        (["submit", "Prove it", "--non_interactive"], "non_interactive", True),
        (["continue", "abc", "Go on", "--non_interactive"], "non_interactive", True),
        (["list", "--pagination_key", "k"], "pagination_key", "k"),
        (["list", "--pagination_key=k"], "pagination_key", "k"),
        (["review", "--base_ref", "main"], "base_ref", "main"),
        (["review", "--base_ref=origin/main", "--dry_run"], "dry_run", True),
        (["ci", "--no_junit", "--dry_run"], "no_junit", True),
        (["ci", "--on_timeout", "leave", "setup", "--force"], "on_timeout", "leave"),
        (["--api_key", "k", "list"], "api_key", "k"),
        (["list", "--api_key=k"], "api_key", "k"),
    ],
)
def test_an_underscore_spelling_is_the_option(argv, dest, value, monkeypatch):
    assert getattr(_parsed(argv, monkeypatch), dest) == value


@pytest.mark.parametrize(
    ("argv", "dest", "value"),
    [
        (["submit", "--", "--project_dir"], "prompt", "--project_dir"),
        (["review", "--instructions", "--base_ref=main is wrong"], "instructions", "--base_ref=main is wrong"),
        (["submit", "--name", "--project_dir=x y", "Prove it"], "name", "--project_dir=x y"),
        (["list", "--limit", "-5"], "limit", -5),
        (
            ["continue", "abc", "Go on", "--files", "a.lean", "--api_key=a b"],
            "files",
            [Path("a.lean"), Path("--api_key=a b")],
        ),
    ],
)
def test_a_value_is_not_respelled(argv, dest, value, monkeypatch):
    assert getattr(_parsed(argv, monkeypatch), dest) == value


@pytest.mark.parametrize(
    ("argv", "dest", "value"),
    [
        (["submit", "--proj", "rtl", "Prove it"], "project_dir", Path("rtl")),
        (["list", "--pag=k"], "pagination_key", "k"),
        (["continue", "abc", "Go on", "--non"], "non_interactive", True),
        (["ci", "--man", "ci.toml", "--dry"], "manifest", Path("ci.toml")),
    ],
)
def test_an_abbreviation_still_selects_its_option(argv, dest, value, monkeypatch):
    assert getattr(_parsed(argv, monkeypatch), dest) == value


@pytest.mark.parametrize(
    ("argv", "prompt"),
    [
        (["continue", "p-1", "--wait", "Go on"], "Go on"),
        (["continue", "p-1", "--json", "--no-questions", "Go on"], "Go on"),
        (["continue", "p-1", "Go on", "--wait"], "Go on"),
        (["continue", "p-1", "--prompt-file", "-"], None),
    ],
)
def test_continue_takes_its_prompt_after_an_option_on_every_python(argv, prompt, monkeypatch):
    # Before 3.12.7, and on 3.13.0, argparse gave the optional prompt nothing as soon as an option followed the project.
    assert _parsed(argv, monkeypatch).prompt == prompt


@pytest.mark.parametrize(
    ("argv", "command", "message"),
    [
        (
            ["submit", "--projectdir", "rtl", "Prove it"],
            "submit",
            "unknown option --projectdir (did you mean --project-dir?)",
        ),
        (
            ["submit", "--project-dirr=rtl", "Prove it"],
            "submit",
            "unknown option --project-dirr (did you mean --project-dir?)",
        ),
        (["submit", "--nmae=My project", "Prove it"], "submit", "unknown option --nmae (did you mean --name?)"),
        (
            ["submit", "---project-dir", "rtl", "Prove it"],
            "submit",
            "unknown option ---project-dir (did you mean --project-dir?)",
        ),
        (["submit", "Prove it", "--name", "--wiat"], "submit", "unknown option --wiat (did you mean --wait?)"),
        (["submit", "--2wait", "./x", "Prove it"], "submit", "unknown option --2wait (did you mean --wait?)"),
        (["continue", "abc", "--fils", "a.lean", "Go on"], "continue", "unknown option --fils (did you mean --files?)"),
        (["continue", "abc", "--jsn"], "continue", "unknown option --jsn (did you mean --json?)"),
        (["list", "--stauts", "IDLE"], "list", "unknown option --stauts (did you mean --status?)"),
        (["list", "--bogus"], "list", "unknown option --bogus"),
        (["list", "--projectdir", "a", "--wiat"], "list", "unknown options --projectdir, --wiat"),
        (["review", "--scpoe", "all"], "review", "unknown option --scpoe (did you mean --scope?)"),
        (["ci", "--manifst", "ci.toml"], "ci", "unknown option --manifst (did you mean --manifest?)"),
        (["ci", "--manifest", "ci.toml", "--dryrun"], "ci", "unknown option --dryrun (did you mean --dry-run?)"),
        (["ci", "setup", "--forse"], "ci setup", "unknown option --forse (did you mean --force?)"),
    ],
)
def test_an_unknown_option_is_named_by_its_command_with_the_closest_option(argv, command, message, monkeypatch, capsys):
    err = _usage_error(argv, monkeypatch, capsys)

    assert err.startswith(f"usage: aristotle {command} [-h]")
    assert err.splitlines()[-1] == f"aristotle {command}: error: {message}"


@pytest.mark.parametrize(
    "argv",
    [
        ["--verison", "submit", "./x", "PROMPT TEXT"],
        ["--verison", "submit"],
        ["--verison", "ci", "setup", "--force", "PROMPT TEXT"],
    ],
)
def test_an_unknown_option_before_the_command_outranks_the_commands_error(argv, monkeypatch, capsys):
    err = _usage_error(argv, monkeypatch, capsys)

    assert err.startswith("usage: aristotle [-h]")
    assert err.splitlines()[-1] == "aristotle: error: unknown option --verison (did you mean --version?)"
    assert "PROMPT TEXT" not in err


def test_an_unknown_option_before_the_command_is_named_by_aristotle(monkeypatch, capsys):
    err = _usage_error(["--apikey", "SECRET-KEY", "list"], monkeypatch, capsys)

    assert err.startswith("usage: aristotle [-h]")
    assert err.splitlines()[-1] == "aristotle: error: unknown option --apikey (did you mean --api-key?)"
    assert "SECRET-KEY" not in err


@pytest.mark.parametrize(
    "argv",
    [
        ["submit", "--projectdir", "./x", "PROMPT TEXT"],
        ["submit", "---project-dir", "./x", "PROMPT TEXT"],
        ["submit", "--2wait", "./x", "PROMPT TEXT"],
        ["submit", "---", "./x", "PROMPT TEXT"],
        ["submit", "---=NAME TEXT", "PROMPT TEXT"],
        ["submit", "--nmae=NAME TEXT", "PROMPT TEXT"],
        ["submit", "--apikey", "SECRET-KEY", "PROMPT TEXT"],
        ["submit", "PROMPT TEXT", "--nmae", "NAME TEXT"],
        ["continue", "abc", "--fils", "a.lean", "PROMPT TEXT"],
    ],
)
def test_the_error_names_only_the_option(argv, monkeypatch, capsys):
    err = _usage_error(argv, monkeypatch, capsys)

    assert not any(text in err for text in ("PROMPT TEXT", "NAME TEXT", "SECRET-KEY", "./x", "a.lean"))


@pytest.mark.skipif(sys.version_info >= (3, 14), reason="argparse 3.14 reads -5x as a negative number")
def test_a_dash_and_digit_that_is_not_a_number_is_an_unknown_option(monkeypatch, capsys):
    err = _usage_error(["submit", "-5x", "./x", "PROMPT TEXT"], monkeypatch, capsys)

    assert err.splitlines()[-1] == "aristotle submit: error: unknown option -5x"
    assert "PROMPT TEXT" not in err


def test_a_stray_argument_is_reported_by_its_command(monkeypatch, capsys):
    err = _usage_error(["list", "extra"], monkeypatch, capsys)

    assert err.startswith("usage: aristotle list [-h]")
    assert err.splitlines()[-1] == "aristotle list: error: unrecognized arguments: extra"


def test_an_unknown_command_is_still_reported_as_one(monkeypatch, capsys):
    err = _usage_error(["submti", "--project_dir", "rtl"], monkeypatch, capsys)

    assert "aristotle: error: argument command: invalid choice: 'submti'" in err


@pytest.mark.parametrize(
    "command",
    [
        "",
        "login",
        "submit",
        "ci",
        "ci setup",
        "review",
        "continue",
        "download",
        "list",
        "show",
        "properties",
        "property",
        "tasks",
        "questions",
        "cancel",
        "rename",
        "logout",
    ],
)
def test_help_is_what_argparse_prints(command, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["aristotle", *command.split(), "--help"])
    with pytest.raises(SystemExit):
        main()
    printed = capsys.readouterr().out

    monkeypatch.setattr(controller, "CLIParser", argparse.ArgumentParser)
    with pytest.raises(SystemExit):
        main()

    assert printed == capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "last_line"),
    [
        (
            ["submit", "--project_dir", "./x", "Prove it"],
            "aristotle submit: error: --project-dir must be a valid directory: x",
        ),
        (
            ["submit", "--projectdir", "./x", "Prove it"],
            "aristotle submit: error: unknown option --projectdir (did you mean --project-dir?)",
        ),
    ],
)
def test_a_misspelled_option_reaches_the_terminal_without_the_prompt(argv, last_line, tmp_path):
    command = [sys.executable, "-m", "aristotlelib.cli.controller", *argv]
    env = {**os.environ, "ARISTOTLE_API_KEY": "test-key", "XDG_CONFIG_HOME": str(tmp_path)}

    run = subprocess.run(command, capture_output=True, text=True, timeout=60, cwd=tmp_path, env=env)

    assert (run.returncode, run.stdout) == (2, "")
    assert run.stderr.startswith("usage: aristotle submit [-h]")
    assert run.stderr.splitlines()[-1] == last_line
    assert "Prove it" not in run.stderr


def _cli(*argv: str, config: Path) -> subprocess.CompletedProcess[str]:
    """Run the CLI in a new process with no API key, stdin empty, and its config directory at ``config``."""
    env = {name: value for name, value in os.environ.items() if name != "ARISTOTLE_API_KEY"}
    env.update(XDG_CONFIG_HOME=str(config), APPDATA=str(config))
    command = [sys.executable, "-m", "aristotlelib.cli.controller", *argv]
    return subprocess.run(
        command, capture_output=True, text=True, timeout=60, cwd=config, env=env, stdin=subprocess.DEVNULL
    )


def test_the_cli_prints_a_message_as_it_is_and_an_error_after_its_level(tmp_path):
    logout = _cli("logout", config=tmp_path)
    no_key = _cli("list", config=tmp_path)

    assert (logout.returncode, logout.stderr) == (0, "No stored API key to remove.\n")
    assert no_key.returncode == 5
    assert no_key.stderr.startswith("ERROR - Aristotle API key not found.\nLog in with:\n  aristotle login\n")


def test_the_cli_prints_its_help_without_a_command(tmp_path):
    run = _cli(config=tmp_path)

    assert run.returncode == 0
    assert run.stdout.startswith("usage: aristotle ")
    assert "Aristotle SDK - formally verify your code from Python or the command line" in run.stdout
    assert "\033[" not in run.stdout
    assert run.stderr == ""


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["submit", ""], "aristotle submit: error: Prompt cannot be empty"),
        (["list", "--limit", "0"], "aristotle list: error: --limit must be between 1 and 100"),
    ],
)
def test_input_is_checked_before_the_api_key(argv: list[str], error: str, tmp_path):
    run = _cli(*argv, config=tmp_path)

    assert run.returncode == 2
    assert run.stderr.splitlines()[-1] == error
    assert "API key" not in run.stderr


def test_a_note_follows_what_was_printed_before_it_where_stdout_and_stderr_share_a_pipe():
    script = (
        "import logging; from aristotlelib.cli import controller; controller._configure_logging(); "
        "print('table'); logging.info('Pagination key for next page: next-page')"
    )
    # With PYTHONUNBUFFERED, stdout isn't held back, and the old handler would pass too.
    env = {name: value for name, value in os.environ.items() if name != "PYTHONUNBUFFERED"}
    run = subprocess.run(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60, env=env
    )

    assert run.stdout == "table\nPagination key for next page: next-page\n"


def test_a_message_is_logged_even_when_stdout_cannot_be_flushed(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _ClosingPipe())
    stderr = io.StringIO()
    handler = _AfterStdoutHandler(stderr)

    handler.emit(logging.LogRecord("aristotle", logging.INFO, __file__, 1, "Pagination key", None, None))

    assert stderr.getvalue() == "Pagination key\n"


class _ClosedPipe(io.StringIO):
    """A stdout whose reader has gone: every write and flush fails as a pipe's would."""

    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


class _ClosingPipe(io.StringIO):
    """A stdout that takes the writes and fails the flush, as a pipe whose reader left after the first bytes."""

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


@pytest.fixture
def discard_output(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Stand in for pointing stdout and stderr at the null device, which would take pytest's capture with them."""
    discard = Mock()
    monkeypatch.setattr(controller, "_discard_output", discard)
    return discard


@pytest.mark.parametrize("stdout", [_ClosedPipe, _ClosingPipe])
def test_output_to_a_closed_pipe_ends_the_command_quietly_with_141(stdout, discard_output, monkeypatch, capsys):
    monkeypatch.setattr(api_request, "API_KEY", "test-key")
    monkeypatch.setattr("sys.argv", ["aristotle", "list", "--json"])
    monkeypatch.setattr(sys, "stdout", stdout())

    with patch("aristotlelib.cli.command.list.Project.list_projects", AsyncMock(return_value=([], None))):
        assert main() == 141

    discard_output.assert_called_once_with()
    assert capsys.readouterr().err == ""


def test_a_closed_pipe_on_stderr_ends_the_command_quietly_with_141(discard_output, monkeypatch):
    monkeypatch.setattr(api_request, "API_KEY", "test-key")
    monkeypatch.setattr("sys.argv", ["aristotle", "list", "--json"])
    monkeypatch.setattr(sys, "stderr", _ClosingPipe())

    with patch("aristotlelib.cli.command.list.Project.list_projects", AsyncMock(return_value=([], None))):
        assert main() == 141

    discard_output.assert_called_once_with()


def test_help_to_a_closed_pipe_ends_quietly_with_141(discard_output, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["aristotle", "--help"])
    monkeypatch.setattr(sys, "stdout", _ClosedPipe())

    assert main() == 141
    assert capsys.readouterr().err == ""


class _FullDisk(io.StringIO):
    """A stdout whose flush fails for a reason other than a closed pipe."""

    def flush(self) -> None:
        raise OSError(28, "No space left on device")


def test_another_failure_to_flush_is_left_to_the_interpreter_as_before(discard_output, monkeypatch, capsys):
    monkeypatch.setattr(api_request, "API_KEY", "test-key")
    monkeypatch.setattr("sys.argv", ["aristotle", "list", "--json"])
    monkeypatch.setattr(sys, "stdout", _FullDisk())

    with patch("aristotlelib.cli.command.list.Project.list_projects", AsyncMock(return_value=([], None))):
        assert main() == 0

    discard_output.assert_not_called()
    assert capsys.readouterr().err == ""


@pytest.mark.skipif(sys.platform == "win32", reason="SIGPIPE and EPIPE are POSIX")
def test_a_pipe_nothing_reads_ends_the_process_with_141_and_nothing_on_stderr(tmp_path):
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        run = subprocess.run(
            [sys.executable, "-m", "aristotlelib.cli.controller", "--help"],
            stdout=write_end,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            cwd=tmp_path,
        )
    finally:
        os.close(write_end)

    assert (run.returncode, run.stderr) == (141, "")


@pytest.mark.skipif(sys.platform == "win32", reason="SIGPIPE and EPIPE are POSIX")
def test_a_log_line_to_a_pipe_nothing_reads_ends_the_process_with_141(tmp_path):
    read_end, write_end = os.pipe()
    os.close(read_end)
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path), "APPDATA": str(tmp_path)}
    try:
        run = subprocess.run(
            [sys.executable, "-m", "aristotlelib.cli.controller", "logout"],
            stdout=subprocess.DEVNULL,
            stderr=write_end,
            timeout=60,
            cwd=tmp_path,
            env=env,
        )
    finally:
        os.close(write_end)

    assert run.returncode == 141


@pytest.mark.parametrize(
    "command",
    [
        "submit",
        "ci",
        "ci setup",
        "review",
        "continue",
        "download",
        "list",
        "show",
        "properties",
        "property",
        "tasks",
        "questions",
        "cancel",
        "rename",
        "logout",
    ],
)
def test_a_commands_help_leaves_the_api_key_to_the_top_level_help(command, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["aristotle", *command.split(), "--help"])

    with pytest.raises(SystemExit):
        main()

    assert "--api-key" not in capsys.readouterr().out


def test_the_top_level_help_says_login_stores_the_api_key(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["aristotle", "--help"])

    with pytest.raises(SystemExit):
        main()

    help_text = " ".join(capsys.readouterr().out.split())
    assert "--api-key API_KEY Aristotle API key for this command only" in help_text
    assert "With `login`, the key to check and store." in help_text


def test_logout_takes_no_api_key(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["aristotle", "logout", "--api-key", "k"])

    with patch.object(LogoutAction, "run_action") as run_action, pytest.raises(SystemExit) as exited:
        main()

    assert exited.value.code == 2
    run_action.assert_not_called()
    assert capsys.readouterr().err.endswith("aristotle logout: error: unknown option --api-key\n")
