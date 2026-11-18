"""Tests for aristotlelib.cli.command.archive."""

import argparse
import json
import logging
from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from aristotlelib import api_request
from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.command.archive import ArchiveAction
from aristotlelib.cli.controller import main
from aristotlelib.project import Project, ProjectStatus

PROJECT_ID = "3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f"
ARCHIVE_BY_ID = "aristotlelib.cli.command.archive.Project.archive_by_id"


def _action() -> ArchiveAction:
    parser = argparse.ArgumentParser()
    action = ArchiveAction(parser.add_subparsers())
    action.add_action_arguments()
    return action


def _archived() -> Project:
    return Project(
        project_id=PROJECT_ID,
        status=ProjectStatus.IDLE,
        created_at=datetime(2026, 11, 1),
        last_updated=datetime(2026, 11, 15, 7),
        description="Old fifo",
        has_input=True,
        has_files=True,
        archived_at=datetime(2026, 11, 15, 7),
    )


async def test_archive_says_it_archived_the_project_and_how_to_bring_it_back(caplog, capsys):
    action = _action()

    with patch(ARCHIVE_BY_ID, AsyncMock(return_value=_archived())) as archive, caplog.at_level(logging.INFO):
        assert await action.run_action(action.parser.parse_args([PROJECT_ID])) is None

    archive.assert_awaited_once_with(PROJECT_ID)
    assert caplog.messages == [
        f"Project {PROJECT_ID} archived.\nUnarchive it with:\n  aristotle unarchive {PROJECT_ID}"
    ]
    assert capsys.readouterr().out == ""


async def test_archive_archives_the_project_a_name_resolves_to_among_unarchived_ones(resolve_project):
    action = _action()
    resolve_project.side_effect = lambda reference, archived: (PROJECT_ID, None)

    with patch(ARCHIVE_BY_ID, AsyncMock(return_value=_archived())) as archive:
        await action.run_action(action.parser.parse_args(["Old fifo"]))

    resolve_project.assert_awaited_once_with("Old fifo", archived=False)
    archive.assert_awaited_once_with(PROJECT_ID)


async def test_archive_json_prints_the_archived_project(capsys):
    action = _action()

    with patch(ARCHIVE_BY_ID, AsyncMock(return_value=_archived())):
        await action.run_action(action.parser.parse_args([PROJECT_ID, "--json"]))

    project = json.loads(capsys.readouterr().out)
    assert (project["project_id"], project["name"], project["archived_at"]) == (
        PROJECT_ID,
        "Old fifo",
        "2026-11-15T07:00:00+00:00",
    )


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (404, f"Project {PROJECT_ID} not found, or not yours to archive."),
        (
            409,
            f"A run of project {PROJECT_ID} is still going, so it can't be archived until the run ends.\n"
            f"Wait for it to finish, or stop it with:\n  aristotle cancel {PROJECT_ID}",
        ),
    ],
    ids=["not yours", "running"],
)
async def test_a_refused_archive_says_why_and_exits_4(status: int, message: str, caplog, capsys):
    action = _action()
    refused = AristotleAPIError(f"API request failed with status {status}", status_code=status)

    with patch(ARCHIVE_BY_ID, AsyncMock(side_effect=refused)):
        assert await action.run_action(action.parser.parse_args([PROJECT_ID, "--json"])) == 4

    assert caplog.messages == [message]
    assert capsys.readouterr().out == ""


async def test_archive_leaves_other_api_errors_to_the_generic_handler():
    action = _action()
    server_error = AristotleAPIError("API request failed with status 500", status_code=500)

    with patch(ARCHIVE_BY_ID, AsyncMock(side_effect=server_error)), pytest.raises(AristotleAPIError):
        await action.run_action(action.parser.parse_args([PROJECT_ID]))


def test_archive_posts_to_the_project_and_prints_what_aristotle_answers(api_server, capsys):
    answer = _archived().model_dump(mode="json")
    sent = api_server(httpx.Response(200, json=answer))

    with patch("sys.argv", ["aristotle", "archive", PROJECT_ID.upper(), "--json"]):
        assert main() == 0

    assert [(request.method, request.url.path) for request in sent] == [
        ("POST", f"/api/v{api_request.API_VERSION}/project/{PROJECT_ID}/archive")
    ]
    assert json.loads(capsys.readouterr().out)["archived_at"] == "2026-11-15T07:00:00+00:00"


def test_archive_of_a_running_project_exits_4_with_the_command_that_stops_its_run(api_server, caplog):
    api_server(httpx.Response(409, json={"detail": "A running or paused project can't be archived"}))

    with patch("sys.argv", ["aristotle", "archive", PROJECT_ID]):
        assert main() == 4

    assert "so it can't be archived until the run ends.\nWait for it to finish, or stop it with:\n" in caplog.text
    assert f"  aristotle cancel {PROJECT_ID}" in caplog.text


@pytest.mark.parametrize(("status", "code"), [(404, 4), (409, 3)])
def test_a_proxys_refusal_page_is_reported_as_a_refusal_not_as_the_projects_state(status, code, api_server, caplog):
    api_server(httpx.Response(status, text="<title>Denied by proxy</title>", headers={"content-type": "text/html"}))

    with patch("sys.argv", ["aristotle", "archive", PROJECT_ID]):
        assert main() == code

    assert "proxy" in caplog.text
    assert "not yours to archive" not in caplog.text
    assert "still going" not in caplog.text
