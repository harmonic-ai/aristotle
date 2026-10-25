"""Tests for aristotlelib.cli.show module."""

import argparse
import asyncio
import json
import logging
from contextlib import suppress
from datetime import datetime, timedelta
from unittest.mock import ANY, AsyncMock, Mock, patch

import httpx
import pydantic
import pytest

from aristotlelib import api_request
from aristotlelib.agent_task import AgentTask, TaskStatus
from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.api_action import resume_hint
from aristotlelib.cli.command.show import ShowAction, broken_proofs_note, format_property_counts, log_property_counts
from aristotlelib.cli.controller import main
from aristotlelib.declaration import (
    Declaration,
    DeclarationEvidence,
    DeclarationFailureReason,
    DeclarationSource,
    DeclarationStatus,
    DeclarationType,
)
from aristotlelib.event import Event, EventStatus, EventType
from aristotlelib.exit_codes import EXIT_INFRASTRUCTURE_ERROR
from aristotlelib.project import Project, ProjectStatus
from aristotlelib.question import Question
from aristotlelib.shell import shell_hint
from tests.cli.conftest import MockableProject, mockable_project, unknown_evidence_page
from tests.conftest import fast_api_clock


@pytest.fixture(autouse=True)
def at_a_terminal():
    """Run as if stdout were a terminal, where `show` follows a running task."""
    with patch("aristotlelib.cli.command.show.progress_utils.stdout_is_terminal", return_value=True) as mock:
        yield mock


@pytest.fixture(autouse=True)
def properties():
    """Patch the declarations fetch `show` makes; empty by default."""
    with patch("aristotlelib.cli.command.show.Declaration.list_all_for_project", return_value=[]) as mock:
        yield mock


READ_EVERY_PAGE = Declaration.list_all_for_project
"""The real fetch, for a test to put back in place of the ``properties`` fixture's."""


def _property(status: DeclarationStatus, *, evidence: DeclarationEvidence | None = None) -> Declaration:
    return Declaration(
        project_id="project-123",
        declaration_id=f"p-{status.name}",
        declaration_type=DeclarationType.PROPERTY,
        source=DeclarationSource.AGENT,
        name=f"Spec.{status.name}",
        status=status,
        evidence=evidence,
        filename="Spec/Property.lean",
        created_at=datetime.now(),
        last_updated=datetime.now(),
    )


def _unreadable_record() -> pydantic.ValidationError:
    """A real ValidationError, as raised for a record this client cannot parse."""
    with pytest.raises(pydantic.ValidationError) as info:
        Declaration.model_validate({"name": "Spec.Broken"})
    return info.value


def _completed_task(task_id: str = "task-456", project_id: str = "project-123") -> Mock:
    task = Mock(spec=AgentTask)
    task.agent_task_id = task_id
    task.project_id = project_id
    task.status = TaskStatus.COMPLETE
    task.read_only = False
    task.created_at = datetime(2000, 1, 1)  # Before any property here, so each is this task's verdict.
    task.show = AsyncMock(return_value=None)
    return task


async def test_show_action_with_completed_task():
    """Test showing a completed task (static display)."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ShowAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["project-123"])

    mock_project = Mock(spec=Project, description=None)
    mock_project.project_id = "project-123"

    mock_task = Mock(spec=AgentTask)
    mock_task.agent_task_id = "task-456"
    mock_task.project_id = "project-123"
    mock_task.status = TaskStatus.COMPLETE
    mock_task.show = AsyncMock(return_value=None)

    mock_project.get_tasks = AsyncMock(return_value=([mock_task], None))

    with patch("aristotlelib.cli.command.show.Project.from_id", return_value=mock_project) as mock_from_id:
        await action.run_action(args)

    mock_from_id.assert_called_once_with("project-123")
    mock_project.get_tasks.assert_called_once_with(limit=1)


async def test_show_action_with_specific_task_id():
    """Test showing a specific task by ID."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ShowAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["project-123", "--task", "task-789"])

    mock_task = Mock(spec=AgentTask)
    mock_task.agent_task_id = "task-789"
    mock_task.project_id = "project-123"
    mock_task.status = TaskStatus.COMPLETE
    mock_task.show = AsyncMock(return_value=None)

    with patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=mock_task) as mock_from_id:
        await action.run_action(args)

    mock_from_id.assert_called_once_with("task-789")


async def test_show_action_task_project_mismatch():
    """Test that show errors if task belongs to different project."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ShowAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["project-123", "--task", "task-789"])

    mock_task = Mock(spec=AgentTask)
    mock_task.agent_task_id = "task-789"
    mock_task.project_id = "different-project"  # Different project
    mock_task.status = TaskStatus.COMPLETE

    with patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=mock_task):
        assert await action.run_action(args) == 4

    # get_events should not be called since there's a mismatch
    mock_task.get_events.assert_not_called()


PROJECT_ID = "2fa30605-173d-4e2b-8cad-849b55641da3"


@pytest.mark.parametrize("typed", [PROJECT_ID.upper(), PROJECT_ID.replace("-", "")])
@pytest.mark.parametrize("json_flag", [[], ["--json"]])
async def test_show_task_accepts_the_project_id_in_another_case_or_without_dashes(typed, json_flag, capsys):
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()
    task = _completed_task(project_id=PROJECT_ID)

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=task),
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(task)),
    ):
        code = await action.run_action(action.parser.parse_args([typed, "--task", "task-456", *json_flag]))

    assert code is None
    if json_flag:
        assert json.loads(capsys.readouterr().out)["project_id"] == PROJECT_ID
    else:
        task.show.assert_awaited_once()


async def test_show_task_of_another_uuid_project_still_exits_4(caplog):
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()
    task = _completed_task(project_id="4d474c50-51a5-4ba9-a31e-c57949e9769f")

    with patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=task):
        assert await action.run_action(action.parser.parse_args([PROJECT_ID.upper(), "--task", "task-456"])) == 4

    task.show.assert_not_awaited()
    assert "belongs to project 4d474c50-51a5-4ba9-a31e-c57949e9769f" in caplog.text


@pytest.mark.parametrize(("task_project", "code"), [(PROJECT_ID, None), ("4d474c50-51a5-4ba9-a31e-c57949e9769f", 4)])
async def test_show_task_checks_the_task_against_the_project_a_name_resolves_to(
    task_project, code, resolve_project, caplog
):
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()
    resolve_project.side_effect = lambda reference: (PROJECT_ID, None)
    task = _completed_task(project_id=task_project)

    with patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=task):
        assert await action.run_action(action.parser.parse_args(["FIFO overflow", "--task", "task-456"])) == code

    resolve_project.assert_awaited_once_with("FIFO overflow")
    if code is None:
        task.show.assert_awaited_once()
    else:
        assert f"belongs to project {task_project}, but project {PROJECT_ID} provided" in caplog.text


async def test_show_action_no_tasks_found():
    """Test showing when no tasks exist for project."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ShowAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["project-empty"])

    mock_project = Mock(spec=Project, description=None)
    mock_project.project_id = "project-empty"
    mock_project.get_tasks = AsyncMock(return_value=([], None))

    with patch("aristotlelib.cli.command.show.Project.from_id", return_value=mock_project):
        # Should not raise error
        await action.run_action(args)


async def test_show_action_with_events_limit():
    """Test showing with custom event limit."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ShowAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["project-123", "--limit", "5"])

    mock_project = Mock(spec=Project, description=None)
    mock_project.project_id = "project-123"

    mock_task = Mock(spec=AgentTask)
    mock_task.agent_task_id = "task-456"
    mock_task.project_id = "project-123"
    mock_task.status = TaskStatus.COMPLETE
    mock_task.show = AsyncMock(return_value=None)

    mock_project.get_tasks = AsyncMock(return_value=([mock_task], None))

    with patch("aristotlelib.cli.command.show.Project.from_id", return_value=mock_project):
        await action.run_action(args)

    # Verify show was called with the correct limit
    mock_task.show.assert_called_once_with(num_events=5, pagination_key=None, title=None)


async def test_show_action_with_pagination_key():
    """Test showing with pagination key for events."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ShowAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["project-123", "--limit", "10", "--pagination-key", "next-key"])

    mock_project = Mock(spec=Project, description=None)
    mock_project.project_id = "project-123"

    mock_task = Mock(spec=AgentTask)
    mock_task.agent_task_id = "task-456"
    mock_task.project_id = "project-123"
    mock_task.status = TaskStatus.COMPLETE
    mock_task.show = AsyncMock(return_value=None)

    mock_project.get_tasks = AsyncMock(return_value=([mock_task], None))

    with patch("aristotlelib.cli.command.show.Project.from_id", return_value=mock_project):
        await action.run_action(args)

    mock_task.show.assert_called_once_with(num_events=10, pagination_key="next-key", title=None)


# --- property counts in the header ---


def test_format_property_counts_counts_each_refuted_property_once():
    declarations = (
        [_property(DeclarationStatus.PROVED)] * 4
        + [
            _property(DeclarationStatus.REFUTED, evidence=evidence)
            for evidence in DeclarationEvidence
            if evidence != DeclarationEvidence.UNKNOWN
        ]
        + [_property(DeclarationStatus.IN_PROGRESS), _property(DeclarationStatus.UNKNOWN)]
    )

    # Every evidence kind counts as one refuted property; UNKNOWN is not counted.
    assert format_property_counts(declarations) == "4 proved · 3 refuted · 1 in progress"
    two = [_property(DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE)] * 2
    assert format_property_counts(two) == "0 proved · 2 refuted · 0 in progress"


def _failed(reason: DeclarationFailureReason) -> Declaration:
    return _property(DeclarationStatus.FAILED).model_copy(update={"failure_reason": reason})


def test_the_broken_proofs_note_counts_the_failed_properties_whose_proof_no_longer_checks():
    broken, out_of_budget = (
        _failed(DeclarationFailureReason.PROOF_CHECK_FAILED),
        _failed(DeclarationFailureReason.OUT_OF_BUDGET),
    )

    assert broken_proofs_note([out_of_budget, _property(DeclarationStatus.PROVED)]) is None
    assert broken_proofs_note([broken, out_of_budget]) == (
        "The proof of 1 property no longer checks, so it isn't verified; re-run to re-prove it."
    )
    assert broken_proofs_note([broken, broken]) == (
        "The proofs of 2 properties no longer check, so they aren't verified; re-run to re-prove them."
    )


async def test_show_follows_the_counts_with_the_broken_proofs_note(properties, caplog):
    caplog.set_level(logging.INFO)
    properties.return_value = [
        _property(DeclarationStatus.PROVED),
        _failed(DeclarationFailureReason.PROOF_CHECK_FAILED),
    ]
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()
    mock_task = _completed_task(task_id="task-789")

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=mock_task),
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(mock_task)),
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--task", "task-789"]))

    assert caplog.messages[-2:] == [
        "Properties: 1 proved · 0 refuted · 0 in progress · 1 unresolved",
        "The proof of 1 property no longer checks, so it isn't verified; re-run to re-prove it.",
    ]


@pytest.mark.parametrize(
    ("task_status", "expected"),
    [
        (TaskStatus.COMPLETE, "1 proved · 1 refuted · 0 in progress"),
        (TaskStatus.CANCELED, "0 proved · 1 refuted · 0 in progress · 1 unchecked"),
        (TaskStatus.OUT_OF_BUDGET, "0 proved · 0 refuted · 0 in progress · 2 unchecked"),
    ],
)
def test_format_property_counts_counts_what_aristotle_never_checked_as_unchecked(task_status, expected):
    declarations = [
        _property(DeclarationStatus.PROVED),
        _property(DeclarationStatus.REFUTED, evidence=DeclarationEvidence.NEGATION_PROVED),
    ]
    task = _completed_task()
    task.status = task_status

    assert format_property_counts(declarations, [task]) == expected


def test_format_property_counts_mentions_failures_only_when_there_are_some():
    assert format_property_counts([]) == "0 proved · 0 refuted · 0 in progress"
    assert (
        format_property_counts([_property(DeclarationStatus.FAILED)])
        == "0 proved · 0 refuted · 0 in progress · 1 unresolved"
    )


async def test_show_action_heads_the_display_with_property_counts(properties, caplog):
    caplog.set_level(logging.INFO)
    properties.return_value = [_property(DeclarationStatus.PROVED)] * 4 + [
        _property(DeclarationStatus.REFUTED, evidence=DeclarationEvidence.NEGATION_PROVED)
    ]
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ShowAction(subparsers)
    action.add_action_arguments()
    args = action.parser.parse_args(["project-123"])

    mock_project = Mock(spec=Project, description=None)
    mock_project.project_id = "project-123"
    mock_task = _completed_task()
    mock_project.get_tasks = AsyncMock(return_value=([mock_task], None))

    with patch("aristotlelib.cli.command.show.Project.from_id", return_value=mock_project):
        await action.run_action(args)

    # The kind filter is server-side.
    properties.assert_called_once_with("project-123", declaration_type=DeclarationType.PROPERTY)
    assert "4 proved · 1 refuted · 0 in progress" in caplog.text
    mock_task.show.assert_called_once()


async def test_show_counts_the_proofs_a_canceled_task_left_unchecked(properties, caplog):
    caplog.set_level(logging.INFO)
    properties.return_value = [
        _property(DeclarationStatus.PROVED),
        _property(DeclarationStatus.REFUTED, evidence=DeclarationEvidence.NEGATION_PROVED),
    ]
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()
    task = _completed_task()
    task.status = TaskStatus.CANCELED

    with patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(task)):
        await action.run_action(action.parser.parse_args(["project-123"]))

    assert "Properties: 0 proved · 1 refuted · 0 in progress · 1 unchecked" in caplog.messages


@pytest.mark.parametrize(
    ("first", "expected"),
    [
        (TaskStatus.COMPLETE, "Properties: 1 proved · 1 refuted · 0 in progress"),
        (TaskStatus.CANCELED, "Properties: 0 proved · 1 refuted · 0 in progress · 1 unchecked"),
    ],
    ids=["set by a task that finished", "set by a cancelled task"],
)
async def test_show_judges_the_verdicts_before_a_cancelled_follow_up_by_the_task_that_set_them(
    properties, caplog, first: TaskStatus, expected: str
):
    caplog.set_level(logging.INFO)
    properties.return_value = [
        _property(DeclarationStatus.PROVED),
        _property(DeclarationStatus.REFUTED, evidence=DeclarationEvidence.NEGATION_PROVED),
    ]
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()
    setter = _completed_task(task_id="task-first")
    setter.status = first
    follow_up = _completed_task()
    follow_up.status = TaskStatus.CANCELED
    follow_up.created_at = datetime.now() + timedelta(minutes=25)

    with patch(
        "aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(follow_up, earlier=[setter])
    ):
        await action.run_action(action.parser.parse_args(["project-123"]))

    assert expected in caplog.messages


async def test_show_task_counts_by_the_latest_task_whose_end_the_properties_stand_at(properties, caplog):
    caplog.set_level(logging.INFO)
    properties.return_value = [_property(DeclarationStatus.PROVED)]
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()
    older = _completed_task(task_id="task-old")
    latest = _completed_task(task_id="task-new")
    latest.status = TaskStatus.CANCELED

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=older),
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(latest)),
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--task", "task-old"]))

    assert "Properties: 0 proved · 0 refuted · 0 in progress · 1 unchecked" in caplog.messages
    older.show.assert_awaited_once()


async def test_a_project_without_properties_gets_no_counts_line(properties, caplog):
    caplog.set_level(logging.INFO)
    properties.return_value = []
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()
    mock_project = Mock(spec=Project, description=None)
    mock_project.project_id = "project-123"
    mock_task = _completed_task()
    mock_project.get_tasks = AsyncMock(return_value=([mock_task], None))

    with patch("aristotlelib.cli.command.show.Project.from_id", return_value=mock_project):
        await action.run_action(action.parser.parse_args(["project-123"]))

    assert "Properties:" not in caplog.text
    mock_task.show.assert_called_once()


async def test_show_action_with_specific_task_id_also_shows_counts(properties, caplog):
    caplog.set_level(logging.INFO)
    properties.return_value = [_property(DeclarationStatus.IN_PROGRESS), _property(DeclarationStatus.FAILED)]
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ShowAction(subparsers)
    action.add_action_arguments()
    args = action.parser.parse_args(["project-123", "--task", "task-789"])

    mock_task = _completed_task(task_id="task-789")

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=mock_task),
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(mock_task)),
    ):
        await action.run_action(args)

    properties.assert_called_once_with("project-123", declaration_type=DeclarationType.PROPERTY)
    assert "0 proved · 0 refuted · 1 in progress · 1 unresolved" in caplog.text
    mock_task.show.assert_called_once()


async def test_property_counts_are_logged_before_the_task_display(properties, caplog):
    caplog.set_level(logging.INFO)
    properties.return_value = [_property(DeclarationStatus.PROVED)]
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ShowAction(subparsers)
    action.add_action_arguments()
    args = action.parser.parse_args(["project-123"])

    mock_project = Mock(spec=Project, description=None)
    mock_project.project_id = "project-123"
    mock_task = _completed_task()
    # Records whether the counts line was logged before the task display ran.
    counts_came_first: list[bool] = []
    mock_task.show = AsyncMock(side_effect=lambda **kwargs: counts_came_first.append("1 proved" in caplog.text))
    mock_project.get_tasks = AsyncMock(return_value=([mock_task], None))

    with patch("aristotlelib.cli.command.show.Project.from_id", return_value=mock_project):
        await action.run_action(args)

    assert counts_came_first == [True]


async def test_property_counts_precede_the_display_of_a_specific_task_too(properties, caplog):
    caplog.set_level(logging.INFO)
    properties.return_value = [_property(DeclarationStatus.PROVED)]
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ShowAction(subparsers)
    action.add_action_arguments()
    args = action.parser.parse_args(["project-123", "--task", "task-789"])

    mock_task = _completed_task(task_id="task-789")
    counts_came_first: list[bool] = []
    mock_task.show = AsyncMock(side_effect=lambda **kwargs: counts_came_first.append("1 proved" in caplog.text))

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=mock_task),
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(mock_task)),
    ):
        await action.run_action(args)

    assert counts_came_first == [True]


@pytest.mark.parametrize(
    "failure",
    [
        AristotleAPIError("declarations are down", status_code=500),
        _unreadable_record(),
        KeyError("declarations"),
        TypeError("pagination_key must be a string or None"),
    ],
    ids=["endpoint-down", "unreadable-record", "missing-key", "bad-cursor"],
)
async def test_show_action_still_shows_the_task_when_counts_are_unavailable(properties, caplog, failure: Exception):
    # No failure fetching the counts may take the task display down.
    caplog.set_level(logging.INFO)
    properties.side_effect = failure
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ShowAction(subparsers)
    action.add_action_arguments()
    args = action.parser.parse_args(["project-123"])

    mock_project = Mock(spec=Project, description=None)
    mock_project.project_id = "project-123"
    mock_task = _completed_task()
    mock_project.get_tasks = AsyncMock(return_value=([mock_task], None))

    with patch("aristotlelib.cli.command.show.Project.from_id", return_value=mock_project):
        await action.run_action(args)  # must not raise

    mock_task.show.assert_called_once()
    assert "proved" not in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_show_action_does_not_count_when_the_task_belongs_elsewhere(properties):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ShowAction(subparsers)
    action.add_action_arguments()
    args = action.parser.parse_args(["project-123", "--task", "task-789"])

    mock_task = _completed_task(task_id="task-789", project_id="different-project")

    with patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=mock_task):
        await action.run_action(args)

    properties.assert_not_called()
    mock_task.show.assert_not_called()


def _running_task(status_after_wait: TaskStatus) -> Mock:
    task = Mock(spec=AgentTask)
    task.agent_task_id = "task-456"
    task.project_id = "project-123"
    task.status = TaskStatus.IN_PROGRESS
    task.created_at = datetime(2000, 1, 1)

    async def wait_for_completion(
        num_events: int,
        interactive: bool = False,
        title: str | None = None,
        carry_on: str | None = None,
        resume: list[list[str]] | None = None,
    ) -> None:
        task.status = status_after_wait
        task.live = status_after_wait in (TaskStatus.QUEUED, TaskStatus.IN_PROGRESS)

    task.wait_for_completion = AsyncMock(side_effect=wait_for_completion)
    return task


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (TaskStatus.COMPLETE, 0),
        (TaskStatus.COMPLETE_WITH_ERRORS, 1),
        (TaskStatus.OUT_OF_BUDGET, 1),
        (TaskStatus.FAILED, 3),
        (TaskStatus.CANCELED, 3),
    ],
)
async def test_show_follows_a_running_task_to_its_end_and_exits_on_how_it_ended_as_wait_does(status, code):
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()
    task = _running_task(status)

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=task),
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(None)),
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", "--task", "task-456"])) == code

    task.wait_for_completion.assert_awaited_once_with(
        num_events=3,
        interactive=True,
        title=None,
        carry_on=None,
        resume=[["aristotle", "continue", "project-123", "Continue", "--wait"]],
    )


DOWNLOAD_HINT = "Get the project's files with:\n  aristotle download project-123"
SPACED = "my results/run 1.tar.gz"
"""A --destination the shell must quote."""
NOT_SAVED = "The project's files were not saved, since the command was interrupted."


def _get_them(destination: str = SPACED) -> str:
    """How a message says to save project-123's files to ``destination``, quoted for the shell the test runs under."""
    return shell_hint("Get them with", ["aristotle", "download", "project-123", f"--destination={destination}"])


@pytest.mark.parametrize(
    ("status", "counts", "code"),
    [
        (TaskStatus.COMPLETE, "1 proved · 1 refuted · 0 in progress", 0),
        # The counts stand as the task's end left them checked.
        (TaskStatus.OUT_OF_BUDGET, "0 proved · 0 refuted · 0 in progress · 2 unchecked", 1),
    ],
    ids=["complete", "out of budget"],
)
async def test_show_counts_the_properties_once_it_has_followed_a_running_task_to_its_end(
    status, counts, code, properties, caplog
):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()
    task = _running_task(status)

    async def listed(project_id: str, declaration_type: DeclarationType) -> list[Declaration]:
        assert task.status == status
        return [
            _property(DeclarationStatus.PROVED),
            _property(DeclarationStatus.REFUTED, evidence=DeclarationEvidence.NEGATION_PROVED),
        ]

    properties.side_effect = listed
    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=task),
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(None)),
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", "--task", "task-456"])) == code

    properties.assert_awaited_once()
    assert caplog.messages[-2:] == [f"Properties: {counts}", DOWNLOAD_HINT]


@pytest.mark.parametrize(
    ("has_files", "hint"),
    [(True, True), (False, False), (None, True)],
    ids=["has files", "no files", "project unavailable"],
)
async def test_show_says_how_to_get_the_files_of_a_followed_task_unless_it_has_none(has_files, hint, caplog):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()
    task = _running_task(TaskStatus.COMPLETE)
    if has_files is None:
        fetched = {"side_effect": AristotleAPIError("bad gateway", status_code=502)}
    else:
        fetched = {"return_value": _project_with(None, has_files=has_files)}

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=task),
        patch("aristotlelib.cli.command.show.Project.from_id", **fetched) as from_id,
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", "--task", "task-456"])) == 0

    from_id.assert_awaited_once_with("project-123")
    assert (DOWNLOAD_HINT in caplog.messages) is hint


async def test_a_followed_task_still_running_gets_the_counts_but_no_download_command(properties, caplog):
    caplog.set_level(logging.INFO)
    properties.return_value = [_property(DeclarationStatus.IN_PROGRESS)]
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()
    task = _running_task(TaskStatus.IN_PROGRESS)

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=task),
        patch("aristotlelib.cli.command.show.Project.from_id") as from_id,
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", "--task", "task-456"])) == 3

    from_id.assert_not_called()
    assert caplog.messages[-1] == "Properties: 0 proved · 0 refuted · 1 in progress"


async def test_show_of_a_finished_task_does_not_say_how_to_get_the_files(caplog):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=_completed_task()),
        patch("aristotlelib.cli.command.show.Project.from_id") as from_id,
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", "--task", "task-456"])) is None

    from_id.assert_not_called()
    assert DOWNLOAD_HINT not in caplog.messages


async def test_show_exits_3_when_the_wait_ends_before_the_task_does():
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()
    task = _running_task(TaskStatus.IN_PROGRESS)

    with patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=task):
        assert await action.run_action(action.parser.parse_args(["project-123", "--task", "task-456"])) == 3


@pytest.mark.parametrize("limit", ["-1", "101"])
async def test_an_out_of_range_event_limit_is_refused_before_any_request(limit: str, capsys, resolve_project):
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.Project.from_id") as from_id,
        pytest.raises(SystemExit) as exited,
    ):
        action.preflight(action.parser.parse_args(["project-123", "--limit", limit]))

    assert exited.value.code == 2
    assert "--limit must be between 0 and 100" in capsys.readouterr().err
    from_id.assert_not_called()
    resolve_project.assert_not_awaited()


async def test_a_pagination_key_with_limit_0_is_refused_before_any_request(capsys, resolve_project):
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.Project.from_id") as from_id,
        pytest.raises(SystemExit) as exited,
    ):
        action.preflight(action.parser.parse_args(["project-123", "--limit", "0", "--pagination-key", "k"]))

    assert exited.value.code == 2
    assert "--pagination-key and --limit 0 don't go together: --limit 0 shows no events" in capsys.readouterr().err
    from_id.assert_not_called()
    resolve_project.assert_not_awaited()


@pytest.mark.parametrize("argv", [[], ["--json"], ["--wait"], ["--non-interactive"]], ids=lambda argv: " ".join(argv))
async def test_a_pagination_key_for_a_finished_task_without_a_limit_is_refused(argv, capsys):
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()

    with (
        patch(
            "aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(_real_task(TaskStatus.COMPLETE))
        ),
        patch.object(AgentTask, "get_events", AsyncMock()) as get_events,
        patch.object(AgentTask, "show", AsyncMock()) as shown,
        pytest.raises(SystemExit) as exited,
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--pagination-key", "not-a-key", *argv]))

    assert exited.value.code == 2
    assert "--pagination-key needs --limit for a finished task, which shows no events without it" in (
        capsys.readouterr().err
    )
    get_events.assert_not_awaited()
    shown.assert_not_awaited()


@pytest.mark.parametrize(
    ("argv", "terminal"),
    [([], True), (["--wait"], True), (["--wait"], False)],
    ids=["at a terminal", "--wait at a terminal", "--wait piped"],
)
async def test_a_pagination_key_for_a_running_task_it_follows_is_refused(argv, terminal, at_a_terminal, capsys):
    at_a_terminal.return_value = terminal
    task = _running_task(TaskStatus.COMPLETE)
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(task)),
        pytest.raises(SystemExit) as exited,
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--pagination-key", "k", *argv]))

    assert exited.value.code == 2
    assert "--pagination-key needs --json while the task runs: following it shows only its newest events" in (
        capsys.readouterr().err
    )
    task.wait_for_completion.assert_not_awaited()


@pytest.mark.parametrize("argv", [[], ["--json"]], ids=["text", "json"])
async def test_a_pagination_key_for_a_project_without_a_task_is_refused(argv, capsys):
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(None)),
        pytest.raises(SystemExit) as exited,
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--pagination-key", "k", *argv]))

    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert "--pagination-key pages through a task's events, and the project has no task" in captured.err
    assert captured.out == ""


async def test_a_pagination_key_names_the_page_a_piped_show_of_a_running_task_prints(at_a_terminal):
    at_a_terminal.return_value = False
    task = _running_task(TaskStatus.COMPLETE)
    task.show = AsyncMock()
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()

    with patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(task)):
        assert await action.run_action(action.parser.parse_args(["project-123", "--pagination-key", "k"])) is None

    task.show.assert_awaited_once_with(num_events=3, pagination_key="k", title=None)
    task.wait_for_completion.assert_not_awaited()


async def test_a_pagination_key_names_the_page_of_a_running_tasks_json():
    _, get_events, wait, _ = await _show_json(["--pagination-key", "k"], _real_task(TaskStatus.IN_PROGRESS))

    get_events.assert_awaited_once_with(limit=3, pagination_key="k")
    wait.assert_not_awaited()


def _real_task(status: TaskStatus, project_id: str = "project-123") -> AgentTask:
    return AgentTask(
        project_id=project_id,
        agent_task_id="task-456",
        status=status,
        created_at=datetime(2026, 9, 1, 12, 0),
        last_updated_at=datetime(2026, 9, 1, 13, 0),
        percent_complete=40,
        file_name=None,
        description="Prove it",
        output_summary=None,
    )


def _event(event_id: str, minute: int) -> Event:
    return Event(
        event_id=event_id,
        agent_task_id="task-456",
        event_type=EventType.PROVING,
        created_at=datetime(2026, 9, 1, 12, minute),
        content=f"step {event_id}",
        file_path=None,
        explanation=None,
        status=EventStatus.COMPLETE,
    )


def _project_with(
    task: AgentTask | None, has_files: bool = True, earlier: list[AgentTask] | None = None
) -> MockableProject:
    """A project whose latest task is ``task``, then ``earlier``'s, newest first."""
    project = mockable_project(task.project_id if task else "project-123", has_files=has_files)
    project.get_tasks = AsyncMock(return_value=([task, *(earlier or [])] if task else [], None))
    return project


async def _show_json(argv: list[str], task: AgentTask | None, events=None, pagination_key=None, earlier=None):
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()
    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(task, earlier=earlier)),
        patch.object(AgentTask, "get_events", AsyncMock(return_value=(events or [], pagination_key))) as get_events,
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[])) as wait,
        patch.object(AgentTask, "show", AsyncMock()) as shown,
    ):
        code = await action.run_action(action.parser.parse_args(["project-123", "--json", *argv]))
    return code, get_events, wait, shown


async def test_json_prints_a_running_task_without_waiting_for_it(properties, capsys):
    counterexample = _property(DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE).model_copy(
        update={"artifact_paths": ["sim/cex.vcd", "sim/cex_tb.sv"]}
    )
    properties.return_value = [_property(DeclarationStatus.PROVED), counterexample]
    events = [_event("e3", 3), _event("e2", 2), _event("e1", 1)]

    code, get_events, wait, shown = await _show_json([], _real_task(TaskStatus.IN_PROGRESS), events)

    assert code is None
    wait.assert_not_awaited()
    shown.assert_not_awaited()
    get_events.assert_awaited_once_with(limit=3, pagination_key=None)
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["project_id"] == "project-123"
    assert snapshot["task"]["status"] == "IN_PROGRESS"
    # Oldest first, as the text display shows them.
    assert [e["event_id"] for e in snapshot["events"]] == ["e1", "e2", "e3"]
    assert snapshot["property_counts"] is None
    assert snapshot["properties"] == [
        {
            "declaration_id": "p-PROVED",
            "name": "Spec.PROVED",
            "status": 3,
            "failure_reason": None,
            "evidence": None,
            "evidence_message": None,
            "unchecked": False,
            "status_name": "PROVED",
            "failure_reason_name": None,
            "source": 1,
            "source_name": "AGENT",
            "artifact_paths": [],
            "outcome": "proved",
        },
        {
            "declaration_id": "p-REFUTED",
            "name": "Spec.REFUTED",
            "status": 2,
            "failure_reason": None,
            "evidence": 1,
            "evidence_message": None,
            "unchecked": False,
            "status_name": "REFUTED",
            "failure_reason_name": None,
            "source": 1,
            "source_name": "AGENT",
            "artifact_paths": ["sim/cex.vcd", "sim/cex_tb.sv"],
            "outcome": "refuted",
        },
    ]


async def test_json_flags_as_unchecked_only_the_verdicts_the_task_that_ended_unchecked_set(properties, capsys):
    # The cancelled task was created at 12:00, after one that finished at 10:00: the first proof is the earlier task's,
    # the second its own.
    first = _real_task(TaskStatus.COMPLETE).model_copy(
        update={"agent_task_id": "task-123", "created_at": datetime(2026, 9, 1, 10, 0)}
    )
    earlier, own = (
        _property(DeclarationStatus.PROVED).model_copy(
            update={"declaration_id": f"p-{name}", "name": f"Spec.{name}", "last_updated": datetime(2026, 9, 1, *time)}
        )
        for name, time in (("Earlier", (11, 0)), ("Own", (12, 30)))
    )
    properties.return_value = [own, earlier]

    await _show_json([], _real_task(TaskStatus.CANCELED), earlier=[first])

    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["property_counts"]["proved"] == snapshot["property_counts"]["unchecked"] == 1
    assert [(p["name"], p["unchecked"]) for p in snapshot["properties"]] == [
        ("Spec.Own", True),
        ("Spec.Earlier", False),
    ]


async def test_json_keeps_the_code_of_a_proof_that_no_longer_checks(properties, capsys):
    properties.return_value = [_failed(DeclarationFailureReason.PROOF_CHECK_FAILED)]

    await _show_json([], _real_task(TaskStatus.COMPLETE))

    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["property_counts"]["unresolved"] == 1
    assert snapshot["properties"][0]["failure_reason"] == 3


async def test_json_for_an_idle_task_has_no_events_unless_a_limit_asks(capsys):
    code, get_events, _, _ = await _show_json([], _real_task(TaskStatus.COMPLETE))

    assert code is None
    get_events.assert_not_awaited()
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["events"] == []
    assert snapshot["property_counts"] == {
        "proved": 0,
        "in_progress": 0,
        "unresolved": 0,
        "unchecked": 0,
        "refuted": 0,
    }


async def test_json_prints_the_next_page_key_after_the_snapshot(caplog):
    caplog.set_level(logging.INFO)

    with patch("aristotlelib.cli.command.show.print_json", side_effect=lambda result: logging.info("snapshot")):
        await _show_json(["--limit", "1"], _real_task(TaskStatus.COMPLETE), [_event("e5", 5)], "k2")

    assert caplog.messages == ["snapshot", "Pagination key for next page: k2"]


async def test_json_pages_events_and_keeps_the_key_on_stderr(capsys, caplog):
    with caplog.at_level(logging.INFO):
        _, get_events, _, _ = await _show_json(
            ["--limit", "1", "--pagination-key", "k1"], _real_task(TaskStatus.COMPLETE), [_event("e5", 5)], "k2"
        )

    get_events.assert_awaited_once_with(limit=1, pagination_key="k1")
    assert [e["event_id"] for e in json.loads(capsys.readouterr().out)["events"]] == ["e5"]
    assert caplog.messages == ["Pagination key for next page: k2"]


async def test_json_for_a_project_without_a_task_is_a_snapshot_with_no_task(capsys, caplog):
    with caplog.at_level(logging.INFO):
        code, _, _, _ = await _show_json([], None)

    assert code is None
    assert json.loads(capsys.readouterr().out)["task"] is None
    assert caplog.messages == []


async def test_json_counts_and_properties_are_null_when_they_cannot_be_fetched(properties, capsys):
    properties.side_effect = AristotleAPIError("bad gateway", status_code=502)

    await _show_json([], _real_task(TaskStatus.COMPLETE))

    snapshot = json.loads(capsys.readouterr().out)
    assert (snapshot["property_counts"], snapshot["properties"]) == (None, None)


@pytest.mark.parametrize(
    ("latest", "listed", "code"),
    [
        ({"return_value": _project_with(_real_task(TaskStatus.IN_PROGRESS))}, 1, None),
        ({"side_effect": AristotleAPIError("bad gateway", status_code=502)}, None, EXIT_INFRASTRUCTURE_ERROR),
    ],
    ids=["a later task runs", "the latest task can't be fetched"],
)
async def test_json_for_an_older_task_counts_by_the_latest_one(latest, listed, code, properties, capsys):
    # The properties stand as the latest task left them; when it can't be fetched, they can't be judged.
    properties.return_value = [_property(DeclarationStatus.PROVED)]
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()
    older = _real_task(TaskStatus.COMPLETE)

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=older),
        patch("aristotlelib.cli.command.show.Project.from_id", **latest),
    ):
        exit_code = await action.run_action(action.parser.parse_args(["project-123", "--task", "task-456", "--json"]))

    assert exit_code == code
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["task"]["status"] == "COMPLETE"
    # The counts wait for the latest task, running or unknown; the properties are judged only once it is known.
    assert (snapshot["property_counts"], snapshot["requested_counts"]) == (None, None)
    assert (None if snapshot["properties"] is None else len(snapshot["properties"])) == listed
    # A project that can't be fetched is null, and its page is still named.
    assert (snapshot["project"] is None) == ("side_effect" in latest)
    assert snapshot["web_url"] == "https://aristotle.harmonic.fun/projects/project-123"


async def test_text_for_an_older_task_warns_when_the_latest_task_cannot_be_fetched(properties, caplog):
    caplog.set_level(logging.INFO)
    properties.return_value = [_property(DeclarationStatus.PROVED)]
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=_real_task(TaskStatus.COMPLETE)),
        patch(
            "aristotlelib.cli.command.show.Project.from_id",
            side_effect=AristotleAPIError("bad gateway", status_code=502),
        ),
        patch.object(AgentTask, "show", AsyncMock()),
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", "--task", "task-456"])) is None

    assert any("latest task couldn't be fetched, so the counts go by task task-456" in m for m in caplog.messages)
    assert "Properties: 1 proved · 0 refuted · 0 in progress" in caplog.messages


async def test_json_with_a_task_of_another_project_prints_nothing_and_exits_4(capsys):
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()

    with patch(
        "aristotlelib.cli.command.show.AgentTask.from_id", return_value=_real_task(TaskStatus.COMPLETE, "other")
    ):
        code = await action.run_action(action.parser.parse_args(["project-123", "--task", "task-456", "--json"]))

    assert code == 4
    assert capsys.readouterr().out == ""


async def test_non_interactive_show_waits_for_a_running_task_and_stops_for_questions(capsys):
    task = _real_task(TaskStatus.IN_PROGRESS)
    question = Question(
        question_id="q-1",
        project_id="project-123",
        agent_run_id="task-456",
        content="Which clock?",
        created_at=datetime(2026, 9, 1, 12, 1),
        updated_at=datetime(2026, 9, 1, 12, 1),
    )
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(task)),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[question])) as wait,
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", "--non-interactive"])) == 10

    wait.assert_awaited_once_with(num_events=3, interactive=False, stop_on_questions=True, carry_on=ANY, resume=ANY)
    assert "aristotle show project-123 --task task-456 --non-interactive" in wait.await_args.kwargs["carry_on"]
    assert wait.await_args.kwargs["resume"] == [
        ["aristotle", "continue", "project-123", "Continue", "--non-interactive"]
    ]
    assert json.loads(capsys.readouterr().out)["questions"][0]["question_id"] == "q-1"


async def test_a_resumed_wait_that_stops_again_keeps_its_destination(tmp_path, caplog):
    task = _real_task(TaskStatus.IN_PROGRESS)
    question = Question(
        question_id="q-2",
        project_id="project-123",
        agent_run_id="task-456",
        content="Which reset?",
        created_at=datetime(2026, 9, 1, 12, 1),
        updated_at=datetime(2026, 9, 1, 12, 1),
    )
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()
    destination = str(tmp_path / "result.tar.gz")

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(task)),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[question])),
        caplog.at_level(logging.INFO),
    ):
        argv = ["project-123", "--non-interactive", "--destination", destination]
        assert await action.run_action(action.parser.parse_args(argv)) == 10

    assert caplog.messages[-1].endswith(resume_hint(task, destination))


async def _resume_with_destination(
    destination: str,
    status_after: TaskStatus,
    project: Mock,
    get_files: AsyncMock,
    flags: tuple[str, ...] = ("--non-interactive",),
):
    """Run ``show project-123 <flags> --destination <destination>`` on a task that ends in status_after."""
    task = _real_task(TaskStatus.IN_PROGRESS)
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()
    project.get_tasks = AsyncMock(return_value=([task], None))
    project.get_files = get_files

    async def finish(
        num_events: int = 3,
        interactive: bool = True,
        stop_on_questions: bool = False,
        title: str | None = None,
        carry_on: str | None = None,
        resume: list[list[str]] | None = None,
    ) -> list:
        task.status = status_after
        return []

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=project),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)),
        patch.object(AgentTask, "get_events", AsyncMock(return_value=([], None))),
    ):
        args = action.parser.parse_args(["project-123", *flags, "--destination", destination])
        assert action.preflight(args) is None
        return await action.run_action(args)


@pytest.mark.parametrize("flags", [("--non-interactive",), ("--wait", "--json")], ids=" ".join)
@pytest.mark.parametrize(("status", "code"), [(TaskStatus.COMPLETE, 0), (TaskStatus.OUT_OF_BUDGET, 1)])
async def test_a_resumed_wait_with_a_destination_saves_the_files_there(status, code, flags, tmp_path, capsys, caplog):
    caplog.set_level(logging.INFO)
    destination = str(tmp_path / "out") + "/"
    saved = tmp_path / "out" / "project-123-aristotle.tar.gz"
    get_files = AsyncMock(return_value=saved)

    assert await _resume_with_destination(destination, status, _project_with(None), get_files, flags) == code

    get_files.assert_awaited_once_with(destination=destination)
    result = json.loads(capsys.readouterr().out)
    assert result["task"]["status"] == status.value
    assert result["result_path"] == str(saved)
    assert caplog.messages[-1] == f"Saved the project's files to {saved}"
    assert DOWNLOAD_HINT not in caplog.messages


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (OSError(28, "No space left on device"), "[Errno 28] No space left on device"),
        (AristotleAPIError("Request failed: timed out"), "Request failed: timed out"),
    ],
)
async def test_a_resumed_wait_whose_save_fails_still_prints_the_task_and_exits_3(
    failure, reason, tmp_path, capsys, caplog
):
    get_files = AsyncMock(side_effect=failure)

    assert await _resume_with_destination(SPACED, TaskStatus.COMPLETE, _project_with(None), get_files) == 3

    result = json.loads(capsys.readouterr().out)
    assert (result["task"]["status"], result["result_path"]) == ("COMPLETE", None)
    assert caplog.records[-1].levelno == logging.ERROR
    assert caplog.messages[-1] == f"The project's files were not saved: {reason}\n{_get_them()}"


@pytest.mark.parametrize(
    ("status", "has_files", "code"),
    [(TaskStatus.COMPLETE, False, 0), (TaskStatus.IN_PROGRESS, True, 3)],
    ids=["no files", "wait broke off"],
)
async def test_a_resumed_wait_with_a_destination_saves_nothing_without_files_or_a_finished_task(
    status, has_files, code, tmp_path, capsys, caplog
):
    caplog.set_level(logging.INFO)
    get_files = AsyncMock()

    project = _project_with(None, has_files=has_files)
    assert await _resume_with_destination(str(tmp_path), status, project, get_files) == code

    get_files.assert_not_awaited()
    assert json.loads(capsys.readouterr().out)["result_path"] is None
    assert not [m for m in caplog.messages if m.startswith(("Saved", "Get the project's files"))]


@pytest.mark.parametrize(("status", "code"), [(TaskStatus.COMPLETE, 0), (TaskStatus.OUT_OF_BUDGET, 1)])
async def test_non_interactive_show_of_a_finished_task_saves_its_files_and_names_where(
    status, code, tmp_path, capsys, caplog
):
    caplog.set_level(logging.INFO)
    saved = tmp_path / "run 1.tar.gz"
    project = _project_with(_real_task(status))
    project.get_files = AsyncMock(return_value=saved)
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=project),
        patch.object(AgentTask, "wait_for_completion", AsyncMock()) as wait,
    ):
        assert (
            await action.run_action(
                action.parser.parse_args(["project-123", "--non-interactive", "--destination", SPACED])
            )
            == code
        )

    wait.assert_not_awaited()
    project.get_files.assert_awaited_once_with(destination=SPACED)
    result = json.loads(capsys.readouterr().out)
    assert (result["task"]["status"], result["result_path"]) == (status.value, str(saved))
    assert caplog.messages[-1] == f"Saved the project's files to {saved}"


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, asyncio.CancelledError])
@pytest.mark.parametrize("flags", [("--non-interactive",), ("--wait", "--json")], ids=" ".join)
@pytest.mark.parametrize(
    ("cut_short", "says"),
    [("wait", False), ("refresh", True), ("events", True), ("download", True), ("counts", False)],
)
async def test_a_json_wait_cut_short_once_the_run_has_finished_says_the_files_were_not_saved(
    cut_short, says, flags, interrupt, properties, tmp_path, capsys, caplog
):
    """Between the run's end and the save, and only then; the archive saved before a later cut stays named."""
    caplog.set_level(logging.INFO)
    saved = tmp_path / "run 1.tar.gz"
    task = _real_task(TaskStatus.IN_PROGRESS)
    project = _project_with(task)
    project.refresh = AsyncMock(side_effect=interrupt if cut_short == "refresh" else None)
    project.get_files = AsyncMock(side_effect=interrupt) if cut_short == "download" else AsyncMock(return_value=saved)
    if cut_short == "counts":
        properties.side_effect = interrupt

    async def finish(**_: object) -> list:
        if cut_short == "wait":
            raise interrupt
        task.status = TaskStatus.COMPLETE
        return []

    events = AsyncMock(side_effect=interrupt) if cut_short == "events" else AsyncMock(return_value=([], None))
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()
    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=project),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)),
        patch.object(AgentTask, "get_events", events),
        pytest.raises(interrupt),
    ):
        await action.run_action(action.parser.parse_args(["project-123", *flags, "--destination", SPACED]))

    result = json.loads(capsys.readouterr().out)
    assert result["property_counts"] is None
    assert result["result_path"] == (str(saved) if cut_short == "counts" else None)
    assert [m for m in caplog.messages if m.startswith(NOT_SAVED)] == ([f"{NOT_SAVED}\n{_get_them()}"] if says else [])


async def test_a_json_wait_without_a_destination_cut_short_after_the_run_says_nothing_of_files(
    properties, capsys, caplog
):
    """It was to print the download command, not to save anything."""
    caplog.set_level(logging.INFO)
    task = _real_task(TaskStatus.IN_PROGRESS)
    properties.side_effect = KeyboardInterrupt

    async def finish(**_: object) -> list:
        task.status = TaskStatus.COMPLETE
        return []

    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()
    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(task)),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)),
        patch.object(AgentTask, "get_events", AsyncMock(return_value=([], None))),
        pytest.raises(KeyboardInterrupt),
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--non-interactive"]))

    assert json.loads(capsys.readouterr().out)["result_path"] is None
    assert caplog.messages == []


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, asyncio.CancelledError])
@pytest.mark.parametrize(("cut_short", "says"), [("wait", False), ("counts", True), ("download", True)])
async def test_a_wait_cut_short_once_the_run_has_finished_says_the_files_were_not_saved(
    cut_short, says, interrupt, properties, caplog
):
    caplog.set_level(logging.INFO)
    task = _running_task(TaskStatus.COMPLETE)
    if cut_short == "wait":
        task.live = True
        task.wait_for_completion.side_effect = interrupt
    if cut_short == "counts":
        properties.side_effect = interrupt
    project = _project_with(None)
    project.get_files = AsyncMock(side_effect=interrupt)
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=task),
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=project),
        pytest.raises(interrupt),
    ):
        argv = ["project-123", "--task", "task-456", "--wait", "--destination", SPACED]
        await action.run_action(action.parser.parse_args(argv))

    assert project.get_files.await_count == (cut_short == "download")
    assert [m for m in caplog.messages if m.startswith(NOT_SAVED)] == ([f"{NOT_SAVED}\n{_get_them()}"] if says else [])


async def test_show_wait_of_a_finished_task_cut_short_in_its_save_says_the_files_were_not_saved(caplog):
    caplog.set_level(logging.INFO)
    project = _project_with(_real_task(TaskStatus.COMPLETE))
    project.get_files = AsyncMock(side_effect=KeyboardInterrupt)
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=project),
        patch.object(AgentTask, "show", AsyncMock()),
        pytest.raises(KeyboardInterrupt),
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--wait", "--destination", SPACED]))

    assert caplog.records[-1].levelno == logging.WARNING
    assert caplog.messages[-1] == f"{NOT_SAVED}\n{_get_them()}"


async def test_an_interrupted_non_interactive_wait_names_the_wait_that_carries_on_and_saves_to_the_destination(
    capsys, caplog
):
    """While the run goes on, nothing is due yet: the command that waits on keeps both flags."""
    caplog.set_level(logging.INFO)
    task = _real_task(TaskStatus.IN_PROGRESS)
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(task)),
        patch("aristotlelib.agent_task.Question.list_for_task", AsyncMock(side_effect=KeyboardInterrupt)),
        pytest.raises(KeyboardInterrupt),
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--non-interactive", "--destination", SPACED]))

    show = ["aristotle", "show", "project-123", "--task", "task-456", "--non-interactive", f"--destination={SPACED}"]
    assert caplog.messages[-1] == (
        f"Task task-456 is still running.\n{shell_hint('Carry on waiting with', show)}\n"
        "Cancel it with: aristotle cancel project-123"
    )
    assert json.loads(capsys.readouterr().out)["task"]["status"] == "IN_PROGRESS"


def test_show_interrupted_in_its_download_exits_130_and_says_how_to_save_the_files_there(
    api_server, tmp_path, monkeypatch, capsys, caplog
):
    monkeypatch.chdir(tmp_path)
    task = _real_task(TaskStatus.COMPLETE).model_dump(mode="json")
    project = mockable_project(has_files=True).model_dump(mode="json")

    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/result"):
            raise KeyboardInterrupt
        if request.url.path.endswith("/tasks"):
            return httpx.Response(200, json={"agent_tasks": [task], "pagination_key": None})
        return httpx.Response(200, json=project)

    requests = api_server(*[serve] * 10)

    with patch("sys.argv", ["aristotle", "show", "project-123", "--non-interactive", "--destination", SPACED]):
        assert main() == 130

    assert requests[-1].url.path.endswith("/result")
    assert caplog.messages[-1] == f"{NOT_SAVED}\n{_get_them()}"
    result = json.loads(capsys.readouterr().out)
    assert (result["task"]["status"], result["result_path"], result["property_counts"]) == ("COMPLETE", None, None)
    assert list(tmp_path.iterdir()) == []


async def test_wait_with_non_interactive_waits_as_non_interactive_does(capsys):
    task = _real_task(TaskStatus.IN_PROGRESS)
    question = Question(
        question_id="q-1",
        project_id="project-123",
        agent_run_id="task-456",
        content="Which clock?",
        created_at=datetime(2026, 9, 1, 12, 1),
        updated_at=datetime(2026, 9, 1, 12, 1),
    )
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()
    args = action.parser.parse_args(["project-123", "--wait", "--non-interactive"])

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(task)),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[question])) as wait,
    ):
        assert action.preflight(args) is None
        assert await action.run_action(args) == 10

    wait.assert_awaited_once_with(num_events=3, interactive=False, stop_on_questions=True, carry_on=ANY, resume=ANY)
    assert "aristotle show project-123 --task task-456 --non-interactive" in wait.await_args.kwargs["carry_on"]
    assert json.loads(capsys.readouterr().out)["questions"][0]["question_id"] == "q-1"


@pytest.mark.parametrize("argv", [["--destination", "out/"], ["--json", "--destination", "out/"]])
async def test_destination_without_non_interactive_is_refused_before_any_request(argv, capsys, resolve_project):
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.Project.from_id") as from_id,
        pytest.raises(SystemExit) as exited,
    ):
        action.preflight(action.parser.parse_args(["project-123", *argv]))

    assert exited.value.code == 2
    assert "--destination needs --wait or --non-interactive" in capsys.readouterr().err
    from_id.assert_not_called()
    resolve_project.assert_not_awaited()


async def test_a_destination_through_a_file_is_refused_before_any_request(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_text("notes")
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.Project.from_id") as from_id,
        pytest.raises(SystemExit) as exited,
    ):
        argv = ["project-123", "--non-interactive", "--destination", "notes.txt/result.tar.gz"]
        action.preflight(action.parser.parse_args(argv))

    assert exited.value.code == 2
    assert "Cannot save the archive to notes.txt/result.tar.gz: notes.txt is not a directory" in capsys.readouterr().err
    from_id.assert_not_called()


@pytest.mark.parametrize("flags", [["--non-interactive"], ["--wait", "--json"]], ids=lambda flags: " ".join(flags))
@pytest.mark.parametrize(
    ("status", "code"),
    [(TaskStatus.COMPLETE, 0), (TaskStatus.OUT_OF_BUDGET, 1), (TaskStatus.FAILED, 3), (TaskStatus.IN_PROGRESS, 3)],
)
async def test_non_interactive_show_prints_the_finished_task_and_exits_on_its_outcome(status, code, flags, capsys):
    task = _real_task(TaskStatus.IN_PROGRESS)
    waits: list[dict] = []
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()

    async def finish(
        num_events: int = 3,
        interactive: bool = True,
        stop_on_questions: bool = False,
        title: str | None = None,
        carry_on: str | None = None,
        resume: list[list[str]] | None = None,
    ) -> list:
        waits.append({"stop_on_questions": stop_on_questions, "carry_on": carry_on, "resume": resume})
        task.status = status
        return []

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(task)),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)),
        patch.object(AgentTask, "get_events", AsyncMock(return_value=([], None))),
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", *flags])) == code

    assert json.loads(capsys.readouterr().out)["task"]["status"] == status.value
    # --wait waits through questions, as `submit --wait` does; both name themselves to carry on.
    assert waits[0]["stop_on_questions"] is (flags == ["--non-interactive"])
    assert f"aristotle show project-123 --task task-456 {' '.join(flags)}" in waits[0]["carry_on"]
    assert waits[0]["resume"] == [["aristotle", "continue", "project-123", "Continue", *flags]]


async def test_non_interactive_show_of_a_finished_task_reports_it_without_waiting(capsys):
    code, _, wait, _ = await _show_json(["--non-interactive"], _real_task(TaskStatus.CANCELED))

    assert code == 3
    wait.assert_not_awaited()
    assert json.loads(capsys.readouterr().out)["task"]["status"] == "CANCELED"


@pytest.mark.parametrize(
    ("status_before", "status_after", "hint"),
    [
        (TaskStatus.IN_PROGRESS, TaskStatus.COMPLETE, True),
        (TaskStatus.QUEUED, TaskStatus.OUT_OF_BUDGET, True),
        (TaskStatus.COMPLETE, TaskStatus.COMPLETE, True),
        (TaskStatus.IN_PROGRESS, TaskStatus.IN_PROGRESS, False),
    ],
    ids=["waited", "waited from the queue", "already finished", "wait broke off"],
)
async def test_non_interactive_show_says_how_to_get_the_files_once_the_task_has_finished(
    status_before, status_after, hint, capsys, caplog
):
    caplog.set_level(logging.INFO)
    task = _real_task(status_before)
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()

    async def finish(
        num_events: int = 3,
        interactive: bool = True,
        stop_on_questions: bool = False,
        title: str | None = None,
        carry_on: str | None = None,
        resume: list[list[str]] | None = None,
    ) -> list:
        task.status = status_after
        return []

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(task)),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)),
        patch.object(AgentTask, "get_events", AsyncMock(return_value=([], None))),
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--non-interactive"]))

    assert json.loads(capsys.readouterr().out)["task"]["status"] == status_after.value
    assert (caplog.messages[-1:] == [DOWNLOAD_HINT]) is hint


@pytest.mark.parametrize(
    ("status", "code"),
    [(TaskStatus.COMPLETE, 0), (TaskStatus.COMPLETE_WITH_ERRORS, 1), (TaskStatus.FAILED, 3)],
)
async def test_show_wait_follows_a_running_task_even_when_piped_and_exits_on_how_it_ended(status, code, caplog):
    caplog.set_level(logging.INFO)
    task = _running_task(status)
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=task),
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(None)),
        patch("aristotlelib.cli.command.show.progress_utils.stdout_is_terminal", return_value=False),
    ):
        assert (
            await action.run_action(action.parser.parse_args(["project-123", "--task", "task-456", "--wait"])) == code
        )

    task.wait_for_completion.assert_awaited_once_with(
        num_events=3, interactive=True, title=None, carry_on=ANY, resume=ANY
    )
    assert "aristotle show project-123 --task task-456 --wait" in task.wait_for_completion.await_args.kwargs["carry_on"]
    assert caplog.messages[-1] == DOWNLOAD_HINT


async def test_show_wait_of_a_finished_task_saves_its_files_and_exits_on_how_it_ended(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    saved = tmp_path / "project-123-aristotle.tar.gz"
    project = _project_with(_real_task(TaskStatus.OUT_OF_BUDGET))
    project.get_files = AsyncMock(return_value=saved)
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=project),
        patch.object(AgentTask, "show", AsyncMock()),
    ):
        args = action.parser.parse_args(["project-123", "--wait", "--destination", str(tmp_path)])
        assert action.preflight(args) is None
        assert await action.run_action(args) == 1

    project.get_files.assert_awaited_once_with(destination=str(tmp_path))
    assert caplog.messages[-1] == f"Saved the project's files to {saved}"


async def test_show_json_without_non_interactive_does_not_say_how_to_get_the_files(capsys, caplog):
    with caplog.at_level(logging.INFO):
        await _show_json([], _real_task(TaskStatus.COMPLETE))

    assert json.loads(capsys.readouterr().out)["task"]["status"] == "COMPLETE"
    assert caplog.messages == []


async def test_json_names_the_project_as_the_server_does_not_as_typed(capsys):
    task = _real_task(TaskStatus.COMPLETE, project_id="11111111-2222-3333-4444-555555555555")
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()

    with patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(task)):
        await action.run_action(action.parser.parse_args(["11111111222233334444555555555555", "--json"]))

    assert json.loads(capsys.readouterr().out)["project_id"] == "11111111-2222-3333-4444-555555555555"


# --- Through the real request client: retries, and what an exhausted retry says ---

RETRIED_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def _project_record() -> httpx.Response:
    project = Project(
        project_id=RETRIED_ID,
        status=ProjectStatus.IDLE,
        created_at=datetime(2026, 10, 6),
        last_updated=datetime(2026, 10, 6),
        description="Prove it",
        has_input=False,
        has_files=True,
    )
    return httpx.Response(200, json=project.model_dump(mode="json"))


def _finished_tasks() -> httpx.Response:
    task = {
        "project_id": RETRIED_ID,
        "agent_task_id": "task-1",
        "status": "COMPLETE",
        "created_at": "2026-10-06T00:00:00",
        "last_updated_at": "2026-10-06T00:10:00",
        "percent_complete": 100,
        "file_name": None,
        "description": "Prove it",
        "output_summary": "Proved it.",
    }
    return httpx.Response(200, json={"agent_tasks": [task], "pagination_key": None})


def _per_second_limit() -> httpx.Response:
    return httpx.Response(429, json={"detail": "Too many requests."})


@pytest.mark.parametrize(
    "failures",
    [[_per_second_limit], [lambda: httpx.ReadTimeout(""), lambda: httpx.ReadTimeout("")]],
    ids=["a 429", "two timeouts"],
)
def test_show_json_exits_0_when_a_request_gets_a_429_or_times_out_twice_then_succeeds(failures, api_server, capsys):
    requests = api_server(*(failure() for failure in failures), _project_record(), _finished_tasks())

    with patch("sys.argv", ["aristotle", "show", RETRIED_ID, "--json"]):
        assert main() == 0

    assert json.loads(capsys.readouterr().out)["task"]["status"] == "COMPLETE"
    assert len(requests) == len(failures) + 2


@pytest.mark.parametrize("json_flag", [[], ["--json"]], ids=["text", "--json"])
def test_show_counts_a_refutation_whose_evidence_kind_this_version_does_not_know(
    json_flag, properties, api_server, capsys, caplog
):
    caplog.set_level(logging.INFO)
    properties.side_effect = READ_EVERY_PAGE

    def serve(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/declarations"):
            return unknown_evidence_page(RETRIED_ID)
        if path.endswith("/tasks"):
            return _finished_tasks()
        if path.endswith("/events"):
            return httpx.Response(200, json={"events": [], "pagination_key": None})
        return _project_record()

    api_server(*[serve] * 20)

    with patch("sys.argv", ["aristotle", "show", RETRIED_ID, *json_flag]):
        assert main() == 0

    out, err = capsys.readouterr()
    assert "Traceback" not in err
    if json_flag:
        snapshot = json.loads(out)
        assert snapshot["property_counts"]["refuted"] == 1
        assert [p["evidence"] for p in snapshot["properties"]] == [0]
    else:
        assert "Properties: 0 proved · 1 refuted · 0 in progress" in caplog.messages


@pytest.mark.parametrize(
    ("failure", "code", "message"),
    [
        (lambda: httpx.ReadTimeout(""), 3, f"ReadTimeout on GET /project/{RETRIED_ID} after 5 attempts over 0 s"),
        (
            _per_second_limit,
            6,
            f"GET /project/{RETRIED_ID} after 5 attempts over 0 s: "
            "Too many requests to Aristotle at once. Wait a few seconds and try again.",
        ),
    ],
    ids=["timeouts", "429s"],
)
@pytest.mark.usefixtures("frozen_api_clock")
def test_show_json_whose_retries_run_out_says_what_failed(failure, code, message, api_server, capsys, caplog):
    api_server(*(lambda request: failure() for _ in range(api_request.RETRY_ATTEMPTS)))

    with patch("sys.argv", ["aristotle", "show", RETRIED_ID, "--json"]):
        assert main() == code

    assert caplog.records[-1].levelno == logging.ERROR
    assert caplog.messages[-1] == message
    assert capsys.readouterr().out == ""


def test_a_show_wait_whose_connection_keeps_failing_names_the_command_that_carries_it_on(
    api_server, monkeypatch, capsys, caplog
):
    fast_api_clock(monkeypatch)
    task = {
        "project_id": RETRIED_ID,
        "agent_task_id": "task-1",
        "status": "IN_PROGRESS",
        "created_at": "2026-10-06T00:00:00",
        "last_updated_at": "2026-10-06T00:10:00",
        "percent_complete": 40,
        "file_name": None,
        "description": "Prove it",
        "output_summary": None,
    }

    def serve(request: httpx.Request):
        path = request.url.path
        if path.endswith("/events-stream"):
            return httpx.ConnectError("unreachable")
        if path.endswith("/events"):
            return httpx.Response(200, json={"events": [], "pagination_key": None})
        if path.endswith("/tasks"):
            return httpx.Response(200, json={"agent_tasks": [task], "pagination_key": None})
        if path.endswith("/questions"):
            return httpx.Response(200, json=[])
        if path.endswith("/declarations"):
            return httpx.Response(200, json={"declarations": [], "pagination_key": None})
        if "/task/" in path:
            return httpx.Response(200, json=task)
        return _project_record()

    api_server(*[serve] * 300)

    with patch("sys.argv", ["aristotle", "show", RETRIED_ID, "--wait", "--json"]):
        assert main() == 3

    assert json.loads(capsys.readouterr().out)["task"]["status"] == "IN_PROGRESS"
    [stopped] = [m for m in caplog.messages if m.startswith("The connection to Aristotle kept failing")]
    # The same wait, through questions and printing JSON, carries on from here.
    assert f"Carry on waiting with:\n  aristotle show {RETRIED_ID} --task task-1 --wait --json" in stopped
    assert stopped.endswith(f"Cancel it with: aristotle cancel {RETRIED_ID}")


def test_show_non_interactive_after_a_given_up_stream_prints_the_task_without_the_events_it_cannot_fetch(
    api_server, monkeypatch, capsys, caplog
):
    fast_api_clock(monkeypatch)
    task = {
        "project_id": RETRIED_ID,
        "agent_task_id": "task-1",
        "status": "IN_PROGRESS",
        "created_at": "2026-10-06T00:00:00",
        "last_updated_at": "2026-10-06T00:10:00",
        "percent_complete": 40,
        "file_name": None,
        "description": "Prove it",
        "output_summary": None,
    }

    def serve(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(("/events-stream", "/events")):
            return httpx.Response(503, json={"detail": "Service Unavailable"})
        if path.endswith("/tasks"):
            return httpx.Response(200, json={"agent_tasks": [task], "pagination_key": None})
        if path.endswith("/questions"):
            return httpx.Response(200, json=[])
        if path.endswith("/declarations"):
            return httpx.Response(200, json={"declarations": [], "pagination_key": None})
        if "/task/" in path:
            return httpx.Response(200, json=task)
        return _project_record()

    api_server(*[serve] * 150)

    with patch("sys.argv", ["aristotle", "show", RETRIED_ID, "--non-interactive"]):
        assert main() == 3

    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["task"]["status"] == "IN_PROGRESS"
    assert snapshot["events"] == []
    assert any(m.startswith("WARNING: The task's recent events could not be fetched: ") for m in caplog.messages)


async def test_piped_show_prints_a_running_task_as_it_stands_without_following_it(at_a_terminal, caplog):
    at_a_terminal.return_value = False
    caplog.set_level(logging.INFO)
    task = _running_task(TaskStatus.COMPLETE)
    task.show = AsyncMock()
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()

    with patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(task)):
        assert await action.run_action(action.parser.parse_args(["project-123"])) is None

    task.wait_for_completion.assert_not_awaited()
    task.show.assert_awaited_once_with(num_events=3, pagination_key=None, title=None)
    assert caplog.messages[-1] == (
        "The task is still running. Follow it at a terminal with `aristotle show project-123`, or wait for it with "
        "`aristotle show project-123 --wait`."
    )


async def test_show_at_a_terminal_heads_the_display_with_the_projects_name():
    task = _running_task(TaskStatus.COMPLETE)
    project = _project_with(task)
    project.description = "rtl: ci @ main 1a2b3c4"
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()

    with patch("aristotlelib.cli.command.show.Project.from_id", return_value=project):
        await action.run_action(action.parser.parse_args(["project-123"]))

    task.wait_for_completion.assert_awaited_once_with(
        num_events=3, interactive=True, title="rtl: ci @ main 1a2b3c4", carry_on=None, resume=ANY
    )


async def test_an_interrupted_non_interactive_show_still_prints_the_task_as_it_last_stood(capsys):
    task = _real_task(TaskStatus.IN_PROGRESS)
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(task)),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=KeyboardInterrupt)),
        pytest.raises(KeyboardInterrupt),
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--non-interactive"]))

    printed = json.loads(capsys.readouterr().out)
    assert printed["task"]["status"] == "IN_PROGRESS"
    assert printed["property_counts"] is None and printed["properties"] is None
    # The project's run, as the latest task has it.
    assert (printed["project"]["status_name"], printed["project"]["latest_run_status"]) == ("RUNNING", "IN_PROGRESS")


async def test_an_interrupted_show_of_an_older_task_keeps_the_project_as_fetched(properties, capsys):
    """A task asked for with --task may predate the run going now, which the project still says is going."""
    older = _real_task(TaskStatus.COMPLETE)
    project = _project_with(_real_task(TaskStatus.IN_PROGRESS))
    project.status = ProjectStatus.RUNNING
    project.latest_run_status = TaskStatus.IN_PROGRESS
    properties.side_effect = KeyboardInterrupt
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=older),
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=project),
        pytest.raises(KeyboardInterrupt),
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--task", "task-456", "--non-interactive"]))

    printed = json.loads(capsys.readouterr().out)
    assert printed["task"]["status"] == "COMPLETE"
    assert (printed["project"]["status_name"], printed["project"]["latest_run_status"]) == ("RUNNING", "IN_PROGRESS")


async def test_a_non_interactive_show_cut_short_after_its_wait_still_prints_the_task_once(properties, capsys):
    task = _real_task(TaskStatus.IN_PROGRESS)
    properties.side_effect = KeyboardInterrupt
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()

    async def finish(
        num_events: int = 3,
        interactive: bool = True,
        stop_on_questions: bool = False,
        carry_on: str | None = None,
        resume: list[list[str]] | None = None,
    ) -> list:
        task.status = TaskStatus.COMPLETE
        return []

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(task)),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)),
        patch.object(AgentTask, "get_events", AsyncMock(return_value=([], None))),
        pytest.raises(KeyboardInterrupt),
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--non-interactive"]))

    out = capsys.readouterr().out
    printed = json.loads(out)
    assert printed["task"]["status"] == "COMPLETE"
    assert printed["property_counts"] is None
    assert out.count('"web_url"') == 1  # printed once


async def test_json_carries_the_project_and_its_page_in_the_web_app(capsys):
    await _show_json([], _real_task(TaskStatus.COMPLETE))

    snapshot = json.loads(capsys.readouterr().out)
    assert list(snapshot) == [
        "project_id",
        "project",
        "task",
        "web_url",
        "events",
        "result_path",
        "property_counts",
        "requested_counts",
        "properties",
    ]
    assert snapshot["project"]["project_id"] == snapshot["project_id"] == "project-123"
    assert snapshot["web_url"] == "https://aristotle.harmonic.fun/projects/project-123"


@pytest.mark.parametrize("flags", [["--non-interactive"], ["--wait", "--json"]], ids=" ".join)
async def test_a_non_interactive_show_prints_the_project_as_it_stands_once_the_wait_ends(flags, capsys):
    task = _real_task(TaskStatus.IN_PROGRESS)
    project = _project_with(task)
    project.status = ProjectStatus.RUNNING

    async def finish(
        num_events: int = 3,
        interactive: bool = True,
        stop_on_questions: bool = False,
        carry_on: str | None = None,
        resume: list[list[str]] | None = None,
    ) -> list:
        task.status = TaskStatus.COMPLETE
        return []

    async def refresh() -> None:
        project.status = ProjectStatus.IDLE

    project.refresh = AsyncMock(side_effect=refresh)
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=project),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)),
        patch.object(AgentTask, "get_events", AsyncMock(return_value=([], None))),
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", *flags])) == 0

    snapshot = json.loads(capsys.readouterr().out)
    assert (snapshot["task"]["status"], snapshot["project"]["status_name"]) == ("COMPLETE", "IDLE")


@pytest.mark.parametrize(
    ("sources", "asked"),
    [
        ([DeclarationSource.USER, DeclarationSource.USER], None),
        ([DeclarationSource.AGENT, DeclarationSource.AGENT], None),
        ([DeclarationSource.USER, DeclarationSource.AGENT], "You asked for 1: 1 proved · 0 refuted · 0 in progress"),
    ],
    ids=["all asked for", "all suggested", "some of each"],
)
def test_the_counts_add_the_ones_you_asked_for_when_aristotle_suggested_others(sources, asked, caplog):
    properties = [
        _property(DeclarationStatus.PROVED).model_copy(update={"declaration_id": f"p-{i}", "source": source})
        for i, source in enumerate(sources)
    ]

    with caplog.at_level(logging.INFO):
        log_property_counts(properties, ())

    assert caplog.messages == ["Properties: 2 proved · 0 refuted · 0 in progress", *([asked] if asked else [])]


@pytest.mark.parametrize("flags", [["--non-interactive"], ["--wait", "--json"]], ids=" ".join)
@pytest.mark.parametrize("argv", [[], ["--task", "task-456"]], ids=["latest task", "resume command"])
@pytest.mark.parametrize(
    "fault",
    [AristotleAPIError("unavailable", status_code=503), KeyboardInterrupt()],
    ids=["refresh fails", "interrupted"],
)
async def test_a_project_that_cant_be_refreshed_after_a_wait_still_has_its_run_as_the_task_has_it(
    argv, fault, flags, capsys
):
    """Also for `show --task <id> --non-interactive`, the command exit 10 prints, which waits on a live task, the
    latest."""
    task = _real_task(TaskStatus.IN_PROGRESS)
    project = _project_with(task)
    project.status = ProjectStatus.RUNNING
    project.latest_run_status = TaskStatus.IN_PROGRESS
    project.refresh = AsyncMock(side_effect=fault)

    async def finish(
        num_events: int = 3,
        interactive: bool = True,
        stop_on_questions: bool = False,
        carry_on: str | None = None,
        resume: list[list[str]] | None = None,
    ) -> list:
        task.status = TaskStatus.COMPLETE
        return []

    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=task),
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=project),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)),
        patch.object(AgentTask, "get_events", AsyncMock(return_value=([], None))),
        suppress(KeyboardInterrupt),
    ):
        await action.run_action(action.parser.parse_args(["project-123", *argv, *flags]))

    out = capsys.readouterr().out
    snapshot = json.loads(out)
    assert snapshot["task"]["status"] == "COMPLETE"
    # Interrupted, it is a result cut short: no counts, printed once.
    assert (snapshot["property_counts"] is None) == isinstance(fault, KeyboardInterrupt)
    assert out.count('"web_url"') == 1
    assert (snapshot["project"]["status_name"], snapshot["project"]["latest_run_status"]) == ("IDLE", "COMPLETE")


async def test_a_resume_command_cut_short_in_its_wait_has_the_projects_run_as_the_live_task_has_it(capsys):
    """The task was live when fetched, so it is the latest, though --task named it."""
    task = _real_task(TaskStatus.IN_PROGRESS)
    project = _project_with(task)
    project.status = ProjectStatus.IDLE
    project.latest_run_status = TaskStatus.COMPLETE
    parser = argparse.ArgumentParser()
    action = ShowAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=task),
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=project),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=KeyboardInterrupt)),
        pytest.raises(KeyboardInterrupt),
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--task", "task-456", "--non-interactive"]))

    snapshot = json.loads(capsys.readouterr().out)
    assert (snapshot["project"]["status_name"], snapshot["project"]["latest_run_status"]) == ("RUNNING", "IN_PROGRESS")


DOWN = AristotleAPIError("Service unavailable", status_code=503)


def _project_whose_earlier_tasks_cannot_be_listed(task: AgentTask) -> MockableProject:
    """A project whose latest task is ``task``, where listing the tasks before it fails."""
    project = _project_with(task)
    project.get_tasks = AsyncMock(side_effect=lambda limit, **kwargs: ([task], None) if limit == 1 else _raise(DOWN))
    return project


def _raise(error: Exception):
    raise error


def _proof_before(task: AgentTask) -> Declaration:
    """A proof updated an hour before ``task`` was created, so another task set it."""
    return _property(DeclarationStatus.PROVED).model_copy(update={"last_updated": task.created_at - timedelta(hours=1)})


async def test_json_nulls_the_counts_and_exits_3_when_the_tasks_that_set_them_cannot_be_listed(
    properties, capsys, caplog
):
    caplog.set_level(logging.INFO)
    task = _real_task(TaskStatus.COMPLETE)
    properties.return_value = [_proof_before(task)]
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()

    with (
        patch(
            "aristotlelib.cli.command.show.Project.from_id",
            return_value=_project_whose_earlier_tasks_cannot_be_listed(task),
        ),
        patch.object(AgentTask, "get_events", AsyncMock(return_value=([], None))),
    ):
        code = await action.run_action(action.parser.parse_args(["project-123", "--json"]))

    assert code == EXIT_INFRASTRUCTURE_ERROR
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["task"]["status"] == "COMPLETE"
    # The result keeps its shape, with every count and the properties null.
    assert list(snapshot) == [
        "project_id",
        "project",
        "task",
        "web_url",
        "events",
        "result_path",
        "property_counts",
        "requested_counts",
        "properties",
    ]
    assert (snapshot["property_counts"], snapshot["requested_counts"], snapshot["properties"]) == (None, None, None)
    assert "`property_counts`, `requested_counts` and `properties` are null" in caplog.text
    assert "aristotle show project-123 --json" in caplog.text


async def test_the_counts_line_warns_when_the_tasks_that_set_them_cannot_be_listed(properties, caplog):
    caplog.set_level(logging.INFO)
    task = _completed_task()
    task.created_at = datetime.now()
    properties.return_value = [_proof_before(task)]
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()

    with patch(
        "aristotlelib.cli.command.show.Project.from_id",
        return_value=_project_whose_earlier_tasks_cannot_be_listed(task),
    ):
        assert await action.run_action(action.parser.parse_args(["project-123"])) is None

    assert any("couldn't all be listed" in m and "may count as checked" in m for m in caplog.messages)
    assert "Properties: 1 proved · 0 refuted · 0 in progress" in caplog.messages


async def test_a_resumed_wait_saves_the_files_before_it_reports_properties_it_couldnt_judge(
    properties, tmp_path, capsys, caplog
):
    caplog.set_level(logging.INFO)
    destination = str(tmp_path / "out") + "/"
    saved = tmp_path / "out" / "project-123-aristotle.tar.gz"
    get_files = AsyncMock(return_value=saved)
    task = _real_task(TaskStatus.IN_PROGRESS)
    properties.return_value = [_proof_before(task)]
    project = _project_whose_earlier_tasks_cannot_be_listed(task)
    project.get_files = get_files
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()

    async def finish(
        num_events: int = 3,
        interactive: bool = True,
        stop_on_questions: bool = False,
        title=None,
        carry_on=None,
        resume=None,
    ) -> list:
        task.status = TaskStatus.COMPLETE
        return []

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=project),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)),
        patch.object(AgentTask, "get_events", AsyncMock(return_value=([], None))),
    ):
        args = action.parser.parse_args(["project-123", "--non-interactive", "--destination", destination])
        assert action.preflight(args) is None
        code = await action.run_action(args)

    assert code == EXIT_INFRASTRUCTURE_ERROR
    get_files.assert_awaited_once_with(destination=destination)
    saved_at = caplog.messages.index(f"Saved the project's files to {saved}")
    assert any("couldn't be listed" in m for m in caplog.messages[saved_at + 1 :])
    snapshot = json.loads(capsys.readouterr().out)
    assert (snapshot["property_counts"], snapshot["requested_counts"], snapshot["properties"]) == (None, None, None)


@pytest.mark.parametrize(
    ("status_code", "hint"),
    [
        (422, "--pagination-key was rejected; pass the key the previous page printed."),
        (500, "A --pagination-key Aristotle can't read gets this answer too"),
    ],
)
async def test_a_wait_with_a_bad_key_still_prints_the_task_as_it_stood(status_code, hint, capsys, caplog):
    """The snapshot a script waited for isn't lost to the events page the key names; the warning names the key."""
    task = _real_task(TaskStatus.IN_PROGRESS)

    async def finish(*args, **kwargs):
        task.status = TaskStatus.COMPLETE
        return []

    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()
    error = AristotleAPIError(f"API request failed with status {status_code}", status_code=status_code)
    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=_project_with(task)),
        patch.object(AgentTask, "get_events", AsyncMock(side_effect=error)),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)),
        caplog.at_level(logging.INFO),
    ):
        args = action.parser.parse_args(["project-123", "--non-interactive", "--pagination-key", "k"])
        code = await action.run_action(args)

    assert json.loads(capsys.readouterr().out)["task"]["status"] == "COMPLETE"
    assert code in (None, 0)
    warning = next(m for m in caplog.messages if m.startswith("WARNING: The task's recent events could not be fetched"))
    assert f"status {status_code}. {hint}" in warning


@pytest.mark.parametrize(
    ("status", "level", "says"),
    [
        (
            TaskStatus.FAILED,
            logging.ERROR,
            "Try it again on the same project with:\n  aristotle continue project-123 Continue --wait",
        ),
        (
            TaskStatus.OUT_OF_BUDGET,
            logging.INFO,
            "Resume it on the same project with:\n  aristotle continue project-123 Continue --wait",
        ),
        (
            TaskStatus.CANCELED,
            logging.INFO,
            "Resume it on the same project with:\n  aristotle continue project-123 Continue --wait",
        ),
    ],
)
async def test_show_of_a_task_that_ended_badly_names_the_command_that_resumes_it(status, level, says, caplog):
    """As the wait does, rather than the status line alone."""
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()
    task = _completed_task()
    task.status = status
    project = Mock(spec=Project, description=None)
    project.project_id = "project-123"
    project.get_tasks = AsyncMock(return_value=([task], None))

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=project),
        caplog.at_level(logging.INFO),
    ):
        # Reported, not judged: a plain show that waited for nothing exits 0.
        assert await action.run_action(action.parser.parse_args(["project-123"])) is None

    assert caplog.records[-1].levelno == level
    assert caplog.messages[-1].endswith(says)


async def test_show_wait_of_a_task_that_ended_badly_resumes_it_saving_where_it_would(caplog):
    """`continue` takes no --destination, so the follow-up is followed by the wait that saves there."""
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()
    task = _completed_task()
    task.status = TaskStatus.CANCELED
    project = Mock(spec=Project, description=None, has_files=False)
    project.project_id = "project-123"
    project.get_tasks = AsyncMock(return_value=([task], None))

    with (
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=project),
        caplog.at_level(logging.INFO),
    ):
        args = action.parser.parse_args(["project-123", "--wait", "--destination", "out/result.tar.gz"])
        assert await action.run_action(args) == 3

    assert (
        "Task was canceled. Resume it on the same project with:\n"
        "  aristotle continue project-123 Continue\n"
        "  aristotle show project-123 --wait --destination=out/result.tar.gz"
    ) in caplog.messages


async def test_show_task_of_a_shared_project_fetches_no_latest_task_for_a_command_it_never_offers(caplog):
    """Only the owner can continue it, so nothing depends on the latest task, and no lookup failure is blamed."""
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()
    task = _completed_task()
    task.status = TaskStatus.FAILED
    task.read_only = True
    project = Mock(spec=Project, description=None)
    project.project_id = "project-123"
    project.get_tasks = AsyncMock(side_effect=AristotleAPIError("bad gateway", status_code=502))

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=task),
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=project),
        caplog.at_level(logging.INFO),
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--task", "task-456"]))

    project.get_tasks.assert_not_awaited()
    assert not any("latest task couldn't be fetched" in m for m in caplog.messages)
    assert "aristotle continue" not in caplog.messages[-1]


@pytest.mark.parametrize(
    ("latest_id", "offers"), [("task-456", True), ("task-newer", False), (None, False)], ids=["it", "older", "unknown"]
)
async def test_show_task_offers_continue_only_for_the_projects_latest_task(latest_id, offers, caplog):
    """A follow-up continues the latest task: from an older one picked by --task it would steer, or start from, another,
    and so might from one when the latest can't be fetched."""
    action = ShowAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()
    task = _completed_task()
    task.status = TaskStatus.FAILED
    project = Mock(spec=Project, description=None)
    project.project_id = "project-123"
    if latest_id is None:
        project.get_tasks = AsyncMock(side_effect=AristotleAPIError("bad gateway", status_code=502))
    else:
        project.get_tasks = AsyncMock(return_value=([_completed_task(task_id=latest_id)], None))

    with (
        patch("aristotlelib.cli.command.show.AgentTask.from_id", return_value=task),
        patch("aristotlelib.cli.command.show.Project.from_id", return_value=project),
        caplog.at_level(logging.INFO),
    ):
        await action.run_action(action.parser.parse_args(["project-123", "--task", "task-456"]))

    assert caplog.messages[-1].startswith("Task failed due to an internal error.")
    assert ("aristotle continue project-123 Continue --wait" in caplog.messages[-1]) is offers
    if latest_id is None:
        warning = next(m for m in caplog.messages if "latest task couldn't be fetched" in m)
        assert "so no command to continue it is offered: bad gateway" in warning
        assert "counts" not in warning
