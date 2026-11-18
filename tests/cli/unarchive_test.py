"""Tests for aristotlelib.cli.command.unarchive."""

import argparse
import json
import logging
from datetime import datetime
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx
import pytest

from aristotlelib import api_request
from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.command.unarchive import UnarchiveAction
from aristotlelib.cli.controller import main
from aristotlelib.project import Project, ProjectStatus

PROJECT_ID = "3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f"
UNARCHIVE_BY_ID = "aristotlelib.cli.command.unarchive.Project.unarchive_by_id"


def _action() -> UnarchiveAction:
    parser = argparse.ArgumentParser()
    action = UnarchiveAction(parser.add_subparsers())
    action.add_action_arguments()
    return action


def _project(archived_at: datetime | None = None) -> Project:
    return Project(
        project_id=PROJECT_ID,
        status=ProjectStatus.IDLE,
        created_at=datetime(2026, 11, 1),
        last_updated=datetime(2026, 11, 15, 7),
        description="Old fifo",
        has_input=True,
        has_files=True,
        archived_at=archived_at,
    )


async def test_unarchive_says_it_unarchived_the_project(caplog, capsys):
    action = _action()

    with patch(UNARCHIVE_BY_ID, AsyncMock(return_value=_project())) as unarchive, caplog.at_level(logging.INFO):
        assert await action.run_action(action.parser.parse_args([PROJECT_ID])) is None

    unarchive.assert_awaited_once_with(PROJECT_ID)
    assert caplog.messages == [f"Project {PROJECT_ID} unarchived."]
    assert capsys.readouterr().out == ""


async def test_unarchive_looks_a_name_up_among_archived_projects(resolve_project):
    action = _action()
    resolve_project.side_effect = lambda reference, archived: (PROJECT_ID, None)

    with patch(UNARCHIVE_BY_ID, AsyncMock(return_value=_project())) as unarchive:
        await action.run_action(action.parser.parse_args(["Old fifo"]))

    resolve_project.assert_awaited_once_with("Old fifo", archived=True)
    unarchive.assert_awaited_once_with(PROJECT_ID)


async def test_unarchive_json_prints_the_unarchived_project(capsys):
    action = _action()

    with patch(UNARCHIVE_BY_ID, AsyncMock(return_value=_project())):
        await action.run_action(action.parser.parse_args([PROJECT_ID, "--json"]))

    project = json.loads(capsys.readouterr().out)
    assert (project["project_id"], project["archived_at"]) == (PROJECT_ID, None)


async def test_unarchive_of_a_project_you_cannot_unarchive_exits_4(caplog, capsys):
    action = _action()
    not_found = AristotleAPIError("API request failed with status 404: Project not found", status_code=404)

    with patch(UNARCHIVE_BY_ID, AsyncMock(side_effect=not_found)):
        assert await action.run_action(action.parser.parse_args([PROJECT_ID, "--json"])) == 4

    assert caplog.messages == [f"Project {PROJECT_ID} not found, or not yours to unarchive."]
    assert capsys.readouterr().out == ""


async def test_unarchive_leaves_other_api_errors_to_the_generic_handler():
    action = _action()
    server_error = AristotleAPIError("API request failed with status 500", status_code=500)

    with patch(UNARCHIVE_BY_ID, AsyncMock(side_effect=server_error)), pytest.raises(AristotleAPIError):
        await action.run_action(action.parser.parse_args([PROJECT_ID]))


def _archived_page(*projects: Project) -> httpx.Response:
    return httpx.Response(200, json={"projects": [p.model_dump(mode="json") for p in projects], "pagination_key": None})


@pytest.mark.usefixtures("project_lookup")
def test_unarchive_finds_an_archived_project_by_name_and_brings_it_back(api_server, caplog):
    archived = _project(archived_at=datetime(2026, 11, 15, 7))
    sent = api_server(_archived_page(archived), httpx.Response(200, json=_project().model_dump(mode="json")))

    with patch("sys.argv", ["aristotle", "unarchive", "Old fifo"]), caplog.at_level(logging.INFO):
        assert main() == 0

    lookup, unarchive = sent
    assert (lookup.method, lookup.url.path) == ("GET", f"/api/v{api_request.API_VERSION}/project")
    assert parse_qs(lookup.url.query.decode()) == {"limit": ["100"], "archived": ["true"]}
    assert (unarchive.method, unarchive.url.path) == (
        "POST",
        f"/api/v{api_request.API_VERSION}/project/{PROJECT_ID}/unarchive",
    )
    assert f"Project {PROJECT_ID} unarchived." in caplog.messages


@pytest.mark.usefixtures("project_lookup")
def test_unarchive_of_a_name_no_archived_project_has_exits_4_and_sends_nothing_else(api_server, caplog):
    sent = api_server(_archived_page())

    with patch("sys.argv", ["aristotle", "unarchive", "Old fifo"]):
        assert main() == 4

    assert len(sent) == 1
    assert "No archived project is named 'Old fifo'." in caplog.text


def test_a_proxys_refusal_page_is_reported_as_a_refusal_not_as_a_missing_project(api_server, caplog):
    api_server(httpx.Response(404, text="<title>Denied by proxy</title>", headers={"content-type": "text/html"}))

    with patch("sys.argv", ["aristotle", "unarchive", PROJECT_ID]):
        assert main() == 4

    assert "proxy" in caplog.text
    assert "not yours to unarchive" not in caplog.text
