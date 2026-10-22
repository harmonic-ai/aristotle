"""Tests for the login flow: the device login, and the paste-based login a server without it falls back to."""

import asyncio
import io
import json
import logging
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.request import getproxies_environment

import httpx
import pytest

from aristotlelib import api_request, credentials
from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli import login_flow
from aristotlelib.results import LoginPending
from tests.cli.conftest import (
    APPROVED_KEY,
    DEVICE_CODE,
    KEY_NAME,
    USER_CODE,
    approved,
    device_started,
    token_error,
)
from tests.conftest import REFUSED_KEYS, shown_key_runs

WEB_URL = "https://aristotle.example"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("ARISTOTLE_WEB_URL", WEB_URL)
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    monkeypatch.setattr(api_request, "API_KEY", None)


@pytest.fixture
def server():
    """The API client of a server without device login, answering its start with 404, and the verification request;
    `seen` records the key it was sent with."""
    seen: list[str] = []
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    async def get(path, params=None):
        seen.append(api_request.get_api_key())
        return MagicMock(spec=httpx.Response)

    client.get = AsyncMock(side_effect=get)
    client.post = AsyncMock(side_effect=AristotleAPIError("API request failed with status 404: Not Found", 404))
    with patch.object(login_flow.api_request, "AristotleRequestClient", return_value=client):
        yield client, seen


def _stdin(text: str, tty: bool = False):
    stream = io.StringIO(text)
    stream.isatty = lambda: tty  # type: ignore[method-assign]
    return patch.object(login_flow.sys, "stdin", stream)


async def test_a_piped_key_is_verified_and_stored(server):
    client, seen = server
    with _stdin("  piped-key\n"):
        result = await login_flow.login()

    assert result.credentials_path == str(credentials.credentials_path())
    assert credentials.read_api_key() == "piped-key"
    assert seen == ["piped-key"]
    client.get.assert_awaited_once_with("/project", params={"limit": 1})


async def test_a_given_key_is_verified_and_stored_without_asking(server, caplog):
    caplog.set_level(logging.INFO)
    client, seen = server
    with (
        _stdin("", tty=True),
        patch.object(login_flow.getpass, "getpass", side_effect=AssertionError("must not prompt")),
        patch.object(login_flow, "_gui_available", return_value=True),
        patch.object(login_flow.webbrowser, "open") as opened,
    ):
        result = await login_flow.login(api_key=" given-key\n")

    assert result.credentials_path == str(credentials.credentials_path())
    assert credentials.read_api_key() == "given-key"
    assert seen == ["given-key"]
    client.post.assert_not_awaited()
    opened.assert_not_called()
    assert "Create an API key" not in caplog.text


async def test_a_given_key_that_is_rejected_is_not_stored(server):
    client, _ = server
    client.get.side_effect = AristotleAPIError("unauthorized", status_code=401)
    with pytest.raises(login_flow.LoginError, match="did not accept") as info:
        await login_flow.login(api_key="bad-key")

    assert info.value.exit_code == 5
    assert credentials.read_api_key() is None


LOGIN_REFUSED_KEYS = [key for key in REFUSED_KEYS if key.id not in ("line break", "only whitespace")]
"""The keys login refuses as REFUSED_KEYS says, wherever they come from; a line break ends a key read from stdin or the
prompt, and a blank one read there is no key entered."""


@pytest.mark.parametrize(("key", "problem"), LOGIN_REFUSED_KEYS)
@pytest.mark.parametrize(
    ("given", "where"), [("--api-key", "given with --api-key"), ("stdin", "from stdin"), ("prompt", "you pasted")]
)
async def test_a_key_that_stripping_cannot_fix_is_refused_by_where_it_came_from_and_not_stored(
    given, where, key, problem, server, caplog
):
    _, seen = server
    with (
        _stdin(f"{key}\n" if given == "stdin" else "", tty=given == "prompt"),
        patch.object(login_flow.getpass, "getpass", return_value=f" {key}\r\n"),
        patch.object(login_flow, "_gui_available", return_value=False),
        pytest.raises(login_flow.LoginError) as raised,
    ):
        await login_flow.login(api_key=f" {key}\n" if given == "--api-key" else None)

    assert (str(raised.value), raised.value.exit_code) == (f"The API key {where} {problem}. Nothing was stored.", 5)
    assert credentials.read_api_key() is None
    assert seen == []
    assert shown_key_runs(str(raised.value) + caplog.text, key) == []


@pytest.mark.parametrize("key", ["", " \t\r\n", "fake-Q7zP\n3wK9vX2m"], ids=["empty", "only whitespace", "line break"])
async def test_a_blank_key_or_one_holding_a_line_break_given_with_api_key_is_refused(key, server):
    _, seen = server
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login(api_key=key)

    problem = "contains a control character" if key.strip() else "is blank"
    assert (str(raised.value), raised.value.exit_code) == (
        f"The API key given with --api-key {problem}. Nothing was stored.",
        5,
    )
    assert credentials.read_api_key() is None
    assert seen == []


async def test_configured_origin_login_preserves_default_credentials(monkeypatch):
    monkeypatch.delenv("ARISTOTLE_WEB_URL")
    default_path = credentials.write_api_key("default-key")
    monkeypatch.setenv("ARISTOTLE_WEB_URL", f"{WEB_URL}/")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    with (
        _stdin("configured-key\n"),
        patch.object(
            api_request.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)
        ),
    ):
        configured_path = Path((await login_flow.login(open_browser=False)).credentials_path)

    assert configured_path != default_path
    assert configured_path.exists()
    assert [str(request.url) for request in requests] == [f"{WEB_URL}/api/v3/project?limit=1"]
    assert requests[0].headers["X-API-Key"] == "configured-key"
    assert api_request.API_KEY is None
    assert credentials.read_api_key() == "configured-key"

    monkeypatch.delenv("ARISTOTLE_WEB_URL")
    assert credentials.read_api_key() == "default-key"
    assert default_path.exists()


async def test_at_a_terminal_the_key_is_read_hidden(server):
    with _stdin("", tty=True), patch.object(login_flow.getpass, "getpass", return_value="typed-key") as prompt:
        with patch.object(login_flow, "_gui_available", return_value=False):
            await login_flow.login()

    prompt.assert_called_once()
    assert credentials.read_api_key() == "typed-key"


def test_one_ctrl_c_interrupts_the_hidden_prompt_under_asyncio_run(server):
    client, _ = server
    reached_after: list[str] = []

    def getpass(prompt: str) -> str:
        signal.raise_signal(signal.SIGINT)
        reached_after.append(prompt)
        return "pasted-after-ctrl-c"

    with _stdin("", tty=True), patch.object(login_flow.getpass, "getpass", getpass):
        with patch.object(login_flow, "_gui_available", return_value=False), pytest.raises(KeyboardInterrupt):
            asyncio.run(login_flow.login())

    assert reached_after == []
    client.get.assert_not_awaited()
    assert credentials.read_api_key() is None


def test_the_ctrl_c_handler_in_place_before_the_prompt_is_restored(server):
    def handler(signum, frame):
        pass

    previous = signal.signal(signal.SIGINT, handler)
    try:
        with _stdin("key\n"):
            asyncio.run(login_flow.login())
        assert signal.getsignal(signal.SIGINT) is handler
    finally:
        signal.signal(signal.SIGINT, previous)


async def test_the_pasted_key_is_verified_even_when_the_environment_has_another(server, monkeypatch):
    _, seen = server
    monkeypatch.setenv("ARISTOTLE_API_KEY", "env-key")
    with _stdin("pasted-key\n"):
        await login_flow.login()

    assert seen == ["pasted-key"]


async def test_a_rejected_key_is_not_stored(server):
    client, _ = server
    client.get.side_effect = AristotleAPIError("unauthorized", status_code=401)
    with _stdin("bad-key\n"), pytest.raises(login_flow.LoginError, match="did not accept") as info:
        await login_flow.login()

    assert info.value.exit_code == 5
    assert "revoke" not in str(info.value)
    assert credentials.read_api_key() is None
    assert api_request.API_KEY is None


async def test_an_unreachable_server_stores_nothing(server):
    client, _ = server
    client.get.side_effect = AristotleAPIError("Request failed: connection refused")
    with _stdin("key\n"), pytest.raises(login_flow.LoginError, match="Nothing was stored") as info:
        await login_flow.login()

    assert info.value.exit_code == 3
    assert credentials.read_api_key() is None


async def test_a_rate_limited_key_check_stores_nothing_and_exits_6(server):
    client, _ = server
    client.get.side_effect = AristotleAPIError("Too many requests to Aristotle at once.", status_code=429)
    with _stdin("key\n"), pytest.raises(login_flow.LoginError, match="Nothing was stored") as info:
        await login_flow.login()

    assert info.value.exit_code == 6
    assert credentials.read_api_key() is None


async def test_an_empty_key_is_an_error(server):
    client, _ = server
    with _stdin("   \n"), pytest.raises(login_flow.LoginError, match="No API key") as info:
        await login_flow.login()

    assert info.value.exit_code == 5
    client.get.assert_not_awaited()


async def test_the_keys_page_is_named_and_opened_only_with_a_graphical_browser(server, caplog):
    caplog.set_level(logging.INFO)
    with _stdin("", tty=True), patch.object(login_flow.getpass, "getpass", return_value="k"):
        with (
            patch.object(login_flow, "_gui_available", return_value=True),
            patch.object(login_flow.webbrowser, "open") as opened,
        ):
            await login_flow.login()
        opened.assert_called_once_with(f"{WEB_URL}/keys")

        with (
            patch.object(login_flow, "_gui_available", return_value=False),
            patch.object(login_flow.webbrowser, "open") as opened,
        ):
            await login_flow.login()
        opened.assert_not_called()

    assert f"{WEB_URL}/keys" in caplog.text


async def test_no_browser_is_opened_when_the_key_is_piped(server):
    with (
        _stdin("k\n"),
        patch.object(login_flow, "_gui_available", return_value=True),
        patch.object(login_flow.webbrowser, "open") as opened,
    ):
        await login_flow.login()

    opened.assert_not_called()


async def test_an_overriding_environment_variable_is_called_out(server, monkeypatch, caplog):
    monkeypatch.setenv("ARISTOTLE_API_KEY", "env-key")
    with _stdin("pasted-key\n"):
        await login_flow.login()

    assert "ARISTOTLE_API_KEY is set" in caplog.text


async def test_a_forbidden_key_is_reported_as_an_access_problem_and_not_stored(server):
    client, _ = server
    client.get.side_effect = AristotleAPIError("forbidden", status_code=403)
    with _stdin("key\n"), pytest.raises(login_flow.LoginError, match="this account doesn't have access") as info:
        await login_flow.login()

    assert info.value.exit_code == 5
    assert str(info.value) == (
        "Error: this account doesn't have access (403)\n\n"
        "Hint: it may be awaiting approval or no longer in the key's organization. Contact aristotle@harmonic.fun."
    )

    assert credentials.read_api_key() is None


async def test_a_closed_stdin_is_no_key_entered(server):
    client, _ = server

    with patch.object(login_flow.sys, "stdin", None), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert (str(raised.value), raised.value.exit_code) == ("No API key was entered.", 5)
    client.get.assert_not_awaited()


async def test_a_key_that_cannot_be_stored_names_the_file_and_exits_3(server, tmp_path, monkeypatch):
    client, _ = server
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blocker))
    monkeypatch.setenv("APPDATA", str(blocker))

    with _stdin("test-key\n"), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login(open_browser=False)

    client.get.assert_awaited_once()
    assert raised.value.exit_code == 3
    message = str(raised.value)
    assert credentials.credentials_path().parent == blocker / "aristotle"
    assert message.startswith(f"Could not store the key in {credentials.credentials_path()}: ")
    assert message.endswith(". Nothing was stored.")
    assert "\n" not in message


async def test_a_key_with_no_home_directory_to_go_to_exits_3(server, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.delenv("APPDATA")
    monkeypatch.setattr(credentials.Path, "home", MagicMock(side_effect=RuntimeError("no home")))

    with _stdin("test-key\n"), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login(open_browser=False)

    assert (str(raised.value), raised.value.exit_code) == (
        "Could not store the key: Could not determine the home directory for the credentials file. Nothing was stored.",
        3,
    )


def _polled_at(requests: list[httpx.Request]) -> list[float]:
    return [r.extensions["polled_at"] for r in requests if r.url.path.endswith(login_flow.DEVICE_TOKEN)]


def _paths(requests: list[httpx.Request]) -> list[str]:
    return [r.url.path.removeprefix("/api/v3") for r in requests]


def _terminal_stderr():
    stream = io.StringIO()
    stream.isatty = lambda: True  # type: ignore[method-assign]
    return stream


async def test_an_approved_device_login_stores_the_key_under_its_name(device_server, caplog):
    caplog.set_level(logging.DEBUG)
    requests = device_server(token_error("authorization_pending"), approved())
    with _stdin(""):
        result = await login_flow.login()

    assert (result.credentials_path, result.key_name) == (str(credentials.credentials_path()), KEY_NAME)
    assert credentials.read_api_key() == APPROVED_KEY
    assert json.loads(credentials.credentials_path().read_text())["name"] == KEY_NAME
    assert credentials.read_device_api_key() == APPROVED_KEY
    assert api_request.API_KEY is None
    assert _paths(requests) == ["/cli/device/start", "/cli/device/token", "/cli/device/token", "/project"]
    start, *polls, check = requests
    assert json.loads(start.content) == {
        "client_name": "aristotle CLI",
        "client_version": login_flow.get_version(),
        "hostname": login_flow.socket.gethostname(),
        "platform": sys.platform,
    }
    assert [json.loads(poll.content) for poll in polls] == [{"device_code": DEVICE_CODE}] * 2
    assert all("X-API-Key" not in request.headers for request in [start, *polls])
    assert check.headers["X-API-Key"] == APPROVED_KEY
    assert f"To log in, open {WEB_URL}/device and enter this code:\n  {USER_CODE}\n" in caplog.text
    assert "The code expires in 10 minutes." in caplog.text
    assert f'Logged in. Key "{KEY_NAME}" stored in {credentials.credentials_path()}.' in caplog.text
    assert APPROVED_KEY not in caplog.text
    assert DEVICE_CODE not in caplog.text


async def test_polls_wait_the_interval_and_5_seconds_more_after_each_slow_down(device_server):
    pending, slow_down = token_error("authorization_pending"), token_error("slow_down")
    requests = device_server(pending, slow_down, pending, slow_down, approved())
    with _stdin(""):
        await login_flow.login()

    assert _polled_at(requests) == [5, 10, 20, 30, 45]


@pytest.mark.parametrize(("interval", "polls"), [(2, [2, 4]), (None, [5, 10]), (0, [5, 10]), ("2", [5, 10])])
async def test_polls_wait_the_interval_start_gives_and_else_5_seconds(device_server, interval, polls):
    requests = device_server(token_error("authorization_pending"), approved(), start=device_started(interval=interval))
    with _stdin(""):
        await login_flow.login()

    assert _polled_at(requests) == polls


async def test_a_denied_login_stores_nothing_and_exits_5(device_server):
    requests = device_server(token_error("authorization_pending"), token_error("access_denied"))
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert (str(raised.value), raised.value.exit_code) == (
        "The login was denied in the browser. Nothing was stored.",
        5,
    )
    assert credentials.read_api_key() is None
    assert "/project" not in _paths(requests)


async def test_an_expired_code_says_to_log_in_again_and_exits_5(device_server):
    device_server(token_error("authorization_pending"), token_error("expired_token"))
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert (str(raised.value), raised.value.exit_code) == (login_flow.EXPIRED, 5)
    assert str(raised.value).endswith("Get a new code with:\n  aristotle login")
    assert credentials.read_api_key() is None


async def test_a_code_still_pending_when_it_expires_ends_the_login(device_server):
    requests = device_server(token_error("authorization_pending"), start=device_started(expires_in=12))
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert (str(raised.value), raised.value.exit_code) == (login_flow.EXPIRED, 5)
    assert _polled_at(requests) == [5, 10, 15]


async def test_a_refusal_rfc_8628_doesnt_name_ends_the_login_and_exits_5(device_server):
    device_server(token_error("invalid_grant"))
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert (str(raised.value), raised.value.exit_code) == (
        "Aristotle refused the login (invalid_grant). Nothing was stored. Try again with:\n  aristotle login",
        5,
    )


@pytest.mark.parametrize(
    ("failure", "exit_code"),
    [
        (httpx.ConnectError("unreachable"), 3),
        (httpx.Response(400, json={"detail": "Bad request"}), 3),
        (httpx.Response(429), 6),
    ],
    ids=["unreachable", "400 without an error code", "rate limited"],
)
async def test_a_poll_that_fails_stores_nothing(device_server, failure, exit_code):
    device_server(failure)
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    message = str(raised.value)
    assert raised.value.exit_code == exit_code
    assert message.startswith("Could not finish the login: ")
    assert message.endswith(". Nothing was stored.")
    assert ".." not in message
    assert credentials.read_api_key() is None


LOST = [httpx.ReadTimeout("timed out"), httpx.Response(502), httpx.Response(200, html="<title>Sign in</title>")]
LOST_IDS = ["reply lost", "server error", "page"]
LOST_AS = ["ReadTimeout", "status 502", "something else answered"]
"""How each of LOST is reported."""


def _maybe_lost(what: str) -> str:
    return (
        f"Could not finish the login: an answer from Aristotle was lost ({what}). If you approved the login, Aristotle "
        f"may have made a key that never arrived: revoke it at {WEB_URL}/keys. Nothing was stored."
    )


@pytest.mark.parametrize(("failure", "what"), zip(LOST, LOST_AS), ids=LOST_IDS)
async def test_after_a_lost_answer_a_pending_one_says_it_held_no_key(device_server, failure, what, caplog):
    pending = token_error("authorization_pending")
    requests = device_server(pending, failure, pending, failure, approved())
    with _stdin(""):
        result = await login_flow.login()

    assert result.key_name == KEY_NAME
    assert _polled_at(requests) == [5, 10, 15, 20, 25]
    assert caplog.text.count(f"An answer from Aristotle was lost ({what}); asking for the key again in 5 s.") == 2


@pytest.mark.parametrize("failure", LOST, ids=LOST_IDS)
@pytest.mark.parametrize(
    "then",
    [token_error("expired_token"), "lost again", httpx.ConnectError("unreachable"), token_error("invalid_grant")],
    ids=["then expired", "then lost again", "then unreachable", "then refused"],
)
async def test_a_lost_answer_that_may_have_held_the_key_exits_7_and_says_where_to_revoke_it(
    device_server, failure, then
):
    requests = device_server(failure, failure if then == "lost again" else then)
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert raised.value.exit_code == 7
    assert str(raised.value) == _maybe_lost(LOST_AS[LOST.index(failure)])
    assert _polled_at(requests)[:2] == [5, 10]
    assert credentials.read_api_key() is None


async def test_a_lost_answer_then_a_denial_is_a_denial(device_server):
    device_server(httpx.Response(502), token_error("access_denied"))
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert (str(raised.value), raised.value.exit_code) == (
        "The login was denied in the browser. Nothing was stored.",
        5,
    )


async def test_a_machine_without_a_hostname_starts_as_unknown(device_server):
    requests = device_server(approved())
    with _stdin(""), patch.object(login_flow.socket, "gethostname", return_value=""):
        await login_flow.login()

    assert json.loads(requests[0].content)["hostname"] == "unknown"


@pytest.mark.parametrize(
    ("failure", "exit_code"),
    [(httpx.ConnectError("unreachable"), 3), (httpx.Response(500), 3), (httpx.Response(429), 6)],
    ids=["unreachable", "server error", "rate limited"],
)
async def test_a_start_that_fails_stores_nothing(device_server, failure, exit_code):
    requests = device_server(start=failure)
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert raised.value.exit_code == exit_code
    assert str(raised.value).startswith("Could not start the login: ")
    assert str(raised.value).endswith(". Nothing was stored.")
    assert set(_paths(requests)) == {"/cli/device/start"}


@pytest.fixture
def no_proxy(monkeypatch):
    """No proxy from the environment or the system, so a refusal's message doesn't depend on the machine."""
    for name in (*api_request.PROXY_VARIABLES, "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(api_request, "getproxies", getproxies_environment)


@pytest.mark.parametrize("refused", ["start", "token"])
async def test_a_refusal_without_aristotles_error_format_keeps_the_message_every_command_gives(
    device_server, no_proxy, refused
):
    blocked = httpx.Response(403, html="<title>Blocked</title>")
    if refused == "start":
        device_server(start=blocked)
    else:
        device_server(blocked)
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert str(raised.value) == (
        "Error: request refused (403 Forbidden)\n\n"
        "Hint: this may come from a proxy or firewall on your network. Run with --verbose to see the response headers."
    )
    assert raised.value.exit_code == 3
    assert credentials.read_api_key() is None


@pytest.mark.parametrize("status", [404, 405])
async def test_a_server_without_device_login_takes_a_pasted_key(device_server, status, caplog):
    caplog.set_level(logging.INFO)
    requests = device_server(start=httpx.Response(status, json={"detail": "Not Found"}))
    with (
        _stdin("", tty=True),
        patch.object(login_flow.getpass, "getpass", return_value=APPROVED_KEY) as prompt,
        patch.object(login_flow, "_gui_available", return_value=False),
    ):
        result = await login_flow.login()

    prompt.assert_called_once()
    assert result.key_name is None
    assert credentials.read_api_key() == APPROVED_KEY
    assert _paths(requests) == ["/cli/device/start", "/project"]
    assert f"Create an API key at {WEB_URL}/keys" in caplog.text
    assert "enter this code" not in caplog.text


async def test_with_nothing_on_stdin_a_server_without_device_login_ends_with_no_key_entered(device_server, caplog):
    caplog.set_level(logging.INFO)
    device_server(start=httpx.Response(404, json={"detail": "Not Found"}))
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert (str(raised.value), raised.value.exit_code) == ("No API key was entered.", 5)
    assert f"Create an API key at {WEB_URL}/keys" in caplog.text


@pytest.mark.parametrize(
    "start",
    [
        device_started(device_code=None),
        device_started(user_code=" "),
        device_started(user_code=f"{USER_CODE}\nThe page moved: open https://other.example"),
        device_started(verification_uri=None),
        device_started(expires_in=None),
        device_started(expires_in=0),
        device_started(expires_in="600"),
        device_started(expires_in=True),
        httpx.Response(200, json=[DEVICE_CODE]),
    ],
)
async def test_a_start_answer_that_cant_be_used_stops_the_login_without_quoting_it(device_server, start, caplog):
    requests = device_server(start=start)
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert (str(raised.value), raised.value.exit_code) == (
        "Aristotle's answer to the login request couldn't be read. Nothing was stored.",
        3,
    )
    assert _paths(requests) == ["/cli/device/start"]
    assert DEVICE_CODE not in caplog.text


@pytest.mark.usefixtures("frozen_api_clock")
async def test_a_start_answered_by_a_page_that_isnt_aristotles_is_retried_then_stops_the_login(
    device_server, monkeypatch
):
    monkeypatch.setattr(api_request, "getproxies", dict)
    requests = device_server(start=httpx.Response(200, html="<title>Sign in</title>"))
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert (str(raised.value), raised.value.exit_code) == (
        "Could not start the login: POST /cli/device/start after 5 attempts over 0 s: the answer from "
        f"{WEB_URL} was 200 OK with text/html, not Aristotle's: a captive portal, a proxy or a wrong ARISTOTLE_WEB_URL "
        "may have answered instead. Nothing was stored.",
        3,
    )
    assert _paths(requests) == [login_flow.DEVICE_START] * api_request.RETRY_ATTEMPTS


@pytest.mark.parametrize(
    "link",
    [
        "https://aristotle.example.net/device",
        "https://aristotle.example@other.example/device",
        "https://other.example\\@aristotle.example/device",
        "https://user@aristotle.example/device",
        "https://aristotle.example/de vice",
        "https://aristotle.example/device\nOpen https://other.example",
        "http://aristotle.example/device",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://[::1/device",
    ],
)
async def test_an_approval_page_on_another_site_stops_the_login(device_server, link):
    requests = device_server(start=device_started(verification_uri=link))
    with (
        _stdin("", tty=True),
        patch.object(login_flow, "_gui_available", return_value=True),
        patch.object(login_flow.webbrowser, "open") as opened,
        pytest.raises(login_flow.LoginError) as raised,
    ):
        await login_flow.login()

    assert (str(raised.value), raised.value.exit_code) == (
        "Aristotle sent an approval page on another site, so the login stopped. Nothing was stored.",
        3,
    )
    opened.assert_not_called()
    assert _paths(requests) == ["/cli/device/start"]


async def test_an_approval_page_on_another_port_of_the_web_host_is_used(device_server, caplog):
    caplog.set_level(logging.INFO)
    device_server(approved(), start=device_started(verification_uri="https://aristotle.example:3000/device"))
    with _stdin(""):
        await login_flow.login()

    assert "To log in, open https://aristotle.example:3000/device and enter this code:" in caplog.text


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, json={"key_name": KEY_NAME}),
        httpx.Response(200, json={"api_key": " ", "key_name": KEY_NAME}),
        httpx.Response(200, json=[APPROVED_KEY]),
    ],
)
async def test_an_approval_without_a_key_stores_nothing_and_quotes_nothing(device_server, answer, caplog):
    caplog.set_level(logging.DEBUG)
    device_server(answer)
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert (str(raised.value), raised.value.exit_code) == (
        "The login was approved, but Aristotle's answer held no key. Nothing was stored.",
        3,
    )
    assert credentials.read_api_key() is None
    assert APPROVED_KEY not in caplog.text


async def test_a_key_sent_as_text_is_a_lost_answer_and_never_shown(device_server, caplog):
    caplog.set_level(logging.DEBUG)
    device_server(httpx.Response(200, text=APPROVED_KEY))
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert (str(raised.value), raised.value.exit_code) == (_maybe_lost("something else answered"), 7)
    assert credentials.read_api_key() is None
    assert APPROVED_KEY not in caplog.text


@pytest.mark.parametrize("key_name", [None, f"{KEY_NAME}\nLogged in as someone else"], ids=["none", "two lines"])
async def test_a_key_approved_without_a_name_to_show_is_stored_without_one(device_server, key_name, caplog):
    caplog.set_level(logging.INFO)
    device_server(approved(key_name=key_name))
    with _stdin(""):
        result = await login_flow.login()

    assert result.key_name is None
    assert json.loads(credentials.credentials_path().read_text())["name"] is None
    assert f"Logged in. Key stored in {credentials.credentials_path()}." in caplog.text


@pytest.mark.parametrize(
    ("check", "message", "exit_code"),
    [
        (httpx.Response(401), "Aristotle did not accept that key. Create one at", 5),
        (httpx.ConnectError("unreachable"), "Could not check the key: ", 3),
        (
            httpx.Response(200, html="<title>Sign in</title>"),
            "Could not check the key: GET /project after 5 attempts",
            3,
        ),
    ],
    ids=["rejected", "unreachable", "page"],
)
async def test_an_approved_key_that_fails_its_check_is_not_stored_and_is_named_to_revoke(
    device_server, check, message, exit_code
):
    device_server(approved(), check=check)
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert str(raised.value).startswith(message)
    assert str(raised.value).endswith(
        f' Aristotle made the key "{KEY_NAME}" for this login: revoke it at {WEB_URL}/keys.'
    )
    assert raised.value.exit_code == exit_code
    assert credentials.read_api_key() is None


async def test_an_approved_key_that_cannot_be_stored_is_named_to_revoke(device_server, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blocker))
    monkeypatch.setenv("APPDATA", str(blocker))
    device_server(approved(key_name=None))
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert str(raised.value).startswith(f"Could not store the key in {credentials.credentials_path()}: ")
    assert str(raised.value).endswith(
        f"Nothing was stored. Aristotle made a key for this login: revoke it at {WEB_URL}/keys."
    )
    assert raised.value.exit_code == 3


async def test_a_piped_key_is_checked_without_a_device_login(device_server):
    requests = device_server(approved())
    with _stdin(f"{APPROVED_KEY}\n"):
        result = await login_flow.login()

    assert _paths(requests) == ["/project"]
    assert result.key_name is None
    assert credentials.read_api_key() == APPROVED_KEY
    assert credentials.read_device_api_key() is None


async def test_a_key_the_device_login_hands_out_that_cannot_be_sent_is_not_stored_and_its_revocation_asked(
    device_server,
):
    requests = device_server(approved(api_key="fake-Q7zP\u00a03wK9vX2m"))
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert str(raised.value) == (
        "The API key that Aristotle sent contains a non-ASCII character. Nothing was stored. "
        f'Aristotle made the key "{KEY_NAME}" for this login: revoke it at {WEB_URL}/keys.'
    )
    assert raised.value.exit_code == 3
    assert credentials.read_api_key() is None
    assert _paths(requests) == ["/cli/device/start", "/cli/device/token"]


async def test_a_blank_line_on_stdin_is_no_key_entered_and_starts_no_device_login(device_server):
    requests = device_server(approved())
    with _stdin("\n"), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert (str(raised.value), raised.value.exit_code) == ("No API key was entered.", 5)
    assert requests == []


async def test_with_stdin_closed_the_device_login_runs(device_server):
    device_server(approved())
    with patch.object(login_flow.sys, "stdin", None):
        result = await login_flow.login()

    assert result.key_name == KEY_NAME
    assert credentials.read_api_key() == APPROVED_KEY


async def test_at_a_terminal_with_a_browser_the_approval_page_is_opened(device_server):
    device_server(approved())
    with (
        _stdin("", tty=True),
        patch.object(login_flow, "_gui_available", return_value=True),
        patch.object(login_flow.webbrowser, "open") as opened,
    ):
        await login_flow.login()

    opened.assert_called_once_with(f"{WEB_URL}/device")


@pytest.mark.parametrize(("tty", "open_browser"), [(False, True), (True, False)], ids=["piped", "no-browser"])
async def test_the_approval_page_is_opened_only_at_a_terminal_and_when_asked(device_server, tty, open_browser):
    device_server(approved())
    with (
        _stdin("", tty=tty),
        patch.object(login_flow, "_gui_available", return_value=True),
        patch.object(login_flow.webbrowser, "open") as opened,
    ):
        await login_flow.login(open_browser=open_browser)

    opened.assert_not_called()


async def test_at_a_terminal_a_line_on_stderr_counts_down_and_is_cleared(device_server):
    device_server(token_error("authorization_pending"), approved())
    stderr = _terminal_stderr()
    with _stdin(""), patch.object(login_flow.sys, "stderr", stderr):
        await login_flow.login()

    written = stderr.getvalue()
    shown = [line for line in written.split("\r") if line.strip()]
    assert shown[0] == "Waiting for approval... 10:00 left"
    assert shown[-1] == "Waiting for approval... 9:51 left"
    assert len(shown) == 10
    assert written.endswith(f"\r{' ' * len(shown[-1])}\r")


async def test_on_pending_gets_what_is_safe_to_show_in_place_of_the_progress_line(device_server):
    device_server(approved())
    pending: list[LoginPending] = []
    stderr = _terminal_stderr()
    with _stdin(""), patch.object(login_flow.sys, "stderr", stderr):
        await login_flow.login(on_pending=pending.append)

    assert pending == [LoginPending(verification_uri=f"{WEB_URL}/device", user_code=USER_CODE, expires_in=600)]
    assert stderr.getvalue() == ""


def test_ctrl_c_while_waiting_for_approval_ends_the_login_under_asyncio_run(
    device_server, ctrl_c_while_waiting, capsys
):
    requests = device_server(token_error("authorization_pending"))
    ctrl_c_while_waiting(requests)
    with _stdin(""), pytest.raises(KeyboardInterrupt):
        asyncio.run(login_flow.login())

    assert _polled_at(requests) == [5]
    assert credentials.read_api_key() is None
    assert capsys.readouterr().err.endswith("\nCancelled. Nothing was stored.\n")


def _interrupted(interrupt: type[BaseException]) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        raise interrupt()

    return handle


MAY_HAVE_MADE_A_KEY = (
    "\nCancelled. Nothing was stored. If you approved the login, Aristotle may have made a key that never arrived: "
    f"revoke it at {WEB_URL}/keys.\n"
)


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, asyncio.CancelledError])
def test_an_interrupt_while_asking_for_the_key_says_a_key_may_have_been_made(device_server, capsys, interrupt):
    device_server(token_error("authorization_pending"), _interrupted(interrupt))
    with _stdin(""), pytest.raises(interrupt):
        asyncio.run(login_flow.login())

    assert capsys.readouterr().err == MAY_HAVE_MADE_A_KEY
    assert credentials.read_api_key() is None


def test_an_interrupt_while_waiting_after_a_lost_answer_says_a_key_may_have_been_made(
    device_server, poll_clock, capsys, monkeypatch
):
    requests = device_server(httpx.Response(502), token_error("authorization_pending"))
    real_sleep = login_flow.asyncio.sleep

    async def sleep(seconds: float) -> None:
        if _polled_at(requests):  # the lost answer is in: interrupt the wait for the next poll
            raise KeyboardInterrupt
        await real_sleep(seconds)

    monkeypatch.setattr(login_flow.asyncio, "sleep", sleep)
    with _stdin(""), pytest.raises(KeyboardInterrupt):
        asyncio.run(login_flow.login())

    assert _paths(requests) == ["/cli/device/start", "/cli/device/token"]
    assert capsys.readouterr().err == MAY_HAVE_MADE_A_KEY


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, asyncio.CancelledError])
def test_an_interrupt_while_checking_the_key_names_the_key_to_revoke(device_server, capsys, interrupt):
    device_server(approved(), check=_interrupted(interrupt))
    with _stdin(""), pytest.raises(interrupt):
        asyncio.run(login_flow.login())

    assert capsys.readouterr().err == (
        f'\nCancelled. Nothing was stored. Aristotle made the key "{KEY_NAME}" for this login: revoke it at '
        f"{WEB_URL}/keys.\n"
    )
    assert credentials.read_api_key() is None


def test_stdin_would_wait_only_for_a_pipe_with_nothing_in_it_yet(tmp_path):
    read, write = os.pipe()
    with open(read, encoding="utf-8") as pipe, open(write, "w", encoding="utf-8") as writer:
        with patch.object(login_flow.sys, "stdin", pipe):
            waits_when_empty = login_flow._stdin_would_wait()
            writer.write("key\n")
            writer.flush()
            waits_with_a_line = login_flow._stdin_would_wait()
    regular = tmp_path / "key.txt"
    regular.write_text("key\n")
    with open(regular, encoding="utf-8") as file, patch.object(login_flow.sys, "stdin", file):
        waits_for_a_file = login_flow._stdin_would_wait()

    if sys.platform == "win32":
        # select() takes sockets only there, so stdin is never said to wait.
        assert (waits_when_empty, waits_with_a_line, waits_for_a_file) == (False, False, False)
    else:
        assert (waits_when_empty, waits_with_a_line, waits_for_a_file) == (True, False, False)


async def test_a_wait_for_a_key_on_stdin_says_how_to_approve_in_a_browser_instead(device_server, caplog):
    caplog.set_level(logging.INFO)
    device_server(approved())
    with _stdin(f"{APPROVED_KEY}\n"), patch.object(login_flow, "_stdin_would_wait", return_value=True):
        await login_flow.login()

    assert (
        "Waiting for an API key on stdin. To approve this login in a browser instead, end stdin (Ctrl-D), or give it "
        "none: aristotle login < /dev/null"
    ) in caplog.text


async def test_a_key_already_on_stdin_is_read_without_a_word(device_server, caplog):
    caplog.set_level(logging.INFO)
    device_server(approved())
    with _stdin(f"{APPROVED_KEY}\n"), patch.object(login_flow, "_stdin_would_wait", return_value=False):
        await login_flow.login()

    assert "Waiting for an API key" not in caplog.text


KEY_FROM_BEFORE = "key-a-device-login-stored-before"


async def test_a_device_login_sends_the_key_a_device_login_stored_before(device_server, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    credentials.write_api_key(KEY_FROM_BEFORE, name=KEY_NAME, device_login=True)
    requests = device_server(token_error("authorization_pending"), approved())
    with _stdin(""):
        await login_flow.login()

    assert json.loads(requests[0].content)[login_flow.PREVIOUS_KEY] == KEY_FROM_BEFORE
    assert all(login_flow.PREVIOUS_KEY not in json.loads(r.content or b"{}") for r in requests[1:])
    assert credentials.read_device_api_key() == APPROVED_KEY
    out, err = capsys.readouterr()
    assert KEY_FROM_BEFORE not in out + err + caplog.text


@pytest.mark.parametrize(
    ("stored", "environment"),
    [
        (None, None),
        ("a-key-given-piped-or-pasted", None),
        (None, "a-key-in-the-environment"),
    ],
    ids=["nothing stored", "a key given, piped or pasted", "a key in ARISTOTLE_API_KEY"],
)
async def test_no_key_but_one_a_device_login_stored_is_sent_as_the_previous_one(
    device_server, monkeypatch, stored, environment
):
    if stored is not None:
        credentials.write_api_key(stored)
    if environment is not None:
        monkeypatch.setenv("ARISTOTLE_API_KEY", environment)
    requests = device_server(approved())
    with _stdin(""):
        await login_flow.login()

    assert login_flow.PREVIOUS_KEY not in json.loads(requests[0].content)


async def test_a_stored_key_longer_than_the_field_takes_isnt_sent(device_server):
    credentials.write_api_key("k" * (login_flow.PREVIOUS_KEY_MAX_LENGTH + 1), device_login=True)
    requests = device_server(approved())
    with _stdin(""):
        await login_flow.login()

    assert login_flow.PREVIOUS_KEY not in json.loads(requests[0].content)


async def test_the_key_in_the_environment_isnt_sent_in_place_of_the_stored_one(device_server, monkeypatch):
    credentials.write_api_key(KEY_FROM_BEFORE, device_login=True)
    monkeypatch.setenv("ARISTOTLE_API_KEY", "a-key-in-the-environment")
    requests = device_server(approved())
    with _stdin(""):
        await login_flow.login()

    assert json.loads(requests[0].content)[login_flow.PREVIOUS_KEY] == KEY_FROM_BEFORE


RETIRED = f"and retired the one stored here: revoke the new one at {WEB_URL}/keys, then run `aristotle login` again."


async def test_a_lost_answer_that_may_have_retired_the_stored_key_says_to_log_in_again(device_server):
    credentials.write_api_key(KEY_FROM_BEFORE, device_login=True)
    device_server(httpx.Response(502), token_error("expired_token"))
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert raised.value.exit_code == 7
    assert str(raised.value) == (
        "Could not finish the login: an answer from Aristotle was lost (status 502). If you approved the login, "
        f"Aristotle may have made a key that never arrived, {RETIRED} Nothing was stored."
    )
    assert KEY_FROM_BEFORE not in str(raised.value)


def test_an_interrupt_while_asking_for_a_key_that_retires_the_stored_one_says_to_log_in_again(device_server, capsys):
    credentials.write_api_key(KEY_FROM_BEFORE, device_login=True)
    device_server(_interrupted(KeyboardInterrupt))
    with _stdin(""), pytest.raises(KeyboardInterrupt):
        asyncio.run(login_flow.login())

    assert capsys.readouterr().err == (
        "\nCancelled. Nothing was stored. If you approved the login, Aristotle may have made a key that never "
        f"arrived, {RETIRED}\n"
    )


async def test_a_new_key_that_fails_its_check_after_retiring_the_stored_one_says_to_log_in_again(device_server):
    credentials.write_api_key(KEY_FROM_BEFORE, device_login=True)
    device_server(approved(), check=httpx.Response(401))
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert str(raised.value).endswith(
        f' Aristotle made the key "{KEY_NAME}" for this login, and may have retired the one stored here: revoke the new '
        f"one at {WEB_URL}/keys, then run `aristotle login` again."
    )
    assert credentials.read_api_key() == KEY_FROM_BEFORE


async def test_a_start_refusal_that_quotes_the_request_doesnt_show_the_stored_key(device_server, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    credentials.write_api_key(KEY_FROM_BEFORE, device_login=True)

    def refuse_quoting_the_body(request: httpx.Request) -> httpx.Response:
        # As a validation error does: the input it rejected, the whole body when a required field is missing.
        error = {"type": "missing", "loc": ["body", "new_field"], "input": json.loads(request.content)}
        return httpx.Response(422, json={"detail": [error]})

    device_server(start=refuse_quoting_the_body)
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert raised.value.exit_code == 3
    assert str(raised.value).startswith("Could not start the login: API request failed with status 422: ")
    assert "<the key stored here>" in str(raised.value)
    assert KEY_FROM_BEFORE not in str(raised.value.__cause__)
    assert raised.value.__cause__.__cause__ is None
    out, err = capsys.readouterr()
    assert KEY_FROM_BEFORE not in str(raised.value) + out + err + caplog.text


def _echoing(status: int) -> Callable[[httpx.Request], httpx.Response]:
    """An answer whose JSON ``detail`` quotes the request's body back."""
    return lambda request: httpx.Response(status, json={"detail": f"Rejected {request.content.decode()}"})


async def test_start_retries_that_quote_the_request_never_show_the_stored_key(device_server, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    credentials.write_api_key(KEY_FROM_BEFORE, device_login=True)
    answers = iter([_echoing(503), *[_echoing(429)] * (api_request.RETRY_ATTEMPTS - 1)])
    requests = device_server(start=lambda request: next(answers)(request))
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert raised.value.exit_code == 6
    assert _paths(requests) == ["/cli/device/start"] * api_request.RETRY_ATTEMPTS
    assert caplog.text.count("<the key stored here>") == api_request.RETRY_ATTEMPTS - 1
    out, err = capsys.readouterr()
    assert KEY_FROM_BEFORE not in str(raised.value) + str(raised.value.__cause__) + out + err + caplog.text


async def test_a_start_that_fails_quoting_the_request_on_every_try_never_shows_the_stored_key(
    device_server, caplog, capsys
):
    caplog.set_level(logging.DEBUG)
    credentials.write_api_key(KEY_FROM_BEFORE, device_login=True)
    device_server(start=_echoing(503))
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert raised.value.exit_code == 3
    assert "<the key stored here>" in str(raised.value)
    out, err = capsys.readouterr()
    assert KEY_FROM_BEFORE not in str(raised.value) + str(raised.value.__cause__) + out + err + caplog.text


async def test_a_poll_refusal_that_quotes_the_request_doesnt_show_the_device_code(device_server, caplog, capsys):
    caplog.set_level(logging.DEBUG)

    def refuse_quoting_the_body(request: httpx.Request) -> httpx.Response:
        error = {"type": "string_too_long", "loc": ["body", "device_code"], "input": json.loads(request.content)}
        return httpx.Response(422, json={"detail": [error]})

    device_server(refuse_quoting_the_body)
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert raised.value.exit_code == 3
    assert str(raised.value).startswith("Could not finish the login: API request failed with status 422: ")
    assert "<the device code>" in str(raised.value)
    assert raised.value.__cause__.__cause__ is None
    out, err = capsys.readouterr()
    assert DEVICE_CODE not in str(raised.value) + str(raised.value.__cause__) + out + err + caplog.text


async def test_poll_retries_that_quote_the_request_never_show_the_device_code(device_server, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    requests = device_server(_echoing(429))
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert raised.value.exit_code == 6
    assert _paths(requests) == ["/cli/device/start", *["/cli/device/token"] * api_request.RETRY_ATTEMPTS]
    assert caplog.text.count("<the device code>") == api_request.RETRY_ATTEMPTS - 1
    out, err = capsys.readouterr()
    assert DEVICE_CODE not in str(raised.value) + str(raised.value.__cause__) + out + err + caplog.text


def _blocked_quoting_the_body(request: httpx.Request) -> httpx.Response:
    """A 403 page, as from a proxy, that quotes the request's body in its title and in a header."""
    body = request.content.decode()
    return httpx.Response(403, html=f"<title>Blocked {body}</title>", headers={"X-Blocked-Body": body})


@pytest.mark.parametrize("refused", ["start", "token"])
async def test_a_refusals_verbose_report_shows_neither_the_stored_key_nor_the_device_code(
    device_server, no_proxy, caplog, capsys, refused
):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger=api_request.http_logger.name)
    credentials.write_api_key(KEY_FROM_BEFORE, device_login=True)
    if refused == "start":
        device_server(start=_blocked_quoting_the_body)
    else:
        device_server(_blocked_quoting_the_body)
    with _stdin(""), pytest.raises(login_flow.LoginError) as raised:
        await login_flow.login()

    assert raised.value.exit_code == 3
    assert "Response excerpt: Blocked {" in caplog.text
    assert ("<the key stored here>" if refused == "start" else "<the device code>") in caplog.text
    out, err = capsys.readouterr()
    for secret in (KEY_FROM_BEFORE, DEVICE_CODE):
        assert secret not in str(raised.value) + out + err + caplog.text
