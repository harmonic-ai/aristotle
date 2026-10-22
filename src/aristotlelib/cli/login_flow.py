"""Login: approve a code in the browser, or paste a key where the server can't do that; verify the key, store it."""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import logging
import math
import os
import select
import signal
import socket
import sys
import time
import webbrowser
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeGuard
from urllib.parse import urlsplit

import httpx

from aristotlelib import api_request, credentials
from aristotlelib.api_request import AristotleAPIError, AristotleOutcomeUnknownError, get_web_url
from aristotlelib.cli.version import get_version
from aristotlelib.exit_codes import (
    EXIT_AUTH_ERROR,
    EXIT_INFRASTRUCTURE_ERROR,
    EXIT_OUTCOME_UNKNOWN,
    EXIT_RATE_LIMITED,
)
from aristotlelib.printable import printable
from aristotlelib.results import LoginPending, LoginResult

logger = logging.getLogger(__name__)

DEVICE_START = "/cli/device/start"
DEVICE_TOKEN = "/cli/device/token"
CLIENT_NAME = "aristotle CLI"
PREVIOUS_KEY = "previous_api_key"
"""The start request's field for the key a device login stored here before, which collecting the new key may retire."""
PREVIOUS_KEY_MAX_LENGTH = 128
"""The longest key the field takes; a longer one is left out, as the server refuses it."""
DEFAULT_INTERVAL_SECONDS = 5
"""Seconds between polls when the server names no interval, as RFC 8628 has it."""
SLOW_DOWN_SECONDS = 5
"""What a ``slow_down`` answer adds to the interval, for the next poll and every one after it."""
EXPIRED = "The code expired before the login was approved. Nothing was stored. Get a new code with:\n  aristotle login"


class LoginError(Exception):
    """No valid key was entered; ``exit_code`` is the process exit code to report it with."""

    def __init__(self, message: str, exit_code: int = EXIT_AUTH_ERROR) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class _NoDeviceLogin(Exception):
    """The server answered the device login's start with 404 or 405: it has no device login."""


@dataclass(frozen=True)
class _Device:
    """A started device login: the secret code to ask for the key with, what the user is shown, the interval, and
    whether collecting the key retires one a device login stored here before."""

    device_code: str = field(repr=False)
    pending: LoginPending
    interval: int
    replaces_key: bool


@dataclass
class _Leftover:
    """What an interrupt would leave on the server: a note on the key the login may have made, or None."""

    note: str | None = None


def keys_url() -> str:
    return f"{get_web_url()}/keys"


def _gui_available() -> bool:
    """Whether webbrowser.open reaches a graphical browser; elsewhere it may run a console one."""
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _may_open_browser(open_browser: bool) -> bool:
    """Whether to open a page for the user: unless ``open_browser`` is false, at a terminal with a graphical browser."""
    return open_browser and sys.stdin is not None and sys.stdin.isatty() and _gui_available()


def _open(url: str) -> None:
    """Open ``url`` in a browser; the caller prints it too, so a browser that can't be started is no error."""
    try:
        webbrowser.open(url)
    except webbrowser.Error:
        pass


@contextlib.contextmanager
def _immediate_ctrl_c() -> Generator[None, None, None]:
    """Ctrl-C raises KeyboardInterrupt inside the block, where asyncio.run's handler (3.11+) would defer it."""
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _read_key() -> str:
    """A hidden prompt at a terminal, where Ctrl-D cancels as Ctrl-C does; otherwise stdin's first line, if it is open."""
    if sys.stdin is None:
        return ""
    if sys.stdin.isatty():
        try:
            return getpass.getpass("Paste your API key (input is hidden): ")
        except EOFError:
            raise KeyboardInterrupt from None
    return sys.stdin.readline()


def _stdin_would_wait() -> bool:
    """Whether reading stdin would wait for input: a pipe or socket with nothing in it yet, where select() can tell."""
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
    except (OSError, ValueError):
        # Windows' select() takes sockets only, and a replaced stdin may have no file descriptor.
        return False
    return not ready


def _piped_key() -> str | None:
    """stdin's first line when stdin is a pipe or a file; None at a terminal, with stdin closed, or at the end of stdin.
    Says what it waits for when stdin has nothing in it yet."""
    if sys.stdin is None or sys.stdin.isatty():
        return None
    if _stdin_would_wait():
        logger.info(
            "Waiting for an API key on stdin. To approve this login in a browser instead, end stdin (Ctrl-D), or give "
            "it none: aristotle login < /dev/null"
        )
    with _immediate_ctrl_c():
        return sys.stdin.readline() or None


def _pasted_key(open_browser: bool) -> str:
    """Point the user at the API keys page, opened at a terminal with a browser, and read the key they paste."""
    url = keys_url()
    with _immediate_ctrl_c():
        if _may_open_browser(open_browser):
            _open(url)
        logger.info(f"Create an API key at {url}")
        return _read_key()


def _failed(action: str, error: AristotleAPIError) -> LoginError:
    """The LoginError for a request to ``action`` that failed: exit 6 when rate limited, else 3. A refusal without
    Aristotle's error format, as from a proxy, keeps the message every command gives it."""
    exit_code = EXIT_RATE_LIMITED if error.status_code == 429 else EXIT_INFRASTRUCTURE_ERROR
    if isinstance(error, api_request._IntermediaryError):
        return LoginError(str(error), exit_code=exit_code)
    return LoginError(f"Could not {action}: {str(error).rstrip('.')}. Nothing was stored.", exit_code=exit_code)


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """The response's body as a JSON object; empty when it isn't one."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _text(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip())


def _shown(value: object) -> TypeGuard[str]:
    """Whether ``value`` is text fit to show the user: not blank, and printable, so on one line."""
    return _text(value) and value.isprintable()


def _whole_seconds(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _on_web_host(link: str) -> bool:
    """Whether ``link`` has ARISTOTLE_WEB_URL's scheme and host, on any port: a web app may run beside its API on another.
    One a browser could read another way, with a backslash, credentials, a space or an unprintable character, hasn't."""
    if "\\" in link or " " in link or not link.isprintable():
        return False
    web = urlsplit(get_web_url())
    try:
        page = urlsplit(link)
    except ValueError:
        return False
    return "@" not in page.netloc and (page.scheme, page.hostname) == (web.scheme, web.hostname)


async def _start(client: api_request.AristotleRequestClient) -> _Device:
    """Start a device login, sending the key a device login stored here before, if one did. Raises _NoDeviceLogin
    when the server has no device login, and LoginError when it fails."""
    body = {
        "client_name": CLIENT_NAME,
        "client_version": get_version(),
        "hostname": socket.gethostname() or "unknown",
        "platform": sys.platform,
    }
    previous = credentials.read_device_api_key()
    if previous is not None and len(previous) > PREVIOUS_KEY_MAX_LENGTH:
        previous = None
    if previous is not None:
        body[PREVIOUS_KEY] = previous
    try:
        response = await client.post(
            DEVICE_START,
            json=body,
            idempotent=True,
            authenticated=False,
            redact=None if previous is None else _without(previous, "<the key stored here>"),
        )
    except AristotleAPIError as e:
        if e.status_code in (404, 405):
            raise _NoDeviceLogin from e
        raise _failed("start the login", e) from e
    return _device(response, replaces_key=previous is not None)


def _without(secret: str, name: str) -> Callable[[str], str]:
    """A redaction for a message a server may have quoted ``secret`` into, as a validation error quotes a request: it
    shows ``name`` in the secret's place."""
    return lambda text: text.replace(secret, name)


def _device(response: httpx.Response, *, replaces_key: bool) -> _Device:
    """The device login a start response describes; LoginError, which doesn't quote the response, if it can't be used."""
    body = _json_object(response)
    device_code, user_code, link = body.get("device_code"), body.get("user_code"), body.get("verification_uri")
    expires_in, interval = body.get("expires_in"), body.get("interval")
    if not (_text(device_code) and _shown(user_code) and _text(link) and _whole_seconds(expires_in)):
        raise LoginError(
            "Aristotle's answer to the login request couldn't be read. Nothing was stored.",
            exit_code=EXIT_INFRASTRUCTURE_ERROR,
        )
    if not _on_web_host(link):
        raise LoginError(
            "Aristotle sent an approval page on another site, so the login stopped. Nothing was stored.",
            exit_code=EXIT_INFRASTRUCTURE_ERROR,
        )
    pending = LoginPending(verification_uri=link, user_code=user_code, expires_in=expires_in)
    return _Device(
        device_code, pending, interval if _whole_seconds(interval) else DEFAULT_INTERVAL_SECONDS, replaces_key
    )


class _ProgressLine:
    """One line on stderr, which show() rewrites in place and clear() blanks."""

    def __init__(self) -> None:
        self._width = 0

    def show(self, text: str) -> None:
        sys.stderr.write(f"\r{text.ljust(self._width)}")
        sys.stderr.flush()
        self._width = len(text)

    def clear(self) -> None:
        if self._width:
            sys.stderr.write(f"\r{' ' * self._width}\r")
            sys.stderr.flush()
            self._width = 0


def _clock(seconds: float) -> str:
    """``seconds``, rounded up and at least 0, as minutes and seconds, such as 9:05."""
    minutes, rest = divmod(max(0, math.ceil(seconds)), 60)
    return f"{minutes}:{rest:02d}"


def _minutes(seconds: int) -> str:
    """``seconds`` rounded up to whole minutes, such as "1 minute" or "10 minutes"."""
    minutes = max(1, math.ceil(seconds / 60))
    return f"{minutes} minute{'' if minutes == 1 else 's'}"


async def _wait(seconds: float, deadline: float, progress: _ProgressLine | None) -> None:
    """Sleep ``seconds``, showing on ``progress`` once a second the time left until ``deadline``."""
    end = time.monotonic() + seconds
    while (now := time.monotonic()) < end:
        if progress is not None:
            progress.show(f"Waiting for approval... {_clock(deadline - now)} left")
        await asyncio.sleep(end - now if progress is None else min(1.0, end - now))


def _refused(error: AristotleAPIError, code: str | None) -> LoginError:
    """The LoginError for a poll whose answer ends the login: denied, expired, refused otherwise, or failed."""
    if code == "access_denied":
        return LoginError("The login was denied in the browser. Nothing was stored.")
    if code == "expired_token":
        return LoginError(EXPIRED)
    if code is not None:
        return LoginError(
            f"Aristotle refused the login ({code}). Nothing was stored. Try again with:\n  aristotle login"
        )
    return _failed("finish the login", error)


def _may_have_made_key(replaces_key: bool) -> str:
    if not replaces_key:
        return (
            f"If you approved the login, Aristotle may have made a key that never arrived: revoke it at {keys_url()}."
        )
    return (
        "If you approved the login, Aristotle may have made a key that never arrived, and retired the one stored here: "
        f"revoke the new one at {keys_url()}, then run `aristotle login` again."
    )


def _made_key(key_name: str | None, replaces_key: bool) -> str:
    made = "a key" if key_name is None else f'the key "{key_name}"'
    if not replaces_key:
        return f"Aristotle made {made} for this login: revoke it at {keys_url()}."
    return (
        f"Aristotle made {made} for this login, and may have retired the one stored here: revoke the new one at "
        f"{keys_url()}, then run `aristotle login` again."
    )


def _report_cancelled(note: str | None) -> None:
    """Say on stderr, after the ^C a terminal echoes, that the login was cancelled, with ``note`` on a key it left."""
    message = "Cancelled. Nothing was stored." if note is None else f"Cancelled. Nothing was stored. {note}"
    sys.stderr.write(f"\n{printable(message)}\n")


def _lost(error: AristotleOutcomeUnknownError) -> str:
    """How an answer to a request for the key was lost: its 5xx status, the transport's failure, such as ReadTimeout, or
    else an answer that wasn't Aristotle's."""
    if error.status_code is not None:
        return f"status {error.status_code}"
    if isinstance(error.__cause__, httpx.RequestError):
        return type(error.__cause__).__name__
    return "something else answered"


def _maybe_lost(error: AristotleOutcomeUnknownError, replaces_key: bool) -> LoginError:
    """The LoginError, exit 7, for a login that ends after ``error`` lost an answer that may have held the key."""
    return LoginError(
        f"Could not finish the login: an answer from Aristotle was lost ({_lost(error)}). "
        f"{_may_have_made_key(replaces_key)} Nothing was stored.",
        exit_code=EXIT_OUTCOME_UNKNOWN,
    )


def _approved_key(response: httpx.Response) -> tuple[str, str | None]:
    """The key and its name from the answer to an approved login; LoginError, which doesn't quote it, without a key."""
    body = _json_object(response)
    api_key, key_name = body.get("api_key"), body.get("key_name")
    if not _text(api_key):
        raise LoginError(
            "The login was approved, but Aristotle's answer held no key. Nothing was stored.",
            exit_code=EXIT_INFRASTRUCTURE_ERROR,
        )
    return api_key, key_name if _shown(key_name) else None


async def _poll(
    client: api_request.AristotleRequestClient, device: _Device, progress: _ProgressLine | None, leftover: _Leftover
) -> tuple[str, str | None]:
    """Ask for the key each interval, longer by SLOW_DOWN_SECONDS after each ``slow_down``, until the login is approved,
    denied or expires; the key and its name. ``progress`` shows the wait, and is cleared before each request.
    ``leftover`` notes a key that may have been made, from each request until an answer says Aristotle is waiting.

    An answer lost after Aristotle may have handed the key out is asked again once: a pending answer then says it held
    none, and anything but that or a denial ends the login with exit 7.
    """
    interval = device.interval
    without_code = _without(device.device_code, "<the device code>")
    deadline = time.monotonic() + device.pending.expires_in
    lost: AristotleOutcomeUnknownError | None = None
    while True:
        await _wait(interval, deadline, progress)
        if progress is not None:
            progress.clear()
        leftover.note = _may_have_made_key(device.replaces_key)
        try:
            # Not repeated by the client: Aristotle hands the key out once, and answers expired_token after.
            response = await client.post(
                DEVICE_TOKEN, json={"device_code": device.device_code}, authenticated=False, redact=without_code
            )
        except AristotleOutcomeUnknownError as e:
            if lost is not None:
                raise _maybe_lost(e, device.replaces_key) from e
            lost = e
            logger.warning(f"An answer from Aristotle was lost ({_lost(e)}); asking for the key again in {interval} s.")
            continue
        except AristotleAPIError as e:
            code = e.error_code if e.status_code == 400 else None
            if code not in ("authorization_pending", "slow_down"):
                if lost is not None and code != "access_denied":
                    raise _maybe_lost(lost, device.replaces_key) from e
                raise _refused(e, code) from e
            lost = None
            leftover.note = None
            if code == "slow_down":
                interval += SLOW_DOWN_SECONDS
            if time.monotonic() >= deadline:
                raise LoginError(EXPIRED) from None
            continue
        api_key, key_name = _approved_key(response)
        leftover.note = _made_key(key_name, device.replaces_key)
        return api_key, key_name


async def _device_login(
    open_browser: bool, on_pending: Callable[[LoginPending], None] | None, leftover: _Leftover
) -> tuple[str, str | None]:
    """The key the user approves in the browser, and its name, with ``leftover`` as _poll keeps it. Raises
    _NoDeviceLogin when the server has no device login."""
    async with api_request.AristotleRequestClient() as client:
        device = await _start(client)
        pending = device.pending
        logger.info(
            f"To log in, open {pending.verification_uri} and enter this code:\n  {pending.user_code}\n"
            f"The code expires in {_minutes(pending.expires_in)}."
        )
        if on_pending is not None:
            on_pending(pending)
        if _may_open_browser(open_browser):
            _open(pending.verification_uri)
        isatty = getattr(sys.stderr, "isatty", None)
        progress = _ProgressLine() if on_pending is None and isatty is not None and isatty() else None
        return await _poll(client, device, progress, leftover)


async def _verify(api_key: str) -> None:
    """Make one authenticated request with the key. Raises LoginError if it is not accepted."""
    previous = api_request.API_KEY
    # set_api_key outranks ARISTOTLE_API_KEY, so the request is made with the key being checked.
    api_request.set_api_key(api_key)
    try:
        async with api_request.AristotleRequestClient() as client:
            await client.get("/project", params={"limit": 1})
    except AristotleAPIError as e:
        if isinstance(e, api_request._IntermediaryError):
            exit_code = EXIT_AUTH_ERROR if e.status_code == 403 else EXIT_INFRASTRUCTURE_ERROR
            raise LoginError(str(e), exit_code=exit_code) from e
        if e.status_code == 401:
            raise LoginError(f"Aristotle did not accept that key. Create one at {keys_url()} and try again.") from e
        if e.status_code == 403:
            raise LoginError(api_request._ACCOUNT_ACCESS_MESSAGE) from e
        raise _failed("check the key", e) from e
    finally:
        api_request.API_KEY = previous


async def login(
    *,
    open_browser: bool = True,
    api_key: str | None = None,
    on_pending: Callable[[LoginPending], None] | None = None,
) -> LoginResult:
    """Verify ``api_key``, or else a key piped to stdin, approved in the browser or, where the server can't do that,
    pasted; store it, and say where.

    ``on_pending`` receives the code to approve while the login waits for it, in place of a progress line on stderr.

    Raises:
        LoginError: no key was entered or approved, api_request.api_key_problem() refused it, Aristotle did not accept
            it, or it could not be stored.
        KeyboardInterrupt: Ctrl-C before the key was read, or Ctrl-D at the prompt; said on stderr, with where to revoke
            a key the login may have made, before it is raised.
        asyncio.CancelledError: Ctrl-C under asyncio.run while the login waits for approval; said on stderr as above.
    """
    leftover = _Leftover()
    try:
        key_name = None
        device_login = False
        given = api_key is not None
        where = "given with --api-key"
        if api_key is None:
            api_key, where = _piped_key(), "from stdin"
        if api_key is None:
            try:
                api_key, key_name = await _device_login(open_browser, on_pending, leftover)
                device_login, where = True, "that Aristotle sent"
            except _NoDeviceLogin:
                api_key, where = _pasted_key(open_browser), "you pasted"
        api_key = api_key.strip()
        if not api_key and not given:
            raise LoginError("No API key was entered.")

        try:
            if (problem := api_request.api_key_problem(api_key)) is not None:
                exit_code = EXIT_INFRASTRUCTURE_ERROR if device_login else EXIT_AUTH_ERROR
                raise LoginError(f"The API key {where} {problem}. Nothing was stored.", exit_code)
            await _verify(api_key)
            path = _store(api_key, key_name, device_login)
        except LoginError as e:
            if leftover.note is None:
                raise
            raise LoginError(f"{e} {leftover.note}", e.exit_code) from e
        leftover.note = None
    except (KeyboardInterrupt, asyncio.CancelledError):
        _report_cancelled(leftover.note)
        raise
    stored = "Key" if key_name is None else f'Key "{key_name}"'
    logger.info(f"Logged in. {stored} stored in {path}.")
    if os.environ.get("ARISTOTLE_API_KEY"):
        logger.warning(
            "ARISTOTLE_API_KEY is set in this environment and takes precedence over the stored key. "
            "Unset it, and remove it from your shell profile, to use the stored key."
        )
    return LoginResult(credentials_path=str(path), key_name=key_name)


def _store(api_key: str, key_name: str | None, device_login: bool) -> Path:
    """Store the key under its name, noting whether a device login made it, and return the file; LoginError, exit 3,
    when the credentials file can't take it."""
    try:
        return credentials.write_api_key(api_key, name=key_name, device_login=device_login)
    except OSError as e:
        raise LoginError(_not_stored(e), exit_code=EXIT_INFRASTRUCTURE_ERROR) from e


def _not_stored(error: OSError) -> str:
    """The message for a key that the credentials file could not take, naming the file when its path is known."""
    try:
        where = f" in {credentials.credentials_path()}"
    except OSError:
        where = ""
    return f"Could not store the key{where}: {str(error).rstrip('.')}. Nothing was stored."
