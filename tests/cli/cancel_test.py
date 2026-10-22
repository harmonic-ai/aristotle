"""Tests for aristotlelib.cli.cancel module."""

import argparse
import json
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from aristotlelib.agent_task import AgentTask, TaskStatus
from aristotlelib.api_request import AristotleAPIError, AristotleNotFoundError
from aristotlelib.cli.command.cancel import CancelAction
from aristotlelib.cli.controller import main
from aristotlelib.project import Project

TASK_ID = "4d474c50-51a5-4ba9-a31e-c57949e9769f"


async def test_cancel_action_with_project_id():
    """Test canceling a task via project ID."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = CancelAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["project-123"])

    mock_project = Mock(spec=Project)
    mock_project.project_id = "project-123"

    mock_task = Mock(spec=AgentTask)
    mock_task.agent_task_id = "task-456"
    mock_task.cancel = AsyncMock()

    mock_project.get_tasks = AsyncMock(return_value=([mock_task], None))

    with patch("aristotlelib.cli.command.cancel.Project.from_id", return_value=mock_project) as mock_from_id:
        await action.run_action(args)

    # Verify Project.from_id was called with correct ID
    mock_from_id.assert_called_once_with("project-123")

    # Verify get_tasks was called and task was canceled
    mock_project.get_tasks.assert_called_once_with(limit=1)
    mock_task.cancel.assert_called_once()


async def test_cancel_action_with_task_id_flag():
    """Test canceling a task directly via --task-id."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = CancelAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["--task-id", "task-789"])

    mock_task = Mock(spec=AgentTask)
    mock_task.agent_task_id = "task-789"
    mock_task.cancel = AsyncMock()

    with patch("aristotlelib.cli.command.cancel.AgentTask.from_id", return_value=mock_task) as mock_from_id:
        await action.run_action(args)

    mock_from_id.assert_called_once_with("task-789")
    mock_task.cancel.assert_called_once()


async def test_cancel_action_with_project_id_flag():
    """Test canceling via --project-id flag."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = CancelAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["--project-id", "proj-abc"])

    mock_project = Mock(spec=Project)
    mock_project.project_id = "proj-abc"

    mock_task = Mock(spec=AgentTask)
    mock_task.agent_task_id = "task-xyz"
    mock_task.cancel = AsyncMock()

    mock_project.get_tasks = AsyncMock(return_value=([mock_task], None))

    with patch("aristotlelib.cli.command.cancel.Project.from_id", return_value=mock_project) as mock_from_id:
        await action.run_action(args)

    mock_from_id.assert_called_once_with("proj-abc")
    mock_project.get_tasks.assert_called_once_with(limit=1)
    mock_task.cancel.assert_called_once()


async def test_cancel_action_fallback_to_task():
    """Test that cancel falls back to task if project lookup fails."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = CancelAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args([TASK_ID])

    mock_task = Mock(spec=AgentTask)
    mock_task.agent_task_id = TASK_ID
    mock_task.cancel = AsyncMock()

    with (
        patch(
            "aristotlelib.cli.command.cancel.Project.from_id",
            side_effect=AristotleNotFoundError("project", TASK_ID, 403),
        ),
        patch("aristotlelib.cli.command.cancel.AgentTask.from_id", return_value=mock_task) as mock_task_from_id,
    ):
        await action.run_action(args)

    mock_task_from_id.assert_called_once_with(TASK_ID)
    mock_task.cancel.assert_called_once()


async def test_cancel_resolves_a_project_reference_and_never_tries_it_as_a_task(resolve_project):
    parser = argparse.ArgumentParser()
    action = CancelAction(parser.add_subparsers())
    action.add_action_arguments()
    resolve_project.side_effect = AristotleNotFoundError("project", "fifo", message="No project is named 'fifo'.")

    with (
        patch("aristotlelib.cli.command.cancel.AgentTask.from_id") as task_from_id,
        pytest.raises(AristotleNotFoundError, match="No project is named 'fifo'."),
    ):
        await action.run_action(action.parser.parse_args(["fifo"]))

    resolve_project.assert_awaited_once_with("fifo")
    task_from_id.assert_not_called()


async def test_cancel_action_does_not_fall_back_to_task_on_other_errors():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = CancelAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["x-1"])

    with (
        patch(
            "aristotlelib.cli.command.cancel.Project.from_id",
            side_effect=AristotleAPIError("boom", status_code=500),
        ),
        patch("aristotlelib.cli.command.cancel.AgentTask.from_id") as mock_task_from_id,
    ):
        with pytest.raises(AristotleAPIError) as info:
            await action.run_action(args)

    assert info.value.status_code == 500
    mock_task_from_id.assert_not_called()


async def test_a_project_without_a_task_exits_4(caplog):
    parser = argparse.ArgumentParser()
    action = CancelAction(parser.add_subparsers())
    action.add_action_arguments()

    mock_project = Mock(spec=Project)
    mock_project.project_id = "project-123"
    mock_project.get_tasks = AsyncMock(return_value=([], None))

    with patch("aristotlelib.cli.command.cancel.Project.from_id", return_value=mock_project):
        assert await action.run_action(action.parser.parse_args(["--project-id", "project-123"])) == 4

    assert caplog.messages == ["No task found"]


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        ([], "the project or task to cancel is required"),
        (["project-123", "--task-id", "task-456"], "give one project or task to cancel"),
        (["--task-id", "task-456", "--project-id", "project-123"], "give one project or task to cancel"),
    ],
    ids=["none", "two", "both flags"],
)
async def test_anything_but_one_project_or_task_is_a_usage_error_without_a_request(
    argv, error, capsys, resolve_project
):
    parser = argparse.ArgumentParser()
    action = CancelAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.cancel.Project.from_id") as project_from_id,
        patch("aristotlelib.cli.command.cancel.AgentTask.from_id") as task_from_id,
        pytest.raises(SystemExit) as exited,
    ):
        action.preflight(action.parser.parse_args(argv))

    assert exited.value.code == 2
    assert capsys.readouterr().err.endswith(f"error: {error}\n")
    project_from_id.assert_not_called()
    task_from_id.assert_not_called()
    resolve_project.assert_not_awaited()


def test_cancel_lists_only_the_positional_way_to_name_what_to_cancel():
    parser = argparse.ArgumentParser()
    action = CancelAction(parser.add_subparsers())
    action.add_arguments()
    help_text = action.parser.format_help()

    assert "project_or_task_id" in help_text
    assert not any(option in help_text for option in ("--task-id", "--project-id", "--api-key"))


async def test_cancel_json_prints_the_cancelled_task(capsys):
    parser = argparse.ArgumentParser()
    action = CancelAction(parser.add_subparsers())
    action.add_action_arguments()
    task = AgentTask(
        project_id="project-123",
        agent_task_id="task-456",
        status=TaskStatus.IN_PROGRESS,
        created_at=datetime(2026, 9, 1, 12, 0),
        last_updated_at=datetime(2026, 9, 1, 12, 0),
        percent_complete=10,
        file_name=None,
        description=None,
        output_summary=None,
    )

    async def cancel() -> None:
        task.status = TaskStatus.CANCELED

    with (
        patch("aristotlelib.cli.command.cancel.AgentTask.from_id", return_value=task),
        patch.object(AgentTask, "cancel", AsyncMock(side_effect=cancel)),
    ):
        assert await action.run_action(action.parser.parse_args(["--task-id", "task-456", "--json"])) is None

    assert json.loads(capsys.readouterr().out)["status"] == "CANCELED"


def _task(status: TaskStatus) -> httpx.Response:
    task = AgentTask(
        project_id="3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f",
        agent_task_id=TASK_ID,
        status=status,
        created_at=datetime(2026, 9, 1, 12, 0),
        last_updated_at=datetime(2026, 9, 1, 12, 0),
        percent_complete=10,
        file_name=None,
        description=None,
        output_summary=None,
    )
    return httpx.Response(200, json=task.model_dump(mode="json"))


CANNOT_CANCEL = httpx.Response(404, json={"detail": "Agent task cannot be canceled"})


def test_a_cancel_whose_response_is_lost_is_sent_again_and_its_404_counts_as_done(api_server, capsys):
    sent = api_server(
        _task(TaskStatus.IN_PROGRESS),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        CANNOT_CANCEL,
        _task(TaskStatus.CANCELED),
    )

    with patch("sys.argv", ["aristotle", "cancel", "--task-id", TASK_ID, "--json"]):
        assert main() == 0

    assert [request.method for request in sent] == ["GET", "POST", "POST", "GET"]
    assert json.loads(capsys.readouterr().out)["status"] == "CANCELED"


def test_cancelling_a_cancelled_task_again_exits_0(api_server, capsys):
    api_server(_task(TaskStatus.CANCELED), CANNOT_CANCEL, _task(TaskStatus.CANCELED))

    with patch("sys.argv", ["aristotle", "cancel", "--task-id", TASK_ID, "--json"]):
        assert main() == 0

    assert json.loads(capsys.readouterr().out)["status"] == "CANCELED"


def test_cancelling_a_finished_task_still_exits_4(api_server, caplog, capsys):
    api_server(_task(TaskStatus.COMPLETE), CANNOT_CANCEL, _task(TaskStatus.COMPLETE))

    with patch("sys.argv", ["aristotle", "cancel", "--task-id", TASK_ID, "--json"]):
        assert main() == 4

    assert caplog.messages[-1] == f"Task {TASK_ID} has already finished (COMPLETE), so it can't be cancelled."
    assert capsys.readouterr().out == ""
