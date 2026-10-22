"""Tests for aristotlelib.cli.list module."""

import argparse
import json
import logging
from datetime import datetime
from unittest.mock import patch

import pytest

from aristotlelib.agent_task import TaskStatus
from aristotlelib.cli.command.list import ListAction
from aristotlelib.project import Project, ProjectStatus


async def test_list_action_default():
    """Test listing projects with default parameters."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ListAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args([])

    mock_projects = [
        Project(
            project_id="proj-1",
            status=ProjectStatus.IDLE,
            created_at=datetime.now(),
            last_updated=datetime.now(),
            description="First project",
            has_input=True,
            has_files=True,
        ),
        Project(
            project_id="proj-2",
            status=ProjectStatus.RUNNING,
            created_at=datetime.now(),
            last_updated=datetime.now(),
            description="Second project",
            has_input=True,
            has_files=False,
        ),
    ]

    with patch(
        "aristotlelib.cli.command.list.Project.list_projects",
        return_value=(mock_projects, None),
    ) as mock_list:
        await action.run_action(args)

    # Verify list_projects was called with default limit
    mock_list.assert_called_once()
    call_args = mock_list.call_args
    assert call_args[1]["limit"] == 10
    assert call_args[1]["pagination_key"] is None


async def test_list_action_with_custom_limit():
    """Test listing projects with custom limit."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ListAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["--limit", "25"])

    with patch("aristotlelib.cli.command.list.Project.list_projects", return_value=([], None)) as mock_list:
        await action.run_action(args)

    call_args = mock_list.call_args
    assert call_args[1]["limit"] == 25


async def test_list_action_with_pagination_key():
    """Test listing projects with pagination key."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ListAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["--pagination-key", "next-page-token"])

    with patch("aristotlelib.cli.command.list.Project.list_projects", return_value=([], None)) as mock_list:
        await action.run_action(args)

    call_args = mock_list.call_args
    assert call_args[1]["pagination_key"] == "next-page-token"


async def test_list_action_with_single_status_filter():
    """Test listing projects with single status filter."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ListAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["--status", "IDLE"])

    mock_projects = [
        Project(
            project_id="proj-idle",
            status=ProjectStatus.IDLE,
            created_at=datetime.now(),
            last_updated=datetime.now(),
            description=None,
            has_input=True,
            has_files=True,
        ),
    ]

    with patch(
        "aristotlelib.cli.command.list.Project.list_projects",
        return_value=(mock_projects, None),
    ) as mock_list:
        await action.run_action(args)

    call_args = mock_list.call_args
    # Should be a list with one status
    assert call_args[1]["status"] == [ProjectStatus.IDLE]


@pytest.mark.parametrize(
    "argv",
    [["--status", "IDLE", "RUNNING"], ["--status", "IDLE", "--status", "RUNNING"]],
    ids=["one flag", "repeated"],
)
async def test_list_action_with_multiple_status_filters(argv):
    """Test listing projects with multiple status filters."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ListAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(argv)

    with patch("aristotlelib.cli.command.list.Project.list_projects", return_value=([], None)) as mock_list:
        await action.run_action(args)

    call_args = mock_list.call_args
    assert call_args[1]["status"] == [
        ProjectStatus.IDLE,
        ProjectStatus.RUNNING,
    ]


async def test_list_action_with_empty_results():
    """Test listing projects when no projects are found."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ListAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args([])

    with patch("aristotlelib.cli.command.list.Project.list_projects", return_value=([], None)):
        # Should not raise error
        await action.run_action(args)


async def test_list_action_with_pagination_key_in_response():
    """Test listing projects when pagination key is returned."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ListAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args([])

    mock_projects = [
        Project(
            project_id="proj-1",
            status=ProjectStatus.IDLE,
            created_at=datetime.now(),
            last_updated=datetime.now(),
            description=None,
            has_input=True,
            has_files=True,
        ),
    ]

    pagination_key = "next-page-key-12345"

    with patch(
        "aristotlelib.cli.command.list.Project.list_projects",
        return_value=(mock_projects, pagination_key),
    ):
        # Should print the pagination key for user to use
        await action.run_action(args)


@pytest.mark.parametrize("limit", ["0", "101"])
async def test_an_out_of_range_limit_is_refused_before_any_request(limit: str, capsys):
    parser = argparse.ArgumentParser()
    action = ListAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.list.Project.list_projects") as request,
        pytest.raises(SystemExit) as exited,
    ):
        action.preflight(action.parser.parse_args([] + ["--limit", limit]))

    assert exited.value.code == 2
    assert "--limit must be between 1 and 100" in capsys.readouterr().err
    request.assert_not_called()


def _project(project_id: str, status: ProjectStatus) -> Project:
    return Project(
        project_id=project_id,
        status=status,
        created_at=datetime(2026, 9, 1, 12, 0),
        last_updated=datetime(2026, 9, 1, 13, 0),
        description="FIFO overflow",
        has_input=True,
        has_files=False,
    )


async def test_json_prints_the_records_and_keeps_the_pagination_key_on_stderr(capsys, caplog):
    parser = argparse.ArgumentParser()
    action = ListAction(parser.add_subparsers())
    action.add_action_arguments()
    projects = [_project("proj-1", ProjectStatus.RUNNING), _project("proj-2", ProjectStatus.IDLE)]

    with (
        patch("aristotlelib.cli.command.list.Project.list_projects", return_value=(projects, "next-page")),
        caplog.at_level(logging.INFO),
    ):
        await action.run_action(action.parser.parse_args(["--json"]))

    page = json.loads(capsys.readouterr().out)
    assert list(page) == ["projects", "pagination_key"]
    assert page["pagination_key"] == "next-page"
    records = page["projects"]
    assert [r["project_id"] for r in records] == ["proj-1", "proj-2"]
    assert records[0] == {
        "project_id": "proj-1",
        "created_at": "2026-09-01T12:00:00+00:00",
        "last_updated": "2026-09-01T13:00:00+00:00",
        "name": "FIFO overflow",
        "status": 1,
        "latest_run_status": None,
        "has_input": True,
        "has_files": False,
        "status_name": "RUNNING",
    }
    # For a person reading stderr, the key is printed there too.
    assert caplog.messages == ["\nPagination key for next page: next-page"]


async def test_json_with_no_projects_is_an_empty_page(capsys, caplog):
    parser = argparse.ArgumentParser()
    action = ListAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.list.Project.list_projects", return_value=([], None)),
        caplog.at_level(logging.INFO),
    ):
        await action.run_action(action.parser.parse_args(["--json"]))

    assert json.loads(capsys.readouterr().out) == {"projects": [], "pagination_key": None}
    assert caplog.messages == []


async def test_the_table_says_how_each_projects_latest_run_stands_or_ended(capsys):
    parser = argparse.ArgumentParser()
    action = ListAction(parser.add_subparsers())
    action.add_action_arguments()
    projects = [
        _project("proj-1", ProjectStatus.IDLE).model_copy(update={"latest_run_status": TaskStatus.FAILED}),
        _project("proj-2", ProjectStatus.RUNNING).model_copy(update={"latest_run_status": TaskStatus.IN_PROGRESS}),
        _project("proj-3", ProjectStatus.IDLE),
    ]

    with patch("aristotlelib.cli.command.list.Project.list_projects", return_value=(projects, None)):
        await action.run_action(action.parser.parse_args([]))

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["ID", "CREATED", "NAME", "STATUS", "LAST", "RUN"]
    # An idle project doesn't say whether its run passed; LAST RUN does.
    assert [line.split()[-2:] for line in lines[2:]] == [["IDLE", "FAILED"], ["RUNNING", "IN_PROGRESS"], ["IDLE", "-"]]
    assert max(map(len, lines)) <= 120


@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        ({"latest_run_status": "COMPLETE_WITH_ERRORS"}, "COMPLETE_WITH_ERRORS"),
        ({"latest_run_status": None}, None),
        ({}, None),
    ],
    ids=["named", "no run yet", "older server"],
)
def test_a_project_carries_its_latest_runs_status_as_the_api_names_it(sent, expected):
    record = _project("proj-1", ProjectStatus.IDLE).model_dump(mode="json")
    record.pop("latest_run_status")

    project = Project.model_validate({**record, **sent})

    assert project.model_dump(mode="json")["latest_run_status"] == expected


def test_a_run_status_this_version_does_not_know_reads_as_unknown_without_a_warning(caplog):
    """Such as the NOT_STARTED of a run whose upload never finished, which every listing would otherwise warn about."""
    record = _project("proj-1", ProjectStatus.RUNNING).model_dump(mode="json")

    with caplog.at_level(logging.WARNING):
        project = Project.model_validate({**record, "latest_run_status": "NOT_STARTED"})

    assert project.latest_run_status == TaskStatus.UNKNOWN
    assert caplog.records == []
