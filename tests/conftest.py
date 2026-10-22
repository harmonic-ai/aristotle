"""Fixtures shared by every test."""

import asyncio.proactor_events
import builtins
import contextlib
import io
import ipaddress
import json
import logging
import os
import socket
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from aristotlelib import api_request, credentials
from aristotlelib.cli.version import get_version

UNREACHABLE_HOST = "192.0.2.1"
"""An address in TEST-NET-1, reserved for documentation, which reaches no computer."""

NEVER_READ = {
    "network": rf"\\{UNREACHABLE_HOST}\share\x.v",
    "network, slashes": f"//{UNREACHABLE_HOST}/share/x.v",
    "network, mixed slashes": rf"/\{UNREACHABLE_HOST}\share/x.v",
    "extended-length": rf"\\?\UNC\{UNREACHABLE_HOST}\share\x.v",
    "device": rf"\\.\UNC\{UNREACHABLE_HOST}\share\x.v",
    "object manager": rf"\??\UNC\{UNREACHABLE_HOST}\share\x.v",
    "drive-relative": f"C:{UNREACHABLE_HOST}.v",
}
"""A path in each form filelist.never_read() refuses, by form, each holding UNREACHABLE_HOST, for no_lookup_of()."""

WILDCARD_FOLDERS = [
    pytest.param("a?b", "axb", id="?", marks=pytest.mark.skipif(os.name == "nt", reason="Windows names can't hold ?")),
    pytest.param(
        "a*b", "axyzb", id="*", marks=pytest.mark.skipif(os.name == "nt", reason="Windows names can't hold *")
    ),
    pytest.param("...b", "b", id="..."),
]
"""A folder's name holding each of slang's wildcards, and a sibling's name that the first, as a pattern, matches."""

NEEDS_INT_DIGIT_LIMIT = pytest.mark.skipif(
    not hasattr(sys, "get_int_max_str_digits"), reason="before 3.10.7, int() converts any number of digits"
)
"""Skips a test of int()'s limit on the digits it converts to or from a string, which 3.10.7 added."""

FAKE_KEY = "fake-Q7zP3wK9vX2m"
"""An API key no account has; no output may show it, or any 6-character run of it."""

PADDED_KEYS = [
    pytest.param(f"{padding}{FAKE_KEY}" if leading else f"{FAKE_KEY}{padding}", id=f"{side} {padding!r}")
    for leading, side in ((True, "leading"), (False, "trailing"))
    for padding in (" ", "\t", "\r", "\n", "\r\n", "\u00a0")
]
"""FAKE_KEY with whitespace before or after it, as str.strip() counts it, which a request sends without."""

REFUSED_KEYS = [
    pytest.param("fake-Q7zP 3wK9vX2m", "contains a space", id="space"),
    pytest.param("fake-Q7zP\t3wK9vX2m", "contains a control character", id="tab"),
    pytest.param("fake-Q7zP\n3wK9vX2m", "contains a control character", id="line break"),
    pytest.param("fake-Q7zP\x1b3wK9vX2m", "contains a control character", id="escape"),
    pytest.param("fake-Q7zP\u200b3wK9vX2m", "contains a non-ASCII character", id="zero-width space"),
    pytest.param("fake-Q7zP3wK9vX2m\u200b", "contains a non-ASCII character", id="zero-width space after"),
    pytest.param("fake-Q7zP\u00a03wK9vX2m", "contains a non-ASCII character", id="no-break space"),
    pytest.param("fake-Q7zP\u00e93wK9vX2m", "contains a non-ASCII character", id="e acute"),
    pytest.param(" \t\r\n", "is blank", id="only whitespace"),
]
"""A key that stripping the whitespace around it can't make sendable, and what its refusal says is wrong with it."""


def shown_key_runs(text: str, key: str) -> list[str]:
    """The 6-character runs of ``key`` that ``text`` shows."""
    return [key[i : i + 6] for i in range(len(key) - 5) if key[i : i + 6] in text]


def _on_this_machine(host: object) -> bool:
    """Whether ``host`` can only name this machine: no host, localhost, or a loopback or unspecified address."""
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host.lower() in ("", "localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


@pytest.fixture(autouse=True)
def no_repository_above_the_tests(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """Keep git, and the CLI, from finding a repository above the tests' temporary folders, such as one a machine keeps
    at /tmp, which would put every one of those folders inside it."""
    ceilings = [str(tmp_path_factory.getbasetemp()), tempfile.gettempdir()]
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", os.pathsep.join(ceilings))


@pytest.fixture(autouse=True)
def no_outside_network(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    """Fail a test that resolves a name or opens a connection beyond this machine, and refuse each such attempt.

    The refusal is an OSError, which the code under test may catch and retry, so the test fails at teardown. Loopback,
    socketpair() and AF_UNIX stay open: the loopback servers and asyncio's own wake-up sockets need them. A test of the
    guard takes this fixture, which counts the attempts, and clears it.
    """
    attempts: Counter[str] = Counter()
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sock_connect = asyncio.proactor_events.BaseProactorEventLoop.sock_connect

    def getaddrinfo(host, port, *args, **kwargs):
        if not _on_this_machine(host):
            name = host.decode("ascii", "replace") if isinstance(host, bytes) else host
            attempts[f"resolve {name}"] += 1
            raise socket.gaierror(socket.EAI_NONAME, f"The tests may not resolve {name}")
        return real_getaddrinfo(host, port, *args, **kwargs)

    def refuse(sock: socket.socket, address) -> None:
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _on_this_machine(address[0]):
            attempts[f"connect to {address[0]}:{address[1]}"] += 1
            raise ConnectionRefusedError(f"The tests may not connect to {address[0]}")

    def connect(self, address):
        refuse(self, address)
        return real_connect(self, address)

    def connect_ex(self, address):
        refuse(self, address)
        return real_connect_ex(self, address)

    # Windows' proactor event loop connects without calling socket.connect.
    async def sock_connect(self, sock, address):
        refuse(sock, address)
        return await real_sock_connect(self, sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(asyncio.proactor_events.BaseProactorEventLoop, "sock_connect", sock_connect)
    yield attempts
    if attempts:
        tried = ", ".join(f"{attempt} ({count}x)" for attempt, count in sorted(attempts.items()))
        pytest.fail(f"{request.node.nodeid} reached beyond this machine: {tried}", pytrace=False)


@pytest.fixture
def no_lookup_of(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Call it with a text: from then on, resolving, examining, listing or opening a path that holds the text fails the
    test, with an exception that code catching OSError, or Exception, can't stop."""

    def guard(text: str) -> None:
        def guarded(real: Callable[..., Any], name: str) -> Callable[..., Any]:
            def call(*args: Any, **kwargs: Any) -> Any:
                for value in (*args[:1], *kwargs.values()):
                    try:
                        held = text in os.fsdecode(value)
                    except TypeError:
                        held = False
                    if held:
                        pytest.fail(
                            f"{name} was called on {value!r}, which should have been refused first", pytrace=False
                        )
                return real(*args, **kwargs)

            return call

        for module, name in (
            (os.path, "realpath"),
            (os.path, "isfile"),
            (os.path, "isdir"),
            (os.path, "exists"),
            (os.path, "lexists"),
            (os, "stat"),
            (os, "lstat"),
            (os, "listdir"),
            (os, "scandir"),
            (io, "open"),
            (builtins, "open"),
        ):
            monkeypatch.setattr(module, name, guarded(getattr(module, name), f"{module.__name__}.{name}"))

    return guard


@pytest.fixture(autouse=True)
def not_under_pwsh(monkeypatch: pytest.MonkeyPatch):
    """Clear PSModulePath, which pwsh sets for what it runs, and Windows and GitHub's Ubuntu runners for every
    program: outside Windows it adds PowerShell's form to a printed command. A test of that sets it."""
    monkeypatch.delenv("PSModulePath", raising=False)


@pytest.fixture(autouse=True)
def not_on_github(monkeypatch: pytest.MonkeyPatch):
    """Clear GITHUB_ACTIONS, which GitHub's runners set for every program: on GitHub a usage error is also an annotation.
    A test of that sets it."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture(autouse=True)
def restore_api_key(monkeypatch: pytest.MonkeyPatch):
    """Undo set_api_key() after each test: a key left set sends a later test's unmocked requests out."""
    monkeypatch.setattr(api_request, "API_KEY", api_request.API_KEY)


@pytest.fixture(autouse=True)
def restore_surface(monkeypatch: pytest.MonkeyPatch):
    """Undo set_surface(), which running a command calls: a later test's requests would name that command."""
    monkeypatch.setattr(api_request, "_surface", api_request._surface)


@pytest.fixture(autouse=True)
def no_retry_backoff(monkeypatch: pytest.MonkeyPatch):
    """Retry at once; a test of the delays sets RETRY_BACKOFF_SECONDS itself."""
    monkeypatch.setattr(api_request, "RETRY_BACKOFF_SECONDS", 0)


@pytest.fixture(autouse=True)
def no_request_pacing(monkeypatch: pytest.MonkeyPatch):
    """Start every request at once; a test of the pacing sets REQUEST_INTERVAL_SECONDS itself."""
    monkeypatch.setattr(api_request, "REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(api_request, "_next_request_at", 0.0)


@pytest.fixture(autouse=True)
def restore_log_formatters():
    """Undo escape_logged_text(), which running a command installs on the root logger's handlers, pytest's included."""
    saved = [(handler, handler.formatter) for handler in logging.root.handlers]
    yield
    for handler, formatter in saved:
        handler.setFormatter(formatter)


@pytest.fixture
def api_server(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[httpx.Request]]:
    """Serve the SDK's requests through httpx's MockTransport, with the key ``test-key``.

    Call it with one outcome per request: a response, an exception for the transport to raise, or a function of the
    request that returns or raises one. It returns the list of requests received.
    """
    monkeypatch.setattr(api_request, "API_KEY", "test-key")
    requests: list[httpx.Request] = []

    def serve(*outcomes) -> list[httpx.Request]:
        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            outcome = outcomes[len(requests) - 1]
            if callable(outcome):
                outcome = outcome(request)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        transport = httpx.MockTransport(handle)
        real_client = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
        return requests

    return serve


@pytest.fixture
def frozen_api_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """api_request's clock, stopped: a message saying how long a request's attempts took says 0 s, however slow the
    runner. It stops at 1000 s, not 0, so a figure that is the clock's reading rather than the time since the first
    attempt fails the test. The autouse fixtures leave no pacing or backoff to wait out."""
    monkeypatch.setattr(api_request, "time", Mock(monotonic=lambda: 1000.0))


def give_key(source: str, key: str, monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> list[str]:
    """Make ``key`` the only API key, from ``source``: "--api-key", "argument" for set_api_key(), "environment", or
    "stored", in a credentials file under ``config_dir`` written by hand. Returns the arguments that give it, if any."""
    monkeypatch.setattr(api_request, "API_KEY", None)
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
    monkeypatch.setenv("APPDATA", str(config_dir))
    if source == "--api-key":
        return ["--api-key", key]
    if source == "argument":
        monkeypatch.setattr(api_request, "API_KEY", key)
    elif source == "environment":
        monkeypatch.setenv("ARISTOTLE_API_KEY", key)
    else:
        path = credentials.credentials_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"api_key": key}) + "\n", encoding="utf-8")
    return []


def user_agent_of(surface: str) -> str:
    """The User-Agent of a request from ``surface``, spelled out apart from api_request.user_agent()."""
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    return f"aristotlelib/{get_version()} ({surface}; python/{python}; {sys.platform})"


def fast_api_clock(monkeypatch: pytest.MonkeyPatch) -> tuple[list[float], AsyncMock]:
    """api_request's clock and its asyncio.sleep, mocked so that each sleep advances the clock and returns at once:
    minutes of reconnect backoff pass in no time."""
    now = [0.0]
    sleep = AsyncMock(side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(api_request, "time", Mock(monotonic=lambda: now[0]))
    monkeypatch.setattr(api_request.asyncio, "sleep", sleep)
    return now, sleep


def fstat_on_another_device(fstat: Callable[[int], os.stat_result]) -> Callable[[int], os.stat_result]:
    """``fstat``, giving another device and inode, as overlayfs did for a file opened on it before Linux 4.19."""

    def fstat_(fd: int) -> os.stat_result:
        result = fstat(fd)
        fields = list(result)
        fields[1], fields[2] = result.st_ino + 1, result.st_dev + 1
        in_sequence = set(type(result).__match_args__)
        extra = {n: getattr(result, n) for n in dir(result) if n.startswith("st_") and n not in in_sequence}
        return os.stat_result(fields, extra)

    return fstat_


def hard_link(source: Path, link: Path) -> None:
    """Make ``link`` a second name for the file ``source``, or skip the test where the platform can't."""
    try:
        os.link(source, link)
    except (AttributeError, OSError):
        pytest.skip("this platform cannot create hard links")


@contextlib.contextmanager
def swapped_for_a_link(path: Path, target: Path) -> Iterator[None]:
    """The first os.open of the file ``path`` finds a link to ``target`` there: a swap between a writer's check of the
    file and its read. Skips the test where the platform can't create links."""
    probe = path.with_name(f"{path.name}.probe")
    try:
        probe.symlink_to(target)
    except OSError:
        pytest.skip("this platform cannot create symlinks")
    probe.unlink()
    real_open = os.open
    swapped = False

    def open_(file: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        if not swapped and os.path.realpath(file) == os.path.realpath(path):
            swapped = True
            path.unlink()
            path.symlink_to(target)
        return real_open(file, flags, *args, **kwargs)

    with patch.object(os, "open", side_effect=open_):
        yield
