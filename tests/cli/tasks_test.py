"""Tests for aristotlelib.cli.tasks module."""

import argparse
import json
import logging
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

from aristotlelib.agent_task import AgentTask, TaskStatus
from aristotlelib.cli.command.tasks import TasksAction
from aristotlelib.project import Project


async def test_tasks_action_default():
    """Test listing tasks with default parameters."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = TasksAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["project-123"])

    mock_project = Mock(spec=Project)
    mock_project.project_id = "project-123"

    mock_tasks = [
        AgentTask(
            project_id="project-123",
            agent_task_id="task-1",
            status=TaskStatus.COMPLETE,
            created_at=datetime.now(),
            last_updated_at=datetime.now(),
            percent_complete=100,
            file_name=None,
            description="First task",
            output_summary=None,
        ),
        AgentTask(
            project_id="project-123",
            agent_task_id="task-2",
            status=TaskStatus.IN_PROGRESS,
            created_at=datetime.now(),
            last_updated_at=datetime.now(),
            percent_complete=50,
            file_name=None,
            description="Second task",
            output_summary=None,
        ),
    ]

    mock_project.get_tasks = AsyncMock(return_value=(mock_tasks, None))

    with patch("aristotlelib.project.Project.from_id", return_value=mock_project) as mock_from_id:
        await action.run_action(args)

    mock_from_id.assert_called_once_with("project-123")
    mock_project.get_tasks.assert_called_once()
    call_args = mock_project.get_tasks.call_args
    assert call_args[1]["limit"] == 10
    assert call_args[1]["pagination_key"] is None


async def test_tasks_action_with_custom_limit():
    """Test listing tasks with custom limit."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = TasksAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["project-123", "--limit", "25"])

    mock_project = Mock(spec=Project)
    mock_project.project_id = "project-123"
    mock_project.get_tasks = AsyncMock(return_value=([], None))

    with patch("aristotlelib.project.Project.from_id", return_value=mock_project):
        await action.run_action(args)

    call_args = mock_project.get_tasks.call_args
    assert call_args[1]["limit"] == 25


async def test_tasks_action_with_pagination_key():
    """Test listing tasks with pagination key."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = TasksAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["project-123", "--pagination-key", "next-page-token"])

    mock_project = Mock(spec=Project)
    mock_project.project_id = "project-123"
    mock_project.get_tasks = AsyncMock(return_value=([], None))

    with patch("aristotlelib.project.Project.from_id", return_value=mock_project):
        await action.run_action(args)

    call_args = mock_project.get_tasks.call_args
    assert call_args[1]["pagination_key"] == "next-page-token"


async def test_tasks_action_with_empty_results():
    """Test listing tasks when no tasks are found."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = TasksAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["project-empty"])

    mock_project = Mock(spec=Project)
    mock_project.project_id = "project-empty"
    mock_project.get_tasks = AsyncMock(return_value=([], None))

    with patch("aristotlelib.project.Project.from_id", return_value=mock_project):
        # Should not raise error
        await action.run_action(args)


async def test_tasks_action_with_pagination_key_in_response():
    """Test listing tasks when pagination key is returned."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = TasksAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["project-123"])

    mock_project = Mock(spec=Project)
    mock_project.project_id = "project-123"

    mock_tasks = [
        AgentTask(
            project_id="project-123",
            agent_task_id="task-1",
            status=TaskStatus.COMPLETE,
            created_at=datetime.now(),
            last_updated_at=datetime.now(),
            percent_complete=100,
            file_name=None,
            description=None,
            output_summary=None,
        ),
    ]

    pagination_key = "next-page-key-12345"
    mock_project.get_tasks = AsyncMock(return_value=(mock_tasks, pagination_key))

    with patch("aristotlelib.project.Project.from_id", return_value=mock_project):
        # Should print the pagination key for user to use
        await action.run_action(args)


async def test_tasks_action_truncates_long_description():
    """Test that long task descriptions are truncated."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = TasksAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["project-123"])

    mock_project = Mock(spec=Project)
    mock_project.project_id = "project-123"

    mock_tasks = [
        AgentTask(
            project_id="project-123",
            agent_task_id="task-1",
            status=TaskStatus.COMPLETE,
            created_at=datetime.now(),
            last_updated_at=datetime.now(),
            percent_complete=100,
            file_name=None,
            description="This is a very long description that should be truncated when displayed",
            output_summary=None,
        ),
    ]

    mock_project.get_tasks = AsyncMock(return_value=(mock_tasks, None))

    with patch("aristotlelib.project.Project.from_id", return_value=mock_project):
        # Should not raise error and should truncate the description
        await action.run_action(args)


@pytest.mark.parametrize("limit", ["0", "101"])
async def test_an_out_of_range_limit_is_refused_before_any_request(limit: str, capsys, resolve_project):
    parser = argparse.ArgumentParser()
    action = TasksAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.project.Project.from_id") as request,
        pytest.raises(SystemExit) as exited,
    ):
        action.preflight(action.parser.parse_args(["project-123"] + ["--limit", limit]))

    assert exited.value.code == 2
    assert "--limit must be between 1 and 100" in capsys.readouterr().err
    request.assert_not_called()
    resolve_project.assert_not_awaited()


async def test_json_prints_the_records_and_keeps_the_pagination_key_on_stderr(capsys, caplog):
    parser = argparse.ArgumentParser()
    action = TasksAction(parser.add_subparsers())
    action.add_action_arguments()
    task = AgentTask(
        project_id="project-123",
        agent_task_id="task-1",
        status=TaskStatus.COMPLETE_WITH_ERRORS,
        created_at=datetime(2026, 9, 1, 12, 0),
        last_updated_at=datetime(2026, 9, 1, 13, 0),
        percent_complete=100,
        file_name=None,
        description="Prove it",
        output_summary="Two of three proved.",
    )
    mock_project = Mock(spec=Project)
    mock_project.project_id = "project-123"
    mock_project.get_tasks = AsyncMock(return_value=([task], "next-page"))

    with (
        patch("aristotlelib.project.Project.from_id", return_value=mock_project),
        caplog.at_level(logging.INFO),
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--json"]))

    page = json.loads(capsys.readouterr().out)
    assert list(page) == ["project_id", "tasks", "pagination_key"]
    assert page["pagination_key"] == "next-page"
    [record] = page["tasks"]
    assert record["agent_task_id"] == "task-1"
    assert record["status"] == "COMPLETE_WITH_ERRORS"
    assert record["output_summary"] == "Two of three proved."
    assert caplog.messages == ["\nPagination key for next page: next-page"]


async def test_json_with_no_tasks_is_an_empty_page(capsys):
    parser = argparse.ArgumentParser()
    action = TasksAction(parser.add_subparsers())
    action.add_action_arguments()
    mock_project = Mock(spec=Project)
    mock_project.project_id = "project-empty"
    mock_project.get_tasks = AsyncMock(return_value=([], None))

    with patch("aristotlelib.project.Project.from_id", return_value=mock_project):
        await action.run_action(action.parser.parse_args(["project-empty", "--json"]))

    assert json.loads(capsys.readouterr().out) == {"project_id": "project-empty", "tasks": [], "pagination_key": None}
