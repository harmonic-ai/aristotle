"""Tests for the login and logout commands, and for the offer to log in."""

import argparse
import asyncio
import gc
import json
import logging
import os
import select
import signal
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from aristotlelib import api_request, credentials
from aristotlelib.cli.api_action import APIAction
from aristotlelib.cli.command.login import LoginAction
from aristotlelib.cli.command.logout import LogoutAction
from aristotlelib.cli.controller import main
from aristotlelib.cli.login_flow import EXPIRED, LoginError
from aristotlelib.results import LoginResult
from tests.cli.conftest import APPROVED_KEY, DEVICE_CODE, KEY_NAME, USER_CODE, approved, token_error
from tests.conftest import user_agent_of

NOT_FOUND = httpx.Response(404, json={"detail": "Not Found"})
"""How a server without device login answers its start."""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("ARISTOTLE_WEB_URL", raising=False)
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    monkeypatch.setattr(api_request, "API_KEY", None)


def _action(cls):
    parser = argparse.ArgumentParser()
    action = cls(parser.add_subparsers())
    action.add_action_arguments()
    return action


async def test_login_opens_the_browser_and_prints_no_json_by_default(capsys):
    action = _action(LoginAction)
    login = AsyncMock(return_value=LoginResult(credentials_path="/tmp/creds", key_name=None))

    with patch("aristotlelib.cli.command.login.login", login):
        await action.run_action(action.parser.parse_args([]))

    assert login.await_args.kwargs["open_browser"] is True
    assert login.await_args.kwargs["on_pending"] is None
    assert capsys.readouterr().out == ""


async def test_no_browser_is_passed_through():
    action = _action(LoginAction)
    login = AsyncMock(return_value=LoginResult(credentials_path="/tmp/creds", key_name=None))

    with patch("aristotlelib.cli.command.login.login", login):
        await action.run_action(action.parser.parse_args(["--no-browser"]))

    assert login.await_args.kwargs["open_browser"] is False


async def test_a_rejected_key_is_a_clean_exit_5(capsys):
    action = _action(LoginAction)

    with patch("aristotlelib.cli.command.login.login", AsyncMock(side_effect=LoginError("rejected"))):
        with pytest.raises(SystemExit) as excinfo:
            await action.run_action(action.parser.parse_args([]))

    assert excinfo.value.code == 5
    assert capsys.readouterr().err == "rejected\n"


async def test_a_key_that_could_not_be_checked_exits_3():
    action = _action(LoginAction)
    unreachable = LoginError("Could not check the key", exit_code=3)

    with patch("aristotlelib.cli.command.login.login", AsyncMock(side_effect=unreachable)):
        with pytest.raises(SystemExit) as excinfo:
            await action.run_action(action.parser.parse_args([]))

    assert excinfo.value.code == 3


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, asyncio.CancelledError])
def test_an_interrupted_login_exits_130_and_adds_nothing_to_what_login_said(interrupt, capsys):
    action = _action(LoginAction)
    action.add_common_arguments()

    with patch("aristotlelib.cli.command.login.login", AsyncMock(side_effect=interrupt)):
        code = action.run(action.parser.parse_args([], argparse.Namespace(api_key=None)))

    assert code == 130
    assert capsys.readouterr().err == ""


_CLI_WITHOUT_A_BROWSER = """
import sys
from unittest.mock import patch
import httpx
# A server without device login, which answers every request 404.
transport = httpx.MockTransport(lambda request: httpx.Response(404, json={"detail": "Not Found"}))
real_client = httpx.AsyncClient
with (
    patch("aristotlelib.cli.login_flow._gui_available", return_value=False),
    patch.object(httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs)),
):
    from aristotlelib.cli.controller import main
    sys.exit(main())
"""


_CTRL_C = b"\x03"


def _run_at_a_terminal(config_dir: Path, argv: list[str], keys: list[tuple[bytes, bytes]]) -> tuple[int, str]:
    """Run the CLI under a pseudo-terminal, typing each key once its prompt shows; the exit code and the output.

    Ctrl-C waits for the terminal to hide input, as getpass does while it reads. If the terminal still hides it after
    a quiet pause, Enter follows: a SIGINT that lands just before the child's read() starts is only handled once read()
    returns, and a login that ignored Ctrl-C would read an empty key instead.
    """
    pty = pytest.importorskip("pty")
    termios = pytest.importorskip("termios")
    env = {k: v for k, v in os.environ.items() if k != "ARISTOTLE_API_KEY"}
    env["XDG_CONFIG_HOME"] = str(config_dir)
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execve(sys.executable, [sys.executable, "-c", _CLI_WITHOUT_A_BROWSER, *argv], env)
        finally:
            os._exit(127)

    def hidden() -> bool:
        """Whether the child's terminal has echo off; a pty master reads the slave's settings."""
        try:
            return not termios.tcgetattr(fd)[3] & termios.ECHO
        except termios.error:  # the child has exited
            return False

    output = b""
    deadline = time.monotonic() + 30
    pending = list(keys)
    interrupted = False
    ready = [fd]
    try:
        while True:
            if pending and pending[0][0] in output and (pending[0][1] != _CTRL_C or hidden()):
                interrupted = pending[0][1] == _CTRL_C
                os.write(fd, pending.pop(0)[1])
            elif interrupted and not ready and hidden():
                os.write(fd, b"\n")
                interrupted = False
            wait = deadline - time.monotonic()
            ready, _, _ = select.select([fd], [], [], max(0.0, min(wait, 0.5) if interrupted or pending else wait))
            if not ready:
                assert time.monotonic() < deadline, f"timed out; output so far: {output!r}"
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:  # Linux reports EIO once the child has exited
                chunk = b""
            if not chunk:
                break
            output += chunk
    finally:
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
        os.close(fd)
    return os.waitstatus_to_exitcode(status), output.decode().replace("\r\n", "\n")


@pytest.mark.parametrize("key", [b"\x03", b"\x04"], ids=["Ctrl-C", "Ctrl-D"])
def test_ctrl_c_or_ctrl_d_at_the_key_prompt_of_a_real_terminal_ends_login(key, tmp_path):
    code, output = _run_at_a_terminal(tmp_path, ["login", "--no-browser"], [(b"Paste your API key", key)])

    assert code == 130
    assert output.endswith("Paste your API key (input is hidden): \nCancelled. Nothing was stored.\n")
    assert credentials.credentials_path().exists() is False


def test_ctrl_d_at_the_offered_key_prompt_of_a_real_terminal_does_what_ctrl_c_does(tmp_path):
    outcomes = {
        name: _run_at_a_terminal(tmp_path, ["list"], [(b"Log in now?", b"\n"), (b"Paste your API key", key)])
        for name, key in [("Ctrl-C", b"\x03"), ("Ctrl-D", b"\x04")]
    }

    assert outcomes["Ctrl-D"] == outcomes["Ctrl-C"]
    assert "Traceback" not in outcomes["Ctrl-D"][1]
    assert credentials.credentials_path().exists() is False


async def test_logout_forgets_the_stored_key():
    credentials.write_api_key("stored-key")
    action = _action(LogoutAction)

    await action.run_action(action.parser.parse_args([]))

    assert credentials.read_api_key() is None


async def test_logout_without_a_stored_key_is_not_an_error(caplog):
    caplog.set_level("INFO")
    action = _action(LogoutAction)

    await action.run_action(action.parser.parse_args([]))

    assert "No stored API key" in caplog.text


class _Stub(APIAction):
    """A command that exists only to exercise the base class's key handling."""

    @property
    def command_name(self) -> str:
        return "stub"

    @property
    def description(self) -> str:
        return "stub"

    @classmethod
    def priority(cls) -> int:
        return 999

    def add_action_arguments(self) -> None:
        pass

    async def run_action(self, args: argparse.Namespace) -> None:
        pass


def _stub():
    parser = argparse.ArgumentParser()
    return _Stub(parser.add_subparsers())


def _at_a_terminal(monkeypatch, yes: bool = True):
    monkeypatch.setattr("sys.stdin.isatty", lambda: yes)
    monkeypatch.setattr("sys.stderr.isatty", lambda: yes)


def _answer(monkeypatch, text: str):
    monkeypatch.setattr("sys.stdin.readline", lambda: text)


def test_a_missing_key_offers_to_log_in_on_stderr(monkeypatch, capsys):
    _at_a_terminal(monkeypatch)
    _answer(monkeypatch, "\n")
    login = AsyncMock(return_value=Path("/tmp/creds"))
    monkeypatch.setattr("aristotlelib.cli.api_action.login", login)

    assert _stub().validate() is True
    login.assert_awaited_once()
    captured = capsys.readouterr()
    assert "Log in now?" in captured.err
    assert captured.out == ""


def test_declining_the_offer_leaves_the_command_unauthenticated(monkeypatch, caplog):
    _at_a_terminal(monkeypatch)
    _answer(monkeypatch, "n\n")

    assert _stub().validate() is False
    assert "aristotle login" in caplog.text


def test_one_ctrl_c_at_the_offered_key_prompt_ends_it(monkeypatch, device_server):
    device_server(start=NOT_FOUND)
    _at_a_terminal(monkeypatch)
    _answer(monkeypatch, "\n")
    reached_after: list[str] = []

    def getpass(prompt: str) -> str:
        signal.raise_signal(signal.SIGINT)
        reached_after.append(prompt)
        return "pasted-after-ctrl-c"

    monkeypatch.setattr("aristotlelib.cli.login_flow._gui_available", lambda: False)
    monkeypatch.setattr("aristotlelib.cli.login_flow.getpass.getpass", getpass)

    with patch.object(_Stub, "run_action", AsyncMock()) as run_action:
        _stub().run(argparse.Namespace(api_key=None))

    assert reached_after == []
    assert credentials.read_api_key() is None
    run_action.assert_not_awaited()


def test_end_of_input_at_the_offer_declines(monkeypatch):
    _at_a_terminal(monkeypatch)
    _answer(monkeypatch, "")
    login = AsyncMock()
    monkeypatch.setattr("aristotlelib.cli.api_action.login", login)

    assert _stub().validate() is False
    login.assert_not_awaited()


def test_a_script_is_told_what_to_do_rather_than_prompted(monkeypatch, caplog):
    _at_a_terminal(monkeypatch, yes=False)
    monkeypatch.setattr("sys.stdin.readline", Mock(side_effect=AssertionError("must not prompt")))

    assert _stub().validate() is False
    assert "ARISTOTLE_API_KEY" in caplog.text


def test_a_closed_stdin_is_told_what_to_do_and_exits_5(monkeypatch, caplog):
    monkeypatch.setattr("sys.stdin", None)

    assert _stub().run(argparse.Namespace(api_key=None)) == 5
    assert "Aristotle API key not found." in caplog.text


def test_login_with_stdin_closed_says_no_key_was_entered_and_exits_5_without_device_login(
    monkeypatch, capsys, device_server
):
    device_server(start=NOT_FOUND)
    monkeypatch.setattr("sys.stdin", None)
    monkeypatch.setattr("sys.argv", ["aristotle", "login"])

    with pytest.raises(SystemExit) as exited:
        main()

    assert exited.value.code == 5
    assert capsys.readouterr().err.endswith("No API key was entered.\n")


@pytest.mark.parametrize("argv", [["login", "--api-key", "given-key"], ["--api-key", "given-key", "login"]])
def test_login_checks_and_stores_the_key_given_with_api_key(argv: list[str], monkeypatch):
    monkeypatch.setattr("sys.stdin", None)
    monkeypatch.setattr("sys.argv", ["aristotle", *argv])
    verify = AsyncMock()
    monkeypatch.setattr("aristotlelib.cli.login_flow._verify", verify)

    assert main() == 0
    verify.assert_awaited_once_with("given-key")
    assert credentials.read_api_key() == "given-key"


@pytest.mark.parametrize("blank", ["", " \t"], ids=["empty", "whitespace"])
def test_a_blank_api_key_for_login_exits_5_and_stores_nothing(blank, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["aristotle", "login", "--api-key", blank])
    verify = AsyncMock()
    monkeypatch.setattr("aristotlelib.cli.login_flow._verify", verify)

    with pytest.raises(SystemExit) as exited:
        main()

    assert exited.value.code == 5
    assert capsys.readouterr().err == "The API key given with --api-key is blank. Nothing was stored.\n"
    verify.assert_not_awaited()
    assert credentials.read_api_key() is None


def _login_as_an_agent(monkeypatch, *argv: str) -> None:
    """Set up `aristotle login` with these arguments as an agent runs it: with stdin closed."""
    monkeypatch.setattr("sys.stdin", None)
    monkeypatch.setattr("sys.argv", ["aristotle", "login", *argv])


def test_login_json_prints_the_code_to_approve_and_then_where_the_key_went(monkeypatch, capsys, caplog, device_server):
    caplog.set_level(logging.DEBUG)
    device_server(token_error("authorization_pending"), approved())
    _login_as_an_agent(monkeypatch, "--json")

    assert main() == 0

    out, err = capsys.readouterr()
    pending, end = json.JSONDecoder().raw_decode(out)
    assert pending == {
        "verification_uri": f"{api_request.get_web_url()}/device",
        "user_code": USER_CODE,
        "expires_in": 600,
    }
    assert json.loads(out[end:]) == {"credentials_path": str(credentials.credentials_path()), "key_name": KEY_NAME}
    assert credentials.read_api_key() == APPROVED_KEY
    for secret in (APPROVED_KEY, DEVICE_CODE):
        assert secret not in out + err + caplog.text


def test_the_device_logins_requests_name_the_cli_in_their_user_agent(monkeypatch, device_server):
    requests = device_server(token_error("authorization_pending"), approved())
    _login_as_an_agent(monkeypatch, "--json")

    assert main() == 0

    assert [request.url.path for request in requests] == [
        "/api/v3/cli/device/start",
        "/api/v3/cli/device/token",
        "/api/v3/cli/device/token",
        "/api/v3/project",
    ]
    assert [request.headers["User-Agent"] for request in requests] == [user_agent_of("cli")] * 4


def test_login_json_with_a_given_key_prints_only_where_it_went(monkeypatch, capsys, caplog, device_server):
    caplog.set_level(logging.DEBUG)
    requests = device_server()
    _login_as_an_agent(monkeypatch, "--json", "--api-key", APPROVED_KEY)

    assert main() == 0

    out, err = capsys.readouterr()
    assert json.loads(out) == {"credentials_path": str(credentials.credentials_path()), "key_name": None}
    assert [request.url.path for request in requests] == ["/api/v3/project"]
    assert APPROVED_KEY not in out + err + caplog.text


def test_ctrl_c_while_login_waits_for_approval_exits_130(monkeypatch, capsys, device_server, ctrl_c_while_waiting):
    ctrl_c_while_waiting(device_server(token_error("authorization_pending")))
    _login_as_an_agent(monkeypatch)

    assert main() == 130
    assert capsys.readouterr().err == "\nCancelled. Nothing was stored.\n"
    assert credentials.credentials_path().exists() is False


def test_an_interrupt_from_the_event_loop_while_login_waits_exits_130_cleanly(
    monkeypatch, capsys, caplog, device_server
):
    def pending_then_interrupt(request: httpx.Request) -> httpx.Response:
        # As SIGTERM, or Ctrl-C on Python 3.10, raises it: in the event loop, not in the task.
        asyncio.get_running_loop().call_soon(_raise_keyboard_interrupt)
        return token_error("authorization_pending")

    device_server(pending_then_interrupt)
    _login_as_an_agent(monkeypatch)

    assert main() == 130
    gc.collect()
    assert capsys.readouterr().err == "\nCancelled. Nothing was stored.\n"
    assert "never retrieved" not in caplog.text
    assert credentials.credentials_path().exists() is False


def test_an_interrupt_while_the_offered_login_waits_is_said_once(monkeypatch, capsys, device_server):
    def pending_then_interrupt(request: httpx.Request) -> httpx.Response:
        asyncio.get_running_loop().call_soon(_raise_keyboard_interrupt)
        return token_error("authorization_pending")

    device_server(pending_then_interrupt)
    _at_a_terminal(monkeypatch)
    _answer(monkeypatch, "\n")
    monkeypatch.setattr("aristotlelib.cli.login_flow._gui_available", lambda: False)

    with patch.object(_Stub, "run_action", AsyncMock()) as run_action:
        assert _stub().run(argparse.Namespace(api_key=None)) == 130

    err = capsys.readouterr().err
    assert err.endswith(" left\nCancelled. Nothing was stored.\n")
    assert err.count("Cancelled") == 1
    run_action.assert_not_awaited()


def _raise_keyboard_interrupt() -> None:
    raise KeyboardInterrupt


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (token_error("access_denied"), "The login was denied in the browser. Nothing was stored."),
        (token_error("expired_token"), EXPIRED),
    ],
    ids=["denied", "expired"],
)
def test_a_denied_or_expired_login_exits_5_and_says_so(monkeypatch, capsys, device_server, answer, message):
    device_server(answer)
    _login_as_an_agent(monkeypatch)

    with pytest.raises(SystemExit) as exited:
        main()

    assert exited.value.code == 5
    assert capsys.readouterr().err.endswith(f"{message}\n")


def test_a_login_that_cannot_reach_aristotle_exits_3(monkeypatch, capsys, device_server):
    device_server(start=httpx.ConnectError("unreachable"))
    _login_as_an_agent(monkeypatch)

    with pytest.raises(SystemExit) as exited:
        main()

    assert exited.value.code == 3
    assert capsys.readouterr().err.startswith("Could not start the login: ")


def test_the_offer_to_log_in_logs_in_by_device(monkeypatch, caplog, device_server):
    caplog.set_level(logging.INFO)
    device_server(approved())
    _at_a_terminal(monkeypatch)
    _answer(monkeypatch, "\n")
    monkeypatch.setattr("aristotlelib.cli.login_flow._gui_available", lambda: False)

    assert _stub().validate() is True
    assert credentials.read_api_key() == APPROVED_KEY
    assert f"enter this code:\n  {USER_CODE}" in caplog.text
    assert f'Logged in. Key "{KEY_NAME}" stored in {credentials.credentials_path()}.' in caplog.text


def test_an_existing_key_is_never_second_guessed(monkeypatch):
    monkeypatch.setenv("ARISTOTLE_API_KEY", "env-key")
    monkeypatch.setattr("sys.stdin.readline", Mock(side_effect=AssertionError("must not prompt")))

    assert _stub().validate() is True


def test_an_invalid_key_message_names_where_the_key_came_from(monkeypatch):
    from aristotlelib.cli.api_action import _invalid_key_message

    assert "ARISTOTLE_API_KEY" in _invalid_key_message("environment")
    assert "--api-key" in _invalid_key_message("argument")
    assert "aristotle login" in _invalid_key_message("stored")


def test_api_key_source_follows_precedence(monkeypatch):
    assert api_request.api_key_source() is None
    credentials.write_api_key("stored")
    assert api_request.api_key_source() == "stored"
    monkeypatch.setenv("ARISTOTLE_API_KEY", "env")
    assert api_request.api_key_source() == "environment"
    monkeypatch.setattr(api_request, "API_KEY", "arg")
    assert api_request.api_key_source() == "argument"
