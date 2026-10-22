"""Tests for aristotlelib.cli.rename module."""

import argparse
import json
import logging
from datetime import datetime
from unittest.mock import patch

import pytest

from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.command.rename import RenameAction
from aristotlelib.cli.controller import main
from aristotlelib.project import Project, ProjectStatus

PROJECT_ID = "3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f"
RENAME_BY_ID = "aristotlelib.cli.command.rename.Project.rename_by_id"


def _action() -> RenameAction:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = RenameAction(subparsers)
    action.add_action_arguments()
    return action


def _project(description: str) -> Project:
    return Project(
        project_id=PROJECT_ID,
        status=ProjectStatus.IDLE,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=description,
        has_input=False,
        has_files=False,
    )


async def test_rename_reports_the_new_name(caplog):
    action = _action()
    args = action.parser.parse_args([PROJECT_ID, "  solana proj  "])

    with patch(RENAME_BY_ID, return_value=_project("solana proj")) as mock_rename:
        with caplog.at_level(logging.INFO):
            await action.run_action(args)

    mock_rename.assert_called_once_with(PROJECT_ID, "solana proj")
    assert f'Project {PROJECT_ID} renamed to "solana proj".' in caplog.messages


async def test_rename_sends_a_name_that_is_not_utf8_escaped():
    action = _action()
    args = action.parser.parse_args([PROJECT_ID, "r\udce9sum\udce9"])

    with patch(RENAME_BY_ID, return_value=_project("r\\xe9sum\\xe9")) as mock_rename:
        await action.run_action(args)

    mock_rename.assert_called_once_with(PROJECT_ID, "r\\xe9sum\\xe9")


@pytest.mark.usefixtures("project_lookup")
async def test_rename_sends_the_id_in_canonical_form():
    action = _action()
    args = action.parser.parse_args([PROJECT_ID.upper().replace("-", ""), "solana proj"])

    with patch(RENAME_BY_ID, return_value=_project("solana proj")) as mock_rename:
        await action.run_action(args)

    mock_rename.assert_called_once_with(PROJECT_ID, "solana proj")


async def test_rename_of_a_project_you_do_not_own_exits_4(caplog):
    action = _action()
    args = action.parser.parse_args([PROJECT_ID, "solana proj"])

    not_found = AristotleAPIError("API request failed with status 404: Project not found", status_code=404)
    with patch(RENAME_BY_ID, side_effect=not_found):
        with pytest.raises(SystemExit) as exited:
            await action.run_action(args)

    assert exited.value.code == 4
    assert f"Project {PROJECT_ID} not found, or not yours to rename." in caplog.messages


async def test_rename_leaves_other_api_errors_to_the_generic_handler():
    action = _action()
    args = action.parser.parse_args([PROJECT_ID, "solana proj"])

    server_error = AristotleAPIError("API request failed with status 500", status_code=500)
    with patch(RENAME_BY_ID, side_effect=server_error):
        with pytest.raises(AristotleAPIError):
            await action.run_action(args)


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        ([PROJECT_ID, ""], "argument name: Project name cannot be empty"),
        ([PROJECT_ID, " \t"], "argument name: Project name cannot be empty"),
    ],
)
def test_rename_rejects_bad_arguments_before_any_request(argv: list[str], error: str, capsys):
    with (
        patch("sys.argv", ["aristotle", "rename", *argv]),
        patch(RENAME_BY_ID) as mock_rename,
        pytest.raises(SystemExit) as exited,
    ):
        main()

    assert exited.value.code == 2
    assert error in capsys.readouterr().err
    mock_rename.assert_not_called()


async def test_rename_json_prints_the_renamed_project(capsys):
    action = _action()

    with patch(RENAME_BY_ID, return_value=_project("FIFO overflow")):
        await action.run_action(action.parser.parse_args([PROJECT_ID, "FIFO overflow", "--json"]))

    project = json.loads(capsys.readouterr().out)
    assert project["project_id"] == PROJECT_ID
    # The project's name is `name` in --json, `description` only as the API and the Python attribute call it.
    assert (project["name"], "description" in project) == ("FIFO overflow", False)


async def test_rename_renames_the_project_a_name_resolves_to(resolve_project, caplog):
    action = _action()
    resolve_project.side_effect = lambda reference: (PROJECT_ID, None)

    with patch(RENAME_BY_ID, return_value=_project("solana proj, take 2")) as mock_rename:
        with caplog.at_level(logging.INFO):
            await action.run_action(action.parser.parse_args(["solana proj", "solana proj, take 2"]))

    resolve_project.assert_awaited_once_with("solana proj")
    mock_rename.assert_called_once_with(PROJECT_ID, "solana proj, take 2")
    assert f'Project {PROJECT_ID} renamed to "solana proj, take 2".' in caplog.messages
