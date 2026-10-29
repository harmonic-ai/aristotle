"""Tests for aristotlelib.api_request module."""

import asyncio
import contextlib
import gc
import logging
import re
import socket
import ssl
import threading
import weakref
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch
from urllib.request import getproxies_environment

import httpx
import pytest

from aristotlelib import api_request, credentials
from aristotlelib.api_request import (
    AristotleAPIError,
    AristotleNotProcessedError,
    AristotleOutcomeUnknownError,
    AristotleProjectLimitError,
    AristotleRequestClient,
    get_api_key,
    set_api_key,
)
from tests.conftest import (
    BILLING_UNAVAILABLE,
    BUY_CREDITS,
    FAKE_KEY,
    OUT_OF_CREDITS,
    PADDED_KEYS,
    REFUSED_KEYS,
    SIGNUP_GRANT_PENDING,
    fast_api_clock,
    give_key,
    shown_key_runs,
    user_agent_of,
)


@pytest.fixture(autouse=True)
def default_web_origin(monkeypatch):
    monkeypatch.delenv("ARISTOTLE_WEB_URL", raising=False)


def test_set_api_key():
    """Test setting the API key."""
    test_key = "test-api-key-123"
    result = set_api_key(test_key)
    assert result == test_key
    assert api_request.API_KEY == test_key


def test_get_api_key_from_global():
    """Test getting API key from global variable."""
    set_api_key("global-key")
    result = get_api_key()
    assert result == "global-key"


def test_get_api_key_from_env():
    """Test getting API key from environment variable."""
    # Clear global key
    api_request.API_KEY = None

    with patch.dict("os.environ", {"ARISTOTLE_API_KEY": "env-key"}):
        result = get_api_key()
        assert result == "env-key"


def test_get_api_key_not_set():
    """Test that getting API key raises error when not set."""
    # Clear both global and env
    api_request.API_KEY = None

    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError) as exc_info:
            get_api_key()
        assert "API key has not been set" in str(exc_info.value)


def test_get_api_key_prefers_global_over_env():
    """Test that global API key takes precedence over environment variable."""
    set_api_key("global-key")

    with patch.dict("os.environ", {"ARISTOTLE_API_KEY": "env-key"}):
        result = get_api_key()
        assert result == "global-key"


@pytest.mark.parametrize("source", ["argument", "environment", "stored"])
@pytest.mark.parametrize("key", PADDED_KEYS)
async def test_a_key_is_sent_without_the_whitespace_around_it_from_every_source(
    source, key, api_server, monkeypatch, tmp_path
):
    requests = api_server(_ok(), httpx.Response(200, content=b'data: {"event": "ready"}\n\n', headers=EVENT_STREAM))
    give_key(source, key, monkeypatch, tmp_path)

    async with AristotleRequestClient() as client:
        await client.get("/project")
        assert [event async for event in client.stream_sse("/events")] == [{"event": "ready"}]

    assert [request.headers["X-API-Key"] for request in requests] == [FAKE_KEY, FAKE_KEY]
    assert api_request.api_key_source() == source


@pytest.mark.parametrize("source", ["argument", "environment", "stored"])
@pytest.mark.parametrize(("key", "problem"), REFUSED_KEYS)
async def test_a_key_that_stripping_cannot_fix_is_refused_by_source_before_any_request_and_never_shown(
    source, key, problem, api_server, monkeypatch, tmp_path
):
    requests = api_server()
    give_key(source, key, monkeypatch, tmp_path)
    expected = {
        "argument": f"The API key passed to aristotlelib.set_api_key() {problem}.",
        "environment": f"The API key in the ARISTOTLE_API_KEY environment variable {problem}.",
        "stored": f"The API key in the credentials file {credentials.credentials_path()} {problem}. Log in again "
        "with:\n  aristotle login",
    }[source]

    async with AristotleRequestClient() as client:
        with pytest.raises(ValueError) as request_error:
            await client.get("/project")
        with pytest.raises(ValueError) as stream_error:
            [event async for event in client.stream_sse("/events")]

    assert str(request_error.value) == str(stream_error.value) == expected
    assert shown_key_runs(expected, key) == []
    assert requests == []


def test_the_cli_names_its_own_argument_for_a_key_it_refuses():
    set_api_key(" fake-Q7zP 3wK9vX2m\n")
    api_request.set_surface("cli")

    with pytest.raises(ValueError, match=r"^The API key given with --api-key contains a space\.$"):
        get_api_key()


async def test_request_client_get():
    """Test making a GET request."""
    set_api_key("test-key")

    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = httpx.Headers({"Content-Type": "application/json"})
    mock_response.json.return_value = {"status": "success"}

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with AristotleRequestClient() as client:
            response = await client.get("/test-endpoint")

        assert response.status_code == 200
        assert response.json() == {"status": "success"}

        # Verify the request was made correctly
        mock_client.request.assert_called_once()
        call_args = mock_client.request.call_args
        assert call_args.kwargs["method"] == "GET"
        assert "/test-endpoint" in call_args.kwargs["url"]
        assert call_args.kwargs["headers"]["X-API-Key"] == "test-key"


@pytest.mark.parametrize(
    ("web_url", "expected_origin"),
    [
        (None, "https://aristotle.harmonic.fun"),
        ("https://alt.example.test/", "https://alt.example.test"),
    ],
)
async def test_requests_and_sse_use_the_selected_web_origin(monkeypatch, web_url, expected_origin):
    if web_url is None:
        monkeypatch.delenv("ARISTOTLE_WEB_URL", raising=False)
    else:
        monkeypatch.setenv("ARISTOTLE_WEB_URL", web_url)
    set_api_key("test-key")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/events"):
            return httpx.Response(200, content=b'data: {"event": "ready"}\n\n', headers=EVENT_STREAM)
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    with patch.object(
        api_request.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)
    ):
        async with AristotleRequestClient() as client:
            assert (await client.get("/project")).json() == {"ok": True}
            events = [event async for event in client.stream_sse("/events")]

    assert events == [{"event": "ready"}]
    assert [str(request.url) for request in requests] == [
        f"{expected_origin}/api/v3/project",
        f"{expected_origin}/api/v3/events",
    ]
    assert [request.headers["X-API-Key"] for request in requests] == ["test-key", "test-key"]


@pytest.mark.parametrize("surface", ["library", "cli", "ci", "action"])
async def test_a_request_names_its_surface_in_the_user_agent_and_changes_nothing_else(api_server, surface):
    requests = api_server(httpx.Response(200, json={}))
    if surface != "library":
        api_request.set_surface(surface)

    async with AristotleRequestClient() as client:
        await client.get("/project", params={"limit": 1})
    async with httpx.AsyncClient() as plain:
        default = plain.build_request("GET", requests[0].url, headers={"X-API-Key": "test-key"})

    sent = dict(requests[0].headers)
    assert sent.pop("user-agent") == user_agent_of(surface)
    assert sent == {name: value for name, value in default.headers.items() if name != "user-agent"}


async def test_uploads_and_event_streams_send_the_user_agent_too(api_server):
    requests = api_server(
        httpx.Response(200, json={}),
        httpx.Response(200, content=b'data: {"event": "ready"}\n\n', headers=EVENT_STREAM),
    )
    api_request.set_surface("ci")

    async with AristotleRequestClient() as client:
        await client.post("/project/p/files", files=[("files", ("a.lean", b"theorem t : True := trivial"))])
        assert [event async for event in client.stream_sse("/events")] == [{"event": "ready"}]

    assert requests[0].headers["Content-Type"].startswith("multipart/form-data")
    assert [request.headers["User-Agent"] for request in requests] == [user_agent_of("ci")] * 2


async def test_non_loopback_http_is_rejected_before_sending_a_key(monkeypatch):
    monkeypatch.setenv("ARISTOTLE_WEB_URL", "http://alt.example.test")
    set_api_key("test-key")
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with AristotleRequestClient() as client:
            with pytest.raises(ValueError, match="must use HTTPS unless its host is loopback"):
                await client.get("/project")
            with pytest.raises(ValueError, match="must use HTTPS unless its host is loopback"):
                [event async for event in client.stream_sse("/events")]

    mock_client.request.assert_not_awaited()
    mock_client.stream.assert_not_called()


async def test_request_client_post_with_data():
    """Test making a POST request with form data."""
    set_api_key("test-key")

    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = 201
    mock_response.headers = httpx.Headers({"Content-Type": "application/json"})

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with AristotleRequestClient() as client:
            await client.post("/create", data={"name": "test"})

        call_args = mock_client.request.call_args
        assert call_args.kwargs["method"] == "POST"
        assert call_args.kwargs["data"] == {"name": "test"}


async def test_request_client_post_with_files():
    """Test making a POST request with file uploads."""
    set_api_key("test-key")

    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = 201
    mock_response.headers = httpx.Headers({"Content-Type": "application/json"})

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()

    files = [("file", ("test.txt", b"content", "text/plain"))]

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with AristotleRequestClient() as client:
            await client.post("/upload", files=files)

        call_args = mock_client.request.call_args
        assert call_args.kwargs["method"] == "POST"
        assert "files" in call_args.kwargs


async def test_request_client_put():
    """Test making a PUT request."""
    set_api_key("test-key")

    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = httpx.Headers({"Content-Type": "application/json"})

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with AristotleRequestClient() as client:
            await client.put("/update", data={"name": "updated"})

        call_args = mock_client.request.call_args
        assert call_args.kwargs["method"] == "PUT"


async def test_request_client_delete():
    """Test making a DELETE request."""
    set_api_key("test-key")

    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = 204

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with AristotleRequestClient() as client:
            await client.delete("/remove/123")

        call_args = mock_client.request.call_args
        assert call_args.kwargs["method"] == "DELETE"


QUEUE_FULL = (
    "Too many runs in progress: 1 can run at once and 2 more can wait in the queue. "
    "Wait for one to finish, or cancel a queued one."
)
FALLBACK = (
    "You have too many projects in progress. Cancel a running task or wait for one to complete before starting another."
)


TOO_MANY_REQUESTS = "Too many requests to Aristotle at once. Wait a few seconds and try again."


@pytest.mark.parametrize(
    ("method", "body", "expected", "project_limit"),
    [
        ("POST", {"json": {"detail": QUEUE_FULL}}, QUEUE_FULL, True),
        (
            "POST",
            {"json": {"detail": "Too many inflight requests. Please wait for them to finish."}},
            FALLBACK,
            True,
        ),
        ("POST", {"json": {"detail": "Too many requests."}}, TOO_MANY_REQUESTS, False),
        ("GET", {"json": {"detail": QUEUE_FULL}}, TOO_MANY_REQUESTS, False),
        ("POST", {"json": {"detail": "  "}}, TOO_MANY_REQUESTS, False),
        ("POST", {"json": {"detail": ["not", "a", "string"]}}, TOO_MANY_REQUESTS, False),
        ("POST", {"json": {}}, TOO_MANY_REQUESTS, False),
        ("POST", {"text": "<html>429 Too Many Requests</html>"}, TOO_MANY_REQUESTS, False),
    ],
    ids=[
        "queue full: the server's detail",
        "refused over the limit: fallback",
        "per-second limit",
        "a read is never the limit on runs",
        "blank detail",
        "non-string detail",
        "no detail",
        "not JSON",
    ],
)
@pytest.mark.usefixtures("frozen_api_clock")
async def test_each_rate_limit_says_which_was_hit(method: str, body: dict, expected: str, project_limit: bool):
    set_api_key("test-key")
    response = httpx.Response(429, **body, request=httpx.Request(method, "https://example.test/project"))
    error = httpx.HTTPStatusError("Too Many Requests", request=response.request, response=response)
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request = AsyncMock(side_effect=error)
    mock_client.aclose = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with AristotleRequestClient() as client:
            with pytest.raises(AristotleAPIError) as exc_info:
                await getattr(client, method.lower())("/project")

    message = expected if project_limit else f"{method} /project after 5 attempts over 0 s: {expected}"
    if method == "POST" and not project_limit:
        # Every attempt turned away before any handler ran: the POST did nothing.
        message += " Aristotle didn't act on it."
    assert str(exc_info.value) == message
    assert isinstance(exc_info.value, AristotleNotProcessedError) is (method == "POST" and not project_limit)
    assert exc_info.value.status_code == 429
    assert isinstance(exc_info.value, AristotleProjectLimitError) is project_limit


async def test_request_client_generic_http_error():
    """Test handling generic HTTP errors."""
    set_api_key("test-key")

    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = 500
    mock_response.headers = httpx.Headers()

    error = httpx.HTTPStatusError("Internal Server Error", request=Mock(), response=mock_response)

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request = AsyncMock(side_effect=error)
    mock_client.aclose = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with AristotleRequestClient() as client:
            with pytest.raises(AristotleAPIError) as exc_info:
                await client.get("/test")

            assert exc_info.value.status_code == 500
            assert "500" in str(exc_info.value)


async def test_request_client_network_error():
    """A transport error names its type and the request."""
    set_api_key("test-key")

    error = httpx.RequestError("Connection failed")

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request = AsyncMock(side_effect=error)
    mock_client.aclose = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with AristotleRequestClient() as client:
            with pytest.raises(AristotleAPIError) as exc_info:
                await client.get("/test")

            assert str(exc_info.value) == "RequestError on GET /test: Connection failed"
            assert exc_info.value.status_code is None


async def test_request_client_context_manager():
    """Test that AristotleRequestClient properly manages async context."""
    set_api_key("test-key")

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.aclose = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with AristotleRequestClient() as client:
            assert client._client is not None

        # Verify client was closed
        mock_client.aclose.assert_called_once()


async def test_request_client_with_params():
    """Test making a request with query parameters."""
    set_api_key("test-key")

    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = httpx.Headers({"Content-Type": "application/json"})

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with AristotleRequestClient() as client:
            await client.get("/test", params={"page": 1, "limit": 10})

        call_args = mock_client.request.call_args
        assert call_args.kwargs["params"] == {"page": 1, "limit": 10}


async def test_request_client_close():
    """Test manually closing the client."""
    set_api_key("test-key")

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.aclose = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        client = AristotleRequestClient()
        client._client = mock_client

        await client.close()

        mock_client.aclose.assert_called_once()
        assert client._client is None


def test_aristotle_api_error_with_status_code():
    """Test AristotleAPIError with status code."""
    error = AristotleAPIError("Test error", status_code=404)
    assert str(error) == "Test error"
    assert error.status_code == 404


def test_aristotle_api_error_without_status_code():
    """Test AristotleAPIError without status code."""
    error = AristotleAPIError("Test error")
    assert str(error) == "Test error"
    assert error.status_code is None


async def test_unset_query_parameters_are_omitted():
    """None params are dropped; the server rejects an empty pagination_key."""
    set_api_key("test-key")

    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = httpx.Headers({"Content-Type": "application/json"})

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with AristotleRequestClient() as client:
            await client.get("/project/abc/declarations", params={"limit": 100, "pagination_key": None})

    assert mock_client.request.call_args.kwargs["params"] == {"limit": 100}


async def test_only_none_counts_as_unset():
    """False and 0 are values, not unset."""
    set_api_key("test-key")

    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = httpx.Headers({"Content-Type": "application/json"})

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with AristotleRequestClient() as client:
            await client.get("/task/t/events", params={"ascending": False, "limit": 0, "pagination_key": None})

    assert mock_client.request.call_args.kwargs["params"] == {"ascending": False, "limit": 0}


async def test_a_request_without_params_still_sends_none():
    """params=None stays None rather than becoming {}."""
    set_api_key("test-key")

    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = httpx.Headers({"Content-Type": "application/json"})

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        async with AristotleRequestClient() as client:
            await client.get("/test")

    assert mock_client.request.call_args.kwargs["params"] is None


@pytest.mark.parametrize(
    ("status", "detail", "message"),
    [
        (429, "Too many requests.", "Too many requests to Aristotle at once. Wait a few seconds and try again."),
        (404, "Agent task not found", "API request failed with status 404: Agent task not found"),
        (503, "Service Unavailable", "API request failed with status 503: Service Unavailable"),
    ],
)
async def test_an_event_stream_refused_with_a_status_raises_an_api_error(status, detail, message, monkeypatch):
    set_api_key("test-key")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json={"detail": detail})

    transport = httpx.MockTransport(handle)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    monkeypatch.setattr(api_request.asyncio, "sleep", AsyncMock())

    with pytest.raises(AristotleAPIError) as exc_info:
        async for _ in AristotleRequestClient().stream_sse("/task/t-1/events-stream"):
            pass

    assert str(exc_info.value) == message
    assert exc_info.value.status_code == status
    # A 429 or 5xx is a failed connection and is retried; any other status is final.
    assert len(requests) == (1 if status == 404 else 5)


EVENT_STREAM = {"Content-Type": "text/event-stream"}
"""The headers of an event stream, as Aristotle serves one."""


class _Body(httpx.AsyncByteStream):
    """A response body that sends ``chunks``, then raises ``error`` (a dropped connection) or ends cleanly."""

    def __init__(self, chunks: list[bytes], error: Exception | None = None):
        self.chunks = chunks
        self.error = error

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error


def _event(event_id: str, status: int = 1) -> bytes:
    return f'data: {{"event_id": "{event_id}", "status": {status}}}\n\n'.encode()


def _serve(monkeypatch, connections: list) -> tuple[list[httpx.Request], AsyncMock]:
    """Serve one scripted connection per stream request; returns the requests seen and the patched sleep."""
    set_api_key("test-key")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        connection = connections[len(requests) - 1]
        if isinstance(connection, Exception):
            raise connection
        if isinstance(connection, httpx.Response):
            return connection
        return httpx.Response(200, stream=connection, headers=EVENT_STREAM)

    transport = httpx.MockTransport(handle)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    sleep = AsyncMock()
    monkeypatch.setattr(api_request.asyncio, "sleep", sleep)
    return requests, sleep


async def test_a_stream_cut_every_minute_through_ten_quiet_minutes_keeps_reconnecting(monkeypatch):
    dropped = httpx.RemoteProtocolError("peer closed connection without sending complete message body")
    # Ten one-minute connections that carry only the server's keepalive, then the run's next event and a clean end.
    quiet = [_Body([b": keepalive\n\n"], dropped) for _ in range(10)]
    requests, sleep = _serve(monkeypatch, [*quiet, _Body([b"id: 2026-01-01T00:10:00\n", _event("e-1")])])
    live = AsyncMock(return_value=False)

    events = [event async for event in AristotleRequestClient().stream_sse("/events", reconnect_if=live)]

    assert events == [{"event_id": "e-1", "status": 1}]
    assert len(requests) == 11
    # Each connection received a line, so none counts toward max_retries and the backoff never grows.
    assert [call.args[0] for call in sleep.await_args_list] == [1] * 10
    live.assert_awaited_once()


async def test_a_reconnect_resumes_after_the_newest_event_and_skips_a_replayed_one(monkeypatch):
    dropped = httpx.RemoteProtocolError("closed")
    first = _Body(
        [
            b"id: 2026-01-01T00:00:02\n",
            _event("e-2"),
            # A later update to an older event carries that event's older id.
            b"id: 2026-01-01T00:00:01\n",
            _event("e-1", status=2),
        ],
        dropped,
    )
    replay = _Body([b"id: 2026-01-01T00:00:02\n", _event("e-2"), b"id: 2026-01-01T00:00:03\n", _event("e-3")])
    requests, _ = _serve(monkeypatch, [first, replay])

    events = [event async for event in AristotleRequestClient().stream_sse("/events")]

    assert [(event["event_id"], event["status"]) for event in events] == [("e-2", 1), ("e-1", 2), ("e-3", 1)]
    assert "Last-Event-ID" not in requests[0].headers
    assert requests[1].headers["Last-Event-ID"] == "2026-01-01T00:00:02"


async def test_a_cut_inside_an_event_resumes_before_that_event(monkeypatch):
    dropped = httpx.RemoteProtocolError("closed")
    # The connection drops after e-2's id line and part of its data line.
    first = _Body([b"id: 2026-01-01T00:00:01\n", _event("e-1"), b'id: 2026-01-01T00:00:02\ndata: {"event_'], dropped)
    replay = _Body([b"id: 2026-01-01T00:00:02\n", _event("e-2")])
    requests, _ = _serve(monkeypatch, [first, replay])

    events = [event async for event in AristotleRequestClient().stream_sse("/events")]

    assert [event["event_id"] for event in events] == ["e-1", "e-2"]
    assert requests[1].headers["Last-Event-ID"] == "2026-01-01T00:00:01"


async def test_a_refused_stream_waits_as_long_as_retry_after_asks_before_reconnecting(monkeypatch):
    refused = httpx.Response(503, json={"detail": "Service unavailable"}, headers={"Retry-After": "7"})
    monkeypatch.setattr(api_request.random, "uniform", lambda low, high: low)
    requests, sleep = _serve(monkeypatch, [refused, _Body([_event("e-1")])])

    events = [event async for event in AristotleRequestClient().stream_sse("/events")]

    assert events == [{"event_id": "e-1", "status": 1}]
    assert len(requests) == 2
    assert [call.args[0] for call in sleep.await_args_list] == [7]


async def test_a_stream_refused_for_longer_than_it_would_ever_wait_gives_up_at_once(monkeypatch):
    refused = httpx.Response(503, json={"detail": "Service unavailable"}, headers={"Retry-After": "3600"})
    requests, sleep = _serve(monkeypatch, [refused, _Body([_event("e-1")])])

    with pytest.raises(AristotleAPIError) as info:
        [event async for event in AristotleRequestClient().stream_sse("/events")]

    assert info.value.status_code == 503
    assert len(requests) == 1
    sleep.assert_not_awaited()


async def test_a_stream_that_cannot_connect_gives_up_after_max_retries(monkeypatch):
    unreachable = httpx.ConnectError("unreachable")
    requests, sleep = _serve(monkeypatch, [unreachable] * 5)

    with pytest.raises(httpx.ConnectError):
        [event async for event in AristotleRequestClient().stream_sse("/events")]

    assert len(requests) == 5
    assert [call.args[0] for call in sleep.await_args_list] == [2, 4, 8, 16]


@pytest.mark.parametrize(
    ("answer", "body"),
    [(lambda: _page(), "text/html"), (lambda: httpx.Response(200, content=b"<html>Sign in</html>"), "no content type")],
    ids=["page", "untyped"],
)
@pytest.mark.usefixtures("no_proxy_settings")
async def test_an_answer_not_typed_as_an_event_stream_fails_the_connection(answer, body, monkeypatch):
    requests, sleep = _serve(monkeypatch, [answer() for _ in range(5)])

    with pytest.raises(httpx.RemoteProtocolError) as info:
        [event async for event in AristotleRequestClient().stream_sse("/events")]

    assert str(info.value) == PAGE_ERROR.replace("text/html", body)
    assert len(requests) == 5
    assert [call.args[0] for call in sleep.await_args_list] == [2, 4, 8, 16]


async def test_an_event_stream_typed_with_a_charset_is_read(monkeypatch):
    stream = httpx.Response(200, content=_event("e-1"), headers={"Content-Type": "text/event-stream; charset=utf-8"})
    _serve(monkeypatch, [stream])

    events = [event async for event in AristotleRequestClient().stream_sse("/events")]

    assert events == [{"event_id": "e-1", "status": 1}]


async def test_a_wait_rides_out_minutes_of_failed_connections_while_the_run_is_live(monkeypatch):
    unreachable = httpx.ConnectError("unreachable")
    # Twelve failures in a row, about five minutes of backoff, then the network is back.
    requests, _ = _serve(monkeypatch, [*[unreachable] * 12, _Body([_event("e-1")])])
    now, sleep = fast_api_clock(monkeypatch)

    events = [
        event
        async for event in AristotleRequestClient().stream_sse(
            "/events", reconnect_if=AsyncMock(return_value=False), live=lambda: True
        )
    ]

    assert events == [{"event_id": "e-1", "status": 1}]
    assert len(requests) == 13
    # The backoff doubles to its cap and stays there.
    assert [call.args[0] for call in sleep.await_args_list] == [2, 4, 8, 16, 30, 30, 30, 30, 30, 30, 30, 30]
    assert now[0] == 270


async def test_a_wait_gives_up_once_its_connections_have_failed_for_the_outage_limit(monkeypatch):
    unreachable = httpx.ConnectError("unreachable")
    requests, _ = _serve(monkeypatch, [unreachable] * 100)
    now, _ = fast_api_clock(monkeypatch)

    with pytest.raises(httpx.ConnectError):
        [event async for event in AristotleRequestClient().stream_sse("/events", live=lambda: True)]

    # The first failure began at 0: it gives up at the first one that ends past STREAM_OUTAGE_SECONDS.
    assert now[0] == 600
    assert len(requests) == 24


async def test_a_wait_stops_riding_out_an_outage_once_its_run_has_ended(monkeypatch):
    unreachable = httpx.ConnectError("unreachable")
    requests, _ = _serve(monkeypatch, [unreachable] * 100)
    now, _ = fast_api_clock(monkeypatch)

    # The wait's refreshes, which still get through, see the run end 100 s in.
    events = [event async for event in AristotleRequestClient().stream_sse("/events", live=lambda: now[0] < 100)]

    assert events == []
    # It rides on at 30, 60 and 90 s; at 120 s the run has ended, so the stream ends instead of raising.
    assert len(requests) == 8
    assert now[0] == 120


async def test_a_wait_reports_an_outage_as_it_begins_and_as_a_line_arrives_again(monkeypatch):
    unreachable = httpx.ConnectError("unreachable")
    _serve(monkeypatch, [*[unreachable] * 4, _Body([b": keepalive\n", _event("e-1")])])
    fast_api_clock(monkeypatch)
    seen: list[tuple[str, object]] = []

    stream = AristotleRequestClient().stream_sse(
        "/events",
        reconnect_if=AsyncMock(return_value=False),
        live=lambda: True,
        on_outage=lambda error: seen.append(("outage", error)),
    )
    async for event in stream:
        seen.append(("event", event["event_id"]))

    # Once for the outage, not for each of its four failures; back at its first line, before its first event.
    assert seen == [("outage", unreachable), ("outage", None), ("event", "e-1")]


async def test_an_outage_the_wait_gives_up_on_is_reported_once(monkeypatch):
    unreachable = httpx.ConnectError("unreachable")
    _serve(monkeypatch, [unreachable] * 100)
    fast_api_clock(monkeypatch)
    on_outage = Mock()

    with pytest.raises(httpx.ConnectError):
        [
            event
            async for event in AristotleRequestClient().stream_sse("/events", live=lambda: True, on_outage=on_outage)
        ]

    on_outage.assert_called_once_with(unreachable)


async def test_a_streams_local_protocol_error_is_reported_and_raised_by_its_class_alone(monkeypatch):
    _serve(monkeypatch, [httpx.LocalProtocolError(HEADER_REFUSAL)] * 100)
    fast_api_clock(monkeypatch)
    outages: list[Exception | None] = []

    with pytest.raises(httpx.LocalProtocolError) as info:
        [
            event
            async for event in AristotleRequestClient().stream_sse(
                "/events", live=lambda: True, on_outage=outages.append
            )
        ]

    assert [(type(error), str(error)) for error in outages] == [(httpx.LocalProtocolError, "")]
    assert str(info.value) == ""


async def test_a_stream_that_rides_out_nothing_reports_no_outage(monkeypatch):
    unreachable = httpx.ConnectError("unreachable")
    refused = httpx.Response(503, json={"detail": "Service unavailable"}, headers={"Retry-After": "3600"})
    _serve(monkeypatch, [refused, *[unreachable] * 5])
    fast_api_clock(monkeypatch)
    on_outage = Mock()
    client = AristotleRequestClient()

    # A refusal for longer than it ever waits gives up at once; a run that has ended isn't waited out.
    with pytest.raises(AristotleAPIError):
        [event async for event in client.stream_sse("/events", live=lambda: True, on_outage=on_outage)]
    assert [event async for event in client.stream_sse("/events", live=lambda: False, on_outage=on_outage)] == []

    on_outage.assert_not_called()


async def test_retries_in_a_quiet_context_are_logged_at_debug_level_while_it_says_so(api_server, caplog):
    """As each retry is logged: a refresh under way when the wait reports an outage goes quiet from then on."""
    caplog.set_level(logging.DEBUG, logger="aristotle")
    api_server(httpx.ConnectError("unreachable"), httpx.ConnectError("unreachable"), _ok())
    quiet = iter([False, True])

    async with AristotleRequestClient() as client:
        with api_request.quietly(lambda: next(quiet)):
            assert (await client.get("/project")).json() == {"ok": True}

    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "Retrying in 0.0 s after ConnectError on GET /project: unreachable"),
        (logging.DEBUG, "Retrying in 0.0 s after ConnectError on GET /project: unreachable"),
    ]


async def test_a_wait_reports_the_end_of_an_outage_once_however_many_connections_follow(monkeypatch):
    unreachable = httpx.ConnectError("unreachable")
    dropped = httpx.RemoteProtocolError("closed")
    _serve(monkeypatch, [unreachable, _Body([_event("e-1")], dropped), _Body([_event("e-2")])])
    fast_api_clock(monkeypatch)
    on_outage = Mock()

    stream = AristotleRequestClient().stream_sse(
        "/events", reconnect_if=AsyncMock(return_value=False), live=lambda: True, on_outage=on_outage
    )
    assert [event["event_id"] async for event in stream] == ["e-1", "e-2"]

    # The connection after the outage drops having received a line: a reconnect, not another outage.
    assert on_outage.call_args_list == [call(unreachable), call(None)]


async def test_a_clean_end_without_a_line_does_not_end_an_outage(monkeypatch):
    unreachable = httpx.ConnectError("unreachable")
    # Amid the refusals, one connection ends before any line, as one to a server going down or coming up may.
    connections = [*[unreachable] * 4, _Body([]), unreachable, unreachable, _Body([_event("e-1")])]
    requests, _ = _serve(monkeypatch, connections)
    _, sleep = fast_api_clock(monkeypatch)

    events = [
        event
        async for event in AristotleRequestClient().stream_sse(
            "/events", reconnect_if=AsyncMock(side_effect=[True, False]), live=lambda: True
        )
    ]

    assert events == [{"event_id": "e-1", "status": 1}]
    assert len(requests) == 8
    assert [call.args[0] for call in sleep.await_args_list] == [2, 4, 8, 16, 30, 30, 30]


async def test_a_clean_end_reconnects_only_while_reconnect_if_says_so(monkeypatch):
    requests, _ = _serve(monkeypatch, [_Body([_event("e-1")]), _Body([_event("e-2")])])
    live = AsyncMock(side_effect=[True, False])

    events = [event async for event in AristotleRequestClient().stream_sse("/events", reconnect_if=live)]

    assert [event["event_id"] for event in events] == ["e-1", "e-2"]
    assert len(requests) == 2


async def test_a_clean_end_without_reconnect_if_ends_the_stream(monkeypatch):
    requests, _ = _serve(monkeypatch, [_Body([_event("e-1")])])

    events = [event async for event in AristotleRequestClient().stream_sse("/events")]

    assert [event["event_id"] for event in events] == ["e-1"]
    assert len(requests) == 1


async def test_clean_ends_that_send_nothing_stop_reconnecting_after_max_retries(monkeypatch):
    requests, _ = _serve(monkeypatch, [_Body([]) for _ in range(5)])

    events = [
        event
        async for event in AristotleRequestClient().stream_sse("/events", reconnect_if=AsyncMock(return_value=True))
    ]

    assert events == []
    assert len(requests) == 5


# --- Proxy and custom certificate authority support ---


@pytest.fixture(autouse=True)
def reset_environment_logging(monkeypatch: pytest.MonkeyPatch):
    """The proxy/CA banner is logged once per process; tests need it reset."""
    monkeypatch.setattr(api_request, "_logged_environment", False)
    monkeypatch.setattr(api_request, "_ignored_ca_bundles", set())
    for variable in (*api_request.CA_BUNDLE_VARIABLES, *api_request.PROXY_VARIABLES, "SSL_CERT_DIR"):
        monkeypatch.delenv(variable, raising=False)


def test_no_ca_bundle_is_configured_by_default():
    assert api_request._ca_bundle() is None


@pytest.mark.parametrize("variable", api_request.CA_BUNDLE_VARIABLES)
def test_each_supported_variable_selects_a_ca_bundle(variable: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bundle = tmp_path / "corp-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv(variable, str(bundle))

    assert api_request._ca_bundle() == str(bundle)


def test_the_first_variable_in_precedence_order_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    first = tmp_path / "aristotle.pem"
    second = tmp_path / "requests.pem"
    first.write_text("a")
    second.write_text("b")
    monkeypatch.setenv("ARISTOTLE_CA_BUNDLE", str(first))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(second))

    assert api_request._ca_bundle() == str(first)


def test_a_ca_bundle_that_does_not_exist_is_ignored_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    # Silently falling back to the default trust store would look like the setting worked.
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/nonexistent/corp-ca.pem")

    with caplog.at_level("WARNING", logger="aristotle"):
        assert api_request._ca_bundle() is None

    assert "not a file" in caplog.text


def test_an_ignored_ca_bundle_is_warned_about_once(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/etc/ssl/certs")

    with caplog.at_level("WARNING", logger="aristotle"):
        for _ in range(3):
            api_request.new_http_client()

    assert [r.getMessage() for r in caplog.records].count(
        "REQUESTS_CA_BUNDLE points at /etc/ssl/certs, which is not a file. Ignoring it."
    ) == 1


def test_a_missing_ssl_cert_file_really_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # httpx reads SSL_CERT_FILE itself under trust_env, so skipping it in _ca_bundle alone still crashed.
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))

    assert api_request.new_http_client() is not None


def test_a_directory_is_not_accepted_as_a_ca_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # SSL_CERT_DIR-style values land in SSL_CERT_FILE often enough to be worth rejecting.
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path))

    assert api_request._ca_bundle() is None


def test_the_client_trusts_the_environment_so_proxies_are_honored():
    client = api_request.new_http_client()

    assert client.trust_env is True


def test_a_proxy_is_reported_without_leaking_its_credentials(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:hunter2@proxy.corp:3128")

    with caplog.at_level("INFO", logger="aristotle"):
        api_request.new_http_client()

    assert "proxy" in caplog.text.lower()
    assert "hunter2" not in caplog.text
    assert "proxy.corp" not in caplog.text


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


@pytest.mark.parametrize(
    ("scheme", "no_proxy", "proxied"),
    [("socks5", "", True), ("socks5h", "", True), ("socks5", "127.0.0.1", False)],
    ids=["socks5", "socks5h", "NO_PROXY"],
)
async def test_a_socks_proxy_from_the_environment_carries_the_requests(
    scheme: str, no_proxy: str, proxied: bool, monkeypatch: pytest.MonkeyPatch
):
    connects: list[tuple[str, int]] = []

    async def api(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(reader)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
        await writer.drain()
        writer.close()

    async def socks(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # RFC 1928 without authentication: the greeting, a CONNECT to an IPv4 address or a name, then a relay.
        _, methods = await reader.readexactly(2)
        await reader.readexactly(methods)
        writer.write(b"\x05\x00")
        _, _, _, address_type = await reader.readexactly(4)
        if address_type == 1:
            host = socket.inet_ntoa(await reader.readexactly(4))
        else:
            host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
        port = int.from_bytes(await reader.readexactly(2), "big")
        connects.append((host, port))
        upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
        writer.write(b"\x05\x00\x00\x01" + bytes(6))
        await asyncio.gather(_relay(reader, upstream_writer), _relay(upstream_reader, writer))

    api_server = await asyncio.start_server(api, "127.0.0.1", 0)
    socks_server = await asyncio.start_server(socks, "127.0.0.1", 0)
    api_port = api_server.sockets[0].getsockname()[1]
    # Deleted before NO_PROXY is set: on Windows, no_proxy names the same variable.
    for variable in ("HTTP_PROXY", "http_proxy", "no_proxy"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("ARISTOTLE_WEB_URL", f"http://127.0.0.1:{api_port}")
    monkeypatch.setenv("ALL_PROXY", f"{scheme}://127.0.0.1:{socks_server.sockets[0].getsockname()[1]}")
    monkeypatch.setenv("NO_PROXY", no_proxy)
    set_api_key("test-key")
    try:
        async with AristotleRequestClient() as client:
            response = await client.get("/project")
    finally:
        api_server.close()
        socks_server.close()

    assert response.json() == {}
    assert connects == ([("127.0.0.1", api_port)] if proxied else [])


@pytest.mark.parametrize(
    ("variable", "value", "start"),
    [
        ("ALL_PROXY", "socks4://user:hunter2@proxy.corp:1080", "A socks4:// proxy from the environment"),
        ("ALL_PROXY", "socks5://user:hun/ter2@proxy.corp:1080", "The proxy settings from the environment"),
        ("NO_PROXY", "localhost,fd00::/8", "The proxy settings from the environment"),
    ],
    ids=["unsupported scheme", "malformed URL", "IPv6 range in NO_PROXY"],
)
def test_proxy_settings_the_client_cannot_use_are_refused_without_their_values(
    variable: str, value: str, start: str, monkeypatch: pytest.MonkeyPatch
):
    # Deleted before the variable is set: on Windows, no_proxy names the same variable as NO_PROXY.
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv(variable, value)

    with pytest.raises(api_request.AristotleAPIError) as info:
        api_request.new_http_client()

    assert str(info.value).startswith(start)
    assert "system" in str(info.value)
    for secret in ("hunter2", "hun", "ter2", "user", "proxy.corp"):
        assert secret not in str(info.value)
    assert info.value.__suppress_context__


def test_a_malformed_ca_bundle_is_reported_against_its_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # httpx raises a bare X509 error here; the message must name the file.
    bundle = tmp_path / "truncated.pem"
    bundle.write_text("not a certificate\n")
    monkeypatch.setenv("ARISTOTLE_CA_BUNDLE", str(bundle))

    with pytest.raises(api_request.AristotleAPIError, match="truncated.pem"):
        api_request.new_http_client()


def test_a_valid_ca_bundle_builds_a_client(monkeypatch: pytest.MonkeyPatch):
    import certifi

    monkeypatch.setenv("ARISTOTLE_CA_BUNDLE", certifi.where())

    assert api_request.new_http_client() is not None


def test_the_environment_is_reported_only_once(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    # Every request may build a client; a per-request banner would flood a CI log.
    import certifi

    monkeypatch.setenv("ARISTOTLE_CA_BUNDLE", certifi.where())

    with caplog.at_level("INFO", logger="aristotle"):
        api_request.new_http_client()
        api_request.new_http_client()

    assert caplog.text.count("certificate authority bundle") == 1


# --- Retries ---


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


def _status(status: int, detail: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail}, headers=headers)


def _page(title: str = "Hotel WiFi - Sign in") -> httpx.Response:
    """A captive portal's sign-in page, answering 200."""
    return httpx.Response(200, html=f"<html><head><title>{title}</title></head><body>Accept the terms</body></html>")


PAGE_ERROR = (
    "the answer from https://aristotle.harmonic.fun was 200 OK with text/html, not Aristotle's: a captive portal or a "
    "proxy may have answered instead."
)
"""What a request answered by _page() says, after its request and attempts."""


@pytest.fixture
def no_proxy_settings(monkeypatch):
    """No proxy from the environment or the system, which an error naming the proxy would show."""
    monkeypatch.setattr(api_request, "getproxies", dict)


TRANSIENT_FAILURES = [
    (
        lambda: httpx.ConnectError("[Errno 111] Connection refused"),
        "ConnectError on GET /project: [Errno 111] Connection refused",
    ),
    (lambda: httpx.ConnectTimeout(""), "ConnectTimeout on GET /project"),
    (lambda: httpx.ReadTimeout(""), "ReadTimeout on GET /project"),
    (
        lambda: httpx.RemoteProtocolError("Server disconnected without sending a response."),
        "RemoteProtocolError on GET /project: Server disconnected without sending a response.",
    ),
    (lambda: _status(503, "Service Unavailable"), "503 on GET /project: Service Unavailable"),
    (lambda: _status(429, "Too many requests."), "429 on GET /project: Too many requests."),
    (lambda: httpx.ProxyError("502 Bad Gateway"), "ProxyError on GET /project: 502 Bad Gateway"),
    (
        lambda: httpx.ProxyError("Proxy Server could not connect: General SOCKS server failure."),
        "ProxyError on GET /project: Proxy Server could not connect: General SOCKS server failure.",
    ),
    (
        lambda: httpx.ProxyError("Proxy Server could not connect: Host unreachable."),
        "ProxyError on GET /project: Proxy Server could not connect: Host unreachable.",
    ),
    (lambda: _page(), "200 on GET /project: text/html, not Aristotle's answer"),
    (lambda: httpx.Response(200, text="OK"), "200 on GET /project: text/plain, not Aristotle's answer"),
    (lambda: httpx.Response(200), "200 on GET /project: an empty body, not Aristotle's answer"),
    (lambda: httpx.Response(200, content=b"OK"), "200 on GET /project: an untyped body, not Aristotle's answer"),
]
TRANSIENT_IDS = [
    "connect error",
    "connect timeout",
    "read timeout",
    "remote protocol error",
    "5xx",
    "per-second 429",
    "proxy 502",
    "socks general failure",
    "socks host unreachable",
    "page",
    "text",
    "empty",
    "untyped",
]


@pytest.mark.parametrize(("failure", "summary"), TRANSIENT_FAILURES, ids=TRANSIENT_IDS)
async def test_a_transient_failure_is_retried_and_named(failure, summary, api_server, caplog):
    requests = api_server(failure(), _ok())

    async with AristotleRequestClient() as client:
        response = await client.get("/project")

    assert response.json() == {"ok": True}
    assert len(requests) == 2
    assert caplog.messages == [f"Retrying in 0.0 s after {summary}"]


async def test_a_request_that_times_out_twice_then_succeeds_returns_the_response(api_server):
    requests = api_server(httpx.ReadTimeout(""), httpx.ReadTimeout(""), _ok())

    async with AristotleRequestClient() as client:
        assert (await client.get("/project")).json() == {"ok": True}

    assert len(requests) == 3


@pytest.mark.parametrize(
    ("failure", "message", "status_code", "cause"),
    [
        (
            lambda: httpx.ReadTimeout(""),
            "ReadTimeout on GET /project/p-1 after 5 attempts over 0 s",
            None,
            httpx.ReadTimeout,
        ),
        (
            lambda: httpx.ConnectError("[Errno 111] Connection refused"),
            "ConnectError on GET /project/p-1 after 5 attempts over 0 s: [Errno 111] Connection refused",
            None,
            httpx.ConnectError,
        ),
        (
            lambda: _status(503, "Service Unavailable"),
            "GET /project/p-1 after 5 attempts over 0 s: API request failed with status 503: Service Unavailable",
            503,
            httpx.HTTPStatusError,
        ),
        (
            lambda: _status(429, "Too many requests."),
            "GET /project/p-1 after 5 attempts over 0 s: "
            "Too many requests to Aristotle at once. Wait a few seconds and try again.",
            429,
            httpx.HTTPStatusError,
        ),
        (_page, f"GET /project/p-1 after 5 attempts over 0 s: {PAGE_ERROR}", None, httpx.HTTPStatusError),
    ],
    ids=["read timeout", "connect error", "5xx", "per-second 429", "page"],
)
@pytest.mark.usefixtures("frozen_api_clock", "no_proxy_settings")
async def test_an_exhausted_retry_says_what_failed_on_which_request(failure, message, status_code, cause, api_server):
    requests = api_server(*(lambda request: failure() for _ in range(api_request.RETRY_ATTEMPTS)))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.get("/project/p-1")

    assert str(info.value) == message
    assert info.value.status_code == status_code
    assert type(info.value) is AristotleAPIError
    assert isinstance(info.value.__cause__, cause)
    assert len(requests) == api_request.RETRY_ATTEMPTS


async def test_a_project_limit_429_is_not_retried(api_server):
    requests = api_server(_status(429, QUEUE_FULL))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleProjectLimitError, match="Too many runs in progress"):
            await client.post("/project")

    assert len(requests) == 1
    assert client.rate_limit_refusals == 0


@pytest.mark.parametrize(
    ("failure", "refusals"),
    [
        (lambda request: _status(429, "Too many requests."), 2),
        (lambda request: _status(503, "Service Unavailable"), 0),
    ],
    ids=["the per-second limit", "a server error"],
)
async def test_a_client_counts_the_attempts_the_per_second_limit_refused(failure, refusals, api_server):
    api_server(_ok(), failure, failure, _ok())

    async with AristotleRequestClient() as client:
        await client.get("/project")
        assert client.rate_limit_refusals == 0
        await client.get("/project")

    assert client.rate_limit_refusals == refusals


@pytest.mark.parametrize(
    ("method", "refusal"),
    [
        ("GET", lambda request: _status(429, QUEUE_FULL)),
        ("POST", lambda request: httpx.Response(429, text="<html>429 Too Many Requests</html>")),
        ("POST", lambda request: httpx.Response(429)),
    ],
    ids=["a read with the run-limit detail", "HTML", "no body"],
)
async def test_a_429_that_is_not_the_limit_on_runs_is_sent_again(method, refusal, api_server):
    requests = api_server(refusal, _ok())

    async with AristotleRequestClient() as client:
        assert (await getattr(client, method.lower())("/project")).json() == {"ok": True}

    assert len(requests) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 410, 422])
async def test_a_client_error_is_not_retried(status, api_server):
    requests = api_server(_status(status, "No."))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.get("/project")

    if status == 403:
        assert str(info.value) == (
            "Error: this account doesn't have access (403)\n\n"
            "Hint: it may be awaiting approval or no longer in the key's organization. Contact aristotle@harmonic.fun."
        )
    else:
        assert str(info.value) == f"API request failed with status {status}: No."
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("body", "error_code"),
    [({"error": "slow_down"}, "slow_down"), ({"error": 7}, None), ({"detail": "No."}, None), ("<html>", None)],
)
async def test_an_error_response_carries_its_json_error_code(body, error_code, api_server):
    api_server(httpx.Response(400, json=body) if isinstance(body, dict) else httpx.Response(400, text=body))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.post("/cli/device/token", json={})

    assert (info.value.status_code, info.value.error_code) == (400, error_code)


SECRET_IN_THE_BODY = "a-secret-the-request-carries"


def _echoing(status: int) -> Callable[[httpx.Request], httpx.Response]:
    """An answer whose JSON ``detail`` quotes the request's body back."""
    return lambda request: httpx.Response(status, json={"detail": f"Rejected {request.content.decode()}"})


async def _post_the_secret(redact):
    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.post(
                "/cli/device/start",
                json={"previous_api_key": SECRET_IN_THE_BODY},
                idempotent=True,
                authenticated=False,
                redact=redact,
            )
    return info.value


@pytest.mark.parametrize("quiet", [False, True], ids=["retries warned", "retries at debug level"])
async def test_a_redaction_rewrites_every_retry_logged_and_the_error_raised(api_server, caplog, quiet):
    caplog.set_level(logging.DEBUG)
    api_server(_echoing(503), *[_echoing(429)] * (api_request.RETRY_ATTEMPTS - 1))
    with api_request.quietly(lambda: quiet):
        error = await _post_the_secret(lambda text: text.replace(SECRET_IN_THE_BODY, "<redacted>"))

    assert (type(error), error.status_code) == (AristotleAPIError, 429)
    assert error.__cause__ is None
    assert SECRET_IN_THE_BODY not in str(error) + caplog.text
    assert caplog.text.count("<redacted>") == api_request.RETRY_ATTEMPTS - 1
    retries = {record.levelname for record in caplog.records if record.name == api_request.logger.name}
    assert retries == {"DEBUG" if quiet else "WARNING"}


async def test_a_redaction_rewrites_the_error_that_quotes_the_body(api_server, caplog):
    caplog.set_level(logging.DEBUG)
    api_server(*[_echoing(503)] * api_request.RETRY_ATTEMPTS)
    error = await _post_the_secret(lambda text: text.replace(SECRET_IN_THE_BODY, "<redacted>"))

    assert error.status_code == 503
    assert "<redacted>" in str(error)
    assert SECRET_IN_THE_BODY not in str(error) + caplog.text


async def test_without_a_redaction_what_the_server_quotes_is_logged_and_raised_as_it_is(api_server, caplog):
    caplog.set_level(logging.DEBUG)
    api_server(*[_echoing(503)] * api_request.RETRY_ATTEMPTS)
    error = await _post_the_secret(None)

    assert SECRET_IN_THE_BODY in str(error)
    assert SECRET_IN_THE_BODY in caplog.text
    assert isinstance(error.__cause__, httpx.HTTPStatusError)


async def test_a_redaction_rewrites_a_refusals_verbose_report(api_server, caplog):
    caplog.set_level(logging.DEBUG, logger=api_request.http_logger.name)

    def blocked(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        return httpx.Response(403, html=f"<title>Blocked {body}</title>", headers={"X-Blocked-Body": body})

    api_server(blocked)
    error = await _post_the_secret(lambda text: text.replace(SECRET_IN_THE_BODY, "<redacted>"))

    assert error.status_code == 403
    assert "Response excerpt: Blocked" in caplog.text
    assert caplog.text.count("<redacted>") == 2
    assert SECRET_IN_THE_BODY not in str(error) + caplog.text


async def test_a_redacted_transport_failure_keeps_its_class_without_its_request_or_the_secret(api_server):
    api_server(httpx.ReadTimeout(f"timed out after sending {SECRET_IN_THE_BODY}"))
    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleOutcomeUnknownError) as info:
            await client.post(
                "/cli/device/token",
                json={"device_code": SECRET_IN_THE_BODY},
                authenticated=False,
                redact=lambda text: text.replace(SECRET_IN_THE_BODY, "<redacted>"),
            )

    cause = info.value.__cause__
    assert type(cause) is httpx.ReadTimeout
    assert str(cause) == "timed out after sending <redacted>"
    with pytest.raises(RuntimeError):
        cause.request
    assert SECRET_IN_THE_BODY not in str(info.value)


HEADER_REFUSAL = f"Illegal header value b'{FAKE_KEY}\\n'"
"""h11's error for a header value it won't send, which quotes the value."""


@pytest.mark.parametrize(
    ("idempotent", "error_class", "message"),
    [
        (True, AristotleAPIError, "LocalProtocolError on POST /project"),
        (False, AristotleNotProcessedError, "LocalProtocolError on POST /project. Aristotle didn't act on it."),
    ],
    ids=["idempotent", "not idempotent"],
)
@pytest.mark.parametrize("redact", [None, lambda text: text], ids=["plain", "redacted"])
async def test_a_local_protocol_error_is_named_by_its_class_alone_counts_as_unsent_and_is_not_retried(
    idempotent, error_class, message, redact, api_server, caplog
):
    requests = api_server(httpx.LocalProtocolError(HEADER_REFUSAL), _ok())

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.post("/project", json={}, idempotent=idempotent, redact=redact)

    assert (type(info.value), str(info.value)) == (error_class, message)
    cause = info.value.__cause__
    assert (type(cause), str(cause), cause.__cause__, cause.__context__) == (httpx.LocalProtocolError, "", None, None)
    assert len(requests) == 1
    assert shown_key_runs(caplog.text, FAKE_KEY) == []


async def test_a_header_h11_refuses_to_send_is_not_quoted_and_nothing_reaches_the_server(monkeypatch):
    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args) -> None:  # noqa: A002 - the base class's name
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ARISTOTLE_WEB_URL", f"http://127.0.0.1:{server.server_address[1]}")
    # A header value past get_api_key()'s checks, as one from elsewhere would be.
    monkeypatch.setattr(api_request, "get_api_key", lambda: f"{FAKE_KEY}\n")
    try:
        async with AristotleRequestClient() as client:
            with pytest.raises(AristotleNotProcessedError) as info:
                await client.post("/project", json={})
    finally:
        server.shutdown()
        server.server_close()

    assert str(info.value) == "LocalProtocolError on POST /project. Aristotle didn't act on it."
    assert str(info.value.__cause__) == ""
    assert received == []


async def test_an_unauthenticated_request_needs_no_key_and_sends_none(api_server, monkeypatch):
    requests = api_server(_ok())
    monkeypatch.setattr(api_request, "API_KEY", None)
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    monkeypatch.setattr(api_request.credentials, "read_api_key", lambda: None)

    async with AristotleRequestClient() as client:
        await client.post("/cli/device/start", json={}, authenticated=False)

    assert "X-API-Key" not in requests[0].headers


@pytest.mark.parametrize(
    "message",
    ["407 Proxy Authentication Required", "Proxy Server could not connect: Connection not allowed by ruleset."],
    ids=["proxy 407", "socks ruleset"],
)
async def test_a_transport_error_that_another_attempt_cannot_fix_is_not_retried(message, api_server):
    requests = api_server(httpx.ProxyError(message))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.get("/project")

    assert str(info.value) == f"ProxyError on GET /project: {message}"
    assert len(requests) == 1


TLS = Path(__file__).parent / "tls"
"""Two test CAs, one without the keyUsage extension, and a localhost certificate from each; see its README."""


def _strict_default_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ssl.create_default_context set VERIFY_X509_STRICT on any Python, as it does from 3.13."""
    real = ssl.create_default_context

    def strict(*args, **kwargs) -> ssl.SSLContext:
        context = real(*args, **kwargs)
        context.verify_flags |= ssl.VERIFY_X509_STRICT
        return context

    monkeypatch.setattr(ssl, "create_default_context", strict)


@contextlib.contextmanager
def _tls_server(kind: str):
    """A loopback HTTPS server with the localhost certificate of the ``kind`` CA; yields its port."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args) -> None:  # noqa: A002 - the base class's name
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(TLS / f"server-of-ca-{kind}.pem", TLS / f"server-of-ca-{kind}.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("kind", ["with-keyusage", "without-keyusage"])
async def test_a_ca_the_bundle_names_is_trusted_without_the_keyusage_extension(
    kind: str, monkeypatch: pytest.MonkeyPatch
):
    # Python 3.13's default context refuses a CA without keyUsage, as many company CAs are; forced here everywhere.
    _strict_default_context(monkeypatch)
    monkeypatch.setenv("ARISTOTLE_CA_BUNDLE", str(TLS / f"ca-{kind}.pem"))

    with _tls_server(kind) as port:
        async with api_request.new_http_client() as client:
            response = await client.get(f"https://localhost:{port}/")

    assert response.status_code == 200


@pytest.mark.parametrize("setting", ["ARISTOTLE_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR"])
def test_only_certificate_authorities_the_user_names_are_checked_without_strict_rules(
    setting: str, monkeypatch: pytest.MonkeyPatch
):
    _strict_default_context(monkeypatch)
    monkeypatch.setenv(setting, str(TLS) if setting == "SSL_CERT_DIR" else str(TLS / "ca-without-keyusage.pem"))

    assert not api_request._ssl_context(api_request._ca_bundle()).verify_flags & ssl.VERIFY_X509_STRICT

    monkeypatch.delenv(setting)
    assert api_request._ssl_context(api_request._ca_bundle()).verify_flags & ssl.VERIFY_X509_STRICT


def _certificate_failure(code: int = 18, reason: str = "self-signed certificate") -> httpx.ConnectError:
    """A ConnectError caused by a certificate that failed verification with OpenSSL's ``code``, as httpx raises it."""
    error = httpx.ConnectError(f"[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: {reason}")
    cause = ssl.SSLCertVerificationError(1, f"certificate verify failed: {reason}")
    cause.verify_code = code
    error.__cause__ = cause
    return error


def _failed_handshake(*handshakes: tuple[str, str]):
    """An outcome for api_server: TLS handshakes, each a ``(hop, host)`` traced as httpcore traces one at ``hop`` with
    ``host``, of which the last fails verification."""

    async def handshake(request: httpx.Request) -> httpx.Response:
        for hop, host in handshakes:
            await request.extensions["trace"](f"{hop}.start_tls.started", {"server_hostname": host})
        raise _certificate_failure()

    return handshake


async def test_a_certificate_this_machine_doesnt_trust_is_not_retried_and_names_the_ca_bundle(api_server):
    requests = api_server(_certificate_failure())

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.get("/project")

    assert str(info.value) == (
        "ConnectError on GET /project: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed "
        "certificate. This machine doesn't trust the server's certificate. Behind a proxy that inspects TLS, set "
        "ARISTOTLE_CA_BUNDLE to the PEM file of the proxy's certificate authority."
    )
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("setting", "held"),
    [
        ("ARISTOTLE_CA_BUNDLE", "that file must hold the proxy's certificate authority"),
        ("REQUESTS_CA_BUNDLE", "that file must hold the proxy's certificate authority"),
        (
            "SSL_CERT_DIR",
            "that folder must hold the proxy's certificate authority under its hashed name, as `openssl rehash` "
            "names it",
        ),
    ],
    ids=["ARISTOTLE_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_DIR"],
)
async def test_with_certificate_authorities_named_the_certificate_error_says_they_failed_rather_than_asking_for_them(
    setting: str, held: str, api_server, monkeypatch
):
    value = str(TLS) if setting == "SSL_CERT_DIR" else str(TLS / "ca-with-keyusage.pem")
    monkeypatch.setenv(setting, value)
    api_server(_certificate_failure())

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.get("/project")

    assert str(info.value) == (
        "ConnectError on GET /project: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed "
        f"certificate. The server's certificate didn't verify against the certificate authorities in {value}, which "
        f"{setting} names. Behind a proxy that inspects TLS, {held}."
    )


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (10, "certificate has expired"),
        (9, "certificate is not yet valid"),
        (62, "Hostname mismatch, certificate is not valid for 'aristotle.harmonic.fun'."),
    ],
    ids=["expired", "not yet valid", "hostname mismatch"],
)
@pytest.mark.parametrize("bundle", [False, True], ids=["no bundle", "a bundle"])
async def test_a_certificate_that_fails_for_its_dates_or_name_gets_no_advice_about_certificate_authorities(
    code: int, reason: str, bundle: bool, api_server, monkeypatch
):
    if bundle:
        monkeypatch.setenv("ARISTOTLE_CA_BUNDLE", str(TLS / "ca-with-keyusage.pem"))
    api_server(_certificate_failure(code, reason))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.get("/project")

    assert str(info.value) == (
        f"ConnectError on GET /project: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: {reason}"
    )


async def test_a_server_certificate_from_an_authority_the_bundle_lacks_is_blamed_on_the_bundle(monkeypatch):
    monkeypatch.setattr(api_request, "API_KEY", "test-key")
    bundle = str(TLS / "ca-without-keyusage.pem")
    monkeypatch.setenv("ARISTOTLE_CA_BUNDLE", bundle)

    with _tls_server("with-keyusage") as port:
        monkeypatch.setenv("ARISTOTLE_WEB_URL", f"https://127.0.0.1:{port}")
        async with AristotleRequestClient() as client:
            with pytest.raises(AristotleAPIError) as info:
                await client.get("/project")

    assert str(info.value).endswith(
        f"The server's certificate didn't verify against the certificate authorities in {bundle}, which "
        "ARISTOTLE_CA_BUNDLE names. Behind a proxy that inspects TLS, that file must hold the proxy's certificate "
        "authority."
    )


async def test_an_https_proxys_own_certificate_that_doesnt_verify_is_blamed_on_the_proxy_not_the_bundle(monkeypatch):
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(api_request, "API_KEY", "test-key")
    monkeypatch.setenv("ARISTOTLE_CA_BUNDLE", str(TLS / "ca-with-keyusage.pem"))
    # Never reached: httpcore checks the proxy's certificate against this machine's authorities, which lack the CA.
    monkeypatch.setenv("ARISTOTLE_WEB_URL", "https://aristotle.invalid")

    with _tls_server("with-keyusage") as port:
        monkeypatch.setenv("HTTPS_PROXY", f"https://127.0.0.1:{port}")
        async with AristotleRequestClient() as client:
            with pytest.raises(AristotleAPIError) as info:
                await client.get("/project")

    assert "The server's certificate" not in str(info.value)
    assert str(info.value).endswith(
        ". The https:// proxy's own certificate didn't verify. It is checked against this machine's trusted "
        "authorities, which ARISTOTLE_CA_BUNDLE doesn't change."
    )


async def test_behind_an_https_proxy_a_certificate_its_own_handshake_refuses_is_blamed_on_the_proxy(
    api_server, monkeypatch
):
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example:8443")
    api_server(_failed_handshake(("connection", "proxy.example")))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.get("/project")

    assert str(info.value) == (
        "ConnectError on GET /project: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed "
        "certificate. The https:// proxy's own certificate didn't verify. It is checked against this machine's trusted "
        "authorities, which ARISTOTLE_CA_BUNDLE doesn't change."
    )


async def test_behind_an_https_proxy_a_certificate_refused_through_its_tunnel_is_the_servers(api_server, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example:8443")
    # Through an https:// proxy, its own handshake comes first.
    api_server(_failed_handshake(("connection", "proxy.example"), ("proxy", "aristotle.harmonic.fun")))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.get("/project")

    assert str(info.value).endswith("set ARISTOTLE_CA_BUNDLE to the PEM file of the proxy's certificate authority.")


# httpx goes straight to the server for each of these; urllib, which used to decide whose certificate failed, doesn't
# for `localhost,*`.
@pytest.mark.parametrize("no_proxy", ["127.0.0.1", "*", "localhost,*"])
async def test_an_https_proxy_that_no_proxy_bypasses_is_not_blamed_for_the_servers_certificate(no_proxy, monkeypatch):
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setattr(api_request, "API_KEY", "test-key")
    bundle = str(TLS / "ca-without-keyusage.pem")
    monkeypatch.setenv("ARISTOTLE_CA_BUNDLE", bundle)
    # Nothing listens there: a request that went through it would fail to connect, not on a certificate.
    monkeypatch.setenv("HTTPS_PROXY", "https://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", no_proxy)

    with _tls_server("with-keyusage") as port:
        monkeypatch.setenv("ARISTOTLE_WEB_URL", f"https://127.0.0.1:{port}")
        async with AristotleRequestClient() as client:
            with pytest.raises(AristotleAPIError) as info:
                await client.get("/project")

    assert str(info.value).endswith(
        f"The server's certificate didn't verify against the certificate authorities in {bundle}, which "
        "ARISTOTLE_CA_BUNDLE names. Behind a proxy that inspects TLS, that file must hold the proxy's certificate "
        "authority."
    )


@pytest.mark.parametrize("code", [2, 7, 19, 20, 21, 30, 31])
async def test_every_code_for_a_certificate_no_trusted_authority_vouches_for_gets_the_advice(
    code, api_server, monkeypatch
):
    monkeypatch.setenv("ARISTOTLE_CA_BUNDLE", str(TLS / "ca-with-keyusage.pem"))
    api_server(_certificate_failure(code, "unable to get local issuer certificate"))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.get("/project")

    assert str(info.value).endswith("that file must hold the proxy's certificate authority.")


@pytest.mark.parametrize(
    ("failure", "message", "status_code"),
    [
        (lambda: httpx.ReadTimeout(""), "ReadTimeout on POST /task/t-1/cancel.", None),
        (lambda: httpx.WriteTimeout(""), "WriteTimeout on POST /task/t-1/cancel.", None),
        (
            lambda: httpx.RemoteProtocolError("Server disconnected without sending a response."),
            "RemoteProtocolError on POST /task/t-1/cancel: Server disconnected without sending a response.",
            None,
        ),
        (lambda: httpx.ReadError(""), "ReadError on POST /task/t-1/cancel.", None),
        (
            lambda: httpx.Response(502, text="Bad Gateway"),
            "POST /task/t-1/cancel: API request failed with status 502: Bad Gateway.",
            502,
        ),
        (
            lambda: _status(500, "Internal Server Error"),
            "POST /task/t-1/cancel: API request failed with status 500: Internal Server Error.",
            500,
        ),
        (_page, f"POST /task/t-1/cancel: {PAGE_ERROR}", None),
    ],
    ids=["read timeout", "write timeout", "remote protocol error", "read error", "502", "500", "page"],
)
@pytest.mark.usefixtures("no_proxy_settings")
async def test_a_post_that_may_have_been_processed_is_not_sent_again(failure, message, status_code, api_server):
    requests = api_server(failure())

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleOutcomeUnknownError) as info:
            await client.post("/task/t-1/cancel")

    assert str(info.value) == f"{message} Aristotle may have acted on it, so it was not sent again."
    assert info.value.status_code == status_code
    assert len(requests) == 1


@pytest.mark.parametrize(
    "failure",
    [
        lambda: httpx.ConnectError("[Errno 111] Connection refused"),
        lambda: httpx.ConnectTimeout(""),
        lambda: httpx.PoolTimeout(""),
        lambda: _status(429, "Too many requests."),
        lambda: httpx.ProxyError("503 Service Unavailable"),
    ],
    ids=["connect error", "connect timeout", "pool timeout", "per-second 429", "proxy 503"],
)
async def test_a_post_that_was_not_processed_is_sent_again(failure, api_server):
    requests = api_server(failure(), _ok())

    async with AristotleRequestClient() as client:
        assert (await client.post("/task/t-1/cancel")).json() == {"ok": True}

    assert len(requests) == 2


@pytest.mark.usefixtures("frozen_api_clock")
async def test_a_post_refused_by_the_per_second_limit_every_time_was_not_processed(api_server):
    api_server(*(lambda request: _status(429, "Too many requests.") for _ in range(api_request.RETRY_ATTEMPTS)))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.post("/project")

    assert type(info.value) is AristotleNotProcessedError
    assert info.value.status_code == 429
    assert str(info.value).startswith("POST /project after 5 attempts over 0 s: Too many requests")
    assert str(info.value).endswith(" Aristotle didn't act on it.")


@pytest.mark.usefixtures("frozen_api_clock")
@pytest.mark.parametrize(
    ("failure", "status_code", "message"),
    [
        (
            lambda request: httpx.ConnectError("[Errno 111] Connection refused"),
            None,
            "ConnectError on GET /project/p-1 after 5 attempts over 0 s: [Errno 111] Connection refused.",
        ),
        (
            lambda request: _status(502, "Bad Gateway"),
            502,
            "GET /project/p-1 after 5 attempts over 0 s: API request failed with status 502: Bad Gateway.",
        ),
        (
            lambda request: _status(429, "Too many requests."),
            429,
            "GET /project/p-1 after 5 attempts over 0 s: Too many requests to Aristotle at once. Wait a few seconds "
            "and try again.",
        ),
        (lambda request: _page(), None, f"GET /project/p-1 after 5 attempts over 0 s: {PAGE_ERROR}"),
    ],
    ids=["no connection", "server error", "per-second 429", "page"],
)
@pytest.mark.usefixtures("no_proxy_settings")
async def test_a_read_that_fails_before_sending_is_not_processed(failure, status_code, message, api_server):
    requests = api_server(*[failure] * api_request.RETRY_ATTEMPTS)

    with pytest.raises(AristotleNotProcessedError) as info, api_request.before_sending("Nothing was sent."):
        async with AristotleRequestClient() as client:
            await client.get("/project/p-1")

    assert str(info.value) == f"{message} Nothing was sent."
    assert info.value.status_code == status_code
    assert type(info.value.__cause__) is AristotleAPIError
    assert len(requests) == api_request.RETRY_ATTEMPTS


@pytest.mark.parametrize(
    "error",
    [
        api_request.AristotleNotFoundError("project", "FIFO overflow"),
        api_request.AristotleNotFoundError("project", "p-1", 404),
        AristotleAPIError("API request failed with status 401: Unauthorized", 401),
        AristotleAPIError("API request failed with status 422: Unprocessable Entity", 422),
    ],
    ids=["no match", "404", "401", "422"],
)
def test_an_answer_before_sending_is_raised_as_it_is(error):
    with pytest.raises(AristotleAPIError) as info, api_request.before_sending("Nothing was sent."):
        raise error

    assert info.value is error


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, json={"ok": True}),
        httpx.Response(200, content=b"\x1f\x8b\x08\x00", headers={"Content-Type": "application/gzip"}),
        httpx.Response(200, content=b"<html>report</html>", headers={"Content-Type": "application/octet-stream"}),
        httpx.Response(200, text='{"ok": true}'),
        httpx.Response(200, content=b'{"ok": true}'),
        httpx.Response(200, content=b"[]", headers={"Content-Type": "Text/HTML; charset=utf-8"}),
        httpx.Response(204),
    ],
    ids=["json", "archive", "file", "json as text", "untyped json", "json as html", "no content"],
)
async def test_json_or_a_file_of_another_type_is_the_answer(answer, api_server, caplog):
    requests = api_server(answer)

    async with AristotleRequestClient() as client:
        response = await client.get("/project")

    assert response.content == answer.content
    assert len(requests) == 1
    assert caplog.messages == []


@pytest.mark.parametrize(
    ("environment", "error"),
    [
        (
            {"ARISTOTLE_WEB_URL": "http://127.0.0.1:8000"},
            "the answer from http://127.0.0.1:8000 was 200 OK with text/html, not Aristotle's: a captive portal, a "
            "proxy or a wrong ARISTOTLE_WEB_URL may have answered instead.",
        ),
        (
            {"HTTPS_PROXY": "http://proxy-user:proxy-password@proxy.corp:3128"},
            "the answer from https://aristotle.harmonic.fun through the proxy proxy.corp was 200 OK with text/html, "
            "not Aristotle's: a captive portal or a proxy may have answered instead.",
        ),
    ],
    ids=["another origin", "through a proxy"],
)
async def test_an_answer_that_isnt_aristotles_says_where_it_came_from(environment, error, api_server, monkeypatch):
    for name in (*api_request.PROXY_VARIABLES, "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(api_request, "getproxies", getproxies_environment)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    api_server(_page())

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleOutcomeUnknownError) as info:
            await client.post("/project")

    assert str(info.value) == f"POST /project: {error} Aristotle may have acted on it, so it was not sent again."


async def test_what_a_page_that_isnt_aristotles_says_is_never_shown(api_server, caplog):
    caplog.set_level(logging.DEBUG)
    api_server(_page("Sign in to continue"))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleOutcomeUnknownError) as info:
            await client.post("/project")

    assert "Sign in" not in str(info.value) + caplog.text


def _key_busy(retry_after: str = "1") -> httpx.Response:
    return _status(
        409, "A request with this Idempotency-Key is still in progress.", headers={"Retry-After": retry_after}
    )


async def test_a_keyed_post_refused_while_its_first_copy_runs_is_sent_again_with_the_same_key(api_server, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(api_request.asyncio, "sleep", sleep)
    requests = api_server(_key_busy("7"), _ok())

    async with AristotleRequestClient() as client:
        assert (await client.post("/project", idempotency_key="k-1")).json() == {"ok": True}

    assert [request.headers["Idempotency-Key"] for request in requests] == ["k-1", "k-1"]
    assert sleep.await_args.args[0] >= 7


@pytest.mark.usefixtures("frozen_api_clock")
async def test_a_keyed_post_whose_first_copy_never_finishes_is_an_unknown_outcome(api_server, monkeypatch):
    monkeypatch.setattr(api_request.asyncio, "sleep", AsyncMock())
    requests = api_server(*(lambda request: _key_busy() for _ in range(api_request.RETRY_ATTEMPTS)))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleOutcomeUnknownError) as info:
            await client.post("/project", idempotency_key="k-1")

    assert info.value.status_code == 409
    assert str(info.value) == (
        "POST /project after 5 attempts over 0 s: an earlier copy sent under its Idempotency-Key was still in "
        "progress, and may yet act."
    )
    assert len(requests) == api_request.RETRY_ATTEMPTS


@pytest.mark.parametrize(
    "last",
    [lambda: _status(429, "Too many requests."), lambda: httpx.ConnectError("[Errno 111] Connection refused")],
    ids=["per-second 429", "connect error"],
)
async def test_a_keyed_post_refused_as_busy_then_unsent_is_still_an_unknown_outcome(last, api_server, monkeypatch):
    monkeypatch.setattr(api_request.asyncio, "sleep", AsyncMock())
    busy = [lambda request: _key_busy() for _ in range(api_request.RETRY_ATTEMPTS - 1)]
    api_server(*busy, lambda request: last())

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleOutcomeUnknownError, match="an earlier copy sent under its Idempotency-Key"):
            await client.post("/project", idempotency_key="k-1")


async def test_a_409_without_a_key_is_not_sent_again(api_server):
    requests = api_server(_status(409, "This question is already closed."))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.post("/question/q-1/answer", json={"answer": "Yes"})

    assert info.value.status_code == 409
    assert len(requests) == 1


async def test_a_keyed_409_that_isnt_the_busy_keys_is_final(api_server):
    requests = api_server(_status(409, "Your organization changed. Reload and try again."))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.post("/project", idempotency_key="k-1")

    assert type(info.value) is AristotleAPIError
    assert str(info.value) == "API request failed with status 409: Your organization changed. Reload and try again."
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("status", "detail", "error"),
    [
        (
            402,
            OUT_OF_CREDITS,
            f"Your organization needs at least 1.00 Credit to start a run. {BUY_CREDITS}",
        ),
        (409, SIGNUP_GRANT_PENDING, SIGNUP_GRANT_PENDING["message"]),
    ],
    ids=["out of credits", "still setting up"],
)
async def test_a_run_billing_refuses_did_nothing_and_is_not_sent_again(status, detail, error, api_server):
    requests = api_server(httpx.Response(status, json={"detail": detail}))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleNotProcessedError) as info:
            await client.post("/project", idempotency_key="k-1")

    assert (str(info.value), info.value.status_code, info.value.error_code) == (error, status, detail["code"])
    assert len(requests) == 1


async def test_a_402_without_a_message_says_where_to_buy_credits_at_the_web_origin(api_server, monkeypatch):
    monkeypatch.setenv("ARISTOTLE_WEB_URL", "http://127.0.0.1:8000")
    api_server(httpx.Response(402, text="Payment Required"))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleNotProcessedError) as info:
            await client.post("/project", idempotency_key="k-1")

    assert str(info.value) == (
        "Your organization needs credits to start a run. An organization admin can buy credits at "
        "http://127.0.0.1:8000/settings/billing."
    )


@pytest.mark.usefixtures("frozen_api_clock")
async def test_a_run_billing_could_not_check_is_sent_again_and_did_nothing(api_server, caplog):
    unavailable = [
        lambda request: httpx.Response(503, json={"detail": BILLING_UNAVAILABLE})
    ] * api_request.RETRY_ATTEMPTS
    requests = api_server(*unavailable)

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleNotProcessedError) as info:
            await client.post("/project", idempotency_key="k-1")

    assert str(info.value) == (
        "POST /project after 5 attempts over 0 s: The credit balance is temporarily unavailable. Aristotle didn't act "
        "on it."
    )
    assert (info.value.status_code, info.value.error_code) == (503, "BILLING_UNAVAILABLE")
    assert [request.headers["Idempotency-Key"] for request in requests] == ["k-1"] * api_request.RETRY_ATTEMPTS
    assert caplog.messages[0].endswith(" after 503 on POST /project: The credit balance is temporarily unavailable.")


async def test_a_run_billing_could_not_check_at_first_goes_through_on_a_retry(api_server):
    requests = api_server(httpx.Response(503, json={"detail": BILLING_UNAVAILABLE}), _ok())

    async with AristotleRequestClient() as client:
        assert (await client.post("/project", idempotency_key="k-1")).json() == {"ok": True}

    assert len(requests) == 2


@pytest.mark.parametrize(
    ("detail", "error", "error_code"),
    [
        ({"code": "LIMIT", "message": "Wait for the export to finish."}, "Wait for the export to finish.", "LIMIT"),
        ({"code": "LIMIT"}, "API request failed with status 400: Bad Request", "LIMIT"),
        ({"message": 7}, "API request failed with status 400: Bad Request", None),
    ],
    ids=["its message", "no message", "a message that isn't text"],
)
async def test_a_detail_object_gives_its_message_alone_and_its_code(detail, error, error_code, api_server):
    api_server(httpx.Response(400, json={"detail": detail}))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.get("/project")

    assert (str(info.value), info.value.error_code) == (error, error_code)


async def test_an_idempotent_post_is_retried_like_a_get(api_server):
    requests = api_server(httpx.ReadTimeout(""), _ok())

    async with AristotleRequestClient() as client:
        assert (await client.post("/project/p-1/rename", json={"name": "n"}, idempotent=True)).json() == {"ok": True}

    assert len(requests) == 2


@pytest.mark.parametrize(("jitter", "delays"), [(min, [1, 2, 4, 8]), (max, [2, 4, 8, 16])], ids=["least", "most"])
async def test_retries_back_off_exponentially_with_jitter(jitter, delays, api_server, monkeypatch):
    monkeypatch.setattr(api_request, "RETRY_BACKOFF_SECONDS", 1)
    monkeypatch.setattr(api_request.random, "uniform", lambda low, high: jitter(low, high))
    sleep = AsyncMock()
    monkeypatch.setattr(api_request.asyncio, "sleep", sleep)
    api_server(*(lambda request: _status(429, "Too many requests.") for _ in range(4)), _ok())

    async with AristotleRequestClient() as client:
        await client.get("/project")

    # Even the shortest wait outlasts the clock second whose request count refused the request.
    assert [call.args[0] for call in sleep.await_args_list] == delays


_NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW


@pytest.mark.parametrize(
    "retry_after", ["30", format_datetime(_NOW + timedelta(seconds=30), usegmt=True)], ids=["seconds", "date"]
)
async def test_a_retry_waits_at_least_as_long_as_retry_after_asks(retry_after, api_server, monkeypatch):
    monkeypatch.setattr(api_request, "RETRY_BACKOFF_SECONDS", 1)
    monkeypatch.setattr(api_request.random, "uniform", lambda low, high: low)
    monkeypatch.setattr(api_request, "datetime", _FrozenDatetime)
    sleep = AsyncMock()
    monkeypatch.setattr(api_request.asyncio, "sleep", sleep)
    api_server(_status(503, "Service unavailable", {"Retry-After": retry_after}), _ok())

    async with AristotleRequestClient() as client:
        await client.get("/project")

    assert [call.args[0] for call in sleep.await_args_list] == [30]


@pytest.mark.parametrize(
    "value",
    [
        "",
        "soon",
        "²",
        "-5",
        "1.5",
        "Wed, 32 Oct 2015 07:28:00 GMT",
        "Wed, 21 Oct 99999999999999999999 07:28:00 GMT",
        "Wed, 3 Apr 2002 12.34.56.78+0800",
    ],
)
def test_a_retry_after_that_is_neither_seconds_nor_a_date_counts_as_none(value):
    assert api_request._retry_after(httpx.Response(503, headers={"Retry-After": value.encode()})) is None


async def test_a_retry_after_shorter_than_the_backoff_keeps_the_backoff(api_server, monkeypatch):
    monkeypatch.setattr(api_request, "RETRY_BACKOFF_SECONDS", 1)
    monkeypatch.setattr(api_request.random, "uniform", lambda low, high: low)
    sleep = AsyncMock()
    monkeypatch.setattr(api_request.asyncio, "sleep", sleep)
    refused = [lambda request: _status(429, "Too many requests.", {"Retry-After": "1"}) for _ in range(3)]
    api_server(*refused, _ok())

    async with AristotleRequestClient() as client:
        await client.get("/project")

    assert [call.args[0] for call in sleep.await_args_list] == [1, 2, 4]


async def test_a_retry_after_past_the_deadline_gives_up_at_once(api_server, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(api_request.asyncio, "sleep", sleep)
    requests = api_server(_status(503, "Service unavailable", {"Retry-After": "120"}))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.get("/project")

    assert len(requests) == 1
    assert info.value.status_code == 503
    sleep.assert_not_awaited()


async def test_a_per_second_refusal_past_the_deadline_says_how_long_aristotle_asked_to_wait(api_server, monkeypatch):
    monkeypatch.setattr(api_request.asyncio, "sleep", AsyncMock())
    api_server(_status(429, "Too many requests.", {"Retry-After": "600"}))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.get("/project")

    assert info.value.status_code == 429
    assert str(info.value) == (
        "GET /project: Too many requests to Aristotle at once. Aristotle asked to wait 10 minutes before trying again."
    )


async def test_a_stream_refused_for_a_minute_says_so(monkeypatch):
    refused = httpx.Response(429, json={"detail": "Too many requests."}, headers={"Retry-After": "60"})
    requests, sleep = _serve(monkeypatch, [refused])

    with pytest.raises(AristotleAPIError) as info:
        [event async for event in AristotleRequestClient().stream_sse("/events")]

    assert str(info.value) == (
        "Too many requests to Aristotle at once. Aristotle asked to wait 60 seconds before trying again."
    )
    sleep.assert_not_awaited()


async def test_a_retry_after_too_long_to_count_reads_as_none(api_server, monkeypatch):
    monkeypatch.setattr(api_request.asyncio, "sleep", AsyncMock())
    api_server(_status(429, "Too many requests.", {"Retry-After": "9" * 400}))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.get("/project")

    assert str(info.value) == "GET /project: Too many requests to Aristotle at once. Wait a few seconds and try again."


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(1, "1 second"), (45.2, "46 seconds"), (119, "119 seconds"), (120, "2 minutes"), (601, "11 minutes")],
)
def test_a_wait_reads_in_seconds_then_minutes(seconds: float, text: str):
    assert api_request._wait_text(seconds) == text


async def test_requests_from_one_process_start_at_most_five_a_second(api_server, monkeypatch):
    now = [0.0]
    started: list[float] = []
    monkeypatch.setattr(api_request, "REQUEST_INTERVAL_SECONDS", 0.2)
    monkeypatch.setattr(api_request, "time", Mock(monotonic=lambda: now[0]))

    async def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(api_request.asyncio, "sleep", sleep)

    def answer(request):
        started.append(now[0])
        now[0] += 0.05
        return _ok()

    api_server(*[answer] * 5)
    async with AristotleRequestClient() as client:
        await client.get("/project")
        await client.get("/project")
    # Another client in the same process keeps the pace, and so do requests sent at once.
    async with AristotleRequestClient() as client:
        await asyncio.gather(client.get("/project"), client.get("/project"))
    now[0] += 5
    async with AristotleRequestClient() as client:
        await client.get("/project")

    assert started == pytest.approx([0, 0.2, 0.4, 0.6, 5.65])


async def test_requests_waiting_for_a_slot_take_them_in_the_order_they_asked(api_server, monkeypatch):
    monkeypatch.setattr(api_request, "REQUEST_INTERVAL_SECONDS", 0.01)
    requests = api_server(*[_ok() for _ in range(20)])

    async with AristotleRequestClient() as client:
        await asyncio.gather(*(client.get(f"/project/{i}") for i in range(20)))

    assert [request.url.path.rsplit("/", 1)[1] for request in requests] == [str(i) for i in range(20)]


def test_an_event_loop_is_not_kept_once_its_requests_are_done(api_server, monkeypatch):
    monkeypatch.setattr(api_request, "REQUEST_INTERVAL_SECONDS", 0.01)
    api_server(*[_ok() for _ in range(3)])
    loops: list[weakref.ref] = []

    async def three_at_once():
        loops.append(weakref.ref(asyncio.get_running_loop()))
        async with AristotleRequestClient() as client:
            await asyncio.gather(*(client.get("/project") for _ in range(3)))

    asyncio.run(three_at_once())
    gc.collect()

    assert loops[0]() is None


def _abandon_a_task_whose_cleanup_sends_a_request() -> None:
    """Leave a task pending on a loop that is then closed; it holds itself, so only the garbage collector frees it."""

    async def sleep_then_clean_up():
        me = asyncio.current_task()  # noqa: F841 - the cycle that leaves freeing it to the garbage collector
        try:
            await asyncio.sleep(3600)
        finally:
            await AristotleRequestClient().get("/cleanup")

    loop = asyncio.new_event_loop()
    loop.create_task(sleep_then_clean_up())
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()


def test_a_request_from_a_cleanup_the_garbage_collector_runs_does_not_hold_up_the_queue(api_server, monkeypatch):
    monkeypatch.setattr(api_request, "REQUEST_INTERVAL_SECONDS", 0.05)
    requests = api_server(*[_ok() for _ in range(4)])

    async def three_requests_with_a_collection_while_one_waits():
        async with AristotleRequestClient() as client:
            await client.get("/project/1")
            second = asyncio.ensure_future(client.get("/project/2"))
            await asyncio.sleep(0.01)  # The second now holds the loop's queue, waiting for its slot.
            gc.collect()  # The abandoned task's cleanup runs here, on this loop, and sends its request.
            third = asyncio.ensure_future(client.get("/project/3"))
            await asyncio.wait_for(asyncio.gather(second, third), 10)

    gc.disable()  # So that the abandoned task is freed only by the collection above.
    try:
        _abandon_a_task_whose_cleanup_sends_a_request()
        asyncio.run(three_requests_with_a_collection_while_one_waits())
    finally:
        gc.enable()

    assert sorted(request.url.path.rsplit("/", 1)[1] for request in requests) == ["1", "2", "3", "cleanup"]


class _Suspends(httpx.AsyncBaseTransport):
    """A transport that yields to the event loop once before it answers."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        return _ok()


# The collected cleanup suspends while closing, which Python reports as an unraisable RuntimeError.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_a_request_from_a_cleanup_the_garbage_collector_runs_does_not_cancel_the_task_it_ran_in(monkeypatch):
    monkeypatch.setattr(api_request, "CONNECTION_DEADLINE_SECONDS", 0.05)
    _use_transport(monkeypatch, _Suspends())

    async def collect_then_wait() -> None:
        gc.collect()  # The abandoned task's cleanup runs here, in this task, and sends its request.
        await asyncio.sleep(0.3)

    gc.disable()  # So that the abandoned task is freed only by the collection above.
    try:
        _abandon_a_task_whose_cleanup_sends_a_request()
        asyncio.run(collect_then_wait())
    finally:
        gc.enable()


# Collecting the abandoned requests reports their lock's wake-up of the closed loop as an unraisable RuntimeError.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_a_loop_closed_with_requests_still_waiting_is_not_kept(api_server, monkeypatch):
    monkeypatch.setattr(api_request, "REQUEST_INTERVAL_SECONDS", 60)
    api_server(*[_ok() for _ in range(4)])
    loop = asyncio.new_event_loop()
    client = AristotleRequestClient()
    for _ in range(3):
        loop.create_task(client.get("/project"))
    # The first request goes; the other two wait for their slots when the loop is closed under them.
    loop.run_until_complete(asyncio.sleep(0.1))
    loop.close()
    closed = weakref.ref(loop)
    del loop, client
    monkeypatch.setattr(api_request, "REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(api_request, "_next_request_at", 0.0)
    # They are collected just as a request on another loop takes its queue.
    real_lock = asyncio.Lock

    def lock_after_collecting():
        gc.collect()
        return real_lock()

    monkeypatch.setattr(api_request.asyncio, "Lock", lock_after_collecting)

    async def one_more():
        async with AristotleRequestClient() as client:
            return await client.get("/project")

    assert asyncio.run(one_more()).json() == {"ok": True}
    assert closed() is None


async def test_the_retry_deadline_counts_from_the_first_attempt_not_from_the_wait_for_its_slot(api_server, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(api_request, "time", Mock(monotonic=lambda: now[0]))

    async def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(api_request.asyncio, "sleep", sleep)
    # Requests started ahead of this one hold the slots of the next 100 s, more than RETRY_DEADLINE_SECONDS.
    monkeypatch.setattr(api_request, "_next_request_at", 100.0)
    requests = api_server(httpx.ConnectError("refused"), _ok())

    async with AristotleRequestClient() as client:
        assert (await client.get("/project")).json() == {"ok": True}

    assert len(requests) == 2


async def test_no_retry_starts_after_the_deadline(api_server, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(api_request, "RETRY_BACKOFF_SECONDS", 1)
    monkeypatch.setattr(api_request.random, "uniform", lambda low, high: low)
    monkeypatch.setattr(api_request, "time", Mock(monotonic=lambda: now[0]))

    async def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(api_request.asyncio, "sleep", sleep)

    def time_out(request):
        now[0] += 30
        return httpx.ReadTimeout("")

    # Attempts start at 0, 31 and 63 s; a fourth would start at 97 s, after RETRY_DEADLINE_SECONDS.
    requests = api_server(*[time_out] * api_request.RETRY_ATTEMPTS)

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.get("/project")

    assert len(requests) == 3
    assert str(info.value) == "ReadTimeout on GET /project after 3 attempts over 93 s"


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (lambda: httpx.ReadTimeout(""), "ReadTimeout on GET /project after 5 attempts over 0 s"),
        (
            lambda: httpx.Response(502),
            "GET /project after 5 attempts over 0 s: API request failed with status 502: Bad Gateway",
        ),
    ],
    ids=["read timeout", "502 without a body"],
)
@pytest.mark.usefixtures("frozen_api_clock")
async def test_a_failure_names_the_request_without_its_query_origin_or_key(failure, message, api_server, caplog):
    api_server(*(lambda request: failure() for _ in range(api_request.RETRY_ATTEMPTS)))

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.get("/project", params={"pagination_key": "k-0", "limit": 1})

    assert str(info.value) == message
    for text in [str(info.value), *caplog.messages]:
        assert "GET /project" in text
        for leaked in ("k-0", "limit", "test-key", "aristotle.harmonic.fun", "/api/v3"):
            assert leaked not in text


async def test_a_connection_gets_less_time_to_open_than_a_response_has_to_arrive(monkeypatch):
    timeouts: list[httpx.Timeout] = []
    real_client = httpx.AsyncClient

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/project"):
            return _ok()
        return httpx.Response(200, content=_event("e-1"), headers=EVENT_STREAM)

    def client(**kwargs):
        timeouts.append(kwargs["timeout"])
        return real_client(transport=httpx.MockTransport(answer), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    set_api_key("test-key")

    async with AristotleRequestClient() as request_client:
        await request_client.get("/project")
        [event async for event in request_client.stream_sse("/events")]

    request, stream = timeouts
    assert (request.connect, request.read) == (api_request.CONNECT_TIMEOUT_SECONDS, api_request.DEFAULT_TIMEOUT_SECONDS)
    assert (stream.connect, stream.read) == (
        api_request.CONNECT_TIMEOUT_SECONDS,
        api_request.STREAM_READ_TIMEOUT_SECONDS,
    )
    assert api_request.CONNECT_TIMEOUT_SECONDS < api_request.DEFAULT_TIMEOUT_SECONDS


# --- Retries over a real socket, so httpx raises its own exception types and messages ---


async def _read_request(reader: asyncio.StreamReader) -> bytes:
    """One HTTP/1.1 request, read in full so closing the connection afterwards sends no reset."""
    head = await reader.readuntil(b"\r\n\r\n")
    length = re.search(rb"(?im)^content-length: *(\d+)", head)
    return head + (await reader.readexactly(int(length.group(1))) if length else b"")


@contextlib.asynccontextmanager
async def _loopback_server(monkeypatch, answer: str):
    """A server the SDK is pointed at that reads each request, then closes ("close") or never answers ("hang")."""
    received: list[bytes] = []
    released = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await _read_request(reader))
        if answer == "hang":
            await released.wait()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv("ARISTOTLE_WEB_URL", f"http://127.0.0.1:{port}")
    for variable in ("HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(variable, raising=False)
    set_api_key("test-key")
    try:
        yield received
    finally:
        released.set()
        server.close()


@pytest.fixture
def short_timeouts(monkeypatch):
    """Every client times out after 0.2 s, and a request gets two attempts."""
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(**{**kwargs, "timeout": 0.2}))
    monkeypatch.setattr(api_request, "RETRY_ATTEMPTS", 2)


async def test_a_refused_connection_is_retried_then_reported_with_its_reason(monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("ARISTOTLE_WEB_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(api_request, "RETRY_ATTEMPTS", 2)
    set_api_key("test-key")

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await client.get("/project")

    assert re.fullmatch(r"ConnectError on GET /project after 2 attempts over \d+ s: .+", str(info.value))
    assert isinstance(info.value.__cause__, httpx.ConnectError)


async def test_a_dropped_connection_is_retried_for_a_get_and_not_for_a_post(monkeypatch):
    dropped = "Server disconnected without sending a response."

    async with _loopback_server(monkeypatch, "close") as received:
        async with AristotleRequestClient() as client:
            with pytest.raises(AristotleAPIError) as got:
                await client.get("/project")
            assert len(received) == api_request.RETRY_ATTEMPTS
            with pytest.raises(AristotleOutcomeUnknownError) as posted:
                await client.post("/project", data={"body": "{}"}, files=[("input", ("a.tar.gz", b"x", "x"))])
            assert len(received) == api_request.RETRY_ATTEMPTS + 1

    assert re.fullmatch(rf"RemoteProtocolError on GET /project after 5 attempts over \d+ s: {dropped}", str(got.value))
    assert str(posted.value) == (
        f"RemoteProtocolError on POST /project: {dropped} Aristotle may have acted on it, so it was not sent again."
    )


@pytest.mark.usefixtures("short_timeouts")
async def test_a_response_that_never_comes_is_retried_for_a_get_and_not_for_a_post(monkeypatch):
    async with _loopback_server(monkeypatch, "hang") as received:
        async with AristotleRequestClient() as client:
            with pytest.raises(AristotleAPIError) as got:
                await client.get("/project")
            assert len(received) == 2
            with pytest.raises(AristotleOutcomeUnknownError) as posted:
                await client.post("/project", data={"body": "{}"})
            assert len(received) == 3

    # httpx's ReadTimeout has an empty message; the type and the request name it.
    assert str(got.value.__cause__) == ""
    assert re.fullmatch(r"ReadTimeout on GET /project after 2 attempts over \d+ s", str(got.value))
    assert (
        str(posted.value) == "ReadTimeout on POST /project. Aristotle may have acted on it, so it was not sent again."
    )


class _Stalled(httpx.AsyncBaseTransport):
    """A transport whose requests never get a connection; counts them."""

    def __init__(self) -> None:
        self.requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _ConnectedThenSlow(httpx.AsyncBaseTransport):
    """A transport that opens its connection at once, as httpcore traces it, then answers after ``seconds``."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.extensions["trace"]("http11.send_request_headers.started", {})
        await asyncio.sleep(self.seconds)
        return _ok()


def _use_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport) -> None:
    monkeypatch.setattr(api_request, "API_KEY", "test-key")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))


@pytest.mark.parametrize("method", ["get", "post"])
async def test_an_attempt_whose_connection_does_not_open_in_time_is_retried_as_unsent(method, monkeypatch):
    monkeypatch.setattr(api_request, "CONNECTION_DEADLINE_SECONDS", 0.05)
    transport = _Stalled()
    _use_transport(monkeypatch, transport)

    async with AristotleRequestClient() as client:
        with pytest.raises(AristotleAPIError) as info:
            await getattr(client, method)("/project")

    # A GET is safe to send again anyway; a POST says that it did nothing.
    assert type(info.value) is (AristotleNotProcessedError if method == "post" else AristotleAPIError)
    suffix = r"\. Aristotle didn't act on it\." if method == "post" else ""
    assert re.fullmatch(
        rf"ConnectTimeout on {method.upper()} /project after 5 attempts over \d+ s: No connection within 0\.05 s{suffix}",
        str(info.value),
    )
    assert transport.requests == api_request.RETRY_ATTEMPTS


async def test_the_connection_deadline_ends_once_the_request_is_being_written(monkeypatch):
    monkeypatch.setattr(api_request, "CONNECTION_DEADLINE_SECONDS", 0.05)
    _use_transport(monkeypatch, _ConnectedThenSlow(0.2))

    async with AristotleRequestClient() as client:
        assert (await client.get("/project")).json() == {"ok": True}


async def test_an_interrupt_during_an_attempt_is_not_taken_for_the_connection_deadline(monkeypatch):
    _use_transport(monkeypatch, _Stalled())

    async with AristotleRequestClient() as client:
        request = asyncio.ensure_future(client.get("/project"))
        await asyncio.sleep(0.05)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request


@contextlib.asynccontextmanager
async def _socks5_proxy(mode: str, monkeypatch: pytest.MonkeyPatch):
    """A local SOCKS5 proxy, set as ALL_PROXY, that never reaches a server; yields the number of connections it took.

    ``stall`` never answers the greeting, ``drop`` closes after the CONNECT request, and ``fail`` and ``ruleset`` refuse
    it with a general failure and "not allowed by ruleset".
    """
    connections = [0]
    # httpcore leaves a socket whose handshake it gave up on to the garbage collector, so a stalled reply waits for this.
    released = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections[0] += 1
        try:
            await reader.read(262)
            if mode == "stall":
                await released.wait()
                return
            writer.write(b"\x05\x00")
            await writer.drain()
            await reader.read(512)
            if mode != "drop":
                reply = {"fail": b"\x01", "ruleset": b"\x02"}[mode]
                writer.write(b"\x05" + reply + b"\x00\x01" + bytes(6))
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALL_PROXY", f"socks5h://127.0.0.1:{server.sockets[0].getsockname()[1]}")
    monkeypatch.setattr(api_request, "API_KEY", "test-key")
    try:
        yield connections
    finally:
        released.set()
        server.close()
        await server.wait_closed()


async def test_a_socks_proxy_that_never_answers_ends_each_attempt_at_the_connection_deadline(monkeypatch):
    monkeypatch.setattr(api_request, "CONNECTION_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(api_request, "RETRY_ATTEMPTS", 2)

    async with _socks5_proxy("stall", monkeypatch) as connections:
        async with AristotleRequestClient() as client:
            with pytest.raises(AristotleAPIError) as info:
                await client.get("/project")

    assert re.fullmatch(
        r"ConnectTimeout on GET /project after 2 attempts over \d+ s: No connection within 0\.2 s", str(info.value)
    )
    assert connections[0] == 2


async def test_an_expired_handshake_does_not_hold_a_pool_slot_from_the_next_attempt(monkeypatch):
    monkeypatch.setattr(api_request, "CONNECTION_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(api_request, "RETRY_ATTEMPTS", 2)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(limits=httpx.Limits(max_connections=1), **kwargs)
    )

    async with _socks5_proxy("stall", monkeypatch) as connections:
        async with AristotleRequestClient() as client:
            with pytest.raises(AristotleAPIError, match="No connection within"):
                await client.get("/project")

    # httpcore keeps a cancelled SOCKS handshake in its pool; the second attempt must still reach the proxy.
    assert connections[0] == 2


async def test_an_http_proxy_that_never_answers_connect_is_a_connection_timeout_not_a_lost_request(monkeypatch):
    monkeypatch.setattr(api_request, "CONNECTION_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(api_request, "RETRY_ATTEMPTS", 2)
    connections = [0]
    released = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections[0] += 1
        await reader.readuntil(b"\r\n\r\n")
        await released.wait()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    for name in ("https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
    monkeypatch.setattr(api_request, "API_KEY", "test-key")
    try:
        async with AristotleRequestClient() as client:
            with pytest.raises(AristotleAPIError) as info:
                await client.post("/project")
    finally:
        released.set()
        server.close()
        await server.wait_closed()

    # Not AristotleOutcomeUnknownError: nothing went through a tunnel that never opened.
    assert type(info.value) is AristotleNotProcessedError
    assert re.fullmatch(
        r"ConnectTimeout on POST /project after 2 attempts over \d+ s: No connection within 0\.2 s\. "
        r"Aristotle didn't act on it\.",
        str(info.value),
    )
    assert connections[0] == 2


@pytest.mark.parametrize(
    ("mode", "attempts", "message"),
    [
        (
            "drop",
            5,
            r"ProxyError on POST /project after 5 attempts over \d+ s: The SOCKS proxy sent a reply it couldn't read, "
            r"or closed the connection: Malformed reply\.",
        ),
        (
            "fail",
            5,
            r"ProxyError on POST /project after 5 attempts over \d+ s: Proxy Server could not connect: General SOCKS "
            r"server failure\.",
        ),
        (
            "ruleset",
            1,
            r"ProxyError on POST /project: Proxy Server could not connect: Connection not allowed by ruleset\.",
        ),
    ],
)
async def test_a_socks_proxy_that_drops_or_refuses_the_tunnel_is_retried_like_a_failure_to_connect(
    mode, attempts, message, monkeypatch
):
    async with _socks5_proxy(mode, monkeypatch) as connections:
        async with AristotleRequestClient() as client:
            with pytest.raises(AristotleAPIError) as info:
                await client.post("/project")

    # Not AristotleOutcomeUnknownError: no request went through a tunnel that never opened.
    assert type(info.value) is AristotleNotProcessedError
    assert re.fullmatch(message + r" Aristotle didn't act on it\.", str(info.value))
    assert connections[0] == attempts


async def test_an_event_stream_through_a_socks_proxy_that_drops_it_gives_up_with_a_proxy_error(monkeypatch):
    monkeypatch.setattr(api_request, "STREAM_RECONNECT_SECONDS", 0)

    async with _socks5_proxy("drop", monkeypatch) as connections:
        with pytest.raises(httpx.ProxyError, match="The SOCKS proxy sent a reply it couldn't read"):
            [event async for event in AristotleRequestClient().stream_sse("/events", max_retries=2)]

    assert connections[0] == 2


async def test_an_event_stream_through_a_socks_proxy_that_never_answers_gives_up_at_the_deadline(monkeypatch):
    monkeypatch.setattr(api_request, "STREAM_RECONNECT_SECONDS", 0)
    monkeypatch.setattr(api_request, "CONNECTION_DEADLINE_SECONDS", 0.2)

    async with _socks5_proxy("stall", monkeypatch) as connections:
        with pytest.raises(httpx.ConnectTimeout):
            [event async for event in AristotleRequestClient().stream_sse("/events", max_retries=2)]

    assert connections[0] == 2


def test_the_connection_deadline_is_under_the_read_timeout():
    # Else a proxy's unanswered CONNECT could end as a ReadTimeout, which a POST reports as possibly sent.
    assert api_request.CONNECTION_DEADLINE_SECONDS < api_request.DEFAULT_TIMEOUT_SECONDS


def test_an_uploads_reply_may_take_as_long_as_the_upload_takes_to_reach_the_server_up_to_ten_minutes():
    assert api_request.upload_timeout(0) == httpx.Timeout(30, connect=10)
    # 3 MB at 16 KB/s, all of which can still be in buffers once the last byte is written.
    assert api_request.upload_timeout(3 * 1024 * 1024) == httpx.Timeout(30, connect=10, read=30 + 192)
    assert api_request.upload_timeout(100 * 1024 * 1024) == httpx.Timeout(30, connect=10, read=600)


async def test_a_post_has_the_timeouts_it_is_given_or_else_the_clients(api_server):
    requests = api_server(httpx.Response(200, json={}), httpx.Response(200, json={}))

    async with AristotleRequestClient() as client:
        await client.post("/project", timeout=httpx.Timeout(5, read=60))
        await client.post("/project")

    assert [request.extensions["timeout"] for request in requests] == [
        {"connect": 5, "read": 60, "write": 5, "pool": 5},
        {"connect": 10, "read": 30, "write": 30, "pool": 30},
    ]
