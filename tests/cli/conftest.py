"""Fixtures shared by the CLI tests."""

import asyncio
import signal
from collections.abc import Callable, Generator
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from aristotlelib import api_request
from aristotlelib.cli import login_flow
from aristotlelib.project import Project, ProjectStatus

real_resolve = Project._resolve

LOOKUP_FAILURES = [
    pytest.param(
        lambda request: httpx.ConnectError("[Errno 111] Connection refused"),
        3,
        "ConnectError on {request} after 5 attempts over 0 s: [Errno 111] Connection refused.",
        id="refused",
    ),
    pytest.param(
        lambda request: httpx.Response(502, text="Bad Gateway"),
        3,
        "{request} after 5 attempts over 0 s: API request failed with status 502: Bad Gateway.",
        id="server error",
    ),
    pytest.param(
        lambda request: httpx.Response(429, json={"detail": "Too many requests."}),
        6,
        "{request} after 5 attempts over 0 s: Too many requests to Aristotle at once. Wait a few seconds and try again.",
        id="per-second 429",
    ),
]
"""A lookup's every attempt failing, its exit code, and its error, for the request it names."""


@pytest.fixture(autouse=True)
def resolve_project():
    """Resolve every project reference to itself, as a UUID is, with no project listed; a test sets ``side_effect`` to
    exercise the lookup."""
    with patch.object(Project, "_resolve", AsyncMock(side_effect=lambda reference: (reference, None))) as resolve:
        yield resolve


@pytest.fixture
def project_lookup(resolve_project):
    """Let Project.resolve_id look projects up for real, through the mocked request client."""
    resolve_project.side_effect = real_resolve


class MockableProject(Project):
    """A real Project, which a --json result can carry, whose methods a test may replace with mocks."""

    def __setattr__(self, name: str, value: object) -> None:
        if name in type(self).model_fields:
            super().__setattr__(name, value)
        else:
            # An instance attribute, such as an AsyncMock for get_tasks, shadows the method.
            object.__setattr__(self, name, value)


def mockable_project(
    project_id: str = "project-123",
    status: ProjectStatus = ProjectStatus.IDLE,
    description: str | None = None,
    has_files: bool = True,
) -> MockableProject:
    """A project record whose refresh changes nothing and sends nothing; a test mocks any other request it makes."""
    project = MockableProject(
        project_id=project_id,
        created_at=datetime(2026, 9, 1, 12, 0),
        last_updated=datetime(2026, 9, 1, 13, 0),
        description=description,
        status=status,
        has_input=True,
        has_files=has_files,
    )
    project.refresh = AsyncMock()
    return project


UNKNOWN_EVIDENCE_NAME = "Spec.FifoNeverOverflows"


def unknown_evidence_page(project_id: str) -> httpx.Response:
    """A declarations page whose one property, UNKNOWN_EVIDENCE_NAME, is refuted with evidence 4, a kind this version
    doesn't know."""
    declaration = {
        "project_id": project_id,
        "declaration_id": "fifo-never-overflows",
        "declaration_type": 1,
        "source": 2,
        "name": UNKNOWN_EVIDENCE_NAME,
        "status": 2,
        "evidence": 4,
        "evidence_message": "Refuted by a kind of evidence added after this version.",
        "filename": "Spec/Property.lean",
        "created_at": "2026-10-20T10:00:00",
        "last_updated": "2026-10-20T11:00:00",
    }
    return httpx.Response(200, json={"declarations": [declaration], "pagination_key": None})


APPROVED_KEY = "key-approved-in-the-browser"
"""The key the fake device login hands out, which no output may show."""
DEVICE_CODE = "device-code-only-the-cli-holds"
"""The secret the CLI polls with, which no output may show either."""
USER_CODE = "WDJB-MJHT"
KEY_NAME = "aristotle CLI on test-host"


def device_started(**fields: object) -> httpx.Response:
    """A start answer whose approval page is on the configured origin, ``fields`` replacing its defaults; a field set
    to None is left out."""
    body = {
        "device_code": DEVICE_CODE,
        "user_code": USER_CODE,
        "verification_uri": f"{api_request.get_web_url()}/device",
        "expires_in": 600,
        "interval": 5,
        **fields,
    }
    return httpx.Response(200, json={name: value for name, value in body.items() if value is not None})


def token_error(code: str) -> httpx.Response:
    """A token answer with RFC 8628's ``error``."""
    return httpx.Response(400, json={"error": code})


def approved(api_key: str = APPROVED_KEY, key_name: str | None = KEY_NAME) -> httpx.Response:
    """A token answer that hands out the key."""
    return httpx.Response(200, json={"api_key": api_key, "key_name": key_name})


@pytest.fixture
def poll_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """login_flow's clock, from 0, and asyncio.sleep, which advances it and yields to the loop once: minutes of waiting
    for approval pass at once, and a cancellation still lands at a sleep."""
    now = [0.0]
    real_sleep = asyncio.sleep

    async def sleep(seconds: float) -> None:
        now[0] += seconds
        await real_sleep(0)

    monkeypatch.setattr(login_flow, "time", Mock(monotonic=lambda: now[0]))
    monkeypatch.setattr(login_flow.asyncio, "sleep", sleep)
    return now


@pytest.fixture
def ctrl_c_while_waiting(
    monkeypatch: pytest.MonkeyPatch, poll_clock: list[float]
) -> Generator[Callable[[list[httpx.Request]], None], None, None]:
    """Call it with device_server's requests: the wait after the first token answer raises SIGINT, where Ctrl-C lands
    on every Python, under asyncio.run's handler (3.11+) or the default one, which this puts in place for the test even
    where the tests started with SIGINT ignored."""

    def arm(requests: list[httpx.Request]) -> None:
        fake_sleep = asyncio.sleep

        async def sleep(seconds: float) -> None:
            if any(request.url.path.endswith(login_flow.DEVICE_TOKEN) for request in requests):
                signal.raise_signal(signal.SIGINT)
            await fake_sleep(seconds)

        monkeypatch.setattr(login_flow.asyncio, "sleep", sleep)

    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield arm
    finally:
        signal.signal(signal.SIGINT, previous)


@pytest.fixture
def device_server(monkeypatch: pytest.MonkeyPatch, poll_clock: list[float]) -> Callable[..., list[httpx.Request]]:
    """Serve the device login and the key check through httpx's MockTransport, on poll_clock.

    Call it with the token answers in order, the last repeated once they run out, ``start`` for the start answer and
    ``check`` for the key check's, which by default accepts APPROVED_KEY only: each a response, an exception for the
    transport to raise, or a function of the request that returns or raises one. It returns the requests received; each
    token request carries the clock's reading as ``request.extensions["polled_at"]``.
    """
    requests: list[httpx.Request] = []

    def serve(*tokens: object, start: object = None, check: object = None) -> list[httpx.Request]:
        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            path = request.url.path.removeprefix(f"/api/v{api_request.API_VERSION}")
            if path == login_flow.DEVICE_START:
                outcome = device_started() if start is None else start
            elif path == login_flow.DEVICE_TOKEN:
                request.extensions["polled_at"] = poll_clock[0]
                polls = sum(r.url.path.endswith(login_flow.DEVICE_TOKEN) for r in requests)
                outcome = tokens[min(polls, len(tokens)) - 1]
            elif path == "/project" and check is not None:
                outcome = check
            elif path == "/project":
                accepted = request.headers.get("X-API-Key") == APPROVED_KEY
                outcome = httpx.Response(200 if accepted else 401, json={"results": []})
            else:
                outcome = httpx.Response(404, json={"detail": "Not Found"})
            if callable(outcome):
                outcome = outcome(request)
            if isinstance(outcome, Exception):
                raise outcome
            assert isinstance(outcome, httpx.Response)
            return outcome

        transport = httpx.MockTransport(handle)
        real_client = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
        return requests

    return serve
