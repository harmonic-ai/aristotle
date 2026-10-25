"""Tests for aristotlelib.project module."""

import asyncio
import copy
import errno
import gc
import gzip
import io
import json
import logging
import os
import pickle
import re
import socket
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import uuid
from contextlib import ExitStack, nullcontext
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch
from urllib.parse import parse_qs

import httpx
import pytest

from aristotlelib import api_request, local_file_utils
from aristotlelib.agent_task import ENDED_BADLY, AgentTask, TaskStatus, ending_notice
from aristotlelib.api_request import AristotleAPIError, AristotleNotFoundError, set_api_key
from aristotlelib.cli.question_flow import DEFAULT_DEADLINE_SECONDS, FlowResult, pending_footer, read_stdin_line
from aristotlelib.declaration import Declaration, DeclarationSource, DeclarationStatus, DeclarationType
from aristotlelib.event import Event, EventType
from aristotlelib.local_file_utils import SECRET_FILE_PATTERNS, LeanProjectError
from aristotlelib.project import (
    AgentQuestionsSetting,
    AmbiguousProjectError,
    ArchiveError,
    Project,
    ProjectHasNoFilesError,
    ProjectStatus,
    _folder_name,
    continuation_uploads,
    download_artifacts_hint,
    file_path_error,
    lookup_at_terminal,
    same_project_id,
    saved_path,
    validate_destination,
)
from aristotlelib.question import Question
from tests.ci.conftest import git, latin1_name
from tests.conftest import fast_api_clock, fstat_on_another_device, hard_link, swapped_for_a_link


async def test_project_from_id():
    """Test creating a Project from an ID."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "project_id": "test-123",
        "status": 2,  # IDLE
        "created_at": "2024-01-01T00:00:00",
        "last_updated": "2024-01-01T01:00:00",
        "description": "Test project",
        "has_input": True,
        "has_files": True,
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        project = await Project.from_id("test-123")

    assert project.project_id == "test-123"
    assert project.status == ProjectStatus.IDLE
    assert project.has_input is True
    assert project.has_files is True


def _client_raising(error: Exception) -> AsyncMock:
    client = AsyncMock()
    client.get = AsyncMock(side_effect=error)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.mark.parametrize("status_code", [403, 404])
async def test_project_from_id_raises_not_found_on_403_and_404(status_code):
    client = _client_raising(AristotleAPIError("Forbidden", status_code=status_code))

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        with pytest.raises(AristotleAPIError) as info:
            await Project.from_id("p-1")

    assert isinstance(info.value, AristotleNotFoundError)
    assert str(info.value) == "Project p-1 not found or not accessible."
    assert (info.value.kind, info.value.object_id, info.value.status_code) == ("project", "p-1", status_code)
    client.get.assert_awaited_once_with("/project/p-1")


async def test_task_from_id_not_found_names_the_task():
    client = _client_raising(AristotleAPIError("Forbidden", status_code=403))

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        with pytest.raises(AristotleNotFoundError) as info:
            await AgentTask.from_id("t-1")

    assert str(info.value) == "Task t-1 not found or not accessible."
    assert info.value.kind == "task"
    client.get.assert_awaited_once_with("/task/t-1")


@pytest.mark.parametrize("status_code", [401, 500, None])
async def test_from_id_passes_other_api_errors_through(status_code):
    error = AristotleAPIError("boom", status_code=status_code)
    client = _client_raising(error)

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        with pytest.raises(AristotleAPIError) as info:
            await Project.from_id("p-1")

    assert info.value is error


async def test_from_id_sends_the_id_as_one_path_segment():
    client = _client_raising(AristotleAPIError("Not Found", status_code=404))

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        with pytest.raises(AristotleNotFoundError):
            await Project.from_id("p-1?limit=1#x %")

    client.get.assert_awaited_once_with("/project/p-1%3Flimit%3D1%23x%20%25")


@pytest.mark.parametrize("object_id", ["", ".", "..", "../project", "p-1/"])
async def test_from_id_rejects_an_id_that_cannot_be_one_path_segment_without_a_request(object_id):
    client = _client_raising(AssertionError("must not request"))

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        with pytest.raises(AristotleNotFoundError) as info:
            await Project.from_id(object_id)

    assert (info.value.kind, info.value.object_id, info.value.status_code) == ("project", object_id, None)
    client.get.assert_not_awaited()


def test_not_found_message_shows_an_empty_id_as_quotes():
    assert str(AristotleNotFoundError("project", "")) == "Project '' not found or not accessible."


@pytest.mark.parametrize(
    "clone", [lambda e: pickle.loads(pickle.dumps(e)), copy.copy, copy.deepcopy], ids=["pickle", "copy", "deepcopy"]
)
def test_not_found_survives_pickle_and_copy(clone):
    restored = clone(AristotleNotFoundError("task", "t-1", 403))

    assert type(restored) is AristotleNotFoundError
    assert (str(restored), restored.kind, restored.object_id, restored.status_code) == (
        "Task t-1 not found or not accessible.",
        "task",
        "t-1",
        403,
    )


def test_not_found_with_a_message_survives_pickle_and_copy():
    error = AristotleNotFoundError("project", "fifo", message="No project is named 'fifo'.")

    for restored in (pickle.loads(pickle.dumps(error)), copy.copy(error), copy.deepcopy(error)):
        assert type(restored) is AristotleNotFoundError
        assert (str(restored), restored.kind, restored.object_id) == ("No project is named 'fifo'.", "project", "fifo")


FIFO_ID = "2fa30605-173d-4e2b-8cad-849b55641da3"
OTHER_ID = "4d474c50-51a5-4ba9-a31e-c57949e9769f"


def _listed(project_id: str, name: str | None, day: int = 1) -> Project:
    return Project(
        project_id=project_id,
        status=ProjectStatus.IDLE,
        created_at=datetime(2026, 9, day),
        last_updated=datetime(2026, 9, day),
        description=name,
        has_input=False,
        has_files=False,
    )


def _page(projects: list[Project], pagination_key: str | None) -> Mock:
    body = {"projects": [p.model_dump(mode="json") for p in projects], "pagination_key": pagination_key}
    return Mock(json=Mock(return_value=body))


def _serving(*pages: list[Project]) -> AsyncMock:
    """A request client answering each GET with the next page; every page but the last has a pagination key."""
    client = AsyncMock()
    client.get = AsyncMock(
        side_effect=[_page(page, None if i == len(pages) - 1 else f"key-{i}") for i, page in enumerate(pages)]
    )
    client.rate_limit_refusals = 0
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _lookup(client: AsyncMock) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch("aristotlelib.api_request.AristotleRequestClient", return_value=client))
    stack.enter_context(patch("aristotlelib.project._LOOKUP_PAGE_INTERVAL", 0))
    return stack


async def _resolve(reference: str, *pages: list[Project]) -> tuple[str, AsyncMock]:
    client = _serving(*pages)
    with _lookup(client):
        return await Project.resolve_id(reference), client.get


@pytest.mark.parametrize(
    "reference", [FIFO_ID, FIFO_ID.upper(), FIFO_ID.replace("-", ""), f"  {FIFO_ID} ", "{" + FIFO_ID + "}"]
)
async def test_resolve_id_returns_a_uuid_in_canonical_form_without_a_request(reference):
    project_id, get = await _resolve(reference)

    assert project_id == FIFO_ID
    get.assert_not_awaited()


@pytest.mark.parametrize("reference", ["fifo", "  fifo  "])
async def test_resolve_id_finds_a_project_by_its_name(reference):
    project_id, get = await _resolve(reference, [_listed(OTHER_ID, "other"), _listed(FIFO_ID, " fifo")])

    assert project_id == FIFO_ID
    get.assert_awaited_once_with("/project", params={"pagination_key": None, "limit": 100})


async def test_an_exact_name_wins_over_the_same_name_in_another_case():
    project_id, _ = await _resolve("fifo", [_listed(OTHER_ID, "FIFO"), _listed(FIFO_ID, "fifo")])

    assert project_id == FIFO_ID


async def test_a_name_in_another_case_matches_when_nothing_matches_exactly():
    project_id, _ = await _resolve("Fifo", [_listed(OTHER_ID, "other"), _listed(FIFO_ID, "FIFO")])

    assert project_id == FIFO_ID


@pytest.mark.parametrize("reference", ["2fa30605", "2FA30605", "2fa30605-173d", "2fa30605173d4e2b"])
async def test_resolve_id_finds_a_project_by_an_id_prefix(reference):
    project_id, _ = await _resolve(reference, [_listed(OTHER_ID, "other"), _listed(FIFO_ID, "fifo")])

    assert project_id == FIFO_ID


async def test_a_prefix_shorter_than_eight_digits_is_only_a_name():
    with pytest.raises(AristotleNotFoundError) as info:
        await _resolve("2fa3060", [_listed(FIFO_ID, "fifo")])

    assert str(info.value) == "No project is named '2fa3060'. An id prefix needs at least 8 characters."
    project_id, _ = await _resolve("2fa3060", [_listed(FIFO_ID, "fifo"), _listed(OTHER_ID, "2fa3060")])
    assert project_id == OTHER_ID


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        ("fifo", "No project is named 'fifo'."),
        ("deadbeef", "No project has the id prefix or name 'deadbeef'."),
        ("add", "No project is named 'add'. An id prefix needs at least 8 characters."),
    ],
)
async def test_a_reference_that_matches_nothing_is_not_found(reference, message):
    with pytest.raises(AristotleNotFoundError) as info:
        await _resolve(reference, [_listed(FIFO_ID, "other"), _listed(OTHER_ID, None)])

    assert (str(info.value), info.value.kind, info.value.object_id, info.value.status_code) == (
        message,
        "project",
        reference,
        None,
    )


@pytest.mark.parametrize("reference", ["", "  "])
async def test_an_empty_reference_is_not_found_without_a_request(reference):
    with pytest.raises(AristotleNotFoundError, match="^Project '' not found or not accessible.$"):
        await _resolve(reference)


async def test_a_name_shared_by_two_projects_is_ambiguous_and_lists_both_newest_first():
    newer, older = _listed(FIFO_ID, "fifo", day=2), _listed(OTHER_ID, "fifo", day=1)

    with pytest.raises(AmbiguousProjectError) as info:
        await _resolve("fifo", [newer, older])

    assert info.value.reference == "fifo"
    assert info.value.matches == [newer, older]
    lines = str(info.value).splitlines()
    assert lines[0] == "'fifo' matches 2 projects. Give the id of one instead:"
    assert lines[1].startswith(f"  {FIFO_ID}  fifo (IDLE, created ")
    assert lines[2].startswith(f"  {OTHER_ID}  fifo (IDLE, created ")


async def test_a_name_in_two_other_cases_is_ambiguous():
    with pytest.raises(AmbiguousProjectError) as info:
        await _resolve("fifo", [_listed(FIFO_ID, "FIFO"), _listed(OTHER_ID, "Fifo")])

    assert [p.project_id for p in info.value.matches] == [FIFO_ID, OTHER_ID]


async def test_a_name_of_one_project_and_an_id_prefix_of_another_are_ambiguous():
    with pytest.raises(AmbiguousProjectError) as info:
        await _resolve("2fa30605", [_listed(FIFO_ID, "fifo"), _listed(OTHER_ID, "2fa30605")])

    assert [p.project_id for p in info.value.matches] == [FIFO_ID, OTHER_ID]


async def test_an_ambiguous_listing_shows_ten_matches_and_counts_the_rest():
    projects = [_listed(str(uuid.UUID(int=i)), "fifo") for i in range(12)]

    with pytest.raises(AmbiguousProjectError) as info:
        await _resolve("fifo", projects)

    lines = str(info.value).splitlines()
    assert len(info.value.matches) == 12
    assert len(lines) == 12
    assert lines[-1] == "  and 2 more"


def test_ambiguous_project_error_survives_pickle_and_copy():
    error = AmbiguousProjectError("fifo", [_listed(FIFO_ID, "fifo"), _listed(OTHER_ID, "fifo")])

    for restored in (pickle.loads(pickle.dumps(error)), copy.copy(error), copy.deepcopy(error)):
        assert type(restored) is AmbiguousProjectError
        assert (str(restored), restored.reference, restored.matches) == (str(error), "fifo", error.matches)


async def test_resolve_id_reads_every_page_over_one_connection_with_the_previous_key():
    client = _serving([_listed(OTHER_ID, "other")], [_listed(FIFO_ID, "fifo")])

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client) as new_client,
        patch("aristotlelib.project._LOOKUP_PAGE_INTERVAL", 0),
    ):
        assert await Project.resolve_id("fifo") == FIFO_ID

    new_client.assert_called_once_with()
    assert client.get.await_args_list == [
        call("/project", params={"pagination_key": None, "limit": 100}),
        call("/project", params={"pagination_key": "key-0", "limit": 100}),
    ]


RETRY_SECONDS = 1.5


async def _page_starts(request_seconds: float, refused_on: int | None = None) -> list[float]:
    """When the request each of four lookup pages was answered on went out, each answered ``request_seconds`` later;
    the per-second limit refuses page ``refused_on`` once, and the client sends it again RETRY_SECONDS later."""
    now = [0.0]
    started: list[float] = []
    other = [_listed(OTHER_ID, "other")]
    client = _serving(other, other, other, [_listed(FIFO_ID, "fifo")])
    serve = client.get

    async def get(*args, **kwargs):
        if len(started) == refused_on:
            client.rate_limit_refusals += 1
            now[0] += RETRY_SECONDS
        started.append(now[0])
        now[0] += request_seconds
        return await serve(*args, **kwargs)

    async def sleep(seconds):
        now[0] += seconds

    client.get = get
    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.project.time", Mock(monotonic=lambda: now[0])),
        patch("aristotlelib.project.asyncio.sleep", sleep),
    ):
        assert await Project.resolve_id("fifo") == FIFO_ID
    return started


@pytest.mark.parametrize("request_seconds", [0.0, 0.3])
async def test_resolve_id_reads_its_pages_with_no_wait_of_its_own(request_seconds):
    started = await _page_starts(request_seconds)

    assert started == pytest.approx([i * request_seconds for i in range(4)])


@pytest.mark.parametrize("request_seconds", [0.0, 0.3, 0.8])
@pytest.mark.parametrize("refused_on", [0, 2])
async def test_after_the_rate_limit_refuses_a_page_the_rest_of_the_lookup_reads_two_pages_a_second(
    refused_on, request_seconds
):
    started = await _page_starts(request_seconds, refused_on)

    # At most two pages a second, so five lookups at once stay under the 10 requests a second each user is allowed;
    # the page after the refused one waits from its retry's answer.
    gap = max(0.5, request_seconds)
    before = [i * request_seconds for i in range(refused_on)]
    retried = refused_on * request_seconds + RETRY_SECONDS
    after = [retried + request_seconds + 0.5 + i * gap for i in range(3 - refused_on)]
    assert started == pytest.approx([*before, retried, *after])


def _served_page_starts(api_server, monkeypatch, *refused: bool) -> list[float]:
    """When each request of a lookup through a real client started, the server answering one page per entry of
    ``refused``, or the per-second limit's 429 where it is true; the last page names the project."""
    now, _ = fast_api_clock(monkeypatch)
    monkeypatch.setattr("aristotlelib.project.time", Mock(monotonic=lambda: now[0]))
    started: list[float] = []

    def answer(refusal: bool, last: bool):
        def respond(request: httpx.Request) -> httpx.Response:
            started.append(now[0])
            if refusal:
                return httpx.Response(429, json={"detail": "Too many requests."})
            projects = [_listed(FIFO_ID if last else OTHER_ID, "fifo" if last else "other")]
            key = None if last else f"key-{len(started)}"
            return httpx.Response(
                200, json={"projects": [p.model_dump(mode="json") for p in projects], "pagination_key": key}
            )

        return respond

    api_server(*(answer(refusal, i == len(refused) - 1) for i, refusal in enumerate(refused)))
    return started


async def test_a_lookup_reads_its_pages_as_fast_as_every_request_is_paced(api_server, monkeypatch):
    monkeypatch.setattr(api_request, "REQUEST_INTERVAL_SECONDS", 0.2)
    started = _served_page_starts(api_server, monkeypatch, False, False, False)

    assert await Project.resolve_id("fifo") == FIFO_ID

    assert started == pytest.approx([0, 0.2, 0.4])


async def test_a_lookup_page_the_rate_limit_refused_is_sent_again_and_the_pages_after_it_slow_down(
    api_server, monkeypatch
):
    monkeypatch.setattr(api_request, "RETRY_BACKOFF_SECONDS", 1)
    monkeypatch.setattr(api_request.random, "uniform", lambda low, high: low)
    started = _served_page_starts(api_server, monkeypatch, False, True, False, False, False)

    assert await Project.resolve_id("fifo") == FIFO_ID

    # The refused page is sent again 1 s later; the pages after it start 0.5 s apart, from the retry.
    assert started == pytest.approx([0, 0, 1, 1.5, 2])


def _endless(first: list[Project]) -> AsyncMock:
    """A request client whose first page is ``first`` and whose every page after it has more to come."""
    client = _serving()
    rest = _page([_listed(OTHER_ID, "other")], "more")
    client.get = AsyncMock(side_effect=[_page(first, "more")] + [rest] * 30)
    return client


async def test_resolve_id_searches_only_the_two_thousand_most_recent_projects():
    client = _endless([_listed(OTHER_ID, "other")])

    with _lookup(client), pytest.raises(AristotleNotFoundError) as info:
        await Project.resolve_id("fifo")

    assert client.get.await_count == 20
    assert str(info.value) == (
        "No project is named 'fifo' among your 2,000 most recent projects. Use the full id of an older one."
    )


async def test_a_match_inside_the_lookup_window_wins_when_more_projects_remain():
    client = _endless([_listed(FIFO_ID, "fifo")])

    with _lookup(client):
        assert await Project.resolve_id("fifo") == FIFO_ID

    assert client.get.await_count == 20


async def test_a_search_past_the_first_page_says_what_it_is_doing(caplog):
    caplog.set_level(logging.INFO, logger="aristotle")

    await _resolve("fifo", [_listed(FIFO_ID, "fifo")])
    assert caplog.text == ""

    await _resolve("fifo", [_listed(OTHER_ID, "other")], [_listed(OTHER_ID, "other")], [_listed(FIFO_ID, "fifo")])
    assert caplog.messages == [
        "Searching your 2,000 most recent projects for 'fifo'. A full project id needs no search."
    ]


@pytest.fixture
def at_a_terminal(monkeypatch):
    """Lookups waited on at a terminal, whose note is due after 0.05 s."""
    monkeypatch.setattr("aristotlelib.project._LOOKUP_NOTE_SECONDS", 0.05)
    token = lookup_at_terminal.set(True)
    yield
    lookup_at_terminal.reset(token)


@pytest.mark.usefixtures("at_a_terminal")
async def test_at_a_terminal_a_lookup_that_ends_before_its_note_is_due_says_nothing(caplog):
    caplog.set_level(logging.INFO, logger="aristotle")

    await _resolve("fifo", [_listed(OTHER_ID, "other")], [_listed(OTHER_ID, "other")], [_listed(FIFO_ID, "fifo")])
    with pytest.raises(AristotleNotFoundError):
        await _resolve("missing", [_listed(OTHER_ID, "other")], [_listed(OTHER_ID, "other")])
    await asyncio.sleep(0.2)

    assert caplog.messages == []


@pytest.mark.usefixtures("at_a_terminal")
async def test_at_a_terminal_a_lookup_still_running_when_its_note_is_due_says_what_it_is_doing(caplog):
    caplog.set_level(logging.INFO, logger="aristotle")
    client = _serving([_listed(OTHER_ID, "other")], [_listed(FIFO_ID, "fifo")])
    serve = client.get

    async def get(*args, **kwargs):
        await asyncio.sleep(0.2)
        return await serve(*args, **kwargs)

    client.get = get
    with _lookup(client):
        assert await Project.resolve_id("fifo") == FIFO_ID

    assert caplog.messages == [
        "Searching your 2,000 most recent projects for 'fifo'. A full project id needs no search."
    ]


async def test_a_lookup_gives_the_project_as_listed_and_a_uuid_none():
    listed = _listed(FIFO_ID, "fifo")
    client = _serving([_listed(OTHER_ID, "other"), listed])

    with _lookup(client):
        assert await Project._resolve("fifo") == (FIFO_ID, listed)
        assert await Project._resolve(FIFO_ID.upper()) == (FIFO_ID, None)

    client.get.assert_awaited_once()


async def test_an_empty_page_ends_the_search_even_with_a_key():
    client = _serving()
    client.get = AsyncMock(side_effect=[_page([_listed(OTHER_ID, "other")], "key-0"), _page([], "key-1")])

    with _lookup(client), pytest.raises(AristotleNotFoundError) as info:
        await Project.resolve_id("fifo")

    assert client.get.await_count == 2
    assert str(info.value) == "No project is named 'fifo'."


async def test_project_from_id_accepts_a_uuid():
    project_id = uuid.UUID("1b4e28ba-2fa1-11d2-883f-0016d3cca427")
    mock_response = Mock()
    mock_response.json.return_value = {
        "project_id": str(project_id),
        "status": 2,
        "created_at": "2024-01-01T00:00:00",
        "last_updated": "2024-01-01T01:00:00",
        "description": None,
        "has_input": False,
        "has_files": False,
    }
    client = AsyncMock()
    client.get = AsyncMock(return_value=mock_response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        project = await Project.from_id(project_id)

    assert project.project_id == str(project_id)
    client.get.assert_awaited_once_with(f"/project/{project_id}")


async def test_project_create_without_file():
    """Test creating a project with only a prompt (no file)."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "project_id": "new-project-123",
        "status": 1,  # RUNNING
        "created_at": "2024-01-01T00:00:00",
        "last_updated": "2024-01-01T00:00:00",
        "description": None,
        "has_input": False,
        "has_files": False,
    }

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        project = await Project.create(prompt="Test prompt")

    assert project.project_id == "new-project-123"
    assert project.status == ProjectStatus.RUNNING

    # Verify the API call
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert "/project" in call_args[0][0]
    assert call_args[1]["data"]["body"] is not None


async def test_project_create_with_tar_file():
    """Test creating a project with a tar file."""
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
        f.write(b"fake tar content")
        tar_path = Path(f.name)

    try:
        mock_response = Mock()
        mock_response.json.return_value = {
            "project_id": "new-project-456",
            "status": 1,  # RUNNING
            "created_at": "2024-01-01T00:00:00",
            "last_updated": "2024-01-01T00:00:00",
            "description": None,
            "has_input": True,
            "has_files": False,
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
            project = await Project.create(prompt="Test with file", tar_file_path=tar_path)

        assert project.project_id == "new-project-456"

        # Verify files were passed
        call_args = mock_client.post.call_args
        assert "files" in call_args[1]
    finally:
        tar_path.unlink()


async def test_project_create_from_directory():
    """Test creating a project from a directory."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # Create some test files
        (temp_path / "test.lean").write_text("theorem test : True := trivial")
        (temp_path / "README.md").write_text("# Test Project")

        # Create a file that should be skipped
        lake_dir = temp_path / ".lake" / "build" / "ir"
        lake_dir.mkdir(parents=True)
        (lake_dir / "should_skip.ir").write_text("skip me")

        mock_response = Mock()
        mock_response.json.return_value = {
            "project_id": "dir-project-789",
            "status": 1,  # RUNNING
            "created_at": "2024-01-01T00:00:00",
            "last_updated": "2024-01-01T00:00:00",
            "description": None,
            "has_input": True,
            "has_files": False,
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "aristotlelib.api_request.AristotleRequestClient",
                return_value=mock_client,
            ),
            patch(
                "aristotlelib.project.local_file_utils.get_expected_toolchain",
                new=AsyncMock(return_value="leanprover/lean4:v4.24.0"),
            ),
        ):
            project = await Project.create_from_directory(prompt="Process directory", project_dir=temp_path)

        assert project.project_id == "dir-project-789"


async def test_project_create_from_directory_not_a_directory():
    """Test that create_from_directory raises error for non-directory path."""
    with tempfile.NamedTemporaryFile() as f:
        file_path = Path(f.name)

        with pytest.raises(ValueError) as exc_info:
            await Project.create_from_directory(prompt="Test", project_dir=file_path)

        assert "not a directory" in str(exc_info.value)


async def test_project_create_rejects_empty_prompt():
    """Test that create rejects an empty prompt without calling the API."""
    mock_client = AsyncMock()
    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        with pytest.raises(ValueError) as exc_info:
            await Project.create(prompt="")

    assert "cannot be empty" in str(exc_info.value)
    mock_client.post.assert_not_called()


async def test_project_create_rejects_whitespace_prompt():
    """Test that create rejects a whitespace-only prompt without calling the API."""
    mock_client = AsyncMock()
    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        with pytest.raises(ValueError) as exc_info:
            await Project.create(prompt="   \n\t")

    assert "cannot be empty" in str(exc_info.value)
    mock_client.post.assert_not_called()


async def test_project_create_from_directory_rejects_empty_prompt():
    """Test that create_from_directory rejects an empty prompt."""
    with tempfile.TemporaryDirectory() as temp_dir:
        with pytest.raises(ValueError) as exc_info:
            await Project.create_from_directory(prompt="", project_dir=Path(temp_dir))

    assert "cannot be empty" in str(exc_info.value)


async def test_project_ask_rejects_empty_prompt():
    """Test that ask rejects an empty prompt without calling the API."""
    project = Project(
        project_id="test-ask-empty",
        status=ProjectStatus.IDLE,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=None,
        has_input=True,
        has_files=True,
    )

    mock_client = AsyncMock()
    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        with pytest.raises(ValueError) as exc_info:
            await project.ask("")

    assert "cannot be empty" in str(exc_info.value)
    mock_client.post.assert_not_called()


async def test_project_refresh():
    """Test refreshing a project's data."""
    project = Project(
        project_id="test-refresh",
        status=ProjectStatus.RUNNING,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=None,
        has_input=True,
        has_files=False,
    )

    mock_response = Mock()
    mock_response.json.return_value = {
        "project_id": "test-refresh",
        "status": 2,  # IDLE
        "created_at": "2024-01-01T00:00:00",
        "last_updated": "2024-01-01T02:00:00",
        "description": "Updated",
        "has_input": True,
        "has_files": True,
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        await project.refresh()

    assert project.status == ProjectStatus.IDLE
    assert project.has_files is True


RENAMED_ID = "3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f"


def _rename_client(description: str) -> AsyncMock:
    mock_response = Mock()
    mock_response.json.return_value = {
        "project_id": RENAMED_ID,
        "status": 1,  # RUNNING
        "created_at": "2024-01-01T00:00:00",
        "last_updated": "2024-01-01T02:00:00",
        "description": description,
        "has_input": False,
        "has_files": False,
    }
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


def _unnamed_project() -> Project:
    return Project(
        project_id=RENAMED_ID,
        status=ProjectStatus.RUNNING,
        created_at=datetime(2024, 1, 1),
        last_updated=datetime(2024, 1, 1),
        description="Test prompt",
        has_input=False,
        has_files=False,
    )


async def test_project_rename_by_id_posts_the_trimmed_name_to_the_canonical_id():
    mock_client = _rename_client("solana proj")

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        project = await Project.rename_by_id("{" + RENAMED_ID.upper() + "}", "  solana proj\n")

    mock_client.post.assert_called_once_with(
        f"/project/{RENAMED_ID}/rename", json={"name": "solana proj"}, idempotent=True
    )
    assert project.project_id == RENAMED_ID
    assert project.description == "solana proj"


async def test_project_rename_updates_the_instance():
    project = _unnamed_project()
    mock_client = _rename_client("solana proj")

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        await project.rename("solana proj")

    mock_client.post.assert_called_once_with(
        f"/project/{RENAMED_ID}/rename", json={"name": "solana proj"}, idempotent=True
    )
    assert project.description == "solana proj"
    assert project.last_updated == datetime(2024, 1, 1, 2)


async def test_project_rename_failure_leaves_the_instance_unchanged():
    project = _unnamed_project()
    mock_client = _rename_client("unused")
    mock_client.post = AsyncMock(side_effect=AristotleAPIError("Project not found", status_code=404))

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        with pytest.raises(AristotleAPIError):
            await project.rename("solana proj")

    assert project.description == "Test prompt"


@pytest.mark.parametrize(
    ("project_id", "name", "error"),
    [
        (RENAMED_ID, "", "Project name cannot be empty"),
        (RENAMED_ID, " \n\t", "Project name cannot be empty"),
        ("not-a-uuid", "solana proj", "'not-a-uuid' is not a project id"),
        ("../keys", "solana proj", "'../keys' is not a project id"),
    ],
)
async def test_project_rename_by_id_rejects_bad_input_without_calling_the_api(project_id: str, name: str, error: str):
    mock_client = AsyncMock()

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        with pytest.raises(ValueError, match=error):
            await Project.rename_by_id(project_id, name)

    mock_client.post.assert_not_called()


async def test_task_cancel():
    """Test canceling a task."""
    task = AgentTask(
        project_id="proj-cancel",
        agent_task_id="task-cancel",
        status=TaskStatus.IN_PROGRESS,
        created_at=datetime.now(),
        last_updated_at=datetime.now(),
        percent_complete=50,
        file_name=None,
        description=None,
        output_summary=None,
    )

    mock_response = Mock()
    mock_response.json.return_value = {
        "project_id": "proj-cancel",
        "agent_task_id": "task-cancel",
        "status": "CANCELED",
        "created_at": "2024-01-01T00:00:00",
        "last_updated_at": "2024-01-01T01:00:00",
        "percent_complete": None,
        "file_name": None,
        "description": None,
        "output_summary": None,
    }

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        await task.cancel()

    assert task.status == TaskStatus.CANCELED


async def test_project_list_projects():
    """Test listing projects."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "projects": [
            {
                "project_id": "proj-1",
                "status": 2,  # IDLE
                "created_at": "2024-01-01T00:00:00",
                "last_updated": "2024-01-01T01:00:00",
                "description": "First project",
                "has_input": True,
                "has_files": True,
            },
            {
                "project_id": "proj-2",
                "status": 1,  # RUNNING
                "created_at": "2024-01-02T00:00:00",
                "last_updated": "2024-01-02T00:30:00",
                "description": "Second project",
                "has_input": True,
                "has_files": False,
            },
        ],
        "pagination_key": "next-page-key",
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        projects, pagination_key = await Project.list_projects(limit=10)

    assert len(projects) == 2
    assert projects[0].project_id == "proj-1"
    assert projects[1].project_id == "proj-2"
    assert projects[0].status == ProjectStatus.IDLE
    assert projects[1].status == ProjectStatus.RUNNING
    assert pagination_key == "next-page-key"


async def test_project_list_projects_with_status_filter():
    """Test listing projects with status filter."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "projects": [],
        "pagination_key": None,
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        projects, _ = await Project.list_projects(status=ProjectStatus.IDLE, limit=20)

    # Verify status filter was passed
    call_args = mock_client.get.call_args
    assert call_args[1]["params"]["status"] == 2  # IDLE = 2


async def test_project_list_projects_with_multiple_statuses():
    """Test listing projects with multiple status filters."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "projects": [],
        "pagination_key": None,
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    statuses = [ProjectStatus.IDLE, ProjectStatus.RUNNING]

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        await Project.list_projects(status=statuses, limit=30)

    call_args = mock_client.get.call_args
    assert call_args[1]["params"]["status"] == [2, 1]  # IDLE=2, RUNNING=1


async def test_project_list_projects_by_latest_run_status():
    mock_response = Mock()
    mock_response.json.return_value = {"projects": [], "pagination_key": None}
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        await Project.list_projects(latest_run_status=[TaskStatus.QUEUED, TaskStatus.IN_PROGRESS])

    assert mock_client.get.call_args[1]["params"]["latest_run_status"] == ["QUEUED", "IN_PROGRESS"]


async def test_project_get_files():
    """Test downloading project files."""
    project = Project(
        project_id="test-files",
        status=ProjectStatus.IDLE,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=None,
        has_input=True,
        has_files=True,
    )

    mock_refresh_response = Mock()
    mock_refresh_response.json.return_value = {
        "project_id": "test-files",
        "status": 2,
        "created_at": "2024-01-01T00:00:00",
        "last_updated": "2024-01-01T01:00:00",
        "description": None,
        "has_input": True,
        "has_files": True,
    }

    mock_download_response = Mock()
    mock_download_response.headers = {"content-disposition": 'attachment; filename="result.tar.gz"'}
    mock_download_response.content = b"file content"

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=[mock_refresh_response, mock_download_response])
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with tempfile.TemporaryDirectory() as temp_dir:
        destination = Path(temp_dir) / "output.tar.gz"

        with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
            result_path = await project.get_files(destination=destination)

        assert result_path == destination
        assert destination.read_bytes() == b"file content"


async def test_project_get_files_default_filename(tmp_path, monkeypatch):
    """Test downloading project files with default filename from headers."""
    monkeypatch.chdir(tmp_path)
    project = Project(
        project_id="test-files-default",
        status=ProjectStatus.IDLE,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=None,
        has_input=True,
        has_files=True,
    )

    mock_refresh_response = Mock()
    mock_refresh_response.json.return_value = {
        "project_id": "test-files-default",
        "status": 2,
        "created_at": "2024-01-01T00:00:00",
        "last_updated": "2024-01-01T01:00:00",
        "description": None,
        "has_input": True,
        "has_files": True,
    }

    mock_download_response = Mock()
    mock_download_response.headers = {"content-disposition": 'attachment; filename="solution.tar.gz"'}
    mock_download_response.content = b"solution content"

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=[mock_refresh_response, mock_download_response])
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        result_path = await project.get_files()

    assert result_path.name == "solution.tar.gz"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["solution.tar.gz"]
    assert (tmp_path / "solution.tar.gz").read_bytes() == b"solution content"


async def test_get_files_of_a_project_without_files_raises_without_downloading():
    project = Project(
        project_id="test-empty",
        status=ProjectStatus.IDLE,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=None,
        has_input=False,
        has_files=False,
    )
    mock_refresh_response = Mock()
    mock_refresh_response.json.return_value = {
        "project_id": "test-empty",
        "status": 2,
        "created_at": "2024-01-01T00:00:00",
        "last_updated": "2024-01-01T01:00:00",
        "description": None,
        "has_input": False,
        "has_files": False,
    }
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_refresh_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client),
        pytest.raises(ProjectHasNoFilesError, match="^Project test-empty has no files to download yet.$") as raised,
    ):
        await project.get_files()

    assert isinstance(raised.value, AristotleAPIError)
    assert raised.value.status_code is None
    mock_client.get.assert_awaited_once_with("/project/test-empty")


ID = "2fa30605-173d-4e2b-8cad-849b55641da3"


@pytest.mark.parametrize(
    ("project_id", "other", "same"),
    [
        (ID, ID, True),
        (ID, ID.upper(), True),
        (ID, ID.replace("-", ""), True),
        (ID, "4d474c50-51a5-4ba9-a31e-c57949e9769f", False),
        (ID, "not-a-uuid", False),
        ("project-123", "project-123", True),
    ],
)
def test_same_project_id_compares_uuids_whatever_their_spelling(project_id: str, other: str, same: bool):
    assert same_project_id(project_id, other) is same
    assert same_project_id(other, project_id) is same


def _archive_client(content_disposition: str | None, description: str | None = None) -> AsyncMock:
    """A client whose project refresh shows files, and whose download sends them with this Content-Disposition."""
    refreshed = Mock()
    refreshed.json.return_value = {
        "project_id": ID,
        "status": 2,
        "created_at": "2024-01-01T00:00:00",
        "last_updated": "2024-01-01T01:00:00",
        "description": description,
        "has_input": True,
        "has_files": True,
    }
    archive = Mock()
    archive.headers = {} if content_disposition is None else {"content-disposition": content_disposition}
    archive.content = b"archive"
    client = AsyncMock()
    client.get = AsyncMock(side_effect=[refreshed, archive])
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


async def _get_files(
    destination: Path | str | None,
    content_disposition: str | None = f'attachment; filename="{ID}-aristotle.tar.gz"',
    description: str | None = None,
) -> Path:
    project = Project(
        project_id=ID,
        status=ProjectStatus.IDLE,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=description,
        has_input=True,
        has_files=True,
    )
    client = _archive_client(content_disposition, description)
    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        return await project.get_files(destination=destination)


async def test_get_files_writes_the_archive_once_its_requests_are_closed(tmp_path: Path):
    """Nothing awaits once the archive is in place, so an interrupt can't land between its save and the return."""
    destination = tmp_path / "result.tar.gz"
    client = _archive_client(f'attachment; filename="{ID}-aristotle.tar.gz"')
    written_when_closed: list[bool] = []
    client.__aexit__ = AsyncMock(side_effect=lambda *exc_info: written_when_closed.append(destination.exists()))
    project = Project(
        project_id=ID,
        status=ProjectStatus.IDLE,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=None,
        has_input=True,
        has_files=True,
    )

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        assert await project.get_files(destination=destination) == destination

    # The refresh's client, then the download's.
    assert written_when_closed == [False, False]
    assert destination.read_bytes() == b"archive"


@pytest.mark.usefixtures("frozen_api_clock")
async def test_a_page_in_place_of_the_archive_is_not_saved(api_server, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(api_request, "getproxies", dict)
    monkeypatch.delenv("ARISTOTLE_WEB_URL", raising=False)
    project = Project(
        project_id=ID,
        status=ProjectStatus.IDLE,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=None,
        has_input=True,
        has_files=True,
    )
    page = httpx.Response(200, html="<title>Blocked by policy</title>")
    requests = api_server(httpx.Response(200, json=project.model_dump(mode="json")), *[page] * 5)

    with pytest.raises(AristotleAPIError) as raised:
        await project.get_files(destination=tmp_path)

    assert str(raised.value) == (
        f"GET /project/{ID}/result after 5 attempts over 0 s: the answer from https://aristotle.harmonic.fun was 200 "
        "OK with text/html, not Aristotle's: a captive portal or a proxy may have answered instead."
    )
    assert len(requests) == 6
    assert list(tmp_path.iterdir()) == []


async def test_get_files_makes_missing_directories(tmp_path: Path):
    destination = tmp_path / "new" / "dir" / "result.tar.gz"

    assert await _get_files(str(destination)) == destination
    assert destination.read_bytes() == b"archive"


async def test_get_files_overwrites_an_existing_file(tmp_path: Path):
    destination = tmp_path / "result.tar.gz"
    destination.write_bytes(b"old")

    assert await _get_files(destination) == destination
    assert destination.read_bytes() == b"archive"


@pytest.mark.parametrize("directory", ["existing", "existing/", "new/", "new/dir/"])
async def test_get_files_saves_into_a_directory_under_the_default_name(directory: str, tmp_path: Path):
    (tmp_path / "existing").mkdir()

    saved = await _get_files(f"{tmp_path}{os.sep}{directory}")

    assert saved == tmp_path / directory / f"{ID}-aristotle.tar.gz"
    assert saved.read_bytes() == b"archive"


async def test_get_files_without_a_destination_saves_in_the_current_directory(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert await _get_files(None) == Path(f"{ID}-aristotle.tar.gz")
    assert (tmp_path / f"{ID}-aristotle.tar.gz").read_bytes() == b"archive"


@pytest.mark.parametrize(
    ("content_disposition", "description", "name"),
    [
        (f'attachment; filename="{ID}-aristotle.tar.gz"', "rtl/hw", f"{ID}-aristotle.tar.gz"),
        ('attachment; filename="../../evil.tar.gz"', None, "_.._evil.tar.gz"),
        ('attachment; filename="..\\..\\evil.tar.gz"', None, "_.._evil.tar.gz"),
        ('attachment; filename="/etc/passwd"', None, "_etc_passwd"),
        ('attachment; filename=".."', None, f"{ID}_aristotle.tar.gz"),
        (None, "rtl/hw: ci @ main 1a2b3c4", "rtl_hw_ ci @ main 1a2b3c4_aristotle.tar.gz"),
        (None, 'C:\\Users\\me\nwhat? <"*|>', "C__Users_me_what_ ______aristotle.tar.gz"),
        (None, "../..", "_.._aristotle.tar.gz"),
        (None, None, f"{ID}_aristotle.tar.gz"),
        ('attachment; filename="CON.tar.gz"', None, "_CON.tar.gz"),
        ('attachment; filename="aux"', None, "_aux"),
        ('attachment; filename="com1 .tar.gz"', None, "_com1 .tar.gz"),
        ('attachment; filename="LPT\u00b9.tar.gz"', None, "_LPT\u00b9.tar.gz"),
        (None, "nul.v", "_nul.v_aristotle.tar.gz"),
        ('attachment; filename="CON_aristotle.tar.gz"', None, "CON_aristotle.tar.gz"),
        (None, "console", "console_aristotle.tar.gz"),
    ],
)
async def test_get_files_makes_the_default_name_one_safe_file_name(
    content_disposition: str | None, description: str | None, name: str, tmp_path: Path
):
    saved = await _get_files(tmp_path, content_disposition, description)

    assert saved == tmp_path / name
    assert [entry.name for entry in tmp_path.iterdir()] == [name]


class _FailsHalfway:
    """A file that writes half of what it's given, then fails with ``error``: in write, or on close as a buffered
    write does."""

    def __init__(self, file, error: int, on_close: bool):
        self.file = file
        self.error = error
        self.on_close = on_close

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.file.close()
        if self.on_close:
            raise OSError(self.error, os.strerror(self.error))

    def fileno(self) -> int:
        return self.file.fileno()

    def write(self, data: bytes) -> int:
        self.file.write(data[: len(data) // 2])
        if not self.on_close:
            raise OSError(self.error, os.strerror(self.error))
        return len(data)


def _fail_writes_halfway(monkeypatch, error: int = errno.ENOSPC, at: str = "write") -> None:
    """Make each write get_files can make, in place or into a temporary file, stop halfway with ``error``; a temporary
    file fails ``at`` its creation, a write or its close."""
    write_bytes = Path.write_bytes

    def write_half(self: Path, data: bytes) -> int:
        write_bytes(self, data[: len(data) // 2])
        raise OSError(error, os.strerror(error))

    def failing_open(*args, **kwargs):
        if at == "creation":
            raise OSError(error, os.strerror(error))
        return _FailsHalfway(open(*args, **kwargs), error, on_close=at == "close")

    monkeypatch.setattr(Path, "write_bytes", write_half)
    monkeypatch.setattr("aristotlelib.project.open", failing_open, raising=False)


@pytest.mark.parametrize(
    ("error", "at"),
    [(errno.ENOSPC, "creation"), (errno.ENOSPC, "write"), (errno.EACCES, "write"), (errno.EACCES, "close")],
    ids=["disk full at creation", "disk full", "write refused", "close refused"],
)
@pytest.mark.parametrize("old", [b"old", None], ids=["over an old archive", "new"])
async def test_a_failed_save_leaves_no_partial_archive_and_keeps_an_old_one(
    old, error, at, tmp_path: Path, monkeypatch
):
    destination = tmp_path / "result.tar.gz"
    if old is not None:
        destination.write_bytes(old)

    _fail_writes_halfway(monkeypatch, error, at)
    with pytest.raises(OSError):
        await _get_files(destination)

    assert [entry.name for entry in tmp_path.iterdir()] == ([] if old is None else ["result.tar.gz"])
    if old is not None:
        assert destination.read_bytes() == old


async def test_a_failed_save_reports_its_own_error_when_its_temporary_file_cannot_go(tmp_path: Path, monkeypatch):
    def held(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(errno.EACCES, "Permission denied")

    _fail_writes_halfway(monkeypatch)
    monkeypatch.setattr(Path, "unlink", held)
    with pytest.raises(OSError) as raised:
        await _get_files(tmp_path / "result.tar.gz")

    assert raised.value.errno == errno.ENOSPC
    assert not (tmp_path / "result.tar.gz").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
async def test_a_symlink_as_the_destination_is_written_through(tmp_path: Path):
    (tmp_path / "target.tar.gz").write_bytes(b"old")
    (tmp_path / "link.tar.gz").symlink_to("target.tar.gz")

    await _get_files(tmp_path / "link.tar.gz")

    assert (tmp_path / "link.tar.gz").is_symlink()
    assert (tmp_path / "target.tar.gz").read_bytes() == b"archive"
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["link.tar.gz", "target.tar.gz"]


@pytest.mark.skipif(not os.path.isdir("/dev/fd"), reason="a pipe named by /dev/fd")
async def test_a_pipe_as_the_destination_is_written_in_place():
    read, write = os.pipe()
    try:
        await _get_files(f"/dev/fd/{write}")
    finally:
        os.close(write)
    try:
        assert os.read(read, 100) == b"archive"
    finally:
        os.close(read)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize("permissions", [0o600, 0o666])
async def test_saving_over_a_file_keeps_its_permissions(permissions: int, tmp_path: Path):
    destination = tmp_path / "result.tar.gz"
    destination.write_bytes(b"old")
    destination.chmod(permissions)

    await _get_files(destination)

    assert destination.read_bytes() == b"archive"
    assert destination.stat().st_mode & 0o777 == permissions


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize("permissions", [0o600, 0o644, 0o666], ids=["0600", "0644", "0666"])
async def test_the_file_beside_the_one_it_replaces_starts_owner_only(permissions: int, tmp_path: Path, monkeypatch):
    destination = tmp_path / "result.tar.gz"
    destination.write_bytes(b"old")
    destination.chmod(permissions)
    modes = []
    write_bytes = Path.write_bytes

    def recording_write_bytes(self: Path, data: bytes) -> int:
        written = write_bytes(self, data)
        modes.append(self.stat().st_mode & 0o777)
        return written

    def recording_open(*args, **kwargs):
        file = open(*args, **kwargs)
        modes.append(os.stat(file.fileno()).st_mode & 0o777)
        return file

    monkeypatch.setattr(Path, "write_bytes", recording_write_bytes)
    monkeypatch.setattr("aristotlelib.project.open", recording_open, raising=False)
    await _get_files(destination)

    assert modes == [0o600]
    assert destination.stat().st_mode & 0o777 == permissions


_UNDEFINED_ID = 0xFFFFFFFF
#: 0604 with uid 65534 denied, as Linux stores a POSIX ACL: user::rw- user:65534:--- group::--- mask::--- other::r--
_DENYING_ACL = struct.pack("<I", 2) + b"".join(
    struct.pack("<HHI", tag, permissions, uid)
    for tag, permissions, uid in [
        (0x01, 6, _UNDEFINED_ID),
        (0x02, 0, 65534),
        (0x04, 0, _UNDEFINED_ID),
        (0x10, 0, _UNDEFINED_ID),
        (0x20, 4, _UNDEFINED_ID),
    ]
)


def _with_denying_acl(path: Path) -> None:
    path.write_bytes(b"old")
    path.chmod(0o604)
    try:
        os.setxattr(path, "system.posix_acl_access", _DENYING_ACL)
    except OSError:
        pytest.skip("this file system takes no POSIX ACL")


class _RecordsAcl:
    """A file that records its POSIX ACL when written to."""

    def __init__(self, file, acls: list[bytes]):
        self.file = file
        self.acls = acls

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.file.close()

    def fileno(self) -> int:
        return self.file.fileno()

    def write(self, data: bytes) -> int:
        self.acls.append(os.getxattr(self.file.fileno(), "system.posix_acl_access"))
        return self.file.write(data)


@pytest.mark.skipif(not hasattr(os, "setxattr"), reason="POSIX ACLs as Linux stores them")
async def test_saving_over_a_file_keeps_its_access_control_list_from_the_start(tmp_path: Path, monkeypatch):
    destination = tmp_path / "result.tar.gz"
    _with_denying_acl(destination)
    while_written: list[bytes] = []

    monkeypatch.setattr(
        "aristotlelib.project.open", lambda *a, **k: _RecordsAcl(open(*a, **k), while_written), raising=False
    )
    await _get_files(destination)

    assert destination.read_bytes() == b"archive"
    assert os.getxattr(destination, "system.posix_acl_access") == _DENYING_ACL
    assert while_written == [_DENYING_ACL]


@pytest.mark.skipif(not hasattr(os, "setxattr"), reason="POSIX ACLs as Linux stores them")
async def test_a_file_whose_access_control_list_cannot_be_carried_is_written_in_place(tmp_path: Path, monkeypatch):
    destination = tmp_path / "result.tar.gz"
    _with_denying_acl(destination)

    with patch("aristotlelib.project.os.setxattr", side_effect=PermissionError(errno.EPERM, "Operation not permitted")):
        await _get_files(destination)

    assert destination.read_bytes() == b"archive"
    assert os.getxattr(destination, "system.posix_acl_access") == _DENYING_ACL
    assert [entry.name for entry in tmp_path.iterdir()] == ["result.tar.gz"]


_GRANTING_DEFAULT_ACL = struct.pack("<I", 2) + b"".join(
    struct.pack("<HHI", tag, permissions, uid)
    for tag, permissions, uid in [
        (0x01, 6, _UNDEFINED_ID),
        (0x02, 4, 65534),
        (0x04, 4, _UNDEFINED_ID),
        (0x10, 4, _UNDEFINED_ID),
        (0x20, 0, _UNDEFINED_ID),
    ]
)


def _in_a_folder_granting_more(folder: Path) -> Path:
    """A 0640 file with no ACL, in ``folder`` once its default ACL grants uid 65534 read, as a new file there inherits."""
    destination = folder / "result.tar.gz"
    destination.write_bytes(b"old")
    destination.chmod(0o640)
    try:
        os.setxattr(folder, "system.posix_acl_default", _GRANTING_DEFAULT_ACL)
    except OSError:
        pytest.skip("this file system takes no POSIX ACL")
    return destination


@pytest.mark.skipif(not hasattr(os, "setxattr"), reason="POSIX ACLs as Linux stores them")
async def test_saving_over_a_file_with_no_access_control_list_grants_nothing_the_folder_default_would(tmp_path: Path):
    destination = _in_a_folder_granting_more(tmp_path)

    await _get_files(destination)

    assert destination.read_bytes() == b"archive"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    assert "system.posix_acl_access" not in os.listxattr(destination)


@pytest.mark.skipif(not hasattr(os, "setxattr"), reason="POSIX ACLs as Linux stores them")
@pytest.mark.parametrize(
    "refusal",
    [PermissionError(errno.EPERM, "Operation not permitted"), OSError(errno.EINVAL, "Invalid argument")],
    ids=["refused", "NFSv4's EINVAL"],
)
async def test_a_file_whose_inherited_access_control_list_cannot_be_dropped_is_written_in_place(
    refusal: OSError, tmp_path: Path
):
    destination = _in_a_folder_granting_more(tmp_path)

    with patch("aristotlelib.project.os.removexattr", side_effect=refusal):
        await _get_files(destination)

    assert destination.read_bytes() == b"archive"
    assert "system.posix_acl_access" not in os.listxattr(destination)
    assert [entry.name for entry in tmp_path.iterdir()] == ["result.tar.gz"]


@pytest.mark.skipif(not hasattr(os, "setxattr"), reason="POSIX ACLs as Linux stores them")
async def test_a_disk_too_full_to_drop_an_inherited_access_control_list_fails_the_save_and_keeps_the_file(
    tmp_path: Path,
):
    destination = _in_a_folder_granting_more(tmp_path)

    with (
        patch("aristotlelib.project.os.removexattr", side_effect=OSError(errno.ENOSPC, "No space left on device")),
        pytest.raises(OSError) as raised,
    ):
        await _get_files(destination)

    assert raised.value.errno == errno.ENOSPC
    assert destination.read_bytes() == b"old"
    assert [entry.name for entry in tmp_path.iterdir()] == ["result.tar.gz"]


@pytest.mark.skipif(os.name != "nt" and os.geteuid() == 0, reason="root writes a read-only file")
async def test_a_read_only_file_under_the_default_name_is_left_as_it_is(tmp_path: Path):
    destination = tmp_path / f"{ID}-aristotle.tar.gz"
    destination.write_bytes(b"old")
    destination.chmod(0o444)

    try:
        with pytest.raises(PermissionError):
            await _get_files(tmp_path)
        assert destination.read_bytes() == b"old"
        assert [entry.name for entry in tmp_path.iterdir()] == [destination.name]
    finally:
        destination.chmod(0o644)


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permission bits, which root bypasses")
async def test_a_file_in_a_folder_that_cannot_be_written_is_written_in_place(tmp_path: Path):
    destination = tmp_path / "locked" / "result.tar.gz"
    destination.parent.mkdir()
    destination.write_bytes(b"old")
    destination.parent.chmod(0o555)

    try:
        validate_destination(destination)
        await _get_files(destination)
    finally:
        destination.parent.chmod(0o755)

    assert destination.read_bytes() == b"archive"


@pytest.mark.parametrize("error", [errno.EACCES, errno.EROFS], ids=["closed to new files", "read-only"])
async def test_a_file_whose_folder_takes_no_new_file_is_written_in_place(error: int, tmp_path: Path, monkeypatch):
    destination = tmp_path / "result.tar.gz"
    destination.write_bytes(b"old")

    def refuse(*args, **kwargs):
        raise OSError(error, os.strerror(error))

    monkeypatch.setattr("aristotlelib.project.open", refuse, raising=False)
    await _get_files(destination)

    assert destination.read_bytes() == b"archive"
    assert [entry.name for entry in tmp_path.iterdir()] == ["result.tar.gz"]


@pytest.mark.parametrize(
    ("step", "error"),
    [
        ("replace", errno.EPERM),
        ("replace", errno.EBUSY),
        ("replace", errno.EXDEV),
        ("replace", errno.ENOTSUP),
        ("chmod", errno.ENOSYS),
    ],
    ids=["sticky folder", "mounted", "device", "no moves", "no modes"],
)
async def test_a_file_that_cannot_be_moved_over_is_written_in_place(step: str, error: int, tmp_path: Path):
    destination = tmp_path / "result.tar.gz"
    destination.write_bytes(b"old")

    with patch(f"aristotlelib.project.os.{step}", side_effect=OSError(error, os.strerror(error))):
        await _get_files(destination)

    assert destination.read_bytes() == b"archive"
    assert [entry.name for entry in tmp_path.iterdir()] == ["result.tar.gz"]


@pytest.mark.skipif(os.name == "nt", reason="Windows' 260-character path limit")
async def test_a_destination_name_as_long_as_the_file_system_takes_is_saved(tmp_path: Path):
    destination = tmp_path / ("a" * 248 + ".tar.gz")

    await _get_files(destination)

    assert destination.read_bytes() == b"archive"


async def test_get_files_raises_oserror_when_a_file_is_in_the_way(tmp_path: Path):
    (tmp_path / "notes.txt").write_text("notes")

    with pytest.raises(OSError):
        await _get_files(str(tmp_path / "notes.txt" / "result.tar.gz"))


@pytest.mark.parametrize(
    "destination",
    ["result.tar.gz", "existing.tar.gz", "folder", "folder/", "new/dir/result.tar.gz", "new/dir/", ""],
)
def test_validate_destination_accepts_what_get_files_can_save(destination: str, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "existing.tar.gz").write_bytes(b"old")
    (tmp_path / "folder").mkdir()

    validate_destination(destination)


@pytest.mark.parametrize(
    ("destination", "blocker"),
    [
        ("notes.txt/result.tar.gz", "notes.txt"),
        ("notes.txt/new/dir/result.tar.gz", "notes.txt"),
        ("notes.txt/", "notes.txt"),
    ],
)
def test_validate_destination_rejects_a_file_where_a_directory_must_be(
    destination: str, blocker: str, tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_text("notes")

    with pytest.raises(ValueError) as raised:
        validate_destination(destination)

    assert str(raised.value) == f"Cannot save the archive to {destination}: {blocker} is not a directory"


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permission bits, which root bypasses")
@pytest.mark.parametrize(
    ("destination", "unwritable"),
    [
        ("locked/result.tar.gz", "locked"),
        ("locked/new/dir/", "locked"),
        ("locked", "locked"),
        ("read-only", "read-only"),
        ("sealed/inner/result.tar.gz", "sealed"),
    ],
)
def test_validate_destination_rejects_what_cannot_be_written(
    destination: str, unwritable: str, tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "read-only").write_bytes(b"old")
    (tmp_path / "read-only").chmod(0o444)
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked").chmod(0o555)
    (tmp_path / "sealed" / "inner").mkdir(parents=True)
    (tmp_path / "sealed").chmod(0o000)

    try:
        with pytest.raises(ValueError) as raised:
            validate_destination(destination)
    finally:
        (tmp_path / "locked").chmod(0o755)
        (tmp_path / "sealed").chmod(0o755)

    assert str(raised.value) == f"Cannot save the archive to {destination}: {unwritable} is not writable"


@pytest.mark.parametrize("folder", ["out", "out/", "folder", "new/dir", "", "."])
def test_validate_destination_of_a_folder_accepts_one_that_exists_or_can_be_made(
    folder: str, tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "folder").mkdir()

    validate_destination(folder, folder=True)


@pytest.mark.parametrize(
    ("folder", "blocker"),
    [("notes.txt", "notes.txt"), ("notes.txt/", "notes.txt"), ("notes.txt/new/dir", "notes.txt")],
)
def test_validate_destination_of_a_folder_rejects_a_file_on_its_path(
    folder: str, blocker: str, tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_text("notes")

    with pytest.raises(ValueError) as raised:
        validate_destination(folder, folder=True)

    assert str(raised.value) == f"Cannot save the files to {folder}: {blocker} is not a directory"


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permission bits, which root bypasses")
@pytest.mark.parametrize(("folder", "unwritable"), [("locked", "locked"), ("locked/new/dir", "locked")])
def test_validate_destination_of_a_folder_rejects_one_that_cannot_be_written(
    folder: str, unwritable: str, tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked").chmod(0o555)

    try:
        with pytest.raises(ValueError) as raised:
            validate_destination(folder, folder=True)
    finally:
        (tmp_path / "locked").chmod(0o755)

    assert str(raised.value) == f"Cannot save the files to {folder}: {unwritable} is not writable"


@pytest.mark.parametrize(
    "file_path",
    ["trace.vcd", "sim/never_at_max_cex.vcd", "sim/tb/deep/trace_tb.sv", "sim/.hidden", "sim/..trace", "-trace.vcd"],
)
def test_a_relative_path_of_named_segments_is_a_path_in_the_project(file_path: str):
    assert file_path_error(file_path) is None


@pytest.mark.parametrize(
    ("file_path", "error"),
    [
        ("", "an empty path names no file"),
        ("sim/\0cex.vcd", "a path in the project can't hold a NUL"),
        ("sim\\cex.vcd", "a path in the project uses /, not a backslash"),
        ("C:\\cex.vcd", "a path in the project uses /, not a backslash"),
        ("\\\\host\\share\\cex.vcd", "a path in the project uses /, not a backslash"),
        ("/etc/passwd", "a path in the project is relative, so it can't start with /"),
        ("//host/share/cex.vcd", "a path in the project is relative, so it can't start with /"),
        ("C:cex.vcd", "a path in the project can't start with a drive"),
        ("C:/cex.vcd", "a path in the project can't start with a drive"),
        ("1:cex.vcd", "a path in the project can't start with a drive"),
        ("..", "a path in the project can't have a .. segment"),
        ("../etc/passwd", "a path in the project can't have a .. segment"),
        ("sim/../../etc/passwd", "a path in the project can't have a .. segment"),
        ("sim/..", "a path in the project can't have a .. segment"),
        (".", "a path in the project can't have a . segment"),
        ("./cex.vcd", "a path in the project can't have a . segment"),
        ("sim/./cex.vcd", "a path in the project can't have a . segment"),
        ("sim//cex.vcd", "a path in the project can't have an empty segment, as // or a / at its end leaves"),
        ("sim/", "a path in the project can't have an empty segment, as // or a / at its end leaves"),
    ],
)
def test_a_path_that_could_name_a_place_outside_its_folder_is_refused(file_path: str, error: str):
    assert file_path_error(file_path) == error


@pytest.mark.parametrize(
    ("file_path", "segment"),
    [
        ("sim/CON", "CON"),
        ("sim/nul.vcd", "nul.vcd"),
        ("COM1/cex.vcd", "COM1"),
        ("sim/cex.vcd:stream", "cex.vcd:stream"),
        ("sim/cex?.vcd", "cex?.vcd"),
        ("sim/cex.vcd.", "cex.vcd."),
        ("sim /cex.vcd", "sim "),
        ("...", "..."),
    ],
)
def test_on_windows_a_name_it_would_save_otherwise_is_refused(file_path: str, segment: str, monkeypatch):
    monkeypatch.setattr("aristotlelib.project.os.name", "posix")
    assert file_path_error(file_path) is None

    monkeypatch.setattr("aristotlelib.project.os.name", "nt")
    assert file_path_error(file_path) == f"Windows can't save a file or folder named {segment!r}"


@pytest.mark.parametrize(
    "file_path",
    [
        ".git",
        ".GIT/config",
        "x/.Git/hooks/pre-commit",
        "sim/.git",
        ".git./hooks/post-checkout",
        ".git /config",
        ".git::$INDEX_ALLOCATION/hooks/post-checkout",
        "GIT~1/hooks/pre-commit",
        "x/git~1/config",
        ".g\u200cit/hooks/pre-commit",
        "\ufeff.git/config",
    ],
)
def test_a_path_that_names_a_git_folder_as_git_reads_one_is_refused(file_path: str):
    assert file_path_error(file_path) == "a path in the project can't name a .git folder"


@pytest.mark.parametrize(
    "file_path",
    [".gitignore", ".github/workflows/ci.yml", "sim/.gitkeep", "rtl/fifo.git", ".git2/x", "git/x", "git~2/x"],
)
def test_a_name_that_only_starts_or_ends_as_git_is_a_path_in_the_project(file_path: str):
    assert file_path_error(file_path) is None


@pytest.mark.parametrize(
    "file_path",
    [
        "HEAD",
        "sim/HEAD",
        "sim/head",
        "sim/Head",
        "sim/HEAD.",
        "sim/HEAD ",
        "sim/HEAD::$DATA",
        "sim/HE\u200cAD",
        "sim/\ufeffHEAD",
    ],
)
def test_a_path_that_names_a_file_git_reads_as_head_is_refused(file_path: str):
    assert (
        file_path_error(file_path)
        == "a path in the project can't name a file HEAD: git takes a folder that holds one for a repository"
    )


@pytest.mark.parametrize(
    "file_path", ["rtl/head/x.sv", "rtl/HEAD/x.sv", "HEAD.v", "sim/header.sv", "sim/heads", "sim/ahead", "sim/HEAD.txt"]
)
def test_a_folder_named_head_or_a_name_that_only_starts_as_head_is_a_path_in_the_project(file_path: str):
    assert file_path_error(file_path) is None


def test_a_file_is_saved_at_its_path_under_the_folder(tmp_path: Path):
    assert saved_path(tmp_path / "out", "sim/tb/cex_tb.sv") == tmp_path / "out" / "sim" / "tb" / "cex_tb.sv"
    assert saved_path("", "sim/cex.vcd") == Path("sim", "cex.vcd")


def test_saved_path_refuses_what_file_path_error_refuses(tmp_path: Path):
    with pytest.raises(ValueError) as raised:
        saved_path(tmp_path, "sim/../../cex.vcd")

    assert str(raised.value) == "a path in the project can't have a .. segment"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize(
    ("link", "points_to"),
    [("sim", "outside"), ("sim/cex.vcd", "outside/cex.vcd"), ("sim/cex.vcd", "outside/gone.vcd")],
    ids=["a linked folder", "a linked file", "a dangling link"],
)
def test_a_place_a_link_leads_out_of_the_folder_is_refused(link: str, points_to: str, tmp_path: Path):
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "cex.vcd").write_bytes(b"theirs")
    (tmp_path / "out" / "sim").mkdir(parents=True)
    if link == "sim":
        (tmp_path / "out" / "sim").rmdir()
    (tmp_path / "out" / link).symlink_to(tmp_path / points_to)

    with pytest.raises(ValueError) as raised:
        saved_path(tmp_path / "out", "sim/cex.vcd")

    target = tmp_path / "out" / "sim" / "cex.vcd"
    assert str(raised.value) == f"{target} is outside {tmp_path / 'out'} once links are followed"
    assert (tmp_path / "outside" / "cex.vcd").read_bytes() == b"theirs"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_links_that_stay_inside_the_folder_are_followed(tmp_path: Path):
    (tmp_path / "real" / "traces").mkdir(parents=True)
    (tmp_path / "out").symlink_to(tmp_path / "real")
    (tmp_path / "real" / "sim").symlink_to("traces")

    assert saved_path(tmp_path / "out", "sim/cex.vcd") == tmp_path / "out" / "sim" / "cex.vcd"


def _checkout_with_repositories(folder: Path) -> bytes:
    """A checkout at ``folder`` holding a bare repository at vendor/lib, a hand-made one at vendor/layout and a
    submodule's .git file at vendor/sub; its .git/config as git init wrote it."""
    git(folder, "init", "-q")
    git(folder, "init", "-q", "--bare", "vendor/lib")
    (folder / "vendor" / "layout" / "objects").mkdir(parents=True)
    (folder / "vendor" / "layout" / "refs").mkdir()
    (folder / "vendor" / "layout" / "HEAD").write_text("ref: refs/heads/main\n")
    (folder / "vendor" / "sub").mkdir()
    (folder / "vendor" / "sub" / ".git").write_text("gitdir: ../../.git/modules/sub\n")
    return (folder / ".git" / "config").read_bytes()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize(
    ("links", "file_path"),
    [
        ({"sim": ".git"}, "sim/config"),
        ({"rtl/tb/sim": "../../.git/hooks"}, "rtl/tb/sim/pre-commit"),
        ({"sim": "hop", "hop": ".git"}, "sim/config"),
        ({"sim/config": "../.git/config"}, "sim/config"),
        ({"sim/repo": "../.git"}, "sim/repo"),
        ({"sim/gitfile": "../vendor/sub/.git"}, "sim/gitfile"),
        ({"sim": "vendor/lib"}, "sim/config"),
        ({"sim": "vendor/layout"}, "sim/config"),
    ],
    ids=[
        "a folder into .git",
        "a nested folder into .git/hooks",
        "a chain of links",
        "a file into .git",
        "the .git folder itself",
        "a submodule's .git file",
        "a bare repository",
        "a bare repository's layout, made by hand",
    ],
)
def test_a_place_a_link_leads_into_git_files_is_refused(links: dict[str, str], file_path: str, tmp_path: Path):
    config = _checkout_with_repositories(tmp_path)
    for link, points_to in links.items():
        (tmp_path / link).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / link).symlink_to(points_to)

    with pytest.raises(ValueError) as raised:
        saved_path(tmp_path, file_path)

    target = Path(tmp_path, *file_path.split("/"))
    assert str(raised.value) == f"{target} is inside a .git folder or a git directory once links are followed"
    assert (tmp_path / ".git" / "config").read_bytes() == config


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize(
    "name", [".GIT", ".Git", ".git.", ".git ", ".git::$INDEX_ALLOCATION", "GIT~1", ".g\u200cit", "\ufeff.git"]
)
def test_a_link_into_a_folder_git_reads_as_dot_git_is_refused(name: str, tmp_path: Path):
    (tmp_path / name).mkdir()
    (tmp_path / "sim").symlink_to(name)

    with pytest.raises(ValueError) as raised:
        saved_path(tmp_path, "sim/config")

    target = tmp_path / "sim" / "config"
    assert str(raised.value) == f"{target} is inside a .git folder or a git directory once links are followed"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("points_to", ["HEAD", "head", "HEAD.", "HEAD::$DATA", "../rtl/HEAD"])
def test_a_link_to_a_file_git_reads_as_head_is_refused(points_to: str, tmp_path: Path):
    (tmp_path / "sim").mkdir()
    (tmp_path / "rtl").mkdir()
    (tmp_path / "sim" / "cex.vcd").symlink_to(points_to)

    with pytest.raises(ValueError) as raised:
        saved_path(tmp_path, "sim/cex.vcd")

    target = tmp_path / "sim" / "cex.vcd"
    assert str(raised.value) == (
        f"{target} is a file HEAD once links are followed: git takes a folder that holds one for a repository"
    )


@pytest.mark.parametrize(
    "folder",
    [".git", ".git/hooks", ".GIT./x", ".git::$INDEX_ALLOCATION/x", "GIT~1/x", "vendor/lib", "vendor/lib/hooks/x"],
)
def test_a_folder_inside_a_git_folder_or_a_git_directory_is_refused(folder: str, tmp_path: Path, monkeypatch):
    _checkout_with_repositories(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError) as raised:
        saved_path(folder, "sim/cex.vcd")
    assert str(raised.value) == f"{folder} is inside a .git folder or a git directory"

    with pytest.raises(ValueError) as raised:
        validate_destination(folder, folder=True)
    assert str(raised.value) == f"Cannot save the files to {folder}: it is inside a .git folder or a git directory"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("points_to", [".git", ".git/hooks", "vendor/lib", "vendor/layout"])
def test_a_folder_that_links_into_a_git_folder_or_a_git_directory_is_refused(points_to: str, tmp_path: Path):
    _checkout_with_repositories(tmp_path)
    (tmp_path / "out").symlink_to(points_to)

    with pytest.raises(ValueError) as raised:
        saved_path(tmp_path / "out", "sim/cex.vcd")

    assert str(raised.value) == f"{tmp_path / 'out'} is inside a .git folder or a git directory"


@pytest.mark.parametrize("folder", ["", ".", "sim", ".github/out", "head/x", "fifo.git", "git~2"])
def test_a_checkout_and_folders_only_named_like_git_files_are_places_to_save(folder: str, tmp_path: Path, monkeypatch):
    _checkout_with_repositories(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert saved_path(folder, "sim/cex.vcd") == Path(folder, "sim", "cex.vcd")
    validate_destination(folder, folder=True)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("points_to", [".github", ".git2", "git~2", "head", "fifo.git"])
def test_a_link_to_a_folder_only_named_like_git_files_is_followed(points_to: str, tmp_path: Path):
    _checkout_with_repositories(tmp_path)
    (tmp_path / points_to).mkdir()
    (tmp_path / "sim").symlink_to(points_to)

    assert saved_path(tmp_path, "sim/cex.vcd") == tmp_path / "sim" / "cex.vcd"


def test_a_worktree_inside_a_bare_repository_is_a_place_to_save(tmp_path: Path):
    git(tmp_path, "init", "-q", "--bare", "proj.git")
    worktree = tmp_path / "proj.git" / "main"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: ../worktrees/main\n")

    assert saved_path(worktree, "sim/cex.vcd") == worktree / "sim" / "cex.vcd"
    validate_destination(worktree, folder=True)


def test_the_artifacts_command_names_only_paths_it_can_give_unquoted_and_counts_them():
    hint = download_artifacts_hint(ID, ["sim/cex.vcd", "sim\\cex.log", "../cex.sh"])

    assert hint == (f"Download 1 of the 3 with:\n  aristotle download {ID} --artifact sim/cex.vcd")
    assert download_artifacts_hint(ID, ["sim\\cex.log", "../cex.sh"]) is None
    assert download_artifacts_hint(ID, []) is None


@pytest.mark.parametrize(
    "file_path",
    [
        "sim/cex&calc.exe&rem .vcd",
        "sim/x\ncalc.exe & rem ",
        "sim/cex|more.vcd",
        "sim/$(calc).vcd",
        "sim/`calc`.vcd",
        "sim/%PATH%.vcd",
        "sim/my trace.vcd",
        "sim/cex\x1b.vcd",
        "sim/=cex.vcd",
        "--",
        "sim/a:b.vcd",
        "rtl/aux.v",
        "sim/cex.vcd.",
        "sim/HEAD",
    ],
)
def test_the_artifacts_command_leaves_out_a_path_some_shell_or_system_would_read_otherwise(file_path: str):
    hint = download_artifacts_hint(ID, ["sim/cex.vcd", file_path])

    assert hint == (f"Download 1 of the 2 with:\n  aristotle download {ID} --artifact sim/cex.vcd")


def test_the_artifacts_command_keeps_a_leading_dash_from_reading_as_an_option():
    assert download_artifacts_hint(ID, ["-cex.vcd", "sim/cex+1.log"]) == (
        f"Download them with:\n  aristotle download {ID} --artifact=-cex.vcd --artifact sim/cex+1.log"
    )


def _project_with_artifacts() -> Project:
    return Project(
        project_id=ID,
        status=ProjectStatus.IDLE,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=None,
        has_input=True,
        has_files=True,
    )


def _file(content: bytes) -> httpx.Response:
    return httpx.Response(200, content=content, headers={"content-type": "application/octet-stream"})


def _no_such_file(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"detail": f"File {request.url.params['file_path']} not found"})


async def _save(paths: list[str], folder: Path | str) -> list[tuple[str, Path | None]]:
    return [saved async for saved in _project_with_artifacts().save_files(paths, folder)]


def test_save_files_has_no_default_folder():
    with pytest.raises(TypeError, match="folder"):
        _project_with_artifacts().save_files(["sim/cex.vcd"])  # type: ignore[call-arg]


async def test_save_files_saves_each_file_at_its_path_and_replaces_one_there(api_server, tmp_path: Path):
    (tmp_path / "out" / "sim").mkdir(parents=True)
    (tmp_path / "out" / "sim" / "cex.vcd").write_bytes(b"old")
    requests = api_server(_file(b"$timescale"), _file(b"module tb;"))

    saved = await _save(["sim/cex.vcd", "sim/tb/cex_tb.sv"], tmp_path / "out")

    assert saved == [
        ("sim/cex.vcd", tmp_path / "out" / "sim" / "cex.vcd"),
        ("sim/tb/cex_tb.sv", tmp_path / "out" / "sim" / "tb" / "cex_tb.sv"),
    ]
    assert (tmp_path / "out" / "sim" / "cex.vcd").read_bytes() == b"$timescale"
    assert (tmp_path / "out" / "sim" / "tb" / "cex_tb.sv").read_bytes() == b"module tb;"
    assert [(r.method, r.url.path, dict(r.url.params)) for r in requests] == [
        ("GET", f"/api/v3/project/{ID}/file/download", {"file_path": "sim/cex.vcd"}),
        ("GET", f"/api/v3/project/{ID}/file/download", {"file_path": "sim/tb/cex_tb.sv"}),
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
async def test_a_saved_script_is_not_executable_unless_it_replaces_an_executable_file(api_server, tmp_path: Path):
    (tmp_path / "sim").mkdir()
    (tmp_path / "sim" / "old.sh").write_bytes(b"old")
    (tmp_path / "sim" / "old.sh").chmod(0o755)
    api_server(_file(b"#!/usr/bin/env bash\necho new\n"), _file(b"#!/usr/bin/env bash\necho old\n"))

    await _save(["sim/new.sh", "sim/old.sh"], tmp_path)

    assert (tmp_path / "sim" / "new.sh").stat().st_mode & 0o111 == 0
    assert (tmp_path / "sim" / "old.sh").read_bytes() == b"#!/usr/bin/env bash\necho old\n"
    assert (tmp_path / "sim" / "old.sh").stat().st_mode & 0o777 == 0o755


async def test_save_files_reads_every_file_over_one_connection(tmp_path: Path):
    response = Mock(content=b"trace")
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client) as connect:
        await _save(["a.vcd", "b.vcd", "c.vcd"], tmp_path)

    connect.assert_called_once_with()
    assert client.get.await_count == 3


async def test_a_file_the_project_lacks_yields_none_and_the_rest_are_still_saved(api_server, tmp_path: Path):
    api_server(_file(b"vcd"), _no_such_file, _file(b"log"))

    saved = await _save(["sim/cex.vcd", "sim/gone.vcd", "sim/cex.log"], tmp_path)

    assert saved == [
        ("sim/cex.vcd", tmp_path / "sim" / "cex.vcd"),
        ("sim/gone.vcd", None),
        ("sim/cex.log", tmp_path / "sim" / "cex.log"),
    ]
    assert sorted(p.name for p in (tmp_path / "sim").iterdir()) == ["cex.log", "cex.vcd"]


async def test_a_project_the_key_cannot_see_is_not_found(api_server, tmp_path: Path):
    api_server(httpx.Response(403, json={"detail": "Forbidden"}))

    with pytest.raises(AristotleNotFoundError) as raised:
        await _save(["sim/cex.vcd"], tmp_path)

    assert (str(raised.value), raised.value.status_code) == (f"Project {ID} not found or not accessible.", 403)


async def test_a_file_request_rides_out_a_server_error_as_every_get_does(api_server, tmp_path: Path):
    requests = api_server(httpx.Response(503), _file(b"vcd"))

    assert await _save(["sim/cex.vcd"], tmp_path) == [("sim/cex.vcd", tmp_path / "sim" / "cex.vcd")]
    assert len(requests) == 2


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
async def test_a_link_that_appears_while_a_file_downloads_is_refused_before_its_write(api_server, tmp_path: Path):
    (tmp_path / "outside").mkdir()
    (tmp_path / "out").mkdir()

    def link_then_file(request: httpx.Request) -> httpx.Response:
        (tmp_path / "out" / "sim").symlink_to(tmp_path / "outside")
        return _file(b"vcd")

    api_server(link_then_file)

    with pytest.raises(ValueError, match="is outside .* once links are followed"):
        await _save(["sim/cex.vcd"], tmp_path / "out")

    assert list((tmp_path / "outside").iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize(
    ("swapped", "folder", "file_path", "error"),
    [
        ("sim", ".", "sim/config", "{target} is inside a .git folder or a git directory once links are followed"),
        ("out", "out", "config", "{folder} is inside a .git folder or a git directory"),
    ],
    ids=["a folder in the destination", "the destination"],
)
async def test_a_folder_swapped_for_a_link_into_git_files_while_a_file_downloads_is_refused_before_its_write(
    swapped: str, folder: str, file_path: str, error: str, api_server, tmp_path: Path
):
    git(tmp_path, "init", "-q")
    config = (tmp_path / ".git" / "config").read_bytes()
    (tmp_path / swapped).mkdir()

    def swap_then_file(request: httpx.Request) -> httpx.Response:
        (tmp_path / swapped).rmdir()
        (tmp_path / swapped).symlink_to(".git")
        return _file(b"[core]\n\tfsmonitor = touch fsmonitor-ran\n")

    requests = api_server(swap_then_file)

    with pytest.raises(ValueError) as raised:
        await _save([file_path], tmp_path / folder)

    target = Path(tmp_path / folder, *file_path.split("/"))
    assert str(raised.value) == error.format(target=target, folder=tmp_path / folder)
    assert len(requests) == 1
    assert (tmp_path / ".git" / "config").read_bytes() == config


async def test_a_refused_path_raises_before_its_request_and_after_the_ones_before_it(api_server, tmp_path: Path):
    requests = api_server(_file(b"vcd"))
    files = _project_with_artifacts().save_files(["sim/cex.vcd", "../cex.vcd"], tmp_path)

    assert await files.__anext__() == ("sim/cex.vcd", tmp_path / "sim" / "cex.vcd")
    with pytest.raises(ValueError, match=r"can't have a \.\. segment"):
        await files.__anext__()
    assert len(requests) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sim"]


async def test_project_get_tasks():
    """Test getting tasks for a project."""
    project = Project(
        project_id="test-tasks",
        status=ProjectStatus.RUNNING,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=None,
        has_input=True,
        has_files=False,
    )

    mock_response = Mock()
    mock_response.json.return_value = {
        "agent_tasks": [
            {
                "project_id": "test-tasks",
                "agent_task_id": "task-1",
                "status": "IN_PROGRESS",
                "created_at": "2024-01-01T00:00:00",
                "last_updated_at": "2024-01-01T00:30:00",
                "percent_complete": 50,
                "file_name": None,
                "description": "First task",
                "output_summary": None,
            },
        ],
        "pagination_key": None,
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        tasks, pagination_key = await project.get_tasks(limit=10)

    assert len(tasks) == 1
    assert tasks[0].agent_task_id == "task-1"
    assert tasks[0].status == TaskStatus.IN_PROGRESS
    assert tasks[0].percent_complete == 50
    assert pagination_key is None


async def test_project_ask():
    """Test asking a project."""
    project = Project(
        project_id="test-ask",
        status=ProjectStatus.IDLE,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=None,
        has_input=True,
        has_files=True,
    )

    mock_response = Mock()
    mock_response.json.return_value = {
        "project_id": "test-ask",
        "agent_task_id": "new-task-123",
        "status": "QUEUED",
        "created_at": "2024-01-01T00:00:00",
        "last_updated_at": "2024-01-01T00:00:00",
        "percent_complete": None,
        "file_name": None,
        "description": "Continue work",
        "output_summary": None,
    }

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        task = await project.ask("Continue with the next step")

    assert task.agent_task_id == "new-task-123"
    assert task.status == TaskStatus.QUEUED

    assert "mode" not in mock_client.post.call_args.kwargs["data"]


async def test_task_status_unknown_handling():
    """Test that unknown status values are handled gracefully."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "project_id": "proj-unknown",
        "agent_task_id": "task-unknown",
        "status": "SOME_NEW_STATUS_FROM_API",
        "created_at": "2024-01-01T00:00:00",
        "last_updated_at": "2024-01-01T00:00:00",
        "percent_complete": None,
        "file_name": None,
        "description": None,
        "output_summary": None,
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        task = await AgentTask.from_id("task-unknown")

    # Should be mapped to UNKNOWN status
    assert task.status == TaskStatus.UNKNOWN


async def test_project_str_representation():
    """Test the string representation of a project."""
    project = Project(
        project_id="test-str",
        status=ProjectStatus.IDLE,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description="Test project description",
        has_input=True,
        has_files=True,
    )

    result = str(project)
    assert "test-str" in result
    assert "Test project description" in result
    assert "IDLE" in result


def test_project_web_url(monkeypatch):
    monkeypatch.delenv("ARISTOTLE_WEB_URL", raising=False)
    project = Project(
        project_id="project-123",
        status=ProjectStatus.RUNNING,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=None,
        has_input=False,
        has_files=False,
    )

    assert project.web_url() == "https://aristotle.harmonic.fun/projects/project-123"

    monkeypatch.setenv("ARISTOTLE_WEB_URL", "https://alt.example.test/")
    assert project.web_url() == "https://alt.example.test/projects/project-123"


def test_project_log_created_includes_web_url(caplog, monkeypatch):
    monkeypatch.delenv("ARISTOTLE_WEB_URL", raising=False)
    project = Project(
        project_id="project-123",
        status=ProjectStatus.RUNNING,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=None,
        has_input=False,
        has_files=False,
    )

    with caplog.at_level(logging.INFO):
        project.log_created()

    assert caplog.messages == [
        "Project created: project-123",
        "Open in Aristotle Web: https://aristotle.harmonic.fun/projects/project-123",
    ]


async def test_task_str_representation():
    """Test the string representation of a task."""
    task = AgentTask(
        project_id="proj-str",
        agent_task_id="task-str",
        status=TaskStatus.IN_PROGRESS,
        created_at=datetime.now(),
        last_updated_at=datetime.now(),
        percent_complete=75,
        file_name=None,
        description="Working on something",
        output_summary=None,
    )

    lines = str(task).splitlines()
    assert lines[0].startswith("IN_PROGRESS (started ")
    assert lines[1:] == ["Working on something", "Task: task-str", "Project: proj-str"]
    assert "%" not in str(task)


@pytest.mark.parametrize(
    ("status", "percent_complete", "verb"),
    [
        (TaskStatus.QUEUED, None, "submitted"),
        (TaskStatus.CANCELED, None, "submitted"),
        (TaskStatus.FAILED, None, "submitted"),
        (TaskStatus.CANCELED, 1, "started"),
        (TaskStatus.FAILED, 100, "started"),
        (TaskStatus.IN_PROGRESS, 1, "started"),
        (TaskStatus.COMPLETE, 100, "started"),
        (TaskStatus.OUT_OF_BUDGET, 100, "started"),
        (TaskStatus.UNKNOWN, None, "submitted"),
        (TaskStatus.UNKNOWN, 1, "started"),
    ],
)
def test_a_task_that_ended_with_no_progress_recorded_shows_when_it_was_submitted_not_started(
    status: TaskStatus, percent_complete: int | None, verb: str
):
    task = _waiting_task()
    task.status = status
    task.percent_complete = percent_complete

    assert str(task).splitlines()[0].startswith(f"{status.name} ({verb} ")


@pytest.mark.parametrize(
    ("fields", "verb"),
    [
        ({"started_at": "2024-01-01T00:00:30", "percent_complete": None}, "started"),
        ({"started_at": None, "percent_complete": None}, "submitted"),
        ({"started_at": "2024-01-01T00:00:30"}, "started"),
        ({"started_at": None}, "submitted"),
        ({"percent_complete": 1}, "started"),
        ({"percent_complete": None}, "submitted"),
    ],
    ids=[
        "start time",
        "no start time",
        "start time, no progress key",
        "no start time, no progress key",
        "no start time key, progress",
        "no start time key, no progress",
    ],
)
def test_a_cancelled_task_started_when_the_server_sends_its_start_time_or_else_progress(
    fields: dict[str, object], verb: str
):
    task = AgentTask.model_validate(
        {
            "project_id": "proj-cancel",
            "agent_task_id": "task-cancel",
            "status": "CANCELED",
            "created_at": "2024-01-01T00:00:00",
            "last_updated_at": "2024-01-01T01:00:00",
            "file_name": None,
            "description": None,
            "output_summary": None,
            **fields,
        }
    )

    assert str(task).splitlines()[0].startswith(f"CANCELED ({verb} ")


async def test_task_wait_for_completion_already_complete():
    """Test wait_for_completion when task completes via SSE stream."""
    task = AgentTask(
        project_id="proj-wait",
        agent_task_id="task-wait-complete",
        status=TaskStatus.QUEUED,
        created_at=datetime.now(),
        last_updated_at=datetime.now(),
        percent_complete=None,
        file_name=None,
        description=None,
        output_summary=None,
    )

    mock_refresh_response = Mock()
    mock_refresh_response.json.return_value = {
        "project_id": "proj-wait",
        "agent_task_id": "task-wait-complete",
        "status": "COMPLETE",
        "created_at": "2024-01-01T00:00:00",
        "last_updated_at": "2024-01-01T01:00:00",
        "percent_complete": 100,
        "file_name": None,
        "description": None,
        "output_summary": "Done!",
    }

    async def mock_stream_sse(endpoint, **_):
        # Yield nothing - stream ends immediately (task complete)
        return
        yield  # Make this an async generator

    mock_client = AsyncMock()
    mock_client.stream_sse = mock_stream_sse
    mock_client.get = AsyncMock(return_value=mock_refresh_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        with patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])):
            with patch("aristotlelib.progress_utils.clear_progress_lines"):
                with patch("aristotlelib.progress_utils.print_progress_lines"):
                    await task.wait_for_completion(interactive=True)

    assert task.status == TaskStatus.COMPLETE


@pytest.mark.parametrize("event_type", [EventType.THINKING, EventType.AGENT_FOLLOW_UP])
async def test_task_get_events(event_type):
    """Test getting events for a task."""
    task = AgentTask(
        project_id="proj-events",
        agent_task_id="task-events",
        status=TaskStatus.IN_PROGRESS,
        created_at=datetime.now(),
        last_updated_at=datetime.now(),
        percent_complete=50,
        file_name=None,
        description=None,
        output_summary=None,
    )

    mock_response = Mock()
    mock_response.json.return_value = {
        "events": [
            {
                "event_id": "event-1",
                "agent_task_id": "task-events",
                "event_type": int(event_type),
                "created_at": "2024-01-01T00:00:00",
                "content": "Thinking about the problem",
                "file_path": None,
                "explanation": "Analyzing input",
                "status": 1,  # COMPLETE
            },
        ],
        "pagination_key": None,
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=mock_client):
        events, pagination_key = await task.get_events(limit=10)

    assert len(events) == 1
    assert events[0].event_id == "event-1"
    assert events[0].event_type == event_type
    assert events[0].model_dump(mode="json")["event_type"] == int(event_type)
    assert pagination_key is None


def _upload_mock_client():
    mock_response = Mock()
    mock_response.json.return_value = {
        "project_id": "counted-1",
        "status": 1,
        "created_at": "2024-01-01T00:00:00",
        "last_updated": "2024-01-01T00:00:00",
        "description": None,
        "has_input": True,
        "has_files": False,
    }
    mock_client = AsyncMock()

    async def post(endpoint: str, **kwargs) -> Mock:
        # The archive is a file, closed once the request is done.
        name, archive, _ = kwargs["files"][0][1]
        mock_client.uploaded = (name, archive.read())
        return mock_response

    mock_client.post = AsyncMock(side_effect=post)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


async def _uploaded(project_dir: Path, include_ignored: bool = False) -> tuple[str, bytes]:
    """The file name and the archive that creating a project from ``project_dir`` uploads."""
    client = _upload_mock_client()
    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch(
            "aristotlelib.project.local_file_utils.get_expected_toolchain",
            new=AsyncMock(return_value="leanprover/lean4:v4.24.0"),
        ),
    ):
        await Project.create_from_directory(
            prompt="Verify this", project_dir=project_dir, include_ignored=include_ignored
        )

    return client.uploaded


async def _upload(project_dir: Path, include_ignored: bool = False) -> dict[str, bytes | None]:
    """The uploaded archive's entries: a file's content, or None for any other kind of entry."""
    _, archive = await _uploaded(project_dir, include_ignored)
    # Read as UTF-8, as Aristotle reads an upload.
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz", encoding="utf-8") as tar:
        return {m.name: tar.extractfile(m).read() if m.isfile() else None for m in tar.getmembers()}


async def _refused_upload(project_dir: Path) -> tuple[str, AsyncMock, AsyncMock]:
    """The error creating a project from ``project_dir`` raises, and the mocks of the toolchain's fetch and the create."""
    client = _upload_mock_client()
    toolchain = AsyncMock(return_value="leanprover/lean4:v4.24.0")
    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.project.local_file_utils.get_expected_toolchain", new=toolchain),
        pytest.raises(LeanProjectError) as info,
    ):
        await Project.create_from_directory(prompt="Verify this", project_dir=project_dir)
    return str(info.value), toolchain, client.post


async def test_an_empty_folder_is_refused_before_anything_is_sent(tmp_path):
    message, toolchain, create = await _refused_upload(tmp_path)

    assert message == f"Nothing to upload: {tmp_path} is empty."
    toolchain.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (["Scratch.lean"], "its only file matches an ignore rule"),
        (["Scratch.lean", "Notes.lean", "README.md"], "all 3 of its files match an ignore rule"),
    ],
    ids=["one", "several"],
)
async def test_a_folder_whose_files_an_ignore_rule_all_holds_back_is_refused_before_anything_is_sent(
    files, expected, tmp_path
):
    git(tmp_path, "init", "-q")
    (tmp_path / ".git" / "info" / "exclude").write_text("*\n")
    for name in files:
        (tmp_path / name).write_text("-- a file\n")

    message, toolchain, create = await _refused_upload(tmp_path)

    assert message == f"Nothing to upload from {tmp_path}: {expected}."
    # The .lean files would have it fetch Aristotle's toolchain first.
    toolchain.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (".env", "its only file is left out, as noted above"),
        (".DS_Store", "its only file, .DS_Store, is one Aristotle skips"),
        (".lake/packages/mathlib/Mathlib.lean", "it holds no file Aristotle reads"),
    ],
    ids=["credentials", "finder droppings", "only skipped folders"],
)
async def test_a_folder_whose_files_are_all_left_out_otherwise_is_refused_too(name, expected, tmp_path):
    (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text("-- a file\n")

    message, toolchain, create = await _refused_upload(tmp_path)

    assert message == f"Nothing to upload from {tmp_path}: {expected}."
    toolchain.assert_not_awaited()
    create.assert_not_awaited()


async def test_a_folder_whose_every_verilog_source_an_ignore_rule_leaves_out_is_refused_before_anything_is_sent(
    tmp_path,
):
    """With nothing else to verify, as when the Verilog is generated from templates and ignored."""
    git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("*.v\n")
    (tmp_path / "top.vp").write_text("module `mname`; endmodule\n")
    (tmp_path / "top.v").write_text("module top; endmodule\n")

    message, toolchain, create = await _refused_upload(tmp_path)

    assert message.startswith(
        f"No Verilog source would go up from {tmp_path}: an ignore rule matches each of its .v and .sv files:\n"
        "  top.v  (.gitignore:1: *.v)\n"
    )
    toolchain.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permission bits, which root bypasses")
@pytest.mark.parametrize(
    ("mode", "files"),
    # Without search permission, Python 3.10 to 3.13 raised PermissionError looking for lake-manifest.json first.
    [(0o000, ["Main.lean"]), (0o300, ["Main.lean"]), (0o600, ["Main.lean"]), (0o600, [])],
    ids=["none", "not readable", "not enterable", "not enterable, empty"],
)
async def test_a_folder_that_cant_be_read_is_refused_and_said_so(mode, files, tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    for name in files:
        (folder / name).write_text("-- a file\n")
    folder.chmod(mode)
    try:
        message, toolchain, create = await _refused_upload(folder)
    finally:
        folder.chmod(0o700)

    assert message == f"Nothing to upload: {folder} can't be read (Permission denied)."
    toolchain.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permission bits, which root bypasses")
async def test_a_folder_whose_only_other_files_are_in_a_folder_that_cant_be_read_names_it(tmp_path):
    """Rather than call .DS_Store its only file, which the folder it can't read may not leave it."""
    (tmp_path / ".DS_Store").write_bytes(b"\0")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Main.lean").write_text("-- a file\n")
    (tmp_path / "src").chmod(0o300)
    try:
        message, toolchain, create = await _refused_upload(tmp_path)
    finally:
        (tmp_path / "src").chmod(0o700)

    assert (
        message == f"Nothing to upload from {tmp_path}: its folder src can't be read, and nothing else in it goes up."
    )
    toolchain.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permission bits, which root bypasses")
@pytest.mark.parametrize("name", ["lake-manifest.json", "lean-toolchain", ".lake"])
async def test_a_lean_file_linked_into_a_folder_it_cant_enter_is_left_out_not_a_crash(name, tmp_path):
    locked = tmp_path / "locked"
    (locked / ".lake").mkdir(parents=True)
    (locked / "lake-manifest.json").write_text('{"packages": []}\n')
    (locked / "lean-toolchain").write_text("leanprover/lean4:v4.24.0\n")
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "Main.lean").write_text("-- a file\n")
    (folder / name).symlink_to(locked / name)
    locked.chmod(0o600)
    try:
        entries = await _upload(folder)
    finally:
        locked.chmod(0o700)

    assert list(entries) == ["Main.lean"]


async def test_result_archives_an_earlier_run_saved_are_left_out_and_named(tmp_path, caplog):
    """As the next submit from the folder submit --wait saved one in would otherwise upload it."""
    (tmp_path / "top.sv").write_text("module top; endmodule\n")
    project_id = "3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f"
    saved = [f"{project_id}-aristotle.tar.gz", f"input-{project_id}.tar.gz", "fifo proofs_aristotle.tar.gz"]
    for name in saved:
        (tmp_path / name).write_bytes(b"\x1f\x8b")
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / f"{project_id}-aristotle.tar.gz").write_bytes(b"\x1f\x8b")
    kept = ["aristotle.tar.gz", "notes_aristotle.txt", "input-1.tar.gz", "build-aristotle.tar.gz.bak"]
    for name in kept:
        (tmp_path / name).write_bytes(b"x")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        entries = await _upload(tmp_path)

    assert sorted(entries) == sorted(["top.sv", *kept])
    left_out = sorted([*saved, f"results/{project_id}-aristotle.tar.gz"])
    assert (
        "Not uploading 4 result archives earlier runs saved; rename one to upload it: " + ", ".join(left_out)
    ) in caplog.messages


@pytest.mark.parametrize(
    ("content_disposition", "description"),
    [
        ('attachment; filename="3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f-aristotle.tar.gz"', None),
        ('attachment; filename="input-3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f.tar.gz"', None),
        ("", "fifo proofs"),
        ("", None),
        ("", "..."),
    ],
)
def test_every_name_the_cli_saves_an_archive_under_is_a_result_archive(content_disposition, description):
    """So that the archive saved by one submit is never part of the next one's upload."""
    project = _listed("3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f", description)
    assert local_file_utils.is_result_archive(Path(project._archive_name(content_disposition)))


async def test_a_file_whose_name_is_not_utf8_is_left_out_of_the_upload_and_named(tmp_path, caplog):
    (tmp_path / "top.sv").write_text("module top; endmodule\n")
    latin1_name(tmp_path)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        entries = await _upload(tmp_path)

    assert list(entries) == ["top.sv"]
    assert (
        "Not uploading 1 file(s) whose name is not UTF-8, which Aristotle can't take; rename one to upload it: "
        "caf\\xe9.v"
    ) in caplog.messages
    assert "Uploading 1 file." in caplog.messages


_UPLOAD_UNDER_AN_ASCII_LOCALE = """
import asyncio, sys
from pathlib import Path
from tests.project_test import _uploaded
if sys.getfilesystemencoding() != "ascii":
    sys.exit(99)
Path(sys.argv[2]).write_bytes(asyncio.run(_uploaded(Path(sys.argv[1])))[1])
"""


def test_under_an_ascii_locale_a_utf8_name_goes_up_as_the_text_it_spells(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "top.sv").write_text("module top; endmodule\n")
    (project_dir / "naïve.v").write_text("module naive; endmodule\n")
    latin1_name(project_dir)
    archive = tmp_path / "upload.tar.gz"
    env = {**os.environ, "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C"}

    run = subprocess.run(
        [sys.executable, "-c", _UPLOAD_UNDER_AN_ASCII_LOCALE, str(project_dir), str(archive)],
        env=env,
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=60,
    )

    if run.returncode == 99:
        pytest.skip("file names here aren't decoded with the locale's encoding")
    assert run.returncode == 0, run.stderr
    # Read as UTF-8, as Aristotle reads an upload.
    with tarfile.open(archive, encoding="utf-8") as tar:
        assert sorted(tar.getnames()) == ["naïve.v", "top.sv"]


_ARCHIVED_UNDER_AN_ASCII_LOCALE = """
import json, sys
from pathlib import Path
from aristotlelib.project import build_archive
if sys.getfilesystemencoding() != "ascii":
    sys.exit(99)
print(json.dumps(build_archive(Path(sys.argv[1]), Path(sys.argv[2])).names))
"""


def test_the_folder_walk_leaves_out_a_git_directory_under_any_name(tmp_path):
    """A vendored lib.git holds git's own files, its config with a remote's token among them."""
    from aristotlelib.project import build_archive

    (tmp_path / "proj" / "vendor" / "lib.git" / "objects").mkdir(parents=True)
    (tmp_path / "proj" / "vendor" / "lib.git" / "refs").mkdir()
    (tmp_path / "proj" / "vendor" / "lib.git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / "proj" / "vendor" / "lib.git" / "config").write_text("[remote]\n  url = https://placeholder@x/y\n")
    (tmp_path / "proj" / "Main.lean").write_text("theorem t : True := trivial\n")
    archived = build_archive(tmp_path / "proj", tmp_path / "out.tar.gz")
    assert archived.names == ["Main.lean"]


def test_the_folder_walk_leaves_out_a_bare_repository_holding_a_stray_git_folder(tmp_path):
    from aristotlelib.project import build_archive

    bare = tmp_path / "proj" / "vendor" / "lib.git"
    (bare / ".git").mkdir(parents=True)
    (bare / "config").write_text("[core]\n\trepositoryformatversion = 0\n[remote]\n  url = https://placeholder@x/y\n")
    (bare / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / "proj" / "Main.lean").write_text("theorem t : True := trivial\n")
    assert build_archive(tmp_path / "proj", tmp_path / "out.tar.gz").names == ["Main.lean"]


def test_a_folder_that_is_itself_config_leaves_out_the_clis_credentials(tmp_path):
    from aristotlelib.project import build_archive

    config = tmp_path / ".config"
    (config / "gh").mkdir(parents=True)
    (config / "gh" / "hosts.yml").write_text("github.com:\n  oauth_token: placeholder\n")
    (config / "Main.lean").write_text("theorem t : True := trivial\n")
    assert build_archive(config, tmp_path / "out.tar.gz").names == ["Main.lean"]


def test_under_an_ascii_locale_the_archived_names_are_the_archives(tmp_path):
    """What a dry run lists is what the archive holds."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "naïve.v").write_text("module naive; endmodule\n")
    archive = tmp_path / "upload.tar.gz"
    env = {**os.environ, "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C"}

    run = subprocess.run(
        [sys.executable, "-c", _ARCHIVED_UNDER_AN_ASCII_LOCALE, str(project_dir), str(archive)],
        env=env,
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=60,
    )

    if run.returncode == 99:
        pytest.skip("file names here aren't decoded with the locale's encoding")
    assert run.returncode == 0, run.stderr
    with tarfile.open(archive, encoding="utf-8") as tar:
        assert json.loads(run.stdout.splitlines()[-1]) == tar.getnames() == ["naïve.v"]


async def test_a_group_name_that_is_not_utf8_does_not_stop_a_utf8_name_going_up(tmp_path, monkeypatch):
    (tmp_path / "naïve.v").write_text("module naive; endmodule\n")
    # As under an ASCII locale: tarfile's default encoding is ASCII, and a UTF-8 group name such as a German
    # domain's decodes with surrogates, so tarfile writes the header in that encoding.
    monkeypatch.setattr(tarfile.TarFile, "encoding", "ascii")
    monkeypatch.setattr(tarfile, "grp", Mock(getgrgid=lambda gid: ("dom\udcc3\udca4nen-benutzer",)))

    assert list(await _upload(tmp_path)) == ["naïve.v"]


@pytest.mark.parametrize(
    ("name", "upload_name"),
    [(b"r\xe9sum\xe9", "r\\xe9sum\\xe9.tar.gz"), (b"\xe9" * 64, "\\xe9" * 64 + ".tar.gz")],
    ids=["short", "longer than a file name may be once escaped"],
)
async def test_a_folder_whose_name_is_not_utf8_goes_up_under_its_escaped_name(name, upload_name, tmp_path):
    if os.name == "nt":
        pytest.skip("Windows file names are Unicode")
    folder = tmp_path / os.fsdecode(name)
    try:
        folder.mkdir()
    except OSError:
        pytest.skip("this file system takes only UTF-8 names")
    (folder / "top.sv").write_text("module top; endmodule\n")

    assert (await _uploaded(folder))[0] == upload_name


@pytest.mark.parametrize("project_dir", [".", os.path.join("rtl", "..")])
async def test_a_folder_given_as_dot_or_dotdot_goes_up_under_its_own_name(project_dir, tmp_path, monkeypatch):
    """As ``submit --project-dir .`` names the project after the folder, not "project"."""
    folder = tmp_path / "ecc_block"
    (folder / "rtl").mkdir(parents=True)
    (folder / "top.sv").write_text("module top; endmodule\n")
    monkeypatch.chdir(folder)

    assert (await _uploaded(Path(project_dir)))[0] == "ecc_block.tar.gz"


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
async def test_a_link_given_by_name_keeps_it_and_dotdot_after_it_names_what_the_os_opens(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere" / "rtl_target"
    target.mkdir(parents=True)
    (target / "top.sv").write_text("module top; endmodule\n")
    (tmp_path / "ecc_block").mkdir()
    (tmp_path / "ecc_block" / "rtlink").symlink_to(target)
    monkeypatch.chdir(tmp_path / "ecc_block")

    assert (await _uploaded(Path("rtlink")))[0] == "rtlink.tar.gz"
    assert (await _uploaded(Path("rtlink", "..")))[0] == "elsewhere.tar.gz"


@pytest.mark.skipif(os.name == "nt", reason="Windows can't remove the working folder")
def test_a_working_folder_that_is_gone_has_no_name(tmp_path, monkeypatch):
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()

    assert _folder_name(Path(".")) == ""


async def test_a_file_that_cant_be_archived_keeps_its_os_error_details(tmp_path):
    """An ArchiveError is the OSError it wraps, errno and filename included, worded as one line."""
    (tmp_path / "top.sv").write_text("module top; endmodule\n")
    gone = FileNotFoundError(2, "No such file or directory", str(tmp_path / "top.sv"))

    with patch("aristotlelib.project._build_archive", side_effect=gone), pytest.raises(ArchiveError) as raised:
        await _uploaded(tmp_path)

    assert isinstance(raised.value, OSError)
    assert (raised.value.errno, raised.value.filename) == (2, str(tmp_path / "top.sv"))
    assert str(raised.value) == f"Could not archive {tmp_path / 'top.sv'}: No such file or directory. Try again."
    assert raised.value.__cause__ is gone
    copied = pickle.loads(pickle.dumps(raised.value))
    assert (type(copied), copied.errno, str(copied)) == (ArchiveError, 2, str(raised.value))


async def test_the_upload_says_what_gitignore_held_back(caplog):
    """A gitignored source file is counted and reported."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        (temp_path / ".gitignore").write_text("vendor/\n")
        (temp_path / "top.sv").write_text("module top; endmodule\n")
        vendor = temp_path / "vendor"
        vendor.mkdir()
        (vendor / "fifo.sv").write_text("module fifo; endmodule\n")
        (vendor / "arbiter.sv").write_text("module arbiter; endmodule\n")

        with caplog.at_level(logging.INFO, logger="aristotle"):
            await _upload(temp_path)

    # .gitignore itself is uploaded, so two files go up and the two vendored ones do not.
    assert "Uploading 2 files; 2 left out because they match an ignore rule." in caplog.text


async def test_a_project_with_nothing_excluded_says_only_what_went_up(caplog):
    """No .gitignore, nothing held back, and no clause implying there was."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        (temp_path / "top.sv").write_text("module top; endmodule\n")

        with caplog.at_level(logging.INFO, logger="aristotle"):
            await _upload(temp_path)

    assert "Uploading 1 file." in caplog.text
    assert "gitignore" not in caplog.text


async def test_editor_droppings_are_not_counted_against_gitignore(caplog):
    """Files the built-in patterns skip are not attributed to .gitignore."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        # A .gitignore that does not mention .DS_Store, so the spec is loaded and consulted
        # and the file is still attributed to the built-in patterns rather than to it.
        (temp_path / ".gitignore").write_text("*.log\n")
        (temp_path / "top.sv").write_text("module top; endmodule\n")
        (temp_path / ".DS_Store").write_bytes(b"\x00\x01")

        with caplog.at_level(logging.INFO, logger="aristotle"):
            await _upload(temp_path)

    # top.sv and .gitignore; .DS_Store skipped and credited to neither.
    assert "Uploading 2 files." in caplog.text
    assert "gitignore excludes" not in caplog.text


def _symlink(link: Path, target: Path | str, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("this platform cannot create symlinks")


def _symlink_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "symbolic link" in r.getMessage()]


async def test_a_symlinked_file_inside_the_project_goes_up_as_its_content(caplog):
    with tempfile.TemporaryDirectory() as temp_dir:
        project = Path(temp_dir)
        (project / "shared").mkdir()
        (project / "shared" / "common.sv").write_bytes(b"module common; endmodule\n")
        _symlink(project / "common.sv", Path("shared") / "common.sv")

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(project)

    assert entries == {"shared/common.sv": b"module common; endmodule\n", "common.sv": b"module common; endmodule\n"}
    assert "Uploading 2 files." in caplog.text
    assert _symlink_warnings(caplog) == []


async def test_broken_looping_and_outward_symlinks_are_left_out_with_one_warning(caplog):
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "id_ed25519").write_bytes(b"private key\n")
        project = root / "project"
        project.mkdir()
        (project / "top.sv").write_bytes(b"module top; endmodule\n")
        _symlink(project / "key", root / "id_ed25519")
        _symlink(project / "relative-key", Path("..") / "id_ed25519")
        _symlink(project / "broken.sv", "missing.sv")
        _symlink(project / "loop.sv", "loop.sv")

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(project)

    assert entries == {"top.sv": b"module top; endmodule\n"}
    assert _symlink_warnings(caplog) == [
        "WARNING: Left out 4 symbolic links; a link is uploaded only when the file it points to is uploaded too:\n"
        "  broken.sv (broken link)\n"
        "  key (target not uploaded)\n"
        "  loop.sv (broken link)\n"
        "  relative-key (target not uploaded)"
    ]
    assert "Uploading 1 file." in caplog.text


async def test_a_symlink_to_a_file_the_upload_excludes_is_left_out(caplog):
    with tempfile.TemporaryDirectory() as temp_dir:
        project = Path(temp_dir)
        (project / ".gitignore").write_bytes(b"secret.txt\n")
        (project / "secret.txt").write_bytes(b"token\n")
        (project / ".env").write_bytes(b"TOKEN=1\n")
        (project / ".git").mkdir()
        (project / ".git" / "config").write_bytes(b"[remote]\n")
        _symlink(project / "notes.txt", "secret.txt")
        _symlink(project / "env.txt", ".env")
        _symlink(project / "git-config", Path(".git") / "config")

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(project)

    assert set(entries) == {".gitignore"}
    assert _symlink_warnings(caplog) == [
        "WARNING: Left out 3 symbolic links; a link is uploaded only when the file it points to is uploaded too:\n"
        "  env.txt (target not uploaded)\n"
        "  git-config (target not uploaded)\n"
        "  notes.txt (target not uploaded)"
    ]
    assert "Uploading 1 file; 1 left out because it matches an ignore rule." in caplog.text


@pytest.mark.parametrize("target", ["a.sv/..", "a.sv/../src"], ids=["file/..", "file/../subdir"])
async def test_a_symlink_through_a_file_is_broken_and_archives_no_directory(target, caplog):
    with tempfile.TemporaryDirectory() as temp_dir:
        project = Path(temp_dir)
        (project / ".gitignore").write_bytes(b"secret.txt\n")
        (project / "a.sv").write_bytes(b"a\n")
        (project / ".env").write_bytes(b"TOKEN=1\n")
        (project / "src").mkdir()
        (project / "src" / "secret.txt").write_bytes(b"token\n")
        (project / "src" / ".env").write_bytes(b"TOKEN=2\n")
        _symlink(project / "notes", target)

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(project)

    assert entries == {".gitignore": b"secret.txt\n", "a.sv": b"a\n"}
    assert _symlink_warnings(caplog) == [
        "WARNING: Left out 1 symbolic link; a link is uploaded only when the file it points to is uploaded too:\n"
        "  notes (broken link)"
    ]
    assert "Uploading 2 files; 1 left out because it matches an ignore rule." in caplog.text


def _special_file(path: Path, kind: str, stack: ExitStack) -> None:
    """Create a named pipe or a listening Unix socket at ``path``, or skip the test where the platform cannot."""
    if kind == "named pipe":
        if not hasattr(os, "mkfifo"):
            pytest.skip("no named pipes on this platform")
        os.mkfifo(path)
        return
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("no Unix sockets on this platform")
    server = stack.enter_context(socket.socket(socket.AF_UNIX))
    try:
        server.bind(str(path))
    except OSError as e:
        pytest.skip(f"cannot bind a Unix socket at {path}: {e}")


@pytest.mark.parametrize("kind", ["named pipe", "socket"])
async def test_named_pipes_and_sockets_are_neither_archived_nor_counted(kind, caplog):
    with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
        project = Path(temp_dir)
        (project / "top.sv").write_bytes(b"module top; endmodule\n")
        _special_file(project / "special", kind, stack)

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(project)

    assert entries == {"top.sv": b"module top; endmodule\n"}
    assert "Uploading 1 file." in caplog.text


def _without_inode_numbers(stat_function):
    """``stat_function``, reporting inode number 0 as some network and Windows filesystems do.

    Every other field is kept, including the platform-specific ones that TemporaryDirectory's cleanup reads on Windows.
    """

    def stat_(*args, **kwargs):
        result = stat_function(*args, **kwargs)
        fields = list(result)
        fields[1] = 0
        in_sequence = set(type(result).__match_args__)
        extra = {n: getattr(result, n) for n in dir(result) if n.startswith("st_") and n not in in_sequence}
        return os.stat_result(fields, extra)

    return stat_


async def test_on_a_filesystem_without_inode_numbers_no_symlink_is_uploaded(caplog):
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "id_ed25519").write_bytes(b"private key\n")
        project = root / "project"
        project.mkdir()
        (project / "top.sv").write_bytes(b"module top; endmodule\n")
        _symlink(project / "key", Path("..") / "id_ed25519")
        _symlink(project / "copy.sv", "top.sv")

        with (
            patch.object(os, "stat", _without_inode_numbers(os.stat)),
            patch.object(os, "lstat", _without_inode_numbers(os.lstat)),
            patch.object(os, "fstat", _without_inode_numbers(os.fstat)),
            caplog.at_level(logging.INFO, logger="aristotle"),
        ):
            entries = await _upload(project)

    assert entries == {"top.sv": b"module top; endmodule\n"}
    assert _symlink_warnings(caplog) == [
        "WARNING: Left out 2 symbolic links; a link is uploaded only when the file it points to is uploaded too:\n"
        "  copy.sv (target not uploaded)\n"
        "  key (target not uploaded)"
    ]
    assert "Uploading 1 file." in caplog.text


async def test_symlinks_are_followed_when_the_project_directory_is_given_through_one():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        real = root / "real"
        real.mkdir()
        (real / "a.sv").write_bytes(b"a\n")
        _symlink(real / "relative.sv", "a.sv")
        _symlink(real / "absolute.sv", real.resolve() / "a.sv")
        alias = root / "alias"
        _symlink(alias, real, directory=True)

        entries = await _upload(alias)

    assert entries == {"a.sv": b"a\n", "relative.sv": b"a\n", "absolute.sv": b"a\n"}


def test_each_name_of_a_file_goes_up_as_a_file_holding_its_content(tmp_path, caplog):
    """A hard link, and links to a file with two names and to one with one; links leading out of the folder or nowhere,
    and a linked folder, stay out."""
    from aristotlelib.project import build_archive

    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    (project / "a.sv").write_bytes(b"module a; endmodule\n")
    (project / "solo.sv").write_bytes(b"module solo; endmodule\n")
    (tmp_path / "id_ed25519").write_bytes(b"private key\n")
    hard_link(project / "a.sv", project / "sub" / "b.sv")
    _symlink(project / "a_link.sv", "a.sv")
    _symlink(project / "solo_link.sv", "solo.sv")
    _symlink(project / "key", tmp_path / "id_ed25519")
    _symlink(project / "broken.sv", "missing.sv")
    _symlink(project / "sub_link", "sub", directory=True)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        archived = build_archive(project, tmp_path / "upload.tar.gz")

    with tarfile.open(tmp_path / "upload.tar.gz", encoding="utf-8") as tar:
        members = {m.name: (m.type, tar.extractfile(m).read() if m.isfile() else None) for m in tar.getmembers()}
    a, solo = (tarfile.REGTYPE, b"module a; endmodule\n"), (tarfile.REGTYPE, b"module solo; endmodule\n")
    assert members == {
        "a.sv": a,
        "a_link.sv": a,
        "solo.sv": solo,
        "solo_link.sv": solo,
        "sub/b.sv": a,
    }
    # What --dry-run lists.
    assert sorted(archived.names) == ["a.sv", "a_link.sv", "solo.sv", "solo_link.sv", "sub/b.sv"]
    assert _symlink_warnings(caplog) == [
        "WARNING: Left out 2 symbolic links; a link is uploaded only when the file it points to is uploaded too:\n"
        "  broken.sv (broken link)\n"
        "  key (target not uploaded)"
    ]


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="without O_NOFOLLOW the open's device and inode are checked")
def test_a_folder_whose_descriptors_give_another_device_and_inode_goes_up(tmp_path, monkeypatch):
    """As overlayfs gave them before Linux 4.19, for a checkout in a container's own file system."""
    from aristotlelib.project import build_archive

    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    (project / "a.sv").write_bytes(b"module a; endmodule\n")
    (project / "top.sv").write_bytes(b"module top; endmodule\n")
    hard_link(project / "a.sv", project / "sub" / "b.sv")
    monkeypatch.setattr(os, "fstat", fstat_on_another_device(os.fstat))

    archived = build_archive(project, tmp_path / "upload.tar.gz")

    with tarfile.open(tmp_path / "upload.tar.gz", encoding="utf-8") as tar:
        members = {m.name: (m.type, tar.extractfile(m).read()) for m in tar.getmembers()}
    a = (tarfile.REGTYPE, b"module a; endmodule\n")
    assert members == {"a.sv": a, "sub/b.sv": a, "top.sv": (tarfile.REGTYPE, b"module top; endmodule\n")}
    assert sorted(archived.names) == ["a.sv", "sub/b.sv", "top.sv"]


def test_a_file_swapped_for_a_link_while_the_folder_is_archived_is_refused_not_followed(tmp_path):
    from aristotlelib.project import build_archive

    project = tmp_path / "project"
    project.mkdir()
    (project / "top.sv").write_bytes(b"module top; endmodule\n")
    (tmp_path / "id_ed25519").write_bytes(b"private key\n")

    with (
        swapped_for_a_link(project / "top.sv", tmp_path / "id_ed25519"),
        pytest.raises(ArchiveError) as raised,
    ):
        build_archive(project, tmp_path / "upload.tar.gz")

    assert str(raised.value) == (
        f"Could not archive {project / 'top.sv'}: it changed while it was being uploaded. Try again."
    )
    assert b"private key" not in gzip.decompress((tmp_path / "upload.tar.gz").read_bytes())


def _secret_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "matching secret patterns" in r.getMessage()]


async def test_every_credential_pattern_is_left_out_in_either_case_and_named(caplog):
    names = [pattern.replace("*", "x") for pattern in SECRET_FILE_PATTERNS]
    spellings = ((".", names), ("lower", names), ("upper", [n.upper() for n in names]))
    with tempfile.TemporaryDirectory() as temp_dir:
        project = Path(temp_dir)
        (project / "top.sv").write_bytes(b"module top; endmodule\n")
        # Separate folders, so that a case-insensitive volume keeps both spellings.
        for folder, spelled in spellings:
            (project / folder).mkdir(exist_ok=True)
            for name in spelled:
                (project / folder / name).write_bytes(b"machine example.invalid login x password y\n")

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(project)

    assert entries == {"top.sv": b"module top; endmodule\n"}
    every = sorted(Path(folder) / name for folder, spelled in spellings for name in spelled)
    assert _secret_warnings(caplog) == [
        f"Excluded {len(every)} file(s) matching secret patterns:" + "".join(f"\n  {p.as_posix()}" for p in every)
    ]
    assert "Uploading 1 file." in caplog.text


async def test_a_file_inside_a_hidden_folder_named_like_a_credential_file_is_left_out(caplog):
    with tempfile.TemporaryDirectory() as temp_dir:
        project = Path(temp_dir)
        (project / "top.sv").write_bytes(b"module top; endmodule\n")
        (project / ".env").mkdir()
        (project / ".env" / "production").write_bytes(b"TOKEN=1\n")

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(project)

    assert entries == {"top.sv": b"module top; endmodule\n"}
    assert _secret_warnings(caplog) == ["Excluded 1 file(s) matching secret patterns: .env/production"]


async def test_docker_logins_and_an_envrc_are_left_out(caplog):
    with tempfile.TemporaryDirectory() as temp_dir:
        project = Path(temp_dir)
        (project / "top.sv").write_bytes(b"module top; endmodule\n")
        (project / ".docker").mkdir()
        (project / ".docker" / "config.json").write_bytes(
            b'{"auths": {"registry.example.invalid": {"auth": "eDp5"}}}\n'
        )
        (project / ".envrc").write_bytes(b"export TOKEN=1\n")

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(project)

    assert entries == {"top.sv": b"module top; endmodule\n"}
    assert _secret_warnings(caplog) == ["Excluded 2 file(s) matching secret patterns: .docker/config.json, .envrc"]


async def test_aws_credentials_are_left_out_but_files_named_after_credentials_go_up(caplog):
    with tempfile.TemporaryDirectory() as temp_dir:
        project = Path(temp_dir)
        (project / ".aws").mkdir()
        (project / ".aws" / "credentials").write_bytes(b"[default]\naws_access_key_id = EXAMPLEINVALID\n")
        (project / "credentials_test.lean").write_bytes(b"theorem t : True := trivial\n")
        (project / "Credentials").mkdir()
        (project / "Credentials" / "Basic.lean").write_bytes(b"def c := 1\n")

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(project)

    assert set(entries) == {"credentials_test.lean", "Credentials/Basic.lean"}
    assert _secret_warnings(caplog) == ["Excluded 1 file(s) matching secret patterns: .aws/credentials"]


async def test_nothing_named_like_a_credential_means_no_warning(caplog):
    with tempfile.TemporaryDirectory() as temp_dir:
        project = Path(temp_dir)
        (project / "top.sv").write_bytes(b"module top; endmodule\n")

        with caplog.at_level(logging.INFO, logger="aristotle"):
            await _upload(project)

    assert _secret_warnings(caplog) == []


async def test_in_a_git_repository_a_nested_gitignore_holds_files_back(caplog):
    with tempfile.TemporaryDirectory() as temp_dir:
        project = Path(temp_dir)
        git(project, "init", "-q")
        (project / "top.sv").write_bytes(b"module top; endmodule\n")
        (project / "rtl" / "gen").mkdir(parents=True)
        (project / "rtl" / ".gitignore").write_bytes(b"gen/\n")
        (project / "rtl" / "gen" / "out.sv").write_bytes(b"module out; endmodule\n")

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(project)

    assert set(entries) == {"top.sv", "rtl/.gitignore"}
    assert "Uploading 2 files; 1 left out because it matches an ignore rule." in caplog.text


async def test_a_subfolder_of_a_git_repository_is_held_to_the_repositorys_gitignore(caplog):
    with tempfile.TemporaryDirectory() as temp_dir:
        repo = Path(temp_dir)
        git(repo, "init", "-q")
        (repo / ".gitignore").write_bytes(b"*.vcd\n")
        (repo / "rtl").mkdir()
        (repo / "rtl" / "fifo.sv").write_bytes(b"module fifo; endmodule\n")
        (repo / "rtl" / "wave.vcd").write_bytes(b"$date\n")

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(repo / "rtl")

    assert entries == {"fifo.sv": b"module fifo; endmodule\n"}
    assert "Uploading 1 file; 1 left out because it matches an ignore rule." in caplog.text


async def test_lake_goes_up_even_when_git_ignores_it():
    with tempfile.TemporaryDirectory() as temp_dir:
        project = Path(temp_dir)
        git(project, "init", "-q")
        (project / ".gitignore").write_bytes(b".lake\n")
        (project / "Main.lean").write_bytes(b"theorem t : True := trivial\n")
        (project / ".lake" / "packages" / "custom").mkdir(parents=True)
        (project / ".lake" / "packages" / "custom" / "Custom.lean").write_bytes(b"def c := 1\n")

        entries = await _upload(project)

    assert set(entries) == {".gitignore", "Main.lean", ".lake/packages/custom/Custom.lean"}


async def test_leans_build_files_go_up_even_when_an_ignore_rule_matches_them(caplog):
    with tempfile.TemporaryDirectory() as temp_dir:
        project = Path(temp_dir)
        git(project, "init", "-q")
        (project / ".gitignore").write_bytes(b"*.json\nlakefile.*\nlean-toolchain\n")
        (project / "Main.lean").write_bytes(b"theorem t : True := trivial\n")
        names = ["lake-manifest.json", "lakefile.lean", "lean-toolchain", "Pkg/lakefile.toml", "Pkg/lake-manifest.json"]
        for name in names:
            (project / name).parent.mkdir(exist_ok=True)
            (project / name).write_bytes(b"{}\n")
        git(project, "add", "-f", "lake-manifest.json")

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(project)

    assert set(entries) == {".gitignore", "Main.lean", *names}
    assert "Uploading 7 files." in caplog.messages
    assert "Not uploading" not in caplog.text


async def test_a_tracked_file_an_ignore_rule_matches_is_held_back_and_named(caplog):
    with tempfile.TemporaryDirectory() as temp_dir:
        repo = Path(temp_dir)
        git(repo, "init", "-q")
        (repo / ".gitignore").write_bytes(b"build/\n*.log\n")
        (repo / "build").mkdir()
        (repo / "build" / "keep.h").write_bytes(b"#define KEEP 1\n")
        (repo / "top.sv").write_bytes(b"module top; endmodule\n")
        (repo / "run.log").write_bytes(b"debug\n")
        git(repo, "add", "-f", "build/keep.h", "top.sv", ".gitignore")

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(repo)

    assert set(entries) == {".gitignore", "top.sv"}
    assert (
        local_file_utils.held_back_warning({Path("build/keep.h"): ".gitignore:1: build/"}, include_ignored=True)
        in caplog.messages
    )
    assert "Uploading 2 files; 2 left out because they match an ignore rule." in caplog.messages


@pytest.mark.parametrize("where", ["info/exclude", "global excludes"])
async def test_only_a_gitignore_holds_back_a_tracked_file_while_any_rule_leaves_out_an_untracked_one(
    caplog, monkeypatch: pytest.MonkeyPatch, where: str
):
    with tempfile.TemporaryDirectory() as temp_dir:
        repo, home = Path(temp_dir) / "repo", Path(temp_dir) / "home"
        repo.mkdir()
        home.mkdir()
        git(repo, "init", "-q")
        (repo / "top.sv").write_bytes(b"module top; endmodule\n")
        (repo / "keep.h").write_bytes(b"#define KEEP 1\n")
        (repo / "junk.h").write_bytes(b"scratch\n")
        git(repo, "add", "top.sv", "keep.h")
        if where == "info/exclude":
            with open(repo / ".git" / "info" / "exclude", "a") as exclude:
                exclude.write("*.h\n")
        else:
            # Named .gitignore, as many global excludes files are; git gives its path whole, not from the repository.
            (home / ".gitignore").write_text("*.h\n")
            (home / "gitconfig").write_text(f"[core]\n\texcludesFile = {(home / '.gitignore').as_posix()}\n")
            monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(repo)

    assert set(entries) == {"top.sv", "keep.h"}
    assert not [message for message in caplog.messages if message.startswith("Not uploading")]
    assert "Uploading 2 files; 1 left out because it matches an ignore rule." in caplog.messages


async def test_a_global_rule_on_a_folder_hides_no_gitignore_rule_inside_it(caplog, monkeypatch: pytest.MonkeyPatch):
    # git names the global folder rule as the one deciding; submit holds the file back for the .gitignore, as ci does.
    with tempfile.TemporaryDirectory() as temp_dir:
        repo, home = Path(temp_dir) / "repo", Path(temp_dir) / "home"
        (repo / "sub").mkdir(parents=True)
        home.mkdir()
        git(repo, "init", "-q")
        (repo / "top.sv").write_bytes(b"module top; endmodule\n")
        (repo / "sub" / "x.log").write_bytes(b"debug\n")
        (repo / ".gitignore").write_bytes(b"*.log\n")
        git(repo, "add", "-f", "top.sv", "sub/x.log", ".gitignore")
        (home / "ignore").write_text("sub/\n")
        (home / "gitconfig").write_text(f"[core]\n\texcludesFile = {(home / 'ignore').as_posix()}\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(repo)

    assert set(entries) == {".gitignore", "top.sv"}
    assert (
        local_file_utils.held_back_warning({Path("sub/x.log"): ".gitignore:1: *.log"}, include_ignored=True)
        in caplog.messages
    )


@pytest.mark.parametrize("global_rule", ["*.sv", "sub/"], ids=["a file rule", "a folder rule over the submodule"])
async def test_a_submodules_tracked_files_are_held_back_by_its_gitignore_and_the_superprojects(
    caplog, monkeypatch: pytest.MonkeyPatch, global_rule: str
):
    with tempfile.TemporaryDirectory() as temp_dir:
        source, repo = Path(temp_dir) / "source", Path(temp_dir) / "repo"
        source.mkdir()
        repo.mkdir()
        git(source, "init", "-q")
        (source / "keep.sv").write_bytes(b"module keep; endmodule\n")
        (source / "x.log").write_bytes(b"debug\n")
        (source / "y.tmp").write_bytes(b"scratch\n")
        (source / ".gitignore").write_bytes(b"*.tmp\n")
        git(source, "add", "-f", "-A")
        git(source, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "sub")
        git(repo, "init", "-q")
        (repo / "top.sv").write_bytes(b"module top; endmodule\n")
        (repo / ".gitignore").write_bytes(b"*.log\n")
        git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "sub")
        git(repo, "add", "top.sv", ".gitignore")
        # A global rule alone holds back no tracked file, the repository's or the submodule's, and a folder rule of
        # its hides no .gitignore's rule.
        (Path(temp_dir) / "ignore").write_text(f"{global_rule}\n")
        (Path(temp_dir) / "gitconfig").write_text(
            f"[core]\n\texcludesFile = {(Path(temp_dir) / 'ignore').as_posix()}\n"
        )
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(Path(temp_dir) / "gitconfig"))

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(repo)

    assert {"top.sv", "sub/keep.sv"} <= set(entries)
    assert not {"sub/x.log", "sub/y.tmp"} & set(entries)
    rules = {Path("sub/x.log"): ".gitignore:1: *.log", Path("sub/y.tmp"): "sub/.gitignore:1: *.tmp"}
    assert local_file_utils.held_back_warning(rules, include_ignored=True) in caplog.messages


async def test_a_file_a_gitignore_lets_back_in_goes_up_under_a_global_rule_on_its_folder(
    caplog, monkeypatch: pytest.MonkeyPatch
):
    with tempfile.TemporaryDirectory() as temp_dir:
        repo = Path(temp_dir) / "repo"
        (repo / "build").mkdir(parents=True)
        git(repo, "init", "-q")
        (repo / "build" / "keep.v").write_bytes(b"module keep; endmodule\n")
        (repo / "build" / "notes.txt").write_bytes(b"generated\n")
        (repo / ".gitignore").write_bytes(b"build/*\n!build/keep.v\n")
        git(repo, "add", "-f", "build/keep.v", "build/notes.txt", ".gitignore")
        (Path(temp_dir) / "ignore").write_text("build/\n")
        (Path(temp_dir) / "gitconfig").write_text(
            f"[core]\n\texcludesFile = {(Path(temp_dir) / 'ignore').as_posix()}\n"
        )
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(Path(temp_dir) / "gitconfig"))

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(repo)

    assert "build/keep.v" in entries
    assert (
        local_file_utils.held_back_warning({Path("build/notes.txt"): ".gitignore:1: build/*"}, include_ignored=True)
        in caplog.messages
    )


async def test_a_repository_that_ignores_case_holds_back_as_ci_does(caplog):
    with tempfile.TemporaryDirectory() as temp_dir:
        repo = Path(temp_dir)
        git(repo, "init", "-q")
        git(repo, "config", "core.ignoreCase", "true")
        (repo / "top.sv").write_bytes(b"module top; endmodule\n")
        (repo / "x.log").write_bytes(b"debug\n")
        (repo / ".gitignore").write_bytes(b"*.LOG\n")
        git(repo, "add", "-f", "top.sv", "x.log", ".gitignore")

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(repo)

    assert "x.log" not in entries
    assert (
        local_file_utils.held_back_warning({Path("x.log"): ".gitignore:1: *.LOG"}, include_ignored=True)
        in caplog.messages
    )


async def test_outside_git_a_nested_gitignore_does_not_hold_files_back(caplog):
    with tempfile.TemporaryDirectory() as temp_dir:
        project = Path(temp_dir)
        (project / "rtl").mkdir()
        (project / "rtl" / ".gitignore").write_bytes(b"*.sv\n")
        (project / "rtl" / "fifo.sv").write_bytes(b"module fifo; endmodule\n")

        with caplog.at_level(logging.INFO, logger="aristotle"):
            entries = await _upload(project)

    assert set(entries) == {"rtl/.gitignore", "rtl/fifo.sv"}
    assert "Uploading 2 files." in caplog.text


@pytest.mark.parametrize(
    ("where", "folder", "expected"),
    [
        ("a nested .gitignore", ".", {"rtl/.gitignore", "rtl/top.sv", "rtl/gen/out.v"}),
        ("info/exclude", ".", {"rtl/top.sv", "rtl/gen/out.v"}),
        ("the global excludes file", ".", {"rtl/top.sv", "rtl/gen/out.v"}),
        ("outside a repository", ".", {".gitignore", "rtl/top.sv", "rtl/gen/out.v"}),
        ("a folder git ignores", "rtl", {".gitignore", "top.sv", "gen/out.v"}),
    ],
)
async def test_include_ignored_uploads_what_an_ignore_rule_matches_wherever_the_rule_is(
    caplog, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, where: str, folder: str, expected: set[str]
):
    repo = tmp_path / "repo"
    (repo / "rtl" / "gen").mkdir(parents=True)
    (repo / "rtl" / "top.sv").write_bytes(b"module top; out o(); endmodule\n")
    (repo / "rtl" / "gen" / "out.v").write_bytes(b"module out; endmodule\n")
    if where != "outside a repository":
        git(repo, "init", "-q")
    if where == "info/exclude":
        with open(repo / ".git" / "info" / "exclude", "a") as exclude:
            exclude.write("gen/\n")
    elif where == "the global excludes file":
        (tmp_path / "ignore").write_text("gen/\n")
        (tmp_path / "gitconfig").write_text(f"[core]\n\texcludesFile = {(tmp_path / 'ignore').as_posix()}\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    elif where == "outside a repository":
        (repo / ".gitignore").write_bytes(b"gen/\n")
    else:
        if where == "a folder git ignores":
            (repo / ".gitignore").write_bytes(b"rtl/\n")
        (repo / "rtl" / ".gitignore").write_bytes(b"gen/\n")
    out = "gen/out.v" if folder == "rtl" else "rtl/gen/out.v"

    assert set(await _upload(repo / folder)) == expected - {out}
    with caplog.at_level(logging.INFO, logger="aristotle"):
        assert set(await _upload(repo / folder, include_ignored=True)) == expected

    assert caplog.messages[-1] == f"Uploading {len(expected)} files; 1 of them matches an ignore rule."
    assert "Not uploading" not in caplog.text


async def test_include_ignored_still_leaves_out_git_credentials_result_archives_and_what_aristotle_skips(
    caplog, tmp_path: Path
):
    git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_bytes(b"*\n!.gitignore\n")
    archive_names = [f"{uuid.UUID(int=1)}-aristotle.tar.gz", f"input-{uuid.UUID(int=2)}.tar.gz"]
    git_config = b"[core]\n\trepositoryformatversion = 0\n"
    files = {
        "top.sv": b"module top; endmodule\n",
        "build/out.v": b"module out; endmodule\n",
        ".env": b"TOKEN=x\n",
        "certs/server.key": b"not a key\n",
        ".ssh/config": b"Host x\n",
        ".config/gh/hosts.yml": b"github.com:\n",
        **dict.fromkeys(archive_names, b"an earlier run's archive\n"),
        ".DS_Store": b"\x00",
        "vendor/lib/.git/config": git_config,
        "vendor/mirror.git/config": git_config,
        ".lake/build/ir/Main.c": b"int main;\n",
    }
    for name, content in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(content)
    (tmp_path / "vendor" / "mirror.git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        entries = await _upload(tmp_path, include_ignored=True)

    assert set(entries) == {".gitignore", "top.sv", "build/out.v"}
    assert (
        "Excluded 4 file(s) matching secret patterns: .config/gh/hosts.yml, .env, .ssh/config, certs/server.key"
    ) in caplog.messages
    assert (
        "Not uploading 2 result archives earlier runs saved; rename one to upload it: " + ", ".join(archive_names)
    ) in caplog.messages
    assert "Uploading 3 files; 2 of them match an ignore rule." in caplog.messages


async def test_include_ignored_still_leaves_out_a_name_that_isnt_utf8(caplog, tmp_path: Path):
    (tmp_path / ".gitignore").write_bytes(b"*.v\n")
    (tmp_path / "top.sv").write_bytes(b"module top; endmodule\n")
    name = latin1_name(tmp_path)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        entries = await _upload(tmp_path, include_ignored=True)

    assert set(entries) == {".gitignore", "top.sv"}
    assert local_file_utils.not_utf8_warning([Path(name)], "file") in caplog.messages
    assert "Uploading 2 files." in caplog.messages


async def test_include_ignored_uploads_a_link_to_an_ignored_file_but_not_one_that_leads_out(caplog, tmp_path: Path):
    (tmp_path / "outside.v").write_bytes(b"module outside; endmodule\n")
    project = tmp_path / "project"
    (project / "gen").mkdir(parents=True)
    (project / ".gitignore").write_bytes(b"gen/\n")
    (project / "top.sv").write_bytes(b"module top; endmodule\n")
    (project / "gen" / "out.v").write_bytes(b"module out; endmodule\n")
    _symlink(project / "gen" / "alias.v", "out.v")
    _symlink(project / "gen" / "escape.v", tmp_path / "outside.v")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        entries = await _upload(project, include_ignored=True)

    assert set(entries) == {".gitignore", "top.sv", "gen/out.v", "gen/alias.v"}
    assert entries["gen/alias.v"] == b"module out; endmodule\n"
    assert local_file_utils.skipped_symlinks_warning({Path("gen/escape.v"): "target not uploaded"}) in "\n".join(
        _symlink_warnings(caplog)
    )
    assert "Uploading 4 files; 2 of them match an ignore rule." in caplog.messages


async def test_include_ignored_adds_nothing_a_junction_reaches(caplog, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """os.walk enters a Windows junction, though it enters no folder link; a real folder stands in for one here."""
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_bytes(b"module.exports = 1;\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.v").write_bytes(b"module out; endmodule\n")
    (tmp_path / ".gitignore").write_bytes(b"node_modules/\nbuild/\n")
    (tmp_path / "top.sv").write_bytes(b"module top; endmodule\n")
    junction = tmp_path / "node_modules" / "pkg"
    monkeypatch.setattr(local_file_utils, "is_junction", lambda path: path == junction)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        entries = await _upload(tmp_path, include_ignored=True)

    assert set(entries) == {".gitignore", "top.sv", "build/out.v"}
    assert "Uploading 3 files; 1 of them matches an ignore rule." in caplog.messages


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows'")
async def test_include_ignored_adds_nothing_a_real_junction_reaches(tmp_path: Path):
    import _winapi

    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "config.json").write_bytes(b"{}\n")
    project = tmp_path / "project"
    (project / "node_modules").mkdir(parents=True)
    (project / ".gitignore").write_bytes(b"node_modules/\n")
    (project / "top.sv").write_bytes(b"module top; endmodule\n")
    _winapi.CreateJunction(str(tmp_path / "elsewhere"), str(project / "node_modules" / "pkg"))

    assert local_file_utils.is_junction(project / "node_modules" / "pkg")
    assert set(await _upload(project, include_ignored=True)) == {".gitignore", "top.sv"}


async def test_the_library_names_include_ignored_for_verilog_an_ignore_rule_leaves_out(tmp_path: Path):
    git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_bytes(b"*.v\n")
    (tmp_path / "top.vp").write_bytes(b"module `mname`; endmodule\n")
    (tmp_path / "top.v").write_bytes(b"module top; endmodule\n")

    message, _, create = await _refused_upload(tmp_path)

    create.assert_not_awaited()
    assert (
        "To upload them, pass --include-ignored (from Python, the include_ignored argument), which uploads the files "
        "ignore rules keep out, or describe the design with a filelist"
    ) in message
    assert set(await _upload(tmp_path, include_ignored=True)) == {".gitignore", "top.vp", "top.v"}


async def test_git_failing_to_say_what_it_ignores_uploads_nothing(tmp_path):
    client = _upload_mock_client()
    (tmp_path / "top.sv").write_bytes(b"module top; endmodule\n")
    failure = LeanProjectError("Could not ask git which files in it ignores: fatal: index file corrupt")

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch.object(local_file_utils, "gitignored_files", side_effect=failure),
        pytest.raises(LeanProjectError, match="index file corrupt"),
    ):
        await Project.create_from_directory(prompt="Verify this", project_dir=tmp_path)

    client.post.assert_not_called()


async def test_an_empty_project_dir_is_refused_rather_than_read_as_the_current_directory():
    client = _upload_mock_client()

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        pytest.raises(ValueError, match="Project directory cannot be empty"),
    ):
        await Project.create_from_directory(prompt="Verify this", project_dir="")

    client.post.assert_not_called()


# How the CLI says to carry on a wait that ends before its task, as carry_on_hint renders it.
RESUME = "Carry on waiting with:\n  aristotle show proj-wait --task task-wait --wait --json"


def _waiting_task() -> AgentTask:
    return AgentTask(
        project_id="proj-wait",
        agent_task_id="task-wait-q",
        status=TaskStatus.IN_PROGRESS,
        created_at=datetime.now(),
        last_updated_at=datetime.now(),
        percent_complete=10,
        file_name=None,
        description=None,
        output_summary=None,
    )


def _pending_question(number: int, response: str | None = None) -> Question:
    return Question(
        question_id=f"q-{number}",
        event_id=f"event-q{number}",
        project_id="proj-wait",
        agent_run_id="task-wait-q",
        content=f"Question number {number}?",
        suggestions=["Yes", "No"],
        response=response,
        created_at=datetime(2024, 1, 1, 0, number),
        updated_at=datetime(2024, 1, 1, 0, number),
    )


def _question_event(event_id: str, status: int = 1, content: str = "Asked a question") -> dict:
    """An AGENT_QUESTION event as the stream sends it: COMPLETE for a durable question, SENT for a legacy one."""
    return {
        "event_id": event_id,
        "agent_task_id": "task-wait-q",
        "event_type": 17,
        "created_at": "2024-01-01T00:00:00",
        "content": content,
        "file_path": None,
        "explanation": None,
        "suggestions": None,
        "status": status,
    }


def _last_live_frame(shown: Mock) -> list[str]:
    """The display's lines before the final frame, which is drawn once the run has ended."""
    return shown.call_args_list[-2].args[0]


def _streaming_client(
    events: list[dict],
    read_only: bool = False,
    live_status: str = "IN_PROGRESS",
    end_status: str = "COMPLETE",
    end_percent: int | None = 100,
) -> AsyncMock:
    """A client whose stream sends ``events``; the task refreshes as ``live_status`` until the stream ends, then as
    ``end_status`` at ``end_percent``."""
    mock_client = AsyncMock()
    mock_client.stream_ended = False

    def refresh_response(_path):
        response = Mock()
        response.json.return_value = {
            "project_id": "proj-wait",
            "agent_task_id": "task-wait-q",
            "status": end_status if mock_client.stream_ended else live_status,
            "created_at": "2024-01-01T00:00:00",
            "last_updated_at": "2024-01-01T01:00:00",
            "percent_complete": end_percent if mock_client.stream_ended else 50,
            "file_name": None,
            "description": None,
            "output_summary": None,
            "read_only": read_only,
        }
        return response

    async def mock_stream_sse(endpoint, **_):
        for event in events:
            yield event
        mock_client.stream_ended = True

    mock_client.stream_sse = mock_stream_sse
    mock_client.get = AsyncMock(side_effect=refresh_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


async def test_wait_for_completion_displays_follow_up_without_prompting():
    task = _waiting_task()
    follow_up = _plain_event("event-follow-up", "I found a bug. Would you like me to fix it?")
    follow_up["event_type"] = 18

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client([follow_up])),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock()) as flow,
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        pending = await task.wait_for_completion(interactive=True, stop_on_questions=True)

    assert task.status == TaskStatus.COMPLETE
    assert pending == []
    assert any(
        "AGENT_FOLLOW_UP: I found a bug. Would you like me to fix it?" in call.args[0] for call in shown.call_args_list
    )
    flow.assert_not_awaited()


async def test_a_wait_given_a_title_heads_its_display_with_it(capsys):
    task = _waiting_task()
    task.description = "This is an automated verification run"

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient",
            return_value=_streaming_client([_plain_event("event-1", "Summing up")]),
        ),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        patch("aristotlelib.progress_utils.stdout_is_terminal", return_value=True),
    ):
        await task.wait_for_completion(interactive=True, title="fifo proofs")

    screen = capsys.readouterr().out.split("\033[2J\033[H")[-1].splitlines()
    assert screen[1:3] == ["fifo proofs", "Task: task-wait-q"]
    assert "automated" not in "\n".join(screen)


@pytest.mark.parametrize(
    ("name", "heading"),
    [
        ("Prove that the FIFO never overflows.\n\nThen check the reset.", "Prove that the FIFO never overflows."),
        ("Prove that the FIFO never overflows.\r\nThen check the reset.", "Prove that the FIFO never overflows."),
        ("x" * 200, "x" * 79 + "…"),
    ],
    ids=["several lines", "Windows line endings", "long"],
)
def test_a_heading_named_after_a_whole_prompt_keeps_its_first_line_cut_short(name: str, heading: str):
    assert _waiting_task()._status_lines(name)[1] == heading


async def test_a_finished_wait_leaves_its_final_frame_on_a_terminal(capsys, caplog):
    task = _waiting_task()

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient",
            return_value=_streaming_client([_plain_event("event-1", "Summing up")]),
        ),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        patch("aristotlelib.progress_utils.stdout_is_terminal", return_value=True),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        await task.wait_for_completion(interactive=True)

    screen = capsys.readouterr().out.split("\033[2J\033[H")[-1].splitlines()
    assert screen[0].startswith("COMPLETE (")
    assert screen[1:] == ["Task: task-wait-q", "Project: proj-wait", "MESSAGE: Summing up"]
    assert caplog.messages[-1] == "Task complete!"


@pytest.mark.parametrize(
    ("live_status", "percent_complete", "verb"), [("QUEUED", None, "submitted"), ("IN_PROGRESS", 1, "started")]
)
async def test_a_wait_says_a_run_cancelled_in_the_queue_was_submitted_not_started(
    live_status, percent_complete, verb, capsys
):
    task = _waiting_task()
    task.status = TaskStatus.QUEUED
    client = _streaming_client(
        [_plain_event("event-1", "Prompt")],
        live_status=live_status,
        end_status="CANCELED",
        end_percent=percent_complete,
    )

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        patch("aristotlelib.progress_utils.stdout_is_terminal", return_value=True),
    ):
        await task.wait_for_completion(interactive=True)

    frames = capsys.readouterr().out.split("\033[2J\033[H")
    assert any(frame.startswith(f"{live_status} (") for frame in frames)
    assert frames[-1].startswith(f"CANCELED ({verb} ")


@pytest.mark.parametrize(
    ("live_status", "started_at", "verb"),
    [("QUEUED", None, "submitted"), ("IN_PROGRESS", "2024-01-01T00:00:30", "started")],
)
async def test_a_wait_reads_a_cancelled_run_by_its_start_time_from_a_server_that_reports_no_progress(
    live_status, started_at, verb, capsys
):
    task = _waiting_task()
    task.status = TaskStatus.QUEUED
    client = _streaming_client([_plain_event("event-1", "Prompt")], live_status=live_status, end_status="CANCELED")
    refresh = client.get.side_effect

    def without_progress(path):
        response = refresh(path)
        response.json.return_value = {**response.json.return_value, "percent_complete": None, "started_at": started_at}
        return response

    client.get = AsyncMock(side_effect=without_progress)
    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        patch("aristotlelib.progress_utils.stdout_is_terminal", return_value=True),
    ):
        await task.wait_for_completion(interactive=True)

    frames = capsys.readouterr().out.split("\033[2J\033[H")
    assert any(frame.startswith(f"{live_status} (") for frame in frames)
    assert frames[-1].startswith(f"CANCELED ({verb} ")


async def test_an_incomplete_run_points_at_its_summary_not_at_the_prompt(caplog):
    client = _streaming_client([_plain_event("event-1", "Stopping")])
    refresh = client.get.side_effect

    def incomplete(path):
        response = refresh(path)
        if client.stream_ended:
            response.json.return_value = {**response.json.return_value, "status": "COMPLETE_WITH_ERRORS"}
        return response

    client.get = AsyncMock(side_effect=incomplete)
    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        await _waiting_task().wait_for_completion(interactive=False)

    assert caplog.messages[-1] == (
        "Aristotle made progress but did not fully complete the request. Its summary says what is left."
    )


@pytest.mark.parametrize(
    ("status", "level", "message"),
    [
        (
            "OUT_OF_BUDGET",
            logging.INFO,
            "Task ran out of budget. Resume it on the same project with:\n  aristotle continue proj-wait Continue --wait",
        ),
        (
            "FAILED",
            logging.ERROR,
            "Task failed due to an internal error. The team at Harmonic has been notified. Try it again on the same "
            "project with:\n  aristotle continue proj-wait Continue --wait",
        ),
        (
            "CANCELED",
            logging.INFO,
            "Task was canceled. Resume it on the same project with:\n  aristotle continue proj-wait Continue --wait",
        ),
    ],
)
async def test_a_wait_that_ends_badly_names_the_command_that_resumes_it(status, level, message, caplog):
    client = _streaming_client([_plain_event("event-1", "Stopping")])
    refresh = client.get.side_effect

    def ended(path):
        response = refresh(path)
        if client.stream_ended:
            response.json.return_value = {**response.json.return_value, "status": status}
        return response

    client.get = AsyncMock(side_effect=ended)
    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        await _waiting_task().wait_for_completion(interactive=False)

    assert (caplog.records[-1].levelno, caplog.messages[-1]) == (level, message)


async def test_a_caller_with_its_own_next_step_gets_no_continue_command(caplog):
    """As ci setup, for which a follow-up writes no manifest."""
    client = _streaming_client([_plain_event("event-1", "Stopping")])
    refresh = client.get.side_effect

    def failed(path):
        response = refresh(path)
        if client.stream_ended:
            response.json.return_value = {**response.json.return_value, "status": "FAILED"}
        return response

    client.get = AsyncMock(side_effect=failed)
    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        await _waiting_task().wait_for_completion(interactive=False, resumable=False)

    assert caplog.messages[-1] == "Task failed due to an internal error. The team at Harmonic has been notified."


async def test_a_wait_names_the_commands_its_caller_resumes_it_with(caplog):
    """As the CLI gives them for a --json wait, so the follow-up waits the same way."""
    client = _streaming_client([_plain_event("event-1", "Stopping")])
    refresh = client.get.side_effect

    def canceled(path):
        response = refresh(path)
        if client.stream_ended:
            response.json.return_value = {**response.json.return_value, "status": "CANCELED"}
        return response

    client.get = AsyncMock(side_effect=canceled)
    resume = [["aristotle", "continue", "proj-wait", "Continue", "--wait", "--json"]]
    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        await _waiting_task().wait_for_completion(interactive=False, resume=resume)

    assert caplog.messages[-1] == (
        "Task was canceled. Resume it on the same project with:\n  aristotle continue proj-wait Continue --wait --json"
    )


def test_the_commands_that_resume_a_task_are_quoted_for_each_shell_that_reads_them_differently(monkeypatch):
    monkeypatch.setattr("aristotlelib.shell.os.name", "posix")
    monkeypatch.setenv("PSModulePath", "/opt/microsoft/powershell/7/Modules")
    resume = [
        ["aristotle", "continue", "proj-1", "Continue"],
        ["aristotle", "show", "proj-1", "--wait", "--destination=Bob's runs/"],
    ]

    assert ending_notice(TaskStatus.FAILED, "proj-1", resume=resume) == (
        "Task failed due to an internal error. The team at Harmonic has been notified. Try it again on the same "
        "project with, in a POSIX shell:\n"
        "  aristotle continue proj-1 Continue\n"
        "  aristotle show proj-1 --wait '--destination=Bob'\"'\"'s runs/'\n"
        "or, in PowerShell:\n"
        "  aristotle continue proj-1 Continue\n"
        "  aristotle show proj-1 --wait '--destination=Bob''s runs/'"
    )


@pytest.mark.parametrize("status", ["FAILED", "CANCELED", "OUT_OF_BUDGET"])
def test_a_task_someone_else_owns_gets_no_command_to_resume_it(status):
    """Only the project's owner can continue it."""
    notice = ending_notice(TaskStatus(status), "proj-1", read_only=True)
    assert notice is not None and "aristotle continue" not in notice


def test_a_task_that_ended_well_or_is_going_gets_no_notice():
    assert all(ending_notice(status, "proj-1") is None for status in TaskStatus if status not in ENDED_BADLY)


async def test_a_wait_writes_no_escape_codes_when_stdout_is_not_a_terminal(capsys):
    task = _waiting_task()
    events = [_plain_event("event-1", "Summing up"), _question_event("event-legacy", status=2, content="Which reset?")]

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client(events)),
        patch(
            "aristotlelib.agent_task.Question.list_for_task",
            new=AsyncMock(side_effect=AristotleAPIError("not found", status_code=404)),
        ),
    ):
        await task.wait_for_completion(interactive=True)

    out = capsys.readouterr().out
    assert "\033" not in out
    assert out.endswith("Task: task-wait-q\nProject: proj-wait\nMESSAGE: Summing up\nAristotle asked: Which reset?\n")


@pytest.mark.parametrize("listed", [True, False], ids=["listed question", "legacy question event"])
async def test_a_wait_with_default_arguments_leaves_the_terminal_alone_and_logs_the_questions(listed, capsys, caplog):
    task = _waiting_task()
    events = [
        _plain_event("event-1", "Summing up"),
        _question_event("event-q1", status=2, content="Question number 1?"),
    ]
    listing = (
        AsyncMock(return_value=[_pending_question(1)])
        if listed
        else AsyncMock(side_effect=AristotleAPIError("not found", status_code=404))
    )

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client(events)),
        patch("aristotlelib.agent_task.Question.list_for_task", new=listing),
        patch("aristotlelib.progress_utils.stdout_is_terminal", return_value=True),
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock(return_value=FlowResult())) as flow,
        patch("aristotlelib.agent_task.read_stdin_line", new=AsyncMock(side_effect=EOFError)) as read,
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        assert await task.wait_for_completion() == []

    assert capsys.readouterr() == ("", "")
    flow.assert_not_called()
    read.assert_not_called()
    assert task.status == TaskStatus.COMPLETE
    assert "Aristotle asked: Question number 1?" in caplog.messages
    assert caplog.messages[-1] == "Task complete!"


@pytest.mark.parametrize(
    ("error", "outcome"),
    [(httpx.ReadError("connection reset"), nullcontext()), (KeyboardInterrupt(), pytest.raises(KeyboardInterrupt))],
)
async def test_a_wait_that_breaks_off_leaves_the_last_frame_on_a_terminal(error, outcome, capsys):
    task = _waiting_task()

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client([])),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=error)),
        patch("aristotlelib.progress_utils.stdout_is_terminal", return_value=True),
        outcome,
    ):
        await task.wait_for_completion(interactive=True)

    screen = capsys.readouterr().out.split("\033[2J\033[H")[-1].splitlines()
    assert screen[0].startswith("IN_PROGRESS (started ")
    assert screen[1:] == ["Task: task-wait-q", "Project: proj-wait"]


def _property(number: int, status: DeclarationStatus, last_updated: datetime = datetime(2024, 1, 1)) -> Declaration:
    return Declaration(
        project_id="proj-wait",
        declaration_id=f"declaration-{number}",
        declaration_type=DeclarationType.PROPERTY,
        source=DeclarationSource.AGENT,
        name=f"property_{number}",
        status=status,
        filename="Spec/Fifo.lean",
        created_at=datetime(2024, 1, 1),
        last_updated=last_updated,
    )


async def _frames_of_a_wait(
    list_properties: AsyncMock, client: AsyncMock, task: AgentTask | None = None, *, interactive: bool = True, **wait
) -> list[list[str]]:
    """The frames a wait draws through ``client``, the project's properties listed by ``list_properties``."""
    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        patch("aristotlelib.agent_task.Declaration.list_all_for_project", new=list_properties),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        await (task or _waiting_task()).wait_for_completion(interactive=interactive, **wait)
    return [frame.args[0] for frame in shown.call_args_list]


def _counts(frames: list[list[str]]) -> list[str | None]:
    """Each frame's line counting the properties, or None."""
    return [next((line for line in frame if line.startswith("Properties: ")), None) for frame in frames]


async def test_a_wait_counts_the_properties_while_the_run_goes_on_and_not_once_it_has_ended(monkeypatch):
    monkeypatch.setattr("aristotlelib.agent_task.QUESTION_REFRESH_SECONDS", 0.05)
    first = [_property(1, DeclarationStatus.IN_PROGRESS), _property(2, DeclarationStatus.IN_PROGRESS)]
    later = [_property(1, DeclarationStatus.PROVED), _property(2, DeclarationStatus.IN_PROGRESS)]
    listings = iter([first])
    list_properties = AsyncMock(side_effect=lambda *_: next(listings, later))

    frames = await _frames_of_a_wait(
        list_properties, _quiet_client([_plain_event("event-1", "Summing up")], quiet_seconds=0.5)
    )

    # The event brings the first refresh, which starts the first count; a frame after it shows the count.
    counts = _counts(frames)
    assert counts[:3] == [None, None, None]
    shown = [count for count in counts if count is not None]
    assert shown[0] == "Properties: 0 proved · 0 refuted · 2 in progress"
    assert shown[-1] == "Properties: 1 proved · 0 refuted · 1 in progress"
    assert frames[counts.index(shown[0])][3:] == [shown[0], "MESSAGE: Summing up"]
    assert frames[-1][0].startswith("COMPLETE") and counts[-1] is None
    list_properties.assert_awaited_with("proj-wait", DeclarationType.PROPERTY)


async def test_a_property_count_that_fails_keeps_the_last_one_quietly_and_the_wait_goes_on(monkeypatch, caplog):
    monkeypatch.setattr("aristotlelib.agent_task.QUESTION_REFRESH_SECONDS", 0.05)
    # Whether each listing ran with its retries logged at debug level.
    quiet: list[bool] = []

    async def list_properties(project_id, declaration_type):
        quiet.append(_quiet_now())
        if len(quiet) == 1:
            return [_property(1, DeclarationStatus.IN_PROGRESS)]
        raise AristotleAPIError("HTTP 502 after 5 attempts", 502)

    with caplog.at_level(logging.DEBUG, logger="aristotle"):
        frames = await _frames_of_a_wait(
            AsyncMock(side_effect=list_properties),
            _quiet_client([_plain_event("event-1", "Summing up")], quiet_seconds=0.5),
        )

    assert len(quiet) > 1 and all(quiet)
    counts = _counts(frames)
    first = next(index for index, count in enumerate(counts) if count is not None)
    assert set(counts[first:-1]) == {"Properties: 0 proved · 0 refuted · 1 in progress"}
    assert "Could not count the project's properties: HTTP 502 after 5 attempts" in caplog.messages
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert caplog.messages[-1] == "Task complete!"


async def test_a_proof_an_earlier_run_left_unchecked_counts_as_unchecked_while_the_next_run_goes_on(monkeypatch):
    monkeypatch.setattr("aristotlelib.agent_task.QUESTION_REFRESH_SECONDS", 0.05)
    task = _waiting_task()
    earlier = task.model_copy(
        update={
            "agent_task_id": "task-earlier",
            "status": TaskStatus.OUT_OF_BUDGET,
            "created_at": datetime(2023, 1, 1),
        }
    )
    # Two proofs the earlier run set and never checked, and one this run, refreshed as begun 2024-01-01, set.
    properties = [
        _property(1, DeclarationStatus.PROVED, last_updated=datetime(2023, 6, 1)),
        _property(2, DeclarationStatus.PROVED, last_updated=datetime(2023, 6, 1)),
        _property(3, DeclarationStatus.PROVED, last_updated=datetime(2024, 1, 2)),
    ]
    list_properties = AsyncMock(return_value=properties)
    project = Mock(get_tasks=AsyncMock(return_value=([task, earlier], None)))

    with patch("aristotlelib.cli.api_action.Project.from_id", new=AsyncMock(return_value=project)):
        frames = await _frames_of_a_wait(
            list_properties, _quiet_client([_plain_event("event-1", "Working")], quiet_seconds=0.5), task
        )

    assert {count for count in _counts(frames) if count is not None} == {
        "Properties: 1 proved · 0 refuted · 0 in progress · 2 unchecked"
    }
    # The earlier tasks have ended, so the first listing of them serves every count.
    assert list_properties.await_count > 1 and project.get_tasks.await_count == 1


async def test_a_count_whose_verdicts_cannot_be_judged_is_not_shown(monkeypatch, caplog):
    """As when the project's tasks can't be listed, so an earlier run's verdicts can't be told checked or not."""
    monkeypatch.setattr("aristotlelib.agent_task.QUESTION_REFRESH_SECONDS", 0.05)
    project = Mock(get_tasks=AsyncMock(side_effect=AristotleAPIError("HTTP 503 after 5 attempts", 503)))

    with (
        patch("aristotlelib.cli.api_action.Project.from_id", new=AsyncMock(return_value=project)),
        caplog.at_level(logging.DEBUG, logger="aristotle"),
    ):
        frames = await _frames_of_a_wait(
            AsyncMock(return_value=[_property(1, DeclarationStatus.PROVED, last_updated=datetime(2023, 6, 1))]),
            _quiet_client([_plain_event("event-1", "Working")], quiet_seconds=0.5),
        )

    assert _counts(frames) == [None] * len(frames)
    assert project.get_tasks.await_count > 1
    assert "Could not count the project's properties: HTTP 503 after 5 attempts" in caplog.messages
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


async def test_a_count_under_way_holds_up_no_frame_runs_alone_and_ends_with_the_wait(monkeypatch):
    monkeypatch.setattr("aristotlelib.agent_task.QUESTION_REFRESH_SECONDS", 0.05)
    cancelled = asyncio.Event()

    async def list_properties(project_id, declaration_type):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    listing = AsyncMock(side_effect=list_properties)
    events = [_plain_event(f"event-{number}", f"Step {number}") for number in range(3)]

    frames = await asyncio.wait_for(_frames_of_a_wait(listing, _quiet_client(events, quiet_seconds=0.5)), 5)
    # The cancellation reaches the count at its next step.
    await asyncio.sleep(0)

    assert ["MESSAGE: Step 0", "MESSAGE: Step 1", "MESSAGE: Step 2"] in [frame[3:] for frame in frames]
    assert frames[-1][0].startswith("COMPLETE")
    assert listing.await_count == 1
    assert cancelled.is_set()


async def test_a_run_that_has_ended_starts_no_count():
    list_properties = AsyncMock(return_value=[_property(1, DeclarationStatus.PROVED)])

    await _frames_of_a_wait(
        list_properties, _streaming_client([_plain_event("event-1", "Done")], live_status="COMPLETE")
    )

    list_properties.assert_not_awaited()


async def test_a_project_without_properties_gets_no_count(monkeypatch):
    monkeypatch.setattr("aristotlelib.agent_task.QUESTION_REFRESH_SECONDS", 0.05)
    list_properties = AsyncMock(return_value=[])

    frames = await _frames_of_a_wait(
        list_properties, _quiet_client([_plain_event("event-1", "Summing up")], quiet_seconds=0.5)
    )

    # Live frames came after finished counts.
    assert list_properties.await_count > 1
    assert _counts(frames) == [None] * len(frames)


async def test_a_wait_without_a_display_counts_no_properties():
    list_properties = AsyncMock(return_value=[_property(1, DeclarationStatus.PROVED)])

    await _frames_of_a_wait(list_properties, _streaming_client([_plain_event("event-1", "Done")]), interactive=False)

    list_properties.assert_not_awaited()


@pytest.mark.parametrize(
    ("activities", "shown"),
    [
        ([(EventType.RUNNING_COMMAND, "")] * 3 + [(EventType.MESSAGE, "Done")], ["Running command", "MESSAGE: Done"]),
        (
            [
                (EventType.RUNNING_COMMAND, ""),
                (EventType.RUNNING_COMMAND, ""),
                (EventType.READING_FILES, ""),
                (EventType.RUNNING_COMMAND, ""),
            ],
            ["Running command", "Reading files", "Running command"],
        ),
        ([(EventType.THINKING, "Checking the reset")] * 2, ["THINKING: Checking the reset"]),
    ],
    ids=["a run of commands", "a change of activity", "the same text"],
)
async def test_a_run_of_one_activity_shows_once(activities, shown):
    events = [
        {**_plain_event(f"event-{number}", content), "event_type": event_type.value}
        for number, (event_type, content) in enumerate(activities)
    ]

    frames = await _frames_of_a_wait(AsyncMock(return_value=[]), _streaming_client(events))

    assert frames[-1][3:] == shown


async def test_wait_for_completion_presents_the_pending_questions_as_a_set():
    """One event triggers a listing and the whole pending set is presented."""
    task = _waiting_task()
    q1, q2, answered = _pending_question(1), _pending_question(2), _pending_question(3, response="Yes")
    # Nothing is pending when the wait starts; the event's listing finds the set, and the refresh after it
    # sees it answered.
    remaining, list_for_task = _listing([], [q1, q2, answered], [answered])

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient",
            return_value=_streaming_client([_question_event("event-q1")]),
        ),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=list_for_task)),
        patch(
            "aristotlelib.agent_task.run_question_flow", new=AsyncMock(return_value=FlowResult(answered=[q1, q2]))
        ) as flow,
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        await task.wait_for_completion(interactive=True)

    assert remaining == []
    flow.assert_called_once()
    assert flow.call_args.args[0] == [q1, q2]
    assert flow.call_args.kwargs["deadline_seconds"] == DEFAULT_DEADLINE_SECONDS
    assert flow.call_args.kwargs["read_line"] is read_stdin_line
    assert flow.call_args.kwargs["run_is_live"] == task.refresh_live
    # Nothing pending, so no footer.
    assert not any("pending" in line for line in shown.call_args_list[-1].args[0])


async def test_a_non_interactive_wait_logs_questions_and_never_touches_stdout(caplog, capsys):
    task = _waiting_task()
    q1 = _pending_question(1)
    events = [_question_event("event-q1"), _question_event("event-legacy", status=2, content="Which reset?")]

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client(events)),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[q1])),
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock()) as flow,
        patch("aristotlelib.agent_task.read_stdin_line", new=AsyncMock()) as read_line,
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        await task.wait_for_completion(interactive=False)

    flow.assert_not_called()
    read_line.assert_not_called()
    assert capsys.readouterr().out == ""
    assert "Aristotle asked: Question number 1?" in caplog.messages
    assert "1 question pending — answer with: aristotle questions proj-wait" in caplog.messages
    assert any("Which reset?" in message for message in caplog.messages)
    assert "Task complete!" in caplog.messages


async def test_stop_on_questions_hands_back_questions_already_pending_when_the_wait_starts(caplog):
    task = _waiting_task()
    q1, q2 = _pending_question(1), _pending_question(2)

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client([])),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[q1, q2])),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        assert await task.wait_for_completion(interactive=False, stop_on_questions=True) == [q1, q2]

    assert "Task complete!" not in caplog.messages


async def test_stop_on_questions_hands_back_a_question_asked_during_the_wait():
    task = _waiting_task()
    q1 = _pending_question(1)
    # Nothing is pending at the start; the question event's listing finds it.
    listings = iter([[], [q1]])

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient",
            return_value=_streaming_client([_question_event("event-q1"), _question_event("event-late")]),
        ),
        patch(
            "aristotlelib.agent_task.Question.list_for_task",
            new=AsyncMock(side_effect=lambda _task_id: next(listings, [q1])),
        ),
    ):
        assert await task.wait_for_completion(interactive=False, stop_on_questions=True) == [q1]


async def test_stop_on_questions_hands_back_a_question_found_by_a_timed_refresh(monkeypatch):
    monkeypatch.setattr("aristotlelib.agent_task.QUESTION_REFRESH_SECONDS", 0.05)
    task = _waiting_task()
    q1 = _pending_question(1)
    # The listings at the start, for the event and in the event's refresh miss the row; a timed one finds it.
    listings = iter([[], [], []])

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient",
            return_value=_quiet_client([_question_event("event-q1")], quiet_seconds=0.4),
        ),
        patch(
            "aristotlelib.agent_task.Question.list_for_task",
            new=AsyncMock(side_effect=lambda _task_id: next(listings, [q1])),
        ),
    ):
        assert await task.wait_for_completion(interactive=False, stop_on_questions=True) == [q1]


async def test_stop_on_questions_waits_to_the_end_when_nothing_is_asked(caplog):
    task = _waiting_task()

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client([])),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        assert await task.wait_for_completion(interactive=False, stop_on_questions=True) == []

    assert "Task complete!" in caplog.messages


async def test_stop_on_questions_does_not_stop_for_questions_only_the_owner_can_answer():
    task = _waiting_task()
    task.read_only = True

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client([], read_only=True)),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[_pending_question(1)])),
    ):
        assert await task.wait_for_completion(interactive=False, stop_on_questions=True) == []

    assert task.status == TaskStatus.COMPLETE


async def test_stop_on_questions_does_not_stop_for_the_questions_of_an_ended_run():
    task = _waiting_task()
    client = _streaming_client([])
    # Every refresh already reports the run as finished.
    client.stream_ended = True

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[_pending_question(1)])),
    ):
        assert await task.wait_for_completion(interactive=False, stop_on_questions=True) == []


async def test_wait_for_completion_does_not_reopen_the_flow_for_events_of_the_same_set():
    task = _waiting_task()
    q1, q2 = _pending_question(1), _pending_question(2)
    events = [_question_event("event-q1"), _question_event("event-q2"), _question_event("event-q1")]

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client(events)),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[q1, q2])),
        patch(
            "aristotlelib.agent_task.run_question_flow", new=AsyncMock(return_value=FlowResult(answered=[q1, q2]))
        ) as flow,
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
    ):
        await task.wait_for_completion(interactive=True)

    flow.assert_called_once()


async def test_wait_for_completion_reopens_the_flow_when_a_new_question_arrives():
    """A later question brings the whole pending set back, including one skipped earlier."""
    task = _waiting_task()
    q1, q2 = _pending_question(1), _pending_question(2)
    events = [_question_event("event-q1"), _question_event("event-q2")]
    # The first refresh re-counts and finds nothing new; the second event is what brings q2.
    listings = [[q1], [q1], [q1, q2]]

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client(events)),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=lambda _id: listings.pop(0))),
        patch(
            "aristotlelib.agent_task.run_question_flow",
            new=AsyncMock(side_effect=[FlowResult(unanswered=[q1]), FlowResult(answered=[q1, q2])]),
        ) as flow,
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
    ):
        await task.wait_for_completion(interactive=True)

    assert [call.args[0] for call in flow.call_args_list] == [[q1], [q1, q2]]
    assert listings == []


async def test_wait_for_completion_falls_back_to_the_event_when_there_is_no_questions_endpoint():
    """On 404 the per-event prompt runs, for SENT events only."""
    task = _waiting_task()
    events = [
        _question_event("event-legacy", status=2, content="Which polarity?"),
        _question_event("event-timed-out", status=1, content="Which width?"),
    ]

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client(events)),
        patch(
            "aristotlelib.agent_task.Question.list_for_task",
            new=AsyncMock(side_effect=AristotleAPIError("not found", status_code=404)),
        ) as listed,
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock()) as flow,
        patch.object(AgentTask, "_prompt_for_answer", new=AsyncMock()) as prompt,
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
    ):
        await task.wait_for_completion(interactive=True)

    flow.assert_not_called()
    # Once the endpoint is known to be missing, it is not asked again.
    listed.assert_called_once()
    prompt.assert_called_once()
    assert prompt.call_args.args[0].event_id == "event-legacy"


async def test_wait_for_completion_uses_the_event_for_a_question_the_list_does_not_know():
    """A SENT event no listed question claims takes the per-event path."""
    task = _waiting_task()
    durable = _pending_question(1, response="Yes")
    events = [_question_event("event-legacy", status=2, content="Which polarity?")]

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client(events)),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[durable])),
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock()) as flow,
        patch.object(AgentTask, "_prompt_for_answer", new=AsyncMock()) as prompt,
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
    ):
        await task.wait_for_completion(interactive=True)

    flow.assert_not_called()
    prompt.assert_called_once()
    assert prompt.call_args.args[0].event_id == "event-legacy"


async def test_wait_for_completion_pins_a_footer_while_questions_stay_pending():
    task = _waiting_task()
    q1, q2 = _pending_question(1), _pending_question(2)

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient",
            return_value=_streaming_client([_question_event("event-q1")]),
        ),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[q1, q2])),
        patch(
            "aristotlelib.agent_task.run_question_flow",
            new=AsyncMock(return_value=FlowResult(unanswered=[q1, q2], cancelled=True)),
        ),
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        await task.wait_for_completion(interactive=True)

    lines = _last_live_frame(shown)
    assert lines[-1] == "2 questions pending — answer with: aristotle questions proj-wait"
    assert lines[-1] == pending_footer(2, "proj-wait")
    # The final frame shows the ended run, to which no answer can go.
    assert not any("pending" in line for line in shown.call_args_list[-1].args[0])


async def test_wait_for_completion_clears_the_footer_once_answered_elsewhere():
    """The periodic refresh re-counts, so an answer given elsewhere clears the footer."""
    task = _waiting_task()
    q1 = _pending_question(1)
    listings = [[q1], [_pending_question(1, response="Yes")]]

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient",
            return_value=_streaming_client([_question_event("event-q1")]),
        ),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=lambda _id: listings.pop(0))),
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock(return_value=FlowResult(unanswered=[q1]))),
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        await task.wait_for_completion(interactive=True)

    assert listings == []
    assert not any("pending" in line for line in shown.call_args_list[-1].args[0])


async def test_wait_for_completion_does_not_prompt_without_a_terminal(caplog):
    task = _waiting_task()
    q1 = _pending_question(1)

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient",
            return_value=_streaming_client([_question_event("event-q1")]),
        ),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[q1])),
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock()) as flow,
        patch("aristotlelib.agent_task.can_prompt", return_value=False),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
        caplog.at_level(logging.INFO),
    ):
        await task.wait_for_completion(interactive=True)

    flow.assert_not_called()
    assert "Aristotle asked: Question number 1?" in caplog.text
    assert "1 question pending — answer with: aristotle questions proj-wait" in caplog.text
    assert _last_live_frame(shown)[-2:] == ["Aristotle asked: Question number 1?", pending_footer(1, "proj-wait")]


def _plain_event(event_id: str, content: str = "Working") -> dict:
    return {
        "event_id": event_id,
        "agent_task_id": "task-wait-q",
        "event_type": 1,
        "created_at": "2024-01-01T00:00:00",
        "content": content,
        "file_path": None,
        "explanation": None,
        "suggestions": None,
        "status": 1,
    }


def _listing(*items):
    """A list_for_task side effect that hands out lists, or raises the errors, in order."""
    remaining = list(items)

    def list_for_task(_id):
        item = remaining.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return remaining, list_for_task


async def test_wait_for_completion_keeps_using_the_questions_endpoint_after_a_transient_error():
    """A 500 on a listing is retried on the next refresh, not treated as a missing endpoint."""
    task = _waiting_task()
    q1 = _pending_question(1)
    remaining, list_for_task = _listing([], AristotleAPIError("API request failed with status 500: boom", 500), [q1])

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient",
            return_value=_streaming_client([_question_event("event-q1")]),
        ),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=list_for_task)),
        patch(
            "aristotlelib.agent_task.run_question_flow", new=AsyncMock(return_value=FlowResult(answered=[q1]))
        ) as flow,
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        await task.wait_for_completion(interactive=True)

    assert remaining == []
    flow.assert_called_once()
    assert flow.call_args.args[0] == [q1]
    assert not any("pending" in line for line in shown.call_args_list[-1].args[0])


async def test_wait_for_completion_presents_a_question_whose_row_was_not_visible_at_its_event():
    """An event whose question row is not yet listed is retried on the next refresh."""
    task = _waiting_task()
    q1 = _pending_question(1)
    remaining, list_for_task = _listing([], [], [q1])

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient",
            return_value=_streaming_client([_question_event("event-q1")]),
        ),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=list_for_task)),
        patch(
            "aristotlelib.agent_task.run_question_flow", new=AsyncMock(return_value=FlowResult(answered=[q1]))
        ) as flow,
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
    ):
        await task.wait_for_completion(interactive=True)

    assert remaining == []
    flow.assert_called_once()
    assert flow.call_args.args[0] == [q1]


async def test_wait_for_completion_keeps_the_footer_when_a_re_count_fails():
    """A failed re-count keeps the last known footer."""
    task = _waiting_task()
    q1 = _pending_question(1)
    remaining, list_for_task = _listing([q1], AristotleAPIError("API request failed with status 500: boom", 500))

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient",
            return_value=_streaming_client([_question_event("event-q1")]),
        ),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=list_for_task)),
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock(return_value=FlowResult(unanswered=[q1]))),
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        await task.wait_for_completion(interactive=True)

    assert remaining == []
    assert _last_live_frame(shown)[-1] == pending_footer(1, "proj-wait")


async def test_wait_for_completion_legacy_fallback_never_reads_a_pipe_as_the_answer(caplog):
    """Without a terminal the per-event prompt logs the question and does not read stdin."""
    task = _waiting_task()
    events = [_question_event("event-legacy", status=2, content="Which polarity?")]

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client(events)),
        patch(
            "aristotlelib.agent_task.Question.list_for_task",
            new=AsyncMock(side_effect=AristotleAPIError("not found", status_code=404)),
        ),
        patch("aristotlelib.agent_task.can_prompt", return_value=False),
        patch("builtins.input", Mock(side_effect=AssertionError("must not read stdin"))) as mock_input,
        patch("aristotlelib.agent_task.Event.answer", new=AsyncMock()) as answered,
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
        caplog.at_level(logging.INFO),
    ):
        await task.wait_for_completion(interactive=True)

    mock_input.assert_not_called()
    answered.assert_not_called()
    assert "Aristotle asked: Which polarity?" in caplog.text
    # The redraw clears the terminal, so the question stays on the display, not only in the log.
    assert any(line.startswith("Aristotle asked: Which polarity?") for line in shown.call_args_list[-1].args[0])


async def test_wait_for_completion_does_not_prompt_for_a_durable_question_answered_elsewhere():
    """An event a listed question claims needs no prompt, even when it arrives after the answer."""
    task = _waiting_task()
    answered = _pending_question(1, response="Yes")  # event_id == "event-q1"
    events = [_question_event("event-q1", status=2, content="Question number 1?")]

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client(events)),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[answered])),
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock()) as flow,
        patch.object(AgentTask, "_prompt_for_answer", new=AsyncMock()) as prompt,
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        await task.wait_for_completion(interactive=True)

    flow.assert_not_called()
    prompt.assert_not_called()
    assert not any("pending" in line for line in shown.call_args_list[-1].args[0])


async def test_wait_for_completion_still_shows_a_timed_out_question_from_an_older_server():
    """A COMPLETE question event with no answer renders as the timed-out notice."""
    task = _waiting_task()
    events = [_question_event("event-timed-out", status=1, content="Which width?")]

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client(events)),
        patch(
            "aristotlelib.agent_task.Question.list_for_task",
            new=AsyncMock(side_effect=AristotleAPIError("not found", status_code=404)),
        ),
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock()) as flow,
        patch.object(AgentTask, "_prompt_for_answer", new=AsyncMock()) as prompt,
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        await task.wait_for_completion(interactive=True)

    flow.assert_not_called()
    prompt.assert_not_called()
    lines = shown.call_args_list[-1].args[0]
    assert (
        "Aristotle asked: Which width?\nAristotle timed out waiting for a response and went with its best guess."
        in lines
    )
    assert not any("pending" in line for line in lines)


@pytest.mark.parametrize(
    "listing",
    [AsyncMock(side_effect=AristotleAPIError("not found", status_code=404)), AsyncMock(return_value=[])],
    ids=["no-questions-endpoint", "no-question-record"],
)
async def test_wait_for_completion_shows_the_timed_out_notice_for_a_question_it_prompted_for(listing):
    """A SENT question that comes back COMPLETE with no answer renders as the timed-out notice."""
    task = _waiting_task()
    events = [
        _question_event("event-legacy", status=2, content="Which width?"),
        _question_event("event-legacy", status=1, content="Which width?"),
    ]

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client(events)),
        patch("aristotlelib.agent_task.Question.list_for_task", new=listing),
        patch.object(AgentTask, "_prompt_for_answer", new=AsyncMock()) as prompt,
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        await task.wait_for_completion(interactive=True)

    prompt.assert_called_once()
    lines = shown.call_args_list[-1].args[0]
    assert (
        "Aristotle asked: Which width?\nAristotle timed out waiting for a response and went with its best guess."
        in lines
    )


async def test_wait_for_completion_keeps_the_flow_outcome_on_the_display():
    """The flow's summary stays in the event window across redraws."""
    task = _waiting_task()
    q1, q2 = _pending_question(1), _pending_question(2)
    summary = ["Answered question 1: Yes", "Question 2 still pending; answer later with: aristotle questions proj-wait"]
    events = [_question_event("event-q1"), _plain_event("event-later")]
    # The re-count after the flow sees the answer the flow sent.
    remaining, list_for_task = _listing([q1, q2], [_pending_question(1, response="Yes"), q2])

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client(events)),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=list_for_task)),
        patch(
            "aristotlelib.agent_task.run_question_flow",
            new=AsyncMock(return_value=FlowResult(answered=[q1], unanswered=[q2], summary=summary)),
        ),
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        await task.wait_for_completion(interactive=True)

    assert remaining == []
    lines = _last_live_frame(shown)
    outcome = lines.index("\n".join(summary))
    # After the outcome comes the later event, and the footer is still pinned last.
    assert lines[outcome + 1] == "MESSAGE: Working"
    assert lines[-1] == pending_footer(1, "proj-wait")


async def test_wait_for_completion_reports_a_skipped_question_the_agent_gave_up_on():
    """A presented question the re-count finds ignored drops the footer and adds a notice."""
    task = _waiting_task()
    q1 = _pending_question(1)
    ignored = q1.model_copy(update={"ignored_at": datetime(2024, 1, 1, 0, 16)})
    remaining, list_for_task = _listing([q1], [ignored])

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient",
            return_value=_streaming_client([_question_event("event-q1")]),
        ),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=list_for_task)),
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock(return_value=FlowResult(unanswered=[q1]))),
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        await task.wait_for_completion(interactive=True)

    assert remaining == []
    lines = shown.call_args_list[-1].args[0]
    assert "Aristotle asked: Question number 1?\nThe question was closed without an answer." in lines
    assert not any("pending" in line for line in lines)


def _quiet_client(events: list[dict], quiet_seconds: float) -> AsyncMock:
    """A stream that sends its events, then goes quiet for a while before ending."""
    client = _streaming_client([])

    async def stream(endpoint, **_):
        for event in events:
            yield event
        await asyncio.sleep(quiet_seconds)
        client.stream_ended = True

    client.stream_sse = stream
    return client


async def test_wait_for_completion_refreshes_on_a_quiet_stream(monkeypatch):
    """The periodic re-list runs even when no event arrives, so a lagging question still gets asked."""
    monkeypatch.setattr("aristotlelib.agent_task.QUESTION_REFRESH_SECONDS", 0.05)
    task = _waiting_task()
    q1 = _pending_question(1)
    # Neither the event's listing nor the refresh that follows it sees the row; only a later, timer-driven
    # listing during the quiet period can.
    listings = iter([[], [], [q1], [q1], [q1], [q1], [q1], [q1]])
    list_for_task = AsyncMock(side_effect=lambda _task_id: next(listings, [q1]))

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient",
            return_value=_quiet_client([_question_event("event-q1")], quiet_seconds=0.4),
        ),
        patch("aristotlelib.agent_task.Question.list_for_task", new=list_for_task),
        patch(
            "aristotlelib.agent_task.run_question_flow", new=AsyncMock(return_value=FlowResult(answered=[q1]))
        ) as flow,
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
    ):
        await task.wait_for_completion(interactive=True)

    flow.assert_called_once()
    assert flow.call_args.args[0] == [q1]
    assert list_for_task.await_count >= 3


async def test_a_lagging_question_row_is_not_reported_as_timed_out():
    task = _waiting_task()
    q1 = _pending_question(1)
    remaining, list_for_task = _listing([], [], [q1])

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient",
            return_value=_streaming_client([_question_event("event-q1")]),
        ),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=list_for_task)),
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock(return_value=FlowResult(answered=[q1]))),
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        await task.wait_for_completion(interactive=True)

    screens = "\n".join("\n".join(call.args[0]) for call in shown.call_args_list)
    assert "timed out" not in screens
    assert "Aristotle asked: Asked a question" not in screens


async def test_a_placeholder_no_record_ever_claims_is_shown_without_a_timeout_claim(monkeypatch):
    monkeypatch.setattr("aristotlelib.agent_task.QUESTION_REFRESH_SECONDS", 0.02)
    task = _waiting_task()
    list_for_task = AsyncMock(return_value=[])

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient",
            return_value=_quiet_client([_question_event("event-orphan", content="Which clock?")], quiet_seconds=0.3),
        ),
        patch("aristotlelib.agent_task.Question.list_for_task", new=list_for_task),
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        await task.wait_for_completion(interactive=True)

    screens = "\n".join("\n".join(call.args[0]) for call in shown.call_args_list)
    assert "Aristotle asked: Which clock?" in screens
    assert "timed out" not in screens


async def test_a_failed_task_refresh_on_a_quiet_stream_does_not_end_the_wait(monkeypatch, caplog):
    monkeypatch.setattr("aristotlelib.agent_task.QUESTION_REFRESH_SECONDS", 0.05)
    task = _waiting_task()
    client = _quiet_client([], quiet_seconds=0.3)
    refresh_response = client.get.side_effect
    # The first quiet-stream refreshes hit a server error; later ones and the final one succeed.
    failures = iter([AristotleAPIError("bad gateway", status_code=502)] * 2)

    def get(path):
        failure = next(failures, None)
        if failure is not None:
            raise failure
        return refresh_response(path)

    client.get = AsyncMock(side_effect=get)

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        await task.wait_for_completion(interactive=True)

    assert "Could not refresh the task status" in caplog.text
    assert "Task complete!" in caplog.text


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadError("connection reset"), httpx.ConnectError("unreachable"), httpx.RemoteProtocolError("closed")],
    ids=["read", "connect", "protocol"],
)
@pytest.mark.parametrize("carry_on", [None, RESUME], ids=["show", "the caller's command"])
async def test_a_stream_that_fails_is_one_error_line(failure, carry_on, caplog):
    task = _waiting_task()
    client = _streaming_client([])

    async def fails(endpoint, **_):
        raise failure
        yield

    client.stream_sse = fails

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        await task.wait_for_completion(interactive=True, carry_on=carry_on)

    assert task.live
    follow = "Follow it with: aristotle show proj-wait" if carry_on is None else RESUME
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == [
        "The connection to Aristotle kept failing for 10 minutes, so the wait stopped. The task goes on.\n"
        f"{follow}\nCancel it with: aristotle cancel proj-wait"
    ]


@pytest.mark.parametrize("status", [503, 429])
async def test_a_stream_that_gives_up_on_a_retryable_status_is_one_error_line(status, caplog):
    task = _waiting_task()
    client = _streaming_client([])
    failure = api_request.AristotleAPIError(f"API request failed with status {status}: Unavailable", status)

    async def fails(endpoint, **_):
        raise failure
        yield

    client.stream_sse = fails

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        assert await task.wait_for_completion(interactive=True) == []

    assert task.live
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == [
        f"The task could not be followed: {failure}. Use 'aristotle show proj-wait' to try again."
    ]


async def test_a_wait_whose_stream_keeps_failing_ends_once_a_refresh_sees_the_run_end(monkeypatch, caplog):
    monkeypatch.setattr("aristotlelib.agent_task.QUESTION_REFRESH_SECONDS", 0.05)
    task = _waiting_task()
    # The task's refreshes find the run complete, while its stream can't connect.
    client = _streaming_client([], live_status="COMPLETE")

    async def keeps_failing(endpoint, *, live, **_):
        # As stream_sse does, it rides the outage out while the run is live, as last seen.
        while live():
            await asyncio.sleep(0.01)
        return
        yield

    client.stream_sse = keeps_failing

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        assert await asyncio.wait_for(task.wait_for_completion(interactive=True), 5) == []

    assert task.status == TaskStatus.COMPLETE
    assert "Task complete!" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def _quiet_now() -> bool:
    """Whether retries logged at this moment, in this context, go to debug level."""
    quiet = api_request._quiet.get()
    return quiet is not None and quiet()


async def test_a_wait_says_once_that_the_connection_failed_and_once_that_it_is_back(monkeypatch, caplog):
    """The refreshes in between, which fail too, log nothing more."""
    monkeypatch.setattr("aristotlelib.agent_task.QUESTION_REFRESH_SECONDS", 0.01)
    task = _waiting_task()
    client = _streaming_client([])
    working = client.get.side_effect
    down = api_request.AristotleAPIError("ConnectError on GET /task/task-wait-q after 5 attempts: unreachable")
    # Whether each refresh and listing in the outage ran with its retries quiet.
    quiet: list[bool] = []
    outage = False

    def refresh(path):
        if not outage:
            return working(path)
        quiet.append(_quiet_now())
        raise down

    async def list_questions(task_id):
        if not outage:
            return []
        quiet.append(_quiet_now())
        raise down

    async def outage_then_back(endpoint, *, on_outage, **_):
        nonlocal outage
        outage = True
        on_outage(httpx.ConnectError("All connection attempts failed"))
        while len(quiet) < 4:
            await asyncio.sleep(0.01)
        outage = False
        on_outage(None)
        client.stream_ended = True
        yield _plain_event("event-1", "Done")

    client.get = AsyncMock(side_effect=refresh)
    client.stream_sse = outage_then_back
    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=list_questions)),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        assert await asyncio.wait_for(task.wait_for_completion(interactive=False), 5) == []

    assert quiet and all(quiet)
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (
            logging.WARNING,
            "The connection to Aristotle failed: ConnectError: All connection attempts failed. Trying again for up to "
            "10 minutes while the task goes on.",
        ),
        (logging.INFO, "The connection to Aristotle is back."),
        (logging.INFO, "Task complete!"),
    ]


async def test_a_wait_that_stops_for_a_question_in_an_outage_says_the_connection_is_back(monkeypatch, caplog):
    """The listing that found the question got through, though the stream hasn't reconnected."""
    monkeypatch.setattr("aristotlelib.agent_task.QUESTION_REFRESH_SECONDS", 0.01)
    task = _waiting_task()
    client = _streaming_client([])
    reported = asyncio.Event()

    async def list_questions(task_id):
        return [_pending_question(1)] if reported.is_set() else []

    async def outage_without_end(endpoint, *, on_outage, **_):
        on_outage(httpx.ConnectError("All connection attempts failed"))
        reported.set()
        await asyncio.sleep(3600)
        yield _plain_event("event-1", "never")

    client.stream_sse = outage_without_end
    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=list_questions)),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        pending = await asyncio.wait_for(task.wait_for_completion(interactive=False, stop_on_questions=True), 5)

    assert [question.question_id for question in pending] == ["q-1"]
    assert caplog.messages[-1] == "The connection to Aristotle is back."


async def test_a_refresh_under_way_when_the_outage_is_reported_goes_quiet_from_then_on(monkeypatch, caplog):
    """The stream can take minutes to notice a network that went silent, while a refresh already retries."""
    monkeypatch.setattr("aristotlelib.agent_task.QUESTION_REFRESH_SECONDS", 0.01)
    task = _waiting_task()
    client = _streaming_client([])
    working = client.get.side_effect
    reported = asyncio.Event()
    quiet: list[bool] = []

    async def refresh(path):
        if reported.is_set() or client.stream_ended:
            return working(path)
        # Under way before the outage is reported, it fails after.
        await reported.wait()
        quiet.append(_quiet_now())
        raise api_request.AristotleAPIError("ReadTimeout on GET /task/task-wait-q after 5 attempts")

    async def outage_then_back(endpoint, *, on_outage, **_):
        # A refresh is under way.
        while not client.get.await_count:
            await asyncio.sleep(0.01)
        on_outage(httpx.ReadTimeout(""))
        reported.set()
        while not quiet:
            await asyncio.sleep(0.01)
        on_outage(None)
        client.stream_ended = True
        yield _plain_event("event-1", "Done")

    client.get = AsyncMock(side_effect=refresh)
    client.stream_sse = outage_then_back
    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        assert await asyncio.wait_for(task.wait_for_completion(interactive=False), 5) == []

    assert quiet == [True]
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == [
        "The connection to Aristotle failed: ReadTimeout. Trying again for up to 10 minutes while the task goes on."
    ]


async def test_a_refresh_after_a_clean_end_runs_quietly(caplog):
    """The stream reports an outage itself, should it then fail to reconnect."""
    task = _waiting_task()
    client = _streaming_client([])
    quiet: list[bool] = []

    def refresh(path):
        quiet.append(_quiet_now())
        raise api_request.AristotleAPIError("API request failed with status 503: Service Unavailable", 503)

    async def ends_cleanly(endpoint, *, reconnect_if, **_):
        client.get = AsyncMock(side_effect=refresh)
        assert await reconnect_if()
        client.get = AsyncMock(side_effect=working)
        client.stream_ended = True
        yield _plain_event("event-1", "Done")

    working = client.get.side_effect
    client.stream_sse = ends_cleanly
    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        await task.wait_for_completion(interactive=False)

    assert quiet == [True]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("num_events", [3, 0])
async def test_a_wait_at_a_terminal_shows_the_outage_while_it_lasts(monkeypatch, num_events):
    """Each frame clears the terminal, log lines included, so the frames carry the outage, whatever --limit is. The
    stream's read, which goes on during a question prompt, draws no frame itself."""
    monkeypatch.setattr("aristotlelib.agent_task.QUESTION_REFRESH_SECONDS", 0.01)
    task = _waiting_task()
    client = _streaming_client([])
    refreshes = 0
    failed = (
        "The connection to Aristotle failed: ConnectError: All connection attempts failed. Trying again for up to 10 "
        "minutes while the task goes on."
    )

    def refresh(path):
        nonlocal refreshes
        refreshes += 1
        return working(path)

    async def outage_then_back(endpoint, *, on_outage, **_):
        drawn = shown.call_count
        on_outage(httpx.ConnectError("All connection attempts failed"))
        assert shown.call_count == drawn
        start = refreshes
        while refreshes < start + 3:
            await asyncio.sleep(0.01)
        during.extend(call.args[0] for call in shown.call_args_list[drawn:])
        drawn = shown.call_count
        on_outage(None)
        assert shown.call_count == drawn
        client.stream_ended = True
        yield _plain_event("event-1", "Done")

    working = client.get.side_effect
    client.get = AsyncMock(side_effect=refresh)
    client.stream_sse = outage_then_back
    during: list[list[str]] = []
    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        await asyncio.wait_for(task.wait_for_completion(num_events=num_events, interactive=True), 5)

    # Every refresh's frame in the outage carries it, pinned last; once it's back, the frames don't.
    assert len(during) >= 3
    assert all(frame[-1] == failed for frame in during)
    assert failed not in shown.call_args_list[-1].args[0]


async def test_a_run_that_ends_in_an_outage_leaves_no_outage_in_the_last_frame(caplog):
    """A cancel adds no event, so a stream reconnecting after it may end without a line; the refresh that then gets
    through says the connection is back."""
    task = _waiting_task()
    client = _streaming_client([], live_status="CANCELED")
    failed = (
        "The connection to Aristotle failed: ConnectError: All connection attempts failed. Trying again for up to 10 "
        "minutes while the task goes on."
    )

    async def outage_then_ends(endpoint, *, on_outage, **_):
        on_outage(httpx.ConnectError("All connection attempts failed"))
        client.stream_ended = True
        return
        yield

    client.stream_sse = outage_then_ends
    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        await task.wait_for_completion(interactive=True)

    assert failed not in shown.call_args_list[-1].args[0]
    assert caplog.messages[:2] == [failed, "The connection to Aristotle is back."]


@pytest.mark.parametrize(
    ("error", "says"),
    [
        (httpx.ConnectError("All connection attempts failed"), "ConnectError: All connection attempts failed"),
        (
            httpx.RemoteProtocolError("Server disconnected without sending a response."),
            "RemoteProtocolError: Server disconnected without sending a response",
        ),
        (httpx.ReadTimeout(""), "ReadTimeout"),
        (
            api_request.AristotleAPIError(
                "Too many requests to Aristotle at once. Wait a few seconds and try again.", status_code=429
            ),
            "HTTP 429",
        ),
        (api_request.AristotleAPIError("API request failed with status 503: Service Unavailable.", 503), "HTTP 503"),
    ],
    ids=["transport", "transport with a full stop", "transport without a message", "429", "5xx"],
)
async def test_an_outage_names_what_failed_in_one_sentence(error, says, caplog):
    """An API error's advice, such as to try again, would contradict the line; a closing full stop would double."""
    task = _waiting_task()
    client = _streaming_client([])

    async def fails_then_ends(endpoint, *, on_outage, **_):
        on_outage(error)
        client.stream_ended = True
        return
        yield

    client.stream_sse = fails_then_ends
    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        await task.wait_for_completion(interactive=False)

    assert caplog.messages[0] == (
        f"The connection to Aristotle failed: {says}. Trying again for up to 10 minutes while the task goes on."
    )


async def test_a_stream_refused_with_a_final_status_raises_it():
    task = _waiting_task()
    client = _streaming_client([])
    refusal = api_request.AristotleAPIError("API request failed with status 404: Not Found", 404)

    async def refuses(endpoint, **_):
        raise refusal
        yield

    client.stream_sse = refuses

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
        pytest.raises(api_request.AristotleAPIError) as info,
    ):
        await task.wait_for_completion(interactive=True)

    assert info.value is refusal


async def test_a_wait_outlasts_a_stream_cut_every_minute_and_reports_the_outcome(monkeypatch, caplog):
    """Ten quiet one-minute connections, then the run's last event: the wait follows it to COMPLETE."""
    monkeypatch.setattr(api_request, "STREAM_RECONNECT_SECONDS", 0)
    set_api_key("test-key")
    task = _waiting_task()
    streams = 0

    async def quiet_minute():
        yield b": keepalive\n\n"
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")

    async def last_event():
        event = {
            "event_id": "e-1",
            "agent_task_id": "task-wait-q",
            "event_type": 16,
            "created_at": "2024-01-01T00:10:00",
            "content": "Proved it.",
            "file_path": None,
            "explanation": None,
            "status": 1,
        }
        yield f"id: 2024-01-01T00:10:00\ndata: {json.dumps(event)}\n\n".encode()

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal streams
        if request.url.path.endswith("/events-stream"):
            streams += 1
            return httpx.Response(
                200,
                content=quiet_minute() if streams <= 10 else last_event(),
                headers={"Content-Type": "text/event-stream"},
            )
        if request.url.path.endswith("/questions"):
            return httpx.Response(200, json=[])
        finished = streams > 10
        return httpx.Response(
            200,
            json={
                "project_id": "proj-wait",
                "agent_task_id": "task-wait-q",
                "status": "COMPLETE" if finished else "IN_PROGRESS",
                "created_at": "2024-01-01T00:00:00",
                "last_updated_at": "2024-01-01T00:10:00",
                "percent_complete": 100 if finished else 10,
                "file_name": None,
                "description": None,
                "output_summary": None,
            },
        )

    transport = httpx.MockTransport(handle)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))

    with caplog.at_level(logging.INFO, logger="aristotle"):
        await task.wait_for_completion(interactive=False)

    assert streams == 11
    assert task.status == TaskStatus.COMPLETE
    assert "Task complete!" in caplog.text
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


async def test_a_wait_reconnects_when_the_stream_goes_silent_without_closing(monkeypatch, caplog):
    """A loopback server sends one keepalive and then nothing, holding the socket open; the wait times out and reconnects."""
    monkeypatch.setattr(api_request, "STREAM_READ_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(api_request, "STREAM_RECONNECT_SECONDS", 0)
    for variable in api_request.PROXY_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    set_api_key("test-key")
    task = _waiting_task()
    streams: list[str | None] = []
    held = asyncio.Event()

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = (await reader.readuntil(b"\r\n\r\n")).decode()
        path = request.split(" ", 2)[1]
        headers = dict(line.split(": ", 1) for line in request.split("\r\n")[1:] if ": " in line)
        if path.endswith("/events-stream"):
            streams.append(headers.get("Last-Event-ID"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
            if len(streams) == 1:
                body = b"id: 2024-01-01T00:01:00\n" + _sse_event("e-1") + b": keepalive\n\n"
                writer.write(b"%x\r\n%s\r\n" % (len(body), body))
                await writer.drain()
                await held.wait()  # Silent, and the socket stays open: no FIN.
            else:
                body = b"id: 2024-01-01T00:02:00\n" + _sse_event("e-2")
                writer.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body))
        else:
            finished = len(streams) > 1
            payload = json.dumps(
                []
                if path.endswith("/questions")
                else {
                    "project_id": "proj-wait",
                    "agent_task_id": "task-wait-q",
                    "status": "COMPLETE" if finished else "IN_PROGRESS",
                    "created_at": "2024-01-01T00:00:00",
                    "last_updated_at": "2024-01-01T00:02:00",
                    "percent_complete": 100 if finished else 10,
                    "file_name": None,
                    "description": None,
                    "output_summary": None,
                }
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                b"Content-Length: %d\r\n\r\n%s" % (len(payload), payload)
            )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv("ARISTOTLE_WEB_URL", f"http://127.0.0.1:{port}")
    try:
        with caplog.at_level(logging.INFO, logger="aristotle"):
            await asyncio.wait_for(task.wait_for_completion(interactive=False), timeout=10)
    finally:
        held.set()
        server.close()
        await server.wait_closed()

    assert streams == [None, "2024-01-01T00:01:00"]
    assert task.status == TaskStatus.COMPLETE
    assert "Task complete!" in caplog.text
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


def _sse_event(event_id: str) -> bytes:
    event = {
        "event_id": event_id,
        "agent_task_id": "task-wait-q",
        "event_type": 16,
        "created_at": "2024-01-01T00:01:00",
        "content": f"Event {event_id}.",
        "file_path": None,
        "explanation": None,
        "status": 1,
    }
    return f"data: {json.dumps(event)}\n\n".encode()


async def test_a_stream_that_ends_before_the_task_does_is_reported(caplog):
    task = _waiting_task()
    client = _streaming_client([])

    async def closes_early(endpoint, **_):
        return
        yield

    # The stream ends without setting stream_ended, so every refresh still reports IN_PROGRESS.
    client.stream_sse = closes_early

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        await task.wait_for_completion(interactive=True)

    assert task.live
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == [
        "The event stream ended before the task finished. Use 'aristotle show proj-wait' to follow it."
    ]


@pytest.mark.parametrize("carry_on", [None, RESUME], ids=["show", "the caller's command"])
async def test_a_stream_that_ends_with_the_status_out_of_reach_still_names_the_next_step(carry_on, caplog):
    task = _waiting_task()
    client = _streaming_client([])
    refresh = client.get.side_effect

    async def closes_early(endpoint, **_):
        client.stream_ended = True
        return
        yield

    def unreachable_once_ended(path):
        if client.stream_ended:
            raise api_request.AristotleAPIError("API request failed with status 503: Unavailable", 503)
        return refresh(path)

    client.stream_sse = closes_early
    client.get = AsyncMock(side_effect=unreachable_once_ended)

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        await task.wait_for_completion(interactive=True, carry_on=carry_on)

    assert task.live
    next_step = " Use 'aristotle show proj-wait' to follow it." if carry_on is None else f"\n{RESUME}"
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == [
        f"The event stream ended, and the task's status could not be checked.{next_step}"
    ]


async def test_a_stream_that_ends_before_a_queued_run_starts_says_it_never_started(caplog):
    task = _waiting_task()
    task.status = TaskStatus.QUEUED
    client = _streaming_client([], live_status="QUEUED")

    async def closes_early(endpoint, **_):
        return
        yield

    client.stream_sse = closes_early

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        await task.wait_for_completion(interactive=True)

    assert task.status == TaskStatus.QUEUED
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == [
        "The event stream ended before the run started.\n"
        "The run has not started yet: it is still queued, behind your other runs in progress or waiting for capacity.\n"
        "Follow it with: aristotle show proj-wait\n"
        "Cancel it with: aristotle cancel proj-wait"
    ]


@pytest.mark.parametrize(
    ("status", "live"),
    [
        (TaskStatus.QUEUED, True),
        (TaskStatus.IN_PROGRESS, True),
        (TaskStatus.COMPLETE, False),
        (TaskStatus.FAILED, False),
        (TaskStatus.CANCELED, False),
    ],
)
async def test_refresh_live_reports_the_refreshed_status(status, live):
    task = _waiting_task()

    async def refreshed() -> None:
        task.status = status

    with patch.object(AgentTask, "refresh", new=AsyncMock(side_effect=refreshed)):
        assert await task.refresh_live() is live


async def test_refresh_live_raises_when_the_refresh_fails():
    task = _waiting_task()

    with patch.object(
        AgentTask, "refresh", new=AsyncMock(side_effect=AristotleAPIError("bad gateway", status_code=502))
    ):
        with pytest.raises(AristotleAPIError):
            await task.refresh_live()


async def test_a_failed_final_refresh_ends_the_wait_with_a_warning(caplog):
    task = _waiting_task()
    client = _streaming_client([])
    client.get = AsyncMock(side_effect=AristotleAPIError("bad gateway", status_code=502))

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[])),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        await task.wait_for_completion(interactive=True)

    assert "Could not refresh the task status: bad gateway" in caplog.text
    assert "Task complete!" not in caplog.text


async def test_the_per_event_prompt_shows_its_question_on_stderr_whatever_logging_does(capsys):
    task = _waiting_task()
    event = Event.model_validate(_question_event("event-legacy", status=2, content="Which polarity?"))

    with (
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.agent_task.read_stdin_line", new=AsyncMock(return_value="Active low")),
        patch("aristotlelib.agent_task.Event.answer", new=AsyncMock()) as answer,
        patch("aristotlelib.progress_utils.clear_progress_lines"),
    ):
        assert await task._prompt_for_answer(event) is True

    assert capsys.readouterr().err == f"{event}\nType your answer:\n"
    answer.assert_awaited_once_with("Active low")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AristotleAPIError("API request failed with status 400: already answered", status_code=400), "already closed"),
        (AristotleAPIError("API request failed with status 409: task has ended", status_code=409), "already closed"),
        (AristotleAPIError("API request failed with status 500: boom", status_code=500), "Error answering question"),
    ],
    ids=["400", "409", "500"],
)
async def test_the_per_event_prompt_reports_a_refused_answer(error, expected, caplog):
    task = _waiting_task()
    event = Event.model_validate(_question_event("event-legacy", status=2, content="Which polarity?"))

    with (
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.agent_task.read_stdin_line", new=AsyncMock(return_value="Active low")),
        patch("aristotlelib.agent_task.Event.answer", new=AsyncMock(side_effect=error)),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        assert await task._prompt_for_answer(event) is True

    assert expected in caplog.text
    if expected == "already closed":
        assert "This question is already closed; the answer was not sent." in caplog.text
        assert "Error answering question" not in caplog.text


async def test_the_per_event_prompt_does_not_read_for_a_shared_projects_viewer(caplog):
    task = _waiting_task()
    task.read_only = True
    events = [_question_event("event-legacy", status=2, content="Which polarity?")]

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client(events, read_only=True)
        ),
        patch(
            "aristotlelib.agent_task.Question.list_for_task",
            new=AsyncMock(side_effect=AristotleAPIError("not found", status_code=404)),
        ),
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("builtins.input", Mock(side_effect=AssertionError("must not read stdin"))) as mock_input,
        patch("aristotlelib.agent_task.Event.answer", new=AsyncMock()) as answered,
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
        caplog.at_level(logging.INFO),
    ):
        await task.wait_for_completion(interactive=True)

    mock_input.assert_not_called()
    answered.assert_not_called()
    assert "Aristotle asked: Which polarity?" in caplog.text
    # The redraw clears the terminal, so the question stays on the display, not only in the log.
    assert any(line.startswith("Aristotle asked: Which polarity?") for line in shown.call_args_list[-1].args[0])


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, asyncio.CancelledError])
async def test_an_interrupted_wait_says_the_task_is_still_running_and_re_raises(interrupt, caplog):
    """Ctrl-C reaches the wait as KeyboardInterrupt, or as a cancellation when asyncio handles SIGINT."""
    task = _waiting_task()

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client([])),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=interrupt)),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        with pytest.raises(interrupt):
            await task.wait_for_completion(interactive=True)

    assert "Task task-wait-q is still running." in caplog.text


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, asyncio.CancelledError])
async def test_an_interrupted_wait_on_a_queued_run_says_it_has_not_started(interrupt, caplog):
    task = _waiting_task()
    task.status = TaskStatus.QUEUED

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client([], live_status="QUEUED")
        ),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=interrupt)),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        with pytest.raises(interrupt):
            await task.wait_for_completion(interactive=True)

    assert caplog.messages == [
        "Task task-wait-q is still queued.\n"
        "The run has not started yet: it is still queued, behind your other runs in progress or waiting for capacity.\n"
        "Follow it with: aristotle show proj-wait\n"
        "Cancel it with: aristotle cancel proj-wait"
    ]


async def test_an_interrupted_wait_on_someone_elses_queued_run_names_its_owner_and_offers_no_cancel(caplog):
    task = _waiting_task()
    task.status = TaskStatus.QUEUED
    task.read_only = True

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient",
            return_value=_streaming_client([], read_only=True, live_status="QUEUED"),
        ),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=KeyboardInterrupt)),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        with pytest.raises(KeyboardInterrupt):
            await task.wait_for_completion(interactive=True)

    assert caplog.messages == [
        "Task task-wait-q is still queued.\n"
        "The run has not started yet: it is still queued, behind its owner's other runs in progress or waiting for "
        "capacity.\n"
        "Follow it with: aristotle show proj-wait"
    ]


async def test_an_interrupted_wait_on_a_run_that_has_since_started_says_it_is_running(caplog):
    task = _waiting_task()
    task.status = TaskStatus.QUEUED

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client([])),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=KeyboardInterrupt)),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        with pytest.raises(KeyboardInterrupt):
            await task.wait_for_completion(interactive=True)

    assert task.status == TaskStatus.IN_PROGRESS
    assert caplog.messages == [
        "Task task-wait-q is still running.\nFollow it with: aristotle show proj-wait\n"
        "Cancel it with: aristotle cancel proj-wait"
    ]


async def test_an_interrupted_wait_names_the_callers_command_to_carry_on(caplog):
    task = _waiting_task()

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client([])),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=KeyboardInterrupt)),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        with pytest.raises(KeyboardInterrupt):
            await task.wait_for_completion(interactive=True, carry_on=RESUME)

    assert caplog.messages == [
        f"Task {task.agent_task_id} is still running.\n{RESUME}\nCancel it with: aristotle cancel proj-wait"
    ]


async def test_an_interrupted_wait_whose_refresh_fails_claims_no_status_and_says_what_to_do(caplog):
    task = _waiting_task()
    task.status = TaskStatus.QUEUED
    client = _streaming_client([])
    client.get = AsyncMock(side_effect=AristotleAPIError("unavailable", status_code=503))

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=KeyboardInterrupt)),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        with pytest.raises(KeyboardInterrupt):
            await task.wait_for_completion(interactive=True)

    assert caplog.messages == [
        "Task task-wait-q has not finished; its current status could not be checked.\n"
        "Follow it with: aristotle show proj-wait\n"
        "Cancel it with: aristotle cancel proj-wait"
    ]


async def test_an_interrupt_after_the_stream_ended_during_a_flow_leaves_no_unretrieved_read(caplog):
    """The next-event read ends with StopAsyncIteration while the flow is open; Ctrl-C must not leave it unretrieved."""
    task = _waiting_task()
    q1 = _pending_question(1)

    async def interrupted_flow(*args, **kwargs) -> FlowResult:
        await asyncio.sleep(0.05)  # the pending read sees the stream end
        raise KeyboardInterrupt

    with (
        patch(
            "aristotlelib.api_request.AristotleRequestClient",
            return_value=_streaming_client([_question_event("event-q1")]),
        ),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=[[], [q1]])),
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock(side_effect=interrupted_flow)),
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
    ):
        with pytest.raises(KeyboardInterrupt):
            await task.wait_for_completion(interactive=True)
    gc.collect()

    assert "exception was never retrieved" not in caplog.text


async def test_a_question_asked_before_the_wait_attached_is_presented():
    """The stream does not replay earlier events, so `show` attaching late must find the question by listing."""
    task = _waiting_task()
    q1 = _pending_question(1)

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client([])),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[q1])),
        patch(
            "aristotlelib.agent_task.run_question_flow", new=AsyncMock(return_value=FlowResult(answered=[q1]))
        ) as flow,
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
    ):
        await task.wait_for_completion(interactive=True)

    flow.assert_called_once()
    assert flow.call_args.args[0] == [q1]


async def test_a_quiet_stream_is_polled_for_questions_without_any_question_event(monkeypatch):
    monkeypatch.setattr("aristotlelib.agent_task.QUESTION_REFRESH_SECONDS", 0.05)
    task = _waiting_task()
    q1 = _pending_question(1)
    # Nothing at the start; the question appears while the stream stays silent.
    listings = iter([[]])
    list_for_task = AsyncMock(side_effect=lambda _task_id: next(listings, [q1]))

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_quiet_client([], quiet_seconds=0.3)),
        patch("aristotlelib.agent_task.Question.list_for_task", new=list_for_task),
        patch(
            "aristotlelib.agent_task.run_question_flow", new=AsyncMock(return_value=FlowResult(answered=[q1]))
        ) as flow,
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
    ):
        await task.wait_for_completion(interactive=True)

    flow.assert_called_once()
    assert flow.call_args.args[0] == [q1]


async def test_a_question_of_an_ended_run_is_shown_without_opening_the_flow():
    """No answer can reach an ended run, so its pending question gets no flow and no footer."""
    task = _waiting_task()
    q1 = _pending_question(1)
    client = _streaming_client([])
    client.stream_ended = True

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[q1])),
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock()) as flow,
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        await task.wait_for_completion(interactive=True)

    flow.assert_not_called()
    screens = [line for call in shown.call_args_list for line in call.args[0]]
    assert f"Aristotle asked: {q1.content}\nThe run has ended, so no answer can reach Aristotle now." in screens
    assert not any("pending" in line for line in screens)


async def test_a_failed_refresh_before_the_flow_keeps_the_last_known_status():
    """The flow still opens; its own check before sending decides whether anything goes out."""
    task = _waiting_task()
    q1 = _pending_question(1)
    client = _streaming_client([])
    refresh_response = client.get.side_effect
    failures = iter([AristotleAPIError("bad gateway", status_code=502)])

    def get(path):
        failure = next(failures, None)
        if failure is not None:
            raise failure
        return refresh_response(path)

    client.get = AsyncMock(side_effect=get)

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[q1])),
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock(return_value=FlowResult())) as flow,
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines"),
    ):
        await task.wait_for_completion(interactive=True)

    flow.assert_called_once()


async def test_no_footer_after_a_flow_that_found_the_run_ended():
    task = _waiting_task()
    q1 = _pending_question(1)
    ended = FlowResult(unanswered=[q1], run_ended=True, summary=["The run has ended, so nothing was sent."])

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client([])),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[q1])),
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock(return_value=ended)),
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        await task.wait_for_completion(interactive=True)

    screens = [line for call in shown.call_args_list for line in call.args[0]]
    assert "The run has ended, so nothing was sent." in screens
    assert not any("pending" in line for line in screens)


async def test_the_footer_clears_once_the_run_has_ended(monkeypatch):
    """A question left pending stops counting once the run has ended, even before the stream closes."""
    monkeypatch.setattr("aristotlelib.agent_task.QUESTION_REFRESH_SECONDS", 0.05)
    task = _waiting_task()
    q1 = _pending_question(1)
    client = _quiet_client([], quiet_seconds=0.4)

    async def skipped(*args, **kwargs) -> FlowResult:
        # The run ends while the stream is still open.
        client.stream_ended = True
        return FlowResult(unanswered=[q1])

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[q1])),
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock(side_effect=skipped)),
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
    ):
        await task.wait_for_completion(interactive=True)

    footers = [any("pending" in line for line in call.args[0]) for call in shown.call_args_list]
    assert True in footers
    assert footers[-1] is False


async def test_a_viewer_of_a_shared_project_is_not_prompted(caplog):
    """The answer endpoint takes the owner only, so a viewer gets the questions logged and a footer to match."""
    task = _waiting_task()
    task.read_only = True
    q1 = _pending_question(1)

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_streaming_client([], read_only=True)),
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(return_value=[q1])),
        patch("aristotlelib.agent_task.run_question_flow", new=AsyncMock()) as flow,
        patch("aristotlelib.agent_task.can_prompt", return_value=True),
        patch("aristotlelib.progress_utils.clear_progress_lines"),
        patch("aristotlelib.progress_utils.print_progress_lines") as shown,
        caplog.at_level(logging.INFO),
    ):
        await task.wait_for_completion(interactive=True)

    flow.assert_not_called()
    assert f"Aristotle asked: {q1.content}" in caplog.text
    footers = [line for call in shown.call_args_list for line in call.args[0] if "pending" in line]
    assert footers and all("only the project's owner can answer" in line for line in footers)
    # The redraw clears the terminal, so the question stays on the display, not only in the log.
    assert f"Aristotle asked: {q1.content}" in shown.call_args_list[-1].args[0]


# --- Requests that fail: what is sent again, and what is not ---

CREATED_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
LOST = "Aristotle may have acted on it, so it was not sent again."


def _record(project_id: str = CREATED_ID) -> httpx.Response:
    return httpx.Response(200, json=_listed(project_id, "Prove it").model_dump(mode="json"))


TASK = {
    "project_id": CREATED_ID,
    "agent_task_id": "task-1",
    "status": "QUEUED",
    "created_at": "2026-10-06T00:00:00",
    "last_updated_at": "2026-10-06T00:00:00",
    "percent_complete": None,
    "file_name": None,
    "description": None,
    "output_summary": None,
}


def _task_record() -> httpx.Response:
    return httpx.Response(200, json=TASK)


def _per_second_limit() -> httpx.Response:
    return httpx.Response(429, json={"detail": "Too many requests."})


@pytest.mark.parametrize(
    ("lost_response", "failure"),
    [
        (lambda: httpx.ReadTimeout(""), "ReadTimeout on POST /project."),
        (
            lambda: httpx.RemoteProtocolError("Server disconnected without sending a response."),
            "RemoteProtocolError on POST /project: Server disconnected without sending a response.",
        ),
        (
            lambda: httpx.Response(504, text="upstream request timeout"),
            "POST /project: API request failed with status 504: upstream request timeout.",
        ),
        # The whole body may have been handed to the socket before the write's completion timed out.
        (lambda: httpx.WriteTimeout(""), "WriteTimeout on POST /project."),
    ],
    ids=["read timeout", "dropped connection", "gateway timeout", "write timeout"],
)
async def test_a_create_whose_response_is_lost_makes_one_project_and_says_how_to_find_it(
    lost_response, failure, api_server
):
    created: list[httpx.Request] = []

    def create_then_lose_the_response(request: httpx.Request):
        created.append(request)
        return lost_response()

    requests = api_server(create_then_lose_the_response)

    with pytest.raises(api_request.AristotleOutcomeUnknownError) as info:
        await Project.create(prompt="Prove it")

    assert len(requests) == len(created) == 1
    assert str(info.value) == (
        f"{failure} {LOST} If it did, the project was created, though it may take a few minutes to show up: check "
        "`aristotle list` before submitting again."
    )


@pytest.mark.parametrize(
    "refusal",
    [lambda: httpx.ConnectError("[Errno 111] Connection refused"), lambda: httpx.ConnectTimeout(""), _per_second_limit],
    ids=["connect error", "connect timeout", "per-second 429"],
)
async def test_a_create_that_was_not_processed_is_sent_again_and_makes_one_project(refusal, api_server, tmp_path):
    tarball = tmp_path / "project.tar.gz"
    tarball.write_bytes(b"\x1f\x8b" + os.urandom(2048))
    created: list[httpx.Request] = []

    def create(request: httpx.Request) -> httpx.Response:
        created.append(request)
        return _record()

    requests = api_server(lambda request: refusal(), create)

    project = await Project.create(prompt="Prove it", tar_file_path=tarball)

    assert project.project_id == CREATED_ID
    assert len(requests) == 2
    assert len(created) == 1
    assert tarball.read_bytes() in requests[1].content


class _ChunkRecorder(httpx.AsyncBaseTransport):
    """A transport that answers every request with a project record, noting the size of each body chunk it reads."""

    def __init__(self) -> None:
        self.chunks: list[int] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async for chunk in request.stream:
            self.chunks.append(len(chunk))
        return _record()


async def test_a_create_sends_its_archive_in_chunks_not_in_one_write(tmp_path, monkeypatch):
    tarball = tmp_path / "project.tar.gz"
    tarball.write_bytes(os.urandom(512 * 1024))
    recorder = _ChunkRecorder()
    monkeypatch.setattr(api_request, "API_KEY", "test-key")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=recorder, **kwargs))

    await Project.create(prompt="Prove it", tar_file_path=tarball)

    assert sum(recorder.chunks) > 512 * 1024
    assert max(recorder.chunks) <= 64 * 1024


async def test_a_creates_reply_may_take_as_long_as_its_archive_takes_to_reach_the_server(api_server, tmp_path):
    tarball = tmp_path / "project.tar.gz"
    tarball.write_bytes(bytes(3 * 1024 * 1024))
    requests = api_server(_record(), _record())

    await Project.create(prompt="Prove it", tar_file_path=tarball)
    await Project.create(prompt="Prove it")

    # 3 MB at 16 KB/s on top of the 30 s, which is about all that is left without an archive, for a short prompt.
    assert [request.extensions["timeout"]["read"] for request in requests] == [
        pytest.approx(30 + 192, abs=0.1),
        pytest.approx(30, abs=0.1),
    ]


async def test_a_creates_prompt_counts_as_its_form_encoding_sends_it(api_server):
    requests = api_server(_record())

    # Each & goes out as %26, three bytes.
    await Project.create(prompt="&" * (1024 * 1024 // 3))

    assert requests[0].extensions["timeout"]["read"] == pytest.approx(30 + 64, abs=0.5)


async def test_a_long_prompt_counts_toward_the_reply_wait_as_an_upload_does(api_server, tmp_path):
    requests = api_server(_record(), _task_record())
    prompt = "Formalize this spec:\n" + "x" * 1024 * 1024

    await Project.create(prompt=prompt)
    await _listed(CREATED_ID, "Prove it").ask(prompt)

    # The prompt's megabyte, at 16 KB/s, on top of the 30 s.
    assert [request.extensions["timeout"]["read"] for request in requests] == [
        pytest.approx(30 + 64, abs=0.1),
        pytest.approx(30 + 64, abs=0.1),
    ]


async def test_a_create_that_fails_closes_its_archive(api_server, tmp_path):
    tarball = tmp_path / "project.tar.gz"
    tarball.write_bytes(b"\x1f\x8b" + os.urandom(2048))
    api_server(*(lambda request: httpx.ReadTimeout("") for _ in range(api_request.RETRY_ATTEMPTS)))
    opened: list[io.BufferedReader] = []
    real_open = open

    def spy(*args, **kwargs):
        opened.append(real_open(*args, **kwargs))
        return opened[-1]

    with patch("aristotlelib.project.open", side_effect=spy, create=True):
        with pytest.raises(api_request.AristotleOutcomeUnknownError):
            await Project.create(prompt="Prove it", tar_file_path=tarball)

    assert [f.closed for f in opened] == [True]


def _fill_fifo(path: Path, data: bytes) -> threading.Thread:
    """Write ``data`` to the named pipe at ``path`` from a thread, stopping quietly if the reader closes it."""

    def write() -> None:
        try:
            with open(path, "wb") as fifo:
                fifo.write(data)
        except BrokenPipeError:
            pass

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    return thread


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipes")
async def test_a_tar_file_that_is_a_pipe_goes_up_in_full(tmp_path, monkeypatch):
    pipe = tmp_path / "project.tar.gz"
    os.mkfifo(pipe)
    data = b"\x1f\x8b" + os.urandom(200 * 1024)
    writer = _fill_fifo(pipe, data)
    bodies: list[bytes] = []

    # A real HTTP/1.1 server: httpx's MockTransport doesn't hold a body to its Content-Length, as h11 does.
    async def create(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        length = re.search(rb"(?i)\r\ncontent-length: *(\d+)", head)
        bodies.append(await reader.readexactly(int(length.group(1))) if length else b"")
        record = json.dumps(_listed(CREATED_ID, "Prove it").model_dump(mode="json")).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(record))
        writer.write(record)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(create, "127.0.0.1", 0)
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARISTOTLE_WEB_URL", f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
    monkeypatch.setattr(api_request, "API_KEY", "test-key")
    try:
        project = await Project.create(prompt="Prove it", tar_file_path=pipe)
    finally:
        server.close()
        await server.wait_closed()

    writer.join(5)
    assert project.project_id == CREATED_ID
    assert len(bodies) == 1
    assert data in bodies[0]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipes")
async def test_a_pipe_over_the_limit_is_refused_before_any_request(api_server, tmp_path, monkeypatch):
    monkeypatch.setattr(local_file_utils, "MAX_FILE_SIZE_BYTES", 1024 * 1024)
    pipe = tmp_path / "project.tar.gz"
    os.mkfifo(pipe)
    writer = _fill_fifo(pipe, bytes(2 * 1024 * 1024))
    requests = api_server()

    with pytest.raises(LeanProjectError) as info:
        await Project.create(prompt="Prove it", tar_file_path=pipe)

    writer.join(5)
    assert str(info.value) == "The upload is 1.0 MB compressed, over the 1 MB limit."
    assert requests == []


async def test_a_tar_file_over_the_limit_is_refused_before_any_request(api_server, tmp_path, monkeypatch):
    monkeypatch.setattr(local_file_utils, "MAX_FILE_SIZE_BYTES", 1024 * 1024)
    tarball = tmp_path / "project.tar.gz"
    tarball.write_bytes(b"\0" * (1024 * 1024 + 1))
    requests = api_server()

    with pytest.raises(LeanProjectError) as info:
        await Project.create(prompt="Prove it", tar_file_path=tarball)

    assert str(info.value) == "The upload is 1.0 MB compressed, over the 1 MB limit."
    assert requests == []


async def test_a_folder_whose_archive_is_over_the_limit_says_how_to_cut_it_down(
    api_server, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(local_file_utils, "MAX_FILE_SIZE_BYTES", 1024 * 1024)
    (tmp_path / "trace.vcd").write_bytes(os.urandom(2 * 1024 * 1024))
    (tmp_path / "top.sv").write_text("module top; endmodule\n")
    requests = api_server()

    with caplog.at_level(logging.INFO, logger="aristotle"), pytest.raises(LeanProjectError) as info:
        await Project.create_from_directory(prompt="Prove it", project_dir=tmp_path)

    assert str(info.value) == (
        "The upload is 2.0 MB compressed, over the 1 MB limit. Exclude large files with .gitignore or submit a "
        "smaller folder."
    )
    assert requests == []
    assert "Uploading" not in caplog.text


def _create_body(request: httpx.Request) -> dict:
    """The JSON ``body`` field a create sent, from its form or its multipart body."""
    content = request.content.decode("latin-1")
    if "Content-Disposition" not in content:
        return json.loads(parse_qs(content)["body"][0])
    match = re.search(r'name="body"\r\n\r\n(.*?)\r\n', content)
    assert match is not None
    return json.loads(match.group(1))


@pytest.mark.parametrize("name", [None, "FIFO overflow"])
async def test_a_create_sends_the_name_only_when_given(name, api_server):
    requests = api_server(_record())

    await Project.create(prompt="Prove it", name=name)

    body = _create_body(requests[0])
    assert body.get("name") == name
    assert ("name" in body) is (name is not None)


async def test_a_name_goes_without_surrounding_whitespace_and_an_empty_one_is_refused(api_server):
    requests = api_server(_record())

    await Project.create(prompt="Prove it", name="  FIFO overflow\t")
    with pytest.raises(ValueError, match="Project name cannot be empty"):
        await Project.create(prompt="Prove it", name="   ")

    assert len(requests) == 1
    assert _create_body(requests[0])["name"] == "FIFO overflow"


async def test_a_folder_with_an_empty_name_is_refused_before_it_is_archived(tmp_path, monkeypatch):
    archived = Mock()
    monkeypatch.setattr(local_file_utils, "gitignored_files", archived)

    with pytest.raises(ValueError, match="Project name cannot be empty"):
        await Project.create_from_directory(prompt="Prove it", project_dir=tmp_path, name=" ")

    archived.assert_not_called()


async def test_a_folder_is_created_with_its_name(api_server, tmp_path, monkeypatch):
    (tmp_path / "top.sv").write_text("module top; endmodule\n")
    requests = api_server(_record())

    await Project.create_from_directory(prompt="Prove it", project_dir=tmp_path, name="FIFO overflow")

    assert _create_body(requests[0])["name"] == "FIFO overflow"


async def test_each_create_and_ask_sends_its_own_idempotency_key_on_every_attempt(api_server):
    requests = api_server(_per_second_limit(), _record(), _record(), _per_second_limit(), _task_record())

    await Project.create(prompt="Prove it")
    await Project.create(prompt="Prove it")
    await _listed(CREATED_ID, "Prove it").ask("Go on")

    keys = [request.headers.get("Idempotency-Key") for request in requests]
    assert all(key is not None and uuid.UUID(key) for key in keys)
    # A retry keeps its request's key; another request gets another.
    assert keys[0] == keys[1]
    assert keys[3] == keys[4]
    assert len({keys[1], keys[2], keys[4]}) == 3


def _questions_setting(request: httpx.Request) -> int:
    """The agent_questions_setting a create or an ask sent, from its form or its multipart body."""
    content = request.content.decode("latin-1")
    if "body=" in content and "Content-Disposition" not in content:
        return json.loads(parse_qs(content)["body"][0])["agent_questions_setting"]
    match = re.search(r'name="(?:agent_questions_setting|body)"\r\n\r\n(.*?)\r\n', content)
    assert match is not None
    value = match.group(1)
    return int(value) if value.isdigit() else json.loads(value)["agent_questions_setting"]


async def test_a_new_project_or_prompt_lets_the_agent_ask_unless_told_not_to(api_server, tmp_path, monkeypatch):
    (tmp_path / "Main.lean").write_text("theorem t : True := trivial\n")
    monkeypatch.setattr(local_file_utils, "get_expected_toolchain", AsyncMock(return_value=None))
    requests = api_server(*[lambda request: _task_record() if request.url.path.endswith("/ask") else _record()] * 6)

    await Project.create(prompt="Prove it")
    await Project.create(prompt="Prove it", agent_questions_setting=AgentQuestionsSetting.DISABLED)
    await Project.create_from_directory(prompt="Prove it", project_dir=tmp_path)
    await Project.create_from_directory(
        prompt="Prove it", project_dir=tmp_path, agent_questions_setting=AgentQuestionsSetting.DISABLED
    )
    await _listed(CREATED_ID, "Prove it").ask("Go on")
    await _listed(CREATED_ID, "Prove it").ask("Go on", agent_questions_setting=AgentQuestionsSetting.DISABLED)

    assert [_questions_setting(request) for request in requests] == [2, 1, 2, 1, 2, 1]


async def test_ask_sends_its_files_in_full_again_when_it_is_retried(api_server, tmp_path):
    lemma = tmp_path / "Lemma.lean"
    lemma.write_bytes(b"theorem t : True := trivial\n" * 4096)
    notes = tmp_path / "notes.md"
    notes.write_bytes(b"# Notes\n")
    project = _listed(CREATED_ID, "Prove it")
    # The refresh that checks the project is idle, a 429 after the server read the upload, then the task.
    requests = api_server(_record(), _per_second_limit(), _task_record())

    task = await project.ask("Go on", files=[lemma, notes], project_dir=tmp_path)

    assert task.agent_task_id == "task-1"
    refused, retried = requests[1], requests[2]
    for sent in (refused, retried):
        assert lemma.read_bytes() in sent.content
        assert notes.read_bytes() in sent.content


@pytest.mark.parametrize("explicit_root", [False, True], ids=["cwd", "project_dir"])
async def test_downloaded_files_keep_their_paths_and_bytes_when_selected_for_continuation(
    explicit_root, api_server, tmp_path, monkeypatch
):
    from email import policy
    from email.parser import BytesParser

    content = {
        "Proofs/Foo.lean": b"-- exact bytes\r\ntheorem t : True := trivial\r\n",
        "Other/Foo.lean": "-- a different proof: λ\ntheorem u : True := trivial\n".encode(),
    }
    project = _listed(CREATED_ID, "Prove it")
    root = tmp_path / "downloaded"
    requests = api_server(
        *[_file(data) for data in content.values()],
        _record(),
        _task_record(),
        _record(),
        _task_record(),
    )
    files = []
    async for path, saved in project.save_files(content, root):
        assert saved == root / path
        files.append(saved)
    if not explicit_root:
        monkeypatch.chdir(root)
    options = {"project_dir": root} if explicit_root else {}
    # A subset retains the same root as the complete selection, even when all its paths share a directory.
    for selection in [files, files[:1]]:
        await project.ask("Use these proofs", files=selection, **options)
        request = requests[-1]
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content
        )
        uploaded = {
            part.get_filename(): part.get_payload(decode=True)
            for part in message.iter_parts()
            if part.get_filename() is not None
        }
        assert uploaded == {path.relative_to(root).as_posix(): path.read_bytes() for path in selection}
    assert [request.url.params["file_path"] for request in requests[:2]] == list(content)


@pytest.mark.parametrize("selection", ["outside", "traversal", "duplicate", "linked outside"])
async def test_ask_refuses_invalid_file_roots_before_any_request(selection, api_server, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    inside = root / "Foo.lean"
    inside.write_bytes(b"-- inside\n")
    outside = tmp_path / "Foo.lean"
    outside.write_bytes(b"-- outside\n")
    if selection == "outside":
        files = [outside]
        reason = "outside project directory"
    elif selection == "traversal":
        files = [root / ".." / "project" / "Foo.lean"]
        reason = "must not contain"
    elif selection == "duplicate":
        files = [inside, root / "." / "Foo.lean"]
        reason = "selected more than once"
    else:
        linked = root / "Linked.lean"
        try:
            linked.symlink_to(outside)
        except OSError:
            pytest.skip("this machine cannot create symlinks")
        files = [linked]
        reason = "outside project directory"
    requests = api_server()
    with pytest.raises(ValueError, match=reason):
        await _listed(CREATED_ID, "Prove it").ask("Use these proofs", files=files, project_dir=root)
    assert requests == []


async def test_ask_preserves_the_selected_name_of_a_link_within_the_root(api_server, tmp_path):
    target = tmp_path / "Original.lean"
    target.write_bytes(b"-- proof\n")
    link = tmp_path / "Named.lean"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this machine cannot create symlinks")
    requests = api_server(_record(), _task_record())
    await _listed(CREATED_ID, "Prove it").ask("Use this", files=[link], project_dir=tmp_path)
    assert b'filename="Named.lean"' in requests[-1].content
    assert b'filename="Original.lean"' not in requests[-1].content
    assert target.read_bytes() in requests[-1].content


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot create these file names")
@pytest.mark.parametrize("name", ['Quote".lean', "Back\\slash.lean", "New\nline.lean"])
async def test_ask_refuses_names_that_cannot_roundtrip_as_multipart_paths(name, api_server, tmp_path):
    file = tmp_path / name
    file.write_bytes(b"-- proof\n")
    requests = api_server()
    with pytest.raises(ValueError, match="cannot be preserved"):
        await _listed(CREATED_ID, "Prove it").ask("Use this", files=[file], project_dir=tmp_path)
    assert requests == []


async def test_an_asks_reply_may_take_as_long_as_its_files_take_to_reach_the_server(api_server, tmp_path):
    lemma = tmp_path / "Lemma.lean"
    lemma.write_bytes(bytes(1024 * 1024))
    notes = tmp_path / "notes.md"
    notes.write_bytes(bytes(512 * 1024))
    project = _listed(CREATED_ID, "Prove it")
    # The refresh that checks the project is idle, then each ask.
    requests = api_server(_record(), _task_record(), _task_record())

    await project.ask("Go on", files=[lemma, notes], project_dir=tmp_path)
    await project.ask("Go on")

    # 1.5 MB at 16 KB/s on top of the 30 s, which is about all that is left without files, for a short prompt.
    assert [request.extensions["timeout"]["read"] for request in requests[1:]] == [
        pytest.approx(30 + 96, abs=0.1),
        pytest.approx(30, abs=0.1),
    ]


@pytest.mark.skipif(os.name == "nt", reason="Windows names are UTF-16, never decoded with the locale")
async def test_ask_sends_a_file_name_the_locale_cannot_decode_as_the_text_it_spells(api_server, tmp_path, monkeypatch):
    (tmp_path / "naïve.lean").write_bytes(b"theorem t : True := trivial\n")
    # How an ASCII locale decodes the name's UTF-8 bytes.
    as_decoded = tmp_path / b"na\xc3\xafve.lean".decode("ascii", "surrogateescape")
    monkeypatch.setattr("aristotlelib.local_file_utils.sys.getfilesystemencoding", lambda: "ascii")
    requests = api_server(_record(), _task_record())

    await _listed(CREATED_ID, "Prove it").ask("Go on", files=[as_decoded], project_dir=tmp_path)

    assert 'filename="naïve.lean"'.encode() in requests[1].content


async def test_ask_with_files_on_a_running_project_says_what_to_do_instead_and_sends_nothing(api_server, tmp_path):
    lemma = tmp_path / "Lemma.lean"
    lemma.write_text("theorem t : True := trivial\n")
    running = _listed(CREATED_ID, "Prove it").model_copy(update={"status": ProjectStatus.RUNNING})
    requests = api_server(httpx.Response(200, json=running.model_dump(mode="json")))

    with pytest.raises(ValueError) as info:
        await _listed(CREATED_ID, "Prove it").ask("Go on", files=[lemma], project_dir=tmp_path)

    assert [request.method for request in requests] == ["GET"]
    assert str(info.value) == (
        f"Files can't be added to project {CREATED_ID} while it runs a task. Send the prompt without files to reach "
        f"that task: its agent reads it right away. Or wait for the task to finish (`aristotle show {CREATED_ID} "
        "--wait`), then send the prompt with files. Cancelling the task first would lose the prompts it was given, "
        "except the project's first."
    )


async def test_ask_with_files_of_one_name_keeps_their_folders(api_server, tmp_path):
    names = ["dupA/notes.txt", "dupB/notes.txt", "a/x.sv", "b/x.sv", "c/x.sv", "y.sv"]
    files = [tmp_path / name for name in names]
    for file in files:
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(b"x\n")
    requests = api_server(_record(), _task_record())

    await _listed(CREATED_ID, "Prove it").ask("Go on", files=files, project_dir=tmp_path)

    assert len(requests) == 2
    for name in names:
        assert f'filename="{name}"'.encode() in requests[1].content


async def test_ask_with_one_file_given_twice_refuses_traversal(api_server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "alu.sv").write_bytes(b"module alu; endmodule\n")
    requests = api_server()

    with pytest.raises(ValueError, match="must not contain"):
        await _listed(CREATED_ID, "Prove it").ask(
            "Go on", files=["rtl/alu.sv", tmp_path / "rtl" / ".." / "rtl" / "alu.sv"]
        )

    assert requests == []


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs a privilege on Windows")
def test_two_files_one_path_would_name_after_a_link_are_told_apart(tmp_path, monkeypatch):
    (tmp_path / "elsewhere" / "deep").mkdir(parents=True)
    (tmp_path / "elsewhere" / "notes.txt").write_text("elsewhere\n")
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "notes.txt").write_text("work\n")
    (tmp_path / "work" / "link").symlink_to(tmp_path / "elsewhere" / "deep")
    monkeypatch.chdir(tmp_path / "work")

    # link/.. is elsewhere, not work.
    with pytest.raises(ValueError, match="must not contain"):
        continuation_uploads(["link/../notes.txt", "notes.txt"])


def test_without_inode_numbers_files_are_told_apart_by_their_paths(tmp_path, monkeypatch):
    for path in ["a/x.sv", "b/x.sv"]:
        (tmp_path / path).parent.mkdir()
        (tmp_path / path).write_text("x\n")
    real_stat = os.stat
    # As some network drives report every file.
    monkeypatch.setattr(os, "stat", lambda path: os.stat_result((real_stat(path).st_mode, 0, *real_stat(path)[2:10])))
    monkeypatch.chdir(tmp_path)

    assert [name for _, name in continuation_uploads(["a/x.sv", "b/x.sv"])] == ["a/x.sv", "b/x.sv"]
    with pytest.raises(ValueError, match="selected more than once"):
        continuation_uploads(["a/x.sv", "a/x.sv"])


async def test_ask_sends_every_file_an_iterator_gives(api_server, tmp_path):
    for name in ["alu.sv", "top.sv"]:
        (tmp_path / name).write_bytes(b"module m; endmodule\n")
    requests = api_server(_record(), _task_record())

    await _listed(CREATED_ID, "Prove it").ask("Go on", files=tmp_path.glob("*.sv"), project_dir=tmp_path)

    assert b'filename="alu.sv"' in requests[1].content
    assert b'filename="top.sv"' in requests[1].content


async def test_ask_that_may_have_reached_the_project_is_not_sent_again_and_says_how_to_check(api_server):
    requests = api_server(httpx.ReadTimeout(""), _task_record())

    with pytest.raises(api_request.AristotleOutcomeUnknownError) as info:
        await _listed(CREATED_ID, "Prove it").ask("Go on")

    assert len(requests) == 1
    assert str(info.value) == (
        f"ReadTimeout on POST /project/{CREATED_ID}/ask. {LOST} If it did, the prompt was sent, though its task may "
        f"take a few minutes to show up: check `aristotle tasks {CREATED_ID}` before sending it again."
    )


def _refused(refusal: str):
    """Every attempt refused before Aristotle acts on it: the connection or the per-second limit."""
    if refusal == "connect error":
        return [httpx.ConnectError("[Errno 111] Connection refused")] * api_request.RETRY_ATTEMPTS, (
            "ConnectError on {request} after 5 attempts over 0 s: [Errno 111] Connection refused."
        )
    return [lambda request: _per_second_limit() for _ in range(api_request.RETRY_ATTEMPTS)], (
        "{request} after 5 attempts over 0 s: Too many requests to Aristotle at once. Wait a few seconds and try again."
    )


@pytest.mark.usefixtures("frozen_api_clock")
@pytest.mark.parametrize("refusal", ["connect error", "per-second 429"])
async def test_a_create_that_no_attempt_got_through_says_no_project_was_created(refusal, api_server):
    outcomes, failure = _refused(refusal)
    api_server(*outcomes)

    with pytest.raises(api_request.AristotleNotProcessedError) as info:
        await Project.create(prompt="Prove it")

    assert str(info.value) == (
        f"{failure.format(request='POST /project')} Aristotle didn't act on it. No project was created, so it is safe "
        "to submit it again."
    )
    assert info.value.status_code == (429 if refusal == "per-second 429" else None)


@pytest.mark.usefixtures("frozen_api_clock")
async def test_ask_that_no_attempt_got_through_says_the_prompt_was_not_sent(api_server):
    outcomes, failure = _refused("connect error")
    api_server(*outcomes)

    with pytest.raises(api_request.AristotleNotProcessedError) as info:
        await _listed(CREATED_ID, "Prove it").ask("Go on")

    assert str(info.value) == (
        f"{failure.format(request=f'POST /project/{CREATED_ID}/ask')} Aristotle didn't act on it. The prompt was not "
        "sent, so it is safe to send it again."
    )


@pytest.mark.usefixtures("frozen_api_clock")
@pytest.mark.parametrize("refusal", ["connect error", "per-second 429"])
async def test_ask_whose_check_that_the_project_is_idle_failed_says_the_prompt_was_not_sent(
    refusal, api_server, tmp_path
):
    lemma = tmp_path / "Lemma.lean"
    lemma.write_text("theorem t : True := trivial\n")
    outcomes, failure = _refused(refusal)
    requests = api_server(*outcomes)

    with pytest.raises(api_request.AristotleNotProcessedError) as info:
        await _listed(CREATED_ID, "Prove it").ask("Go on", files=[lemma], project_dir=tmp_path)

    assert [request.method for request in requests] == ["GET"] * api_request.RETRY_ATTEMPTS
    assert str(info.value) == (
        f"{failure.format(request=f'GET /project/{CREATED_ID}')} The prompt was not sent, so it is safe to send it again."
    )
    assert info.value.status_code == (429 if refusal == "per-second 429" else None)


async def test_a_rename_is_sent_again_after_a_timeout(api_server):
    requests = api_server(httpx.ReadTimeout(""), _record(RENAMED_ID))

    project = await Project.rename_by_id(RENAMED_ID, "Prove it")

    assert project.project_id == RENAMED_ID
    assert len(requests) == 2


async def test_an_answer_is_not_sent_again_after_a_timeout(api_server):
    requests = api_server(httpx.ReadTimeout(""), _task_record())

    with pytest.raises(api_request.AristotleOutcomeUnknownError) as info:
        await _pending_question(1).answer("Yes")

    assert len(requests) == 1
    assert str(info.value) == (
        f"ReadTimeout on POST /question/q-1/answer. {LOST} If it did, the question was answered: check "
        "`aristotle questions proj-wait --json`, where an answered question has `response` set, before answering it "
        "again."
    )


@pytest.mark.usefixtures("frozen_api_clock")
async def test_an_answer_that_no_attempt_got_through_says_the_question_was_not_answered(api_server):
    outcomes, failure = _refused("connect error")
    api_server(*outcomes)

    with pytest.raises(api_request.AristotleNotProcessedError) as info:
        await _pending_question(1).answer("Yes")

    assert str(info.value) == (
        f"{failure.format(request='POST /question/q-1/answer')} Aristotle didn't act on it. The question was not "
        "answered, so it is safe to answer it again."
    )


def _task_in(status: str) -> httpx.Response:
    return httpx.Response(200, json={**TASK, "status": status})


CANNOT_CANCEL = httpx.Response(404, json={"detail": "Agent task cannot be canceled"})


async def test_a_cancel_is_sent_again_after_a_timeout(api_server):
    requests = api_server(httpx.ReadTimeout(""), _task_in("CANCELED"))
    task = AgentTask.model_validate(TASK)

    await task.cancel()

    assert [r.method for r in requests] == ["POST", "POST"]
    assert task.status == TaskStatus.CANCELED


@pytest.mark.parametrize(
    "before",
    [[], [lambda request: httpx.ReadTimeout("")]],
    ids=["cancelled before", "by the lost first send"],
)
async def test_a_cancel_refused_for_a_task_already_cancelled_counts_as_done(before, api_server):
    requests = api_server(*before, CANNOT_CANCEL, _task_in("CANCELED"))
    task = AgentTask.model_validate(TASK)

    await task.cancel()

    assert [r.method for r in requests] == ["POST"] * (len(before) + 1) + ["GET"]
    assert task.status == TaskStatus.CANCELED


async def test_a_cancel_refused_when_the_task_cannot_then_be_checked_raises_why_it_cannot(api_server):
    unavailable = httpx.Response(503, json={"detail": "Service Unavailable"})
    api_server(CANNOT_CANCEL, *[lambda request: unavailable] * api_request.RETRY_ATTEMPTS)
    task = AgentTask.model_validate(TASK)

    with pytest.raises(api_request.AristotleAPIError) as info:
        await task.cancel()

    assert info.value.status_code == 503
    assert str(info.value).startswith("GET /task/task-1 after 5 attempts")


async def test_a_cancel_refused_for_a_finished_task_says_it_has_finished(api_server):
    api_server(CANNOT_CANCEL, _task_in("COMPLETE"))
    task = AgentTask.model_validate(TASK)

    with pytest.raises(api_request.AristotleAPIError) as info:
        await task.cancel()

    assert info.value.status_code == 404
    assert str(info.value) == f"Task {task.agent_task_id} has already finished (COMPLETE), so it can't be cancelled."
    assert task.status == TaskStatus.COMPLETE
