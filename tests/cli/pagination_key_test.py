"""A --pagination-key Aristotle can't read, on list, tasks and show."""

import argparse
from unittest.mock import AsyncMock, Mock, patch

import pytest

from aristotlelib.agent_task import AgentTask, TaskStatus
from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.api_action import REJECTED_PAGINATION_KEY
from aristotlelib.cli.command.list import ListAction
from aristotlelib.cli.command.show import ShowAction
from aristotlelib.cli.command.tasks import TasksAction
from aristotlelib.project import Project


def _action(cls):
    action = cls(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()
    return action


async def _run(command: str, error: AristotleAPIError, key: str | None = "not-a-key") -> None:
    """Run ``command`` with its paged request failing with ``error``."""
    paging = ["--pagination-key", key] if key is not None else []
    if command == "list":
        action = _action(ListAction)
        with patch("aristotlelib.cli.command.list.Project.list_projects", AsyncMock(side_effect=error)):
            await action.run_action(action.parser.parse_args(paging))
        return
    project = Mock(spec=Project, description=None)
    project.project_id = "project-123"
    if command == "tasks":
        action = _action(TasksAction)
        project.get_tasks = AsyncMock(side_effect=error)
        with patch("aristotlelib.project.Project.from_id", return_value=project):
            await action.run_action(action.parser.parse_args(["project-123", *paging]))
        return
    action = _action(ShowAction)
    task = Mock(spec=AgentTask)
    task.agent_task_id = "task-456"
    task.project_id = "project-123"
    task.status = TaskStatus.COMPLETE
    task.show = AsyncMock(side_effect=error)
    project.get_tasks = AsyncMock(return_value=([task], None))
    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=project),
        patch("aristotlelib.cli.command.show.fetch_properties", AsyncMock(return_value=[])),
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--limit", "5", *paging]))


@pytest.mark.parametrize("command", ["list", "tasks", "show"])
async def test_a_rejected_key_is_a_usage_error(command: str, capsys):
    """Once the server answers 422: no retry, and the message names the option."""
    with pytest.raises(SystemExit) as exited:
        await _run(command, AristotleAPIError("API request failed with status 422", status_code=422))
    assert exited.value.code == 2
    assert REJECTED_PAGINATION_KEY in capsys.readouterr().err


@pytest.mark.parametrize("command", ["list", "tasks", "show"])
async def test_a_server_error_names_the_key_as_the_likely_cause(command: str):
    """Until the server says 422, it answers some keys it can't read with a 500, which reads like an outage."""
    with pytest.raises(AristotleAPIError) as raised:
        await _run(command, AristotleAPIError("GET /project after 5 attempts over 21 s: status 500", status_code=500))
    assert raised.value.status_code == 500
    assert str(raised.value).startswith("GET /project after 5 attempts over 21 s: status 500. A --pagination-key")
    assert "A --pagination-key Aristotle can't read gets this answer too" in str(raised.value)


@pytest.mark.parametrize("command", ["list", "tasks", "show"])
async def test_without_a_key_an_error_is_unchanged(command: str):
    error = AristotleAPIError("GET /project: status 500", status_code=500)
    with pytest.raises(AristotleAPIError) as raised:
        await _run(command, error, key=None)
    assert raised.value is error
