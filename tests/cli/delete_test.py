"""Tests for aristotlelib.cli.command.delete."""

import argparse
import io
import json
import logging
from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.command.delete import DeleteAction
from aristotlelib.cli.controller import main
from aristotlelib.project import Project, ProjectStatus

PROJECT_ID = "3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f"
DELETE_BY_ID = "aristotlelib.cli.command.delete.Project.delete_by_id"
FROM_ID = "aristotlelib.cli.api_action.Project.from_id"


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def _action() -> DeleteAction:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = DeleteAction(subparsers)
    action.add_action_arguments()
    return action


def _project(description: str = "Old fifo") -> Project:
    return Project(
        project_id=PROJECT_ID,
        status=ProjectStatus.IDLE,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=description,
        has_input=False,
        has_files=False,
    )


@pytest.mark.parametrize(("answer", "deleted"), [("y\n", True), ("YES\n", True), ("\n", False), ("n\n", False)])
async def test_delete_asks_at_a_terminal_and_deletes_only_on_yes(answer: str, deleted: bool, caplog):
    action = _action()
    stderr = _Terminal()
    with (
        patch("sys.stdin", _Terminal(answer)),
        patch("sys.stderr", stderr),
        patch(FROM_ID, AsyncMock(return_value=_project())),
        patch(DELETE_BY_ID, AsyncMock()) as delete,
        caplog.at_level(logging.INFO),
    ):
        assert await action.run_action(action.parser.parse_args([PROJECT_ID])) is None

    assert stderr.getvalue() == (
        f'Delete project "Old fifo" ({PROJECT_ID})? Its runs and results go with it, and its shared link stops '
        "working. This can't be undone. [y/N] "
    )
    if deleted:
        delete.assert_awaited_once_with(PROJECT_ID)
        assert f"Project {PROJECT_ID} deleted." in caplog.messages
    else:
        delete.assert_not_awaited()
        assert "Nothing was deleted." in caplog.messages


async def test_delete_yes_deletes_without_asking_and_prints_json(capsys):
    action = _action()
    with (
        patch("sys.stdin", _Terminal("")),
        patch(FROM_ID, AsyncMock(return_value=_project())),
        patch(DELETE_BY_ID, AsyncMock()) as delete,
    ):
        await action.run_action(action.parser.parse_args([PROJECT_ID, "--yes", "--json"]))

    delete.assert_awaited_once_with(PROJECT_ID)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"project_id": PROJECT_ID, "deleted": True}
    assert "[y/N]" not in captured.err


@pytest.mark.parametrize("argv", [[PROJECT_ID], [PROJECT_ID, "--json"]], ids=["no terminal", "json"])
def test_delete_without_yes_refuses_unless_someone_can_answer(argv: list[str], capsys):
    with (
        patch("sys.argv", ["aristotle", "delete", *argv]),
        patch(DELETE_BY_ID) as delete,
        pytest.raises(SystemExit) as exited,
    ):
        main()

    assert exited.value.code == 2
    assert "deleting can't be undone: pass --yes to delete without being asked" in capsys.readouterr().err
    delete.assert_not_called()


@pytest.mark.parametrize(
    ("status", "code", "message"),
    [
        (404, 4, f"Project {PROJECT_ID} not found, or not yours to delete."),
        (409, 4, f"A run of project {PROJECT_ID} is still going, so it can't be deleted until the run ends."),
    ],
    ids=["not yours", "running"],
)
async def test_a_refused_delete_says_why(status: int, code: int, message: str, caplog):
    action = _action()
    refused = AristotleAPIError(f"API request failed with status {status}", status_code=status)
    with patch(FROM_ID, AsyncMock(return_value=_project())), patch(DELETE_BY_ID, AsyncMock(side_effect=refused)):
        assert await action.run_action(action.parser.parse_args([PROJECT_ID, "--yes"])) == code

    assert caplog.messages[0].startswith(message)
    if status == 409:
        assert f"aristotle cancel {PROJECT_ID}" in caplog.messages[0]


async def test_delete_leaves_other_api_errors_to_the_generic_handler():
    action = _action()
    server_error = AristotleAPIError("API request failed with status 500", status_code=500)
    with (
        patch(FROM_ID, AsyncMock(return_value=_project())),
        patch(DELETE_BY_ID, AsyncMock(side_effect=server_error)),
        pytest.raises(AristotleAPIError),
    ):
        await action.run_action(action.parser.parse_args([PROJECT_ID, "--yes"]))


async def test_the_prompt_escapes_a_name_that_could_redraw_it():
    action = _action()
    stderr = _Terminal()
    with (
        patch("sys.stdin", _Terminal("n\n")),
        patch("sys.stderr", stderr),
        patch(FROM_ID, AsyncMock(return_value=_project('Other\r\x1b[2KDelete project "Scratch"'))),
        patch(DELETE_BY_ID, AsyncMock()),
    ):
        await action.run_action(action.parser.parse_args([PROJECT_ID]))

    assert stderr.getvalue().startswith(f'Delete project "Other\\r\\x1b[2KDelete project "Scratch"" ({PROJECT_ID})? ')


def _project_response() -> httpx.Response:
    return httpx.Response(200, json=_project().model_dump(mode="json"))


GONE = httpx.Response(410, json={"detail": "This project was deleted"})
NOT_YOURS = httpx.Response(404, json={"detail": "Project not found"})


def test_a_delete_whose_reply_is_lost_counts_its_retry_404_as_deleted_when_the_project_reads_410(api_server, capsys):
    sent = api_server(
        _project_response(),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        NOT_YOURS,
        GONE,
    )

    with patch("sys.argv", ["aristotle", "delete", PROJECT_ID, "--yes", "--json"]):
        assert main() == 0

    assert [request.method for request in sent] == ["GET", "DELETE", "DELETE", "GET"]
    assert json.loads(capsys.readouterr().out) == {"project_id": PROJECT_ID, "deleted": True}


def test_a_404_on_a_project_still_there_is_still_not_yours(api_server, caplog):
    api_server(_project_response(), NOT_YOURS, _project_response())

    with patch("sys.argv", ["aristotle", "delete", PROJECT_ID, "--yes"]):
        assert main() == 4

    assert f"Project {PROJECT_ID} not found, or not yours to delete." in caplog.messages
