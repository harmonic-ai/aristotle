"""Tests for aristotlelib.cli.continue_ module."""

import argparse
import asyncio
import io
import json
import logging
import os
import sys
from datetime import datetime
from unittest.mock import ANY, AsyncMock, Mock, patch

import httpx
import pytest

from aristotlelib import api_request
from aristotlelib.agent_task import AgentTask, TaskStatus
from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.command.continue_ import ContinueAction
from aristotlelib.cli.controller import main
from aristotlelib.declaration import (
    Declaration,
    DeclarationFailureReason,
    DeclarationSource,
    DeclarationStatus,
    DeclarationType,
)
from aristotlelib.project import AgentQuestionsSetting, Project, ProjectStatus
from aristotlelib.question import Question
from tests.cli.conftest import LOOKUP_FAILURES, mockable_project


@pytest.fixture(autouse=True)
def properties():
    """Patch the properties fetch a JSON result makes; empty by default."""
    with patch("aristotlelib.cli.api_action.Declaration.list_all_for_project", return_value=[]) as mock:
        yield mock


def _property(name: str, status: DeclarationStatus, reason: DeclarationFailureReason | None = None) -> Declaration:
    return Declaration(
        project_id="project-123",
        declaration_id=f"id-{name}",
        declaration_type=DeclarationType.PROPERTY,
        source=DeclarationSource.USER,
        name=name,
        status=status,
        failure_reason=reason,
        filename="Spec/Property.lean",
        lean_code="theorem t : True := trivial",
        created_at=datetime(2026, 9, 1, 12, 0),
        last_updated=datetime(2026, 9, 1, 12, 0),
    )


async def test_continue_action_basic():
    """Test continuing a project with a prompt."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ContinueAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["project-123", "Continue with the proof"])

    mock_project = Mock(spec=Project, description=None, status=ProjectStatus.IDLE)
    mock_project.project_id = "project-123"

    mock_task = AgentTask(
        project_id="project-123",
        agent_task_id="task-456",
        status=TaskStatus.QUEUED,
        created_at=datetime.now(),
        last_updated_at=datetime.now(),
        percent_complete=None,
        file_name=None,
        description="Continue with the proof",
        output_summary=None,
    )
    mock_project.ask = AsyncMock(return_value=mock_task)

    with patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=mock_project) as mock_from_id:
        await action.run_action(args)

    # Verify Project.from_id was called with correct ID
    mock_from_id.assert_called_once_with("project-123")

    # Verify ask was called with the prompt and default options
    mock_project.ask.assert_called_once_with(
        "Continue with the proof",
        files=None,
        project_dir=None,
        agent_questions_setting=AgentQuestionsSetting.TIMEOUT_15_MIN,
    )


async def test_continue_action_with_different_prompt():
    """Test continuing with a different prompt."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ContinueAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["proj-abc", "Try a different approach using induction"])

    mock_project = Mock(spec=Project, description=None, status=ProjectStatus.IDLE)
    mock_project.project_id = "proj-abc"
    mock_project.ask = AsyncMock()

    with patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=mock_project):
        await action.run_action(args)

    mock_project.ask.assert_called_once_with(
        "Try a different approach using induction",
        files=None,
        project_dir=None,
        agent_questions_setting=AgentQuestionsSetting.TIMEOUT_15_MIN,
    )


async def test_continue_asks_with_the_prompt_from_stdin(monkeypatch):
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    stdin = Mock(isatty=Mock(return_value=False), buffer=io.BytesIO(b"Try induction on the length.\n"))
    monkeypatch.setattr(sys, "stdin", stdin)
    project = Mock(spec=Project, status=ProjectStatus.IDLE)
    project.project_id = "proj-abc"
    project.ask = AsyncMock()

    with patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=project):
        args = action.parser.parse_args(["proj-abc", "--prompt-file", "-"])
        assert action.preflight(args) is None
        await action.run_action(args)

    project.ask.assert_called_once_with(
        "Try induction on the length.",
        files=None,
        project_dir=None,
        agent_questions_setting=AgentQuestionsSetting.TIMEOUT_15_MIN,
    )


async def test_continue_action_rejects_empty_prompt(capsys, resolve_project):
    """Test that continue rejects an empty prompt as a usage error before any API call."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = ContinueAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["project-123", ""])

    with patch("aristotlelib.cli.command.continue_.Project.from_id") as mock_from_id:
        with pytest.raises(SystemExit) as exc_info:
            action.preflight(args)

    assert exc_info.value.code == 2
    assert "Prompt cannot be empty" in capsys.readouterr().err
    mock_from_id.assert_not_called()
    resolve_project.assert_not_awaited()


@pytest.mark.parametrize("explicit_root", [False, True], ids=["cwd", "project_dir"])
async def test_continue_sends_selected_paths_relative_to_a_stable_root(
    explicit_root, tmp_path, monkeypatch, api_server, caplog
):
    caplog.set_level(logging.INFO)
    root = tmp_path / "downloaded"
    files = [root / "Proofs" / "Foo.lean", root / "Other" / "Foo.lean"]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"theorem t : True := trivial\r\n")
    monkeypatch.chdir(tmp_path if explicit_root else root)
    args = ["project-123", "Use these proofs", "--no-questions"]
    if explicit_root:
        args += ["--project-dir", "downloaded"]
    args += ["--files", *[os.path.relpath(path) for path in files]]
    project = mockable_project()
    record = httpx.Response(200, json=project.model_dump(mode="json"))
    requests = api_server(record, record, httpx.Response(200, json=_queued_task().model_dump(mode="json")))
    action = ContinueAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()
    parsed = action.parser.parse_args(args)
    assert action.preflight(parsed) is None
    assert await action.run_action(parsed) is None
    assert b'filename="Proofs/Foo.lean"' in requests[-1].content
    assert b'filename="Other/Foo.lean"' in requests[-1].content
    assert b'filename="Foo.lean"' not in requests[-1].content
    assert any("Proofs/Foo.lean\n  Other/Foo.lean" in message for message in caplog.messages)


@pytest.mark.parametrize("kind", ["outside", "traversal", "empty root", "missing root"])
def test_continue_rejects_invalid_roots_before_project_lookup(kind, tmp_path, capsys, resolve_project):
    root = tmp_path / "project"
    root.mkdir()
    file = root / "Foo.lean"
    file.write_bytes(b"-- proof\n")
    args = ["project-123", "Use this", "--project-dir", str(root), "--files", str(file)]
    if kind == "outside":
        args[-1] = str(tmp_path / "Outside.lean")
        (tmp_path / "Outside.lean").write_bytes(b"-- outside\n")
    elif kind == "traversal":
        args[-1] = str(root / ".." / "project" / "Foo.lean")
    elif kind == "empty root":
        args[3] = ""
    else:
        args[3] = str(tmp_path / "missing")
    action = ContinueAction(argparse.ArgumentParser().add_subparsers())
    action.add_action_arguments()
    with pytest.raises(SystemExit) as exited:
        action.preflight(action.parser.parse_args(args))
    assert exited.value.code == 2
    assert "error: --files:" in capsys.readouterr().err
    resolve_project.assert_not_awaited()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (TaskStatus.COMPLETE, 0),
        (TaskStatus.COMPLETE_WITH_ERRORS, 1),
        (TaskStatus.FAILED, 3),
        (TaskStatus.IN_PROGRESS, 3),
    ],
)
async def test_continue_wait_exits_on_how_the_task_ended(status: TaskStatus, code: int):
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()

    mock_task = Mock(spec=AgentTask, agent_task_id="task-456", project_id="project-123")
    mock_task.status = TaskStatus.QUEUED
    mock_task.live = True

    async def run_to(
        num_events: int = 3,
        interactive: bool = True,
        stop_on_questions: bool = False,
        title: str | None = None,
        carry_on: str | None = None,
        resume: list[list[str]] | None = None,
    ) -> None:
        mock_task.status = status

    mock_task.wait_for_completion = AsyncMock(side_effect=run_to)
    mock_project = Mock(spec=Project, description=None)
    mock_project.ask = AsyncMock(return_value=mock_task)

    with patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=mock_project):
        assert await action.run_action(action.parser.parse_args(["project-123", "Go on", "--wait"])) == code

    mock_task.wait_for_completion.assert_awaited_once()


async def test_continue_without_wait_exits_0_whatever_the_task_status():
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()

    mock_task = Mock(spec=AgentTask, agent_task_id="task-456", project_id="project-123")
    mock_task.status = TaskStatus.QUEUED
    mock_project = Mock(spec=Project, description=None, status=ProjectStatus.IDLE)
    mock_project.ask = AsyncMock(return_value=mock_task)

    with patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=mock_project):
        assert await action.run_action(action.parser.parse_args(["project-123", "Go on"])) is None


async def test_a_files_path_that_is_not_a_file_is_refused_before_any_request(tmp_path, capsys, resolve_project):
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    missing = tmp_path / "missing.lean"

    with (
        patch("aristotlelib.cli.command.continue_.Project.from_id") as from_id,
        pytest.raises(SystemExit) as exited,
    ):
        action.preflight(
            action.parser.parse_args(
                ["project-123", "Use this", "--project-dir", str(tmp_path), "--files", str(missing)]
            )
        )

    assert exited.value.code == 2
    assert f"--files: {missing} is not a file" in capsys.readouterr().err
    from_id.assert_not_called()
    resolve_project.assert_not_awaited()


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("too long", "is not a file"),
        ("in a folder that can't be entered", "is not a file"),
        ("unreadable", "can't be read"),
    ],
)
async def test_a_files_path_out_of_reach_is_refused_before_any_request(
    kind, expected, tmp_path, capsys, resolve_project
):
    if kind == "too long":
        path = tmp_path / ("a" * 300)
    else:
        if os.name == "nt" or os.geteuid() == 0:
            pytest.skip("file modes don't restrict this user")
        sealed = tmp_path / "sealed"
        sealed.mkdir()
        path = sealed / "lemma.lean"
        path.write_text("theorem t : True := trivial\n")
        (sealed if kind != "unreadable" else path).chmod(0)
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()

    try:
        with (
            patch("aristotlelib.cli.command.continue_.Project.from_id") as from_id,
            pytest.raises(SystemExit) as exited,
        ):
            action.preflight(
                action.parser.parse_args(
                    ["project-123", "Use this", "--project-dir", str(tmp_path), "--files", str(path)]
                )
            )
    finally:
        if kind != "too long":
            (tmp_path / "sealed").chmod(0o700)
            path.chmod(0o600)

    assert exited.value.code == 2
    assert f"--files: {path} {expected}" in capsys.readouterr().err
    from_id.assert_not_called()
    resolve_project.assert_not_awaited()


async def test_a_files_name_that_is_not_utf8_is_refused_with_escapes_before_any_request(
    tmp_path, capsys, resolve_project
):
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    lemma = tmp_path / "caf\udce9.lean"
    try:
        lemma.write_text("theorem t : True := trivial\n")
    except (OSError, UnicodeEncodeError):
        pytest.skip("this file system takes only UTF-8 names")

    with (
        patch("aristotlelib.cli.command.continue_.Project.from_id") as from_id,
        pytest.raises(SystemExit) as exited,
    ):
        action.preflight(
            action.parser.parse_args(["project-123", "Use this", "--project-dir", str(tmp_path), "--files", str(lemma)])
        )

    assert exited.value.code == 2
    assert f"--files: the name of {tmp_path.as_posix()}/caf\\xe9.lean is not UTF-8" in capsys.readouterr().err
    from_id.assert_not_called()
    resolve_project.assert_not_awaited()


@pytest.mark.skipif(os.name == "nt", reason="Windows names are UTF-16, never decoded with the locale")
@pytest.mark.parametrize(
    ("encoding", "name", "as_decoded"),
    [
        ("ascii", "naïve.lean", b"na\xc3\xafve.lean".decode("ascii", "surrogateescape")),
        ("latin-1", "café.lean", "café.lean"),
    ],
    ids=["UTF-8 bytes the locale can't decode", "text a Latin-1 locale decodes"],
)
async def test_a_files_name_that_can_go_up_is_not_refused_whatever_the_locale(
    encoding, name, as_decoded, tmp_path, monkeypatch, resolve_project, caplog
):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    (tmp_path / name).write_text("theorem t : True := trivial\n")
    as_decoded = tmp_path / as_decoded
    monkeypatch.setattr("aristotlelib.local_file_utils.sys.getfilesystemencoding", lambda: encoding)
    project = Mock(status=ProjectStatus.IDLE, project_id="project-123")
    project.ask = AsyncMock(return_value=Mock(spec=AgentTask, agent_task_id="task-456", project_id="project-123"))

    with patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=project):
        args = action.parser.parse_args(
            ["project-123", "Use this", "--project-dir", str(tmp_path), "--files", str(as_decoded)]
        )
        assert action.preflight(args) is None
        await action.run_action(args)

    project.ask.assert_awaited_once()
    # Named as it went up.
    assert f"Uploaded 1 file (28 bytes) with the prompt:\n  {name}" in caplog.messages


async def test_duplicate_upload_path_is_refused_before_any_request(tmp_path, monkeypatch, capsys, resolve_project):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_text("Notes\n")
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.continue_.Project.from_id") as from_id,
        pytest.raises(SystemExit) as exited,
    ):
        action.preflight(
            action.parser.parse_args(["project-123", "Use these", "--files", "notes.txt", str(tmp_path / "notes.txt")])
        )

    assert exited.value.code == 2
    assert "File path 'notes.txt' is selected more than once" in capsys.readouterr().err
    from_id.assert_not_called()
    resolve_project.assert_not_awaited()


def _write(root, paths: list[str]) -> None:
    for path in paths:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("x\n")


@pytest.mark.parametrize("relative", [True, False], ids=["traversal", "absolute"])
async def test_files_outside_the_current_folder_are_refused_before_any_request(
    relative, tmp_path, monkeypatch, capsys, resolve_project
):
    work = tmp_path / "work"
    _write(tmp_path, ["work/rtl/alu.sv", "defs.svh"])
    monkeypatch.chdir(work)
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    files = ["rtl/alu.sv", "../defs.svh" if relative else str(tmp_path / "defs.svh")]

    with (
        patch("aristotlelib.cli.command.continue_.Project.from_id") as from_id,
        pytest.raises(SystemExit) as exited,
    ):
        action.preflight(action.parser.parse_args(["project-123", "Use these", "--files", *files]))

    assert exited.value.code == 2
    assert ("must not contain .." if relative else "outside project directory") in capsys.readouterr().err
    from_id.assert_not_called()
    resolve_project.assert_not_awaited()


async def test_files_in_the_current_folder_get_no_warning(tmp_path, monkeypatch, caplog):
    _write(tmp_path, ["top.sv", "notes.txt"])
    monkeypatch.chdir(tmp_path)
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()

    action.preflight(
        action.parser.parse_args(["project-123", "Use these", "--files", "top.sv", str(tmp_path / "notes.txt")])
    )

    assert caplog.messages == []


@pytest.mark.parametrize("json_flag", [False, True], ids=["text", "json"])
async def test_files_sent_with_a_prompt_are_counted_sized_and_named_on_stderr(json_flag, tmp_path, caplog, capsys):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    (tmp_path / "reports").mkdir()
    timing = tmp_path / "reports" / "timing.rpt"
    timing.write_bytes(b"t" * 1500)
    area = tmp_path / "area.rpt"
    area.write_bytes(b"a" * 100)
    project = mockable_project()
    project.ask = AsyncMock(return_value=_queued_task())
    argv = [
        "project-123",
        "Use these",
        "--project-dir",
        str(tmp_path),
        "--files",
        str(timing),
        str(area),
        *(["--json"] if json_flag else []),
    ]

    with patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=project):
        assert await action.run_action(action.parser.parse_args(argv)) is None

    assert caplog.messages[:2] == [
        "Prompt submitted to project project-123, task task-456",
        "Uploaded 2 files (1.6 KB) with the prompt:\n  reports/timing.rpt\n  area.rpt",
    ]
    out = capsys.readouterr().out
    if json_flag:
        assert list(json.loads(out)) == [
            "project_id",
            "project",
            "task",
            "web_url",
            "property_counts",
            "requested_counts",
            "properties",
        ]
    else:
        assert out == ""


async def test_files_sent_with_a_prompt_are_named_up_to_ten_and_counted_past_them(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    parts = [tmp_path / "Proofs" / f"Part{i:02}.lean" for i in range(12)]
    (tmp_path / "Proofs").mkdir()
    for part in parts:
        part.write_bytes(bytes(1024))
    project = mockable_project()
    project.ask = AsyncMock(return_value=_queued_task())

    with patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=project):
        await action.run_action(
            action.parser.parse_args(
                ["project-123", "Use these", "--project-dir", str(tmp_path), "--files", *map(str, parts)]
            )
        )

    assert caplog.messages[1] == "\n".join(
        [
            "Uploaded 12 files (12.0 KB) with the prompt:",
            *[f"  Proofs/Part{i:02}.lean" for i in range(10)],
            "  and 2 more",
        ]
    )


async def test_a_file_gone_after_the_send_is_still_listed_at_the_size_it_went_up_with(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    lemma = tmp_path / "Lemma.lean"
    lemma.write_bytes(bytes(1536))
    project = mockable_project()

    async def send_then_remove(*args, **kwargs) -> AgentTask:
        lemma.unlink()
        return _queued_task()

    project.ask = AsyncMock(side_effect=send_then_remove)

    with patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=project):
        args = action.parser.parse_args(
            ["project-123", "Use this", "--project-dir", str(tmp_path), "--files", str(lemma)]
        )
        assert await action.run_action(args) is None

    assert caplog.messages[1] == "Uploaded 1 file (1.5 KB) with the prompt:\n  Lemma.lean"


@pytest.mark.parametrize("argv", [[], ["--files"]], ids=["no --files", "--files naming none"])
async def test_a_prompt_sent_without_files_lists_none(argv, caplog):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    project = mockable_project()
    project.ask = AsyncMock(return_value=_queued_task())

    with patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=project):
        await action.run_action(action.parser.parse_args(["project-123", "Go on", *argv]))

    assert caplog.messages[0] == "Prompt submitted to project project-123, task task-456"
    assert not [message for message in caplog.messages if message.startswith("Uploaded")]


async def test_files_whose_prompt_did_not_go_through_are_not_said_to_have_gone_up(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    lemma = tmp_path / "Lemma.lean"
    lemma.write_text("theorem t : True := trivial\n")
    project = mockable_project()
    project.ask = AsyncMock(side_effect=api_request.AristotleNotProcessedError("Not sent.", 503))

    with (
        patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=project),
        pytest.raises(api_request.AristotleNotProcessedError),
    ):
        await action.run_action(
            action.parser.parse_args(["project-123", "Use this", "--project-dir", str(tmp_path), "--files", str(lemma)])
        )

    assert caplog.messages == []


async def test_files_on_a_running_project_are_refused_before_the_upload(tmp_path, capsys):
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    lemma = tmp_path / "Lemma.lean"
    lemma.write_text("theorem t : True := trivial\n")
    running = Project(
        project_id="project-123",
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=None,
        status=ProjectStatus.RUNNING,
        has_input=True,
        has_files=False,
    )

    with (
        patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=running),
        patch.object(Project, "ask") as ask,
        pytest.raises(SystemExit) as exited,
    ):
        await action.run_action(
            action.parser.parse_args(["project-123", "Use this", "--project-dir", str(tmp_path), "--files", str(lemma)])
        )

    assert exited.value.code == 2
    assert capsys.readouterr().err.endswith(
        "error: Files can't be added to project project-123 while it runs a task. Send the prompt without --files to "
        "reach that task: its agent reads it right away. Or wait for the task to finish (`aristotle show project-123 "
        "--wait`), then send the prompt with --files. Cancelling the task first would lose the prompts it was given, "
        "except the project's first.\n"
    )
    ask.assert_not_called()


def _queued_task() -> AgentTask:
    return AgentTask(
        project_id="project-123",
        agent_task_id="task-456",
        status=TaskStatus.QUEUED,
        created_at=datetime(2026, 9, 1, 12, 0),
        last_updated_at=datetime(2026, 9, 1, 12, 0),
        percent_complete=None,
        file_name=None,
        description="Go on",
        output_summary=None,
    )


async def test_continue_json_prints_the_task_and_the_projects_properties(properties, capsys):
    properties.return_value = [
        _property("Spec.Holds", DeclarationStatus.PROVED).model_copy(update={"artifact_paths": ["sim/holds.vcd"]}),
        _property("Spec.Unfinished", DeclarationStatus.FAILED, DeclarationFailureReason.OUT_OF_BUDGET),
    ]
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    mock_project = mockable_project()
    mock_project.ask = AsyncMock(return_value=_queued_task())

    with patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=mock_project):
        assert await action.run_action(action.parser.parse_args(["project-123", "Go on", "--json"])) is None

    properties.assert_awaited_once_with("project-123", declaration_type=DeclarationType.PROPERTY)
    result = json.loads(capsys.readouterr().out)
    assert list(result) == [
        "project_id",
        "project",
        "task",
        "web_url",
        "property_counts",
        "requested_counts",
        "properties",
    ]
    assert result["project_id"] == result["project"]["project_id"] == "project-123"
    assert result["web_url"] == "https://aristotle.harmonic.fun/projects/project-123"
    assert result["task"]["agent_task_id"] == "task-456"
    assert result["task"]["status"] == "QUEUED"
    assert result["property_counts"] is None
    assert result["properties"] == [
        {
            "declaration_id": "id-Spec.Holds",
            "name": "Spec.Holds",
            "status": 3,
            "failure_reason": None,
            "unchecked": False,
            "status_name": "PROVED",
            "failure_reason_name": None,
            "source": 2,
            "source_name": "USER",
            "artifact_paths": ["sim/holds.vcd"],
            "outcome": "proved",
        },
        {
            "declaration_id": "id-Spec.Unfinished",
            "name": "Spec.Unfinished",
            "status": 6,
            "failure_reason": 1,
            "unchecked": False,
            "status_name": "FAILED",
            "failure_reason_name": "OUT_OF_BUDGET",
            "source": 2,
            "source_name": "USER",
            "artifact_paths": [],
            "outcome": "unresolved",
        },
    ]


async def test_continue_wait_json_waits_quietly_and_prints_the_finished_task(properties, capsys):
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    task = _queued_task()
    mock_project = mockable_project()
    mock_project.ask = AsyncMock(return_value=task)

    async def finish(
        num_events: int = 3,
        interactive: bool = True,
        stop_on_questions: bool = False,
        title: str | None = None,
        carry_on: str | None = None,
        resume: list[list[str]] | None = None,
    ) -> None:
        task.status = TaskStatus.COMPLETE_WITH_ERRORS

    async def listed(project_id: str, declaration_type: DeclarationType) -> list[Declaration]:
        assert task.status == TaskStatus.COMPLETE_WITH_ERRORS
        return [_property("Spec.Fails", DeclarationStatus.COUNTEREXAMPLE)]

    properties.side_effect = listed
    mock_project.has_files = True
    with (
        patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=mock_project),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)) as wait,
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", "Go on", "--wait", "--json"])) == 1

    wait.assert_awaited_once_with(interactive=False, stop_on_questions=False, title=ANY, carry_on=ANY, resume=ANY)
    # Broken off, it names the wait that carries on the same way: through questions, printing JSON.
    assert "aristotle show project-123 --task task-456 --wait --json" in wait.await_args.kwargs["carry_on"]
    # Ended badly, it names the follow-up that waits the same way.
    assert wait.await_args.kwargs["resume"] == [
        ["aristotle", "continue", "project-123", "Continue", "--wait", "--json"]
    ]
    result = json.loads(capsys.readouterr().out)
    assert result["task"]["status"] == "COMPLETE_WITH_ERRORS"
    assert result["property_counts"]["counterexample"] == 1
    assert [p["status"] for p in result["properties"]] == [5]


async def test_continue_json_properties_are_null_when_they_cannot_be_fetched(properties, capsys):
    properties.side_effect = AristotleAPIError("bad gateway", status_code=502)
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    mock_project = mockable_project()
    mock_project.ask = AsyncMock(return_value=_queued_task())

    with patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=mock_project):
        assert await action.run_action(action.parser.parse_args(["project-123", "Go on", "--json"])) is None

    result = json.loads(capsys.readouterr().out)
    assert result["task"]["status"] == "QUEUED"
    assert (result["property_counts"], result["properties"]) == (None, None)


async def test_non_interactive_continue_stops_for_questions_and_exits_10(properties, capsys):
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    task = _queued_task()
    question = Question(
        question_id="q-1",
        project_id="project-123",
        agent_run_id="task-456",
        content="Which clock?",
        created_at=datetime(2026, 9, 1, 12, 1),
        updated_at=datetime(2026, 9, 1, 12, 1),
    )
    mock_project = Mock(spec=Project, description=None)
    mock_project.ask = AsyncMock(return_value=task)

    with (
        patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=mock_project),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[question])) as wait,
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", "Go on", "--non-interactive"])) == 10

    assert mock_project.ask.call_args.kwargs["agent_questions_setting"] == AgentQuestionsSetting.TIMEOUT_15_MIN
    wait.assert_awaited_once_with(interactive=False, stop_on_questions=True, title=ANY, carry_on=ANY, resume=ANY)
    assert "aristotle show project-123 --task task-456 --non-interactive" in wait.await_args.kwargs["carry_on"]
    assert wait.await_args.kwargs["resume"] == [
        ["aristotle", "continue", "project-123", "Continue", "--non-interactive"]
    ]
    properties.assert_not_awaited()
    assert [q["question_id"] for q in json.loads(capsys.readouterr().out)["questions"]] == ["q-1"]


async def test_non_interactive_continue_without_questions_prints_the_finished_task(capsys):
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    task = _queued_task()
    mock_project = mockable_project()
    mock_project.has_files = True
    mock_project.ask = AsyncMock(return_value=task)

    async def finish(
        num_events: int = 3,
        interactive: bool = True,
        stop_on_questions: bool = False,
        title: str | None = None,
        carry_on: str | None = None,
        resume: list[list[str]] | None = None,
    ) -> list:
        task.status = TaskStatus.OUT_OF_BUDGET
        return []

    with (
        patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=mock_project),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)),
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", "Go on", "--non-interactive"])) == 1

    assert json.loads(capsys.readouterr().out)["task"]["status"] == "OUT_OF_BUDGET"


DOWNLOAD_HINT = "Get the project's files with:\n  aristotle download project-123"


async def _continue_and_wait(argv: list[str], status_after: TaskStatus, has_files: bool = True) -> tuple[int, Mock]:
    """Run ``continue project-123 "Go on" --wait`` plus argv on an idle project whose task ends in status_after."""
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    task = _queued_task()
    mock_project = mockable_project(has_files=has_files)
    mock_project.ask = AsyncMock(return_value=task)

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
        patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=mock_project) as from_id,
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)),
    ):
        code = await action.run_action(action.parser.parse_args(["project-123", "Go on", "--wait", *argv]))
    return code, from_id


async def test_continue_wait_ends_with_the_property_counts_and_how_to_get_the_files(properties, caplog, capsys):
    caplog.set_level(logging.INFO)
    properties.return_value = [
        _property("Spec.Holds", DeclarationStatus.PROVED),
        _property("Spec.Broken", DeclarationStatus.FAILED, DeclarationFailureReason.PROOF_CHECK_FAILED),
    ]

    code, from_id = await _continue_and_wait([], TaskStatus.COMPLETE)

    assert code == 0
    assert from_id.await_count == 2
    assert caplog.messages[-3:] == [
        "Properties: 1 proved · 0 disproved · 0 in progress · 1 unresolved",
        "The proof of 1 property no longer checks, so it isn't verified; re-run to re-prove it.",
        DOWNLOAD_HINT,
    ]
    assert capsys.readouterr().out == ""


async def test_continue_wait_counts_the_proofs_a_task_out_of_budget_left_unchecked(properties, caplog):
    caplog.set_level(logging.INFO)
    properties.return_value = [_property("Spec.Holds", DeclarationStatus.PROVED)]

    code, _ = await _continue_and_wait([], TaskStatus.OUT_OF_BUDGET)

    assert code == 1
    assert "Properties: 0 proved · 0 disproved · 0 in progress · 1 unchecked" in caplog.messages


@pytest.mark.parametrize(
    ("status", "has_files"),
    [(TaskStatus.IN_PROGRESS, True), (TaskStatus.COMPLETE, False)],
    ids=["wait broke off", "no files"],
)
async def test_continue_wait_names_no_download_while_the_task_runs_or_when_there_are_no_files(
    status, has_files, properties, caplog
):
    caplog.set_level(logging.INFO)
    properties.return_value = [_property("Spec.Holds", DeclarationStatus.PROVED)]

    await _continue_and_wait([], status, has_files=has_files)

    assert caplog.messages[-1] == "Properties: 1 proved · 0 disproved · 0 in progress"


async def test_continue_wait_json_says_how_to_get_the_files_on_stderr_only(properties, caplog, capsys):
    caplog.set_level(logging.INFO)
    properties.return_value = [_property("Spec.Holds", DeclarationStatus.PROVED)]

    code, _ = await _continue_and_wait(["--json"], TaskStatus.COMPLETE)

    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["property_counts"]["proved"] == 1
    assert caplog.messages[-1] == DOWNLOAD_HINT
    assert not [message for message in caplog.messages if message.startswith("Properties:")]


@pytest.mark.usefixtures("frozen_api_clock")
def test_a_continue_that_no_attempt_got_through_exits_3_with_the_error_as_json(api_server, caplog, capsys):
    project_id = "3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f"
    project = Project(
        project_id=project_id,
        status=ProjectStatus.IDLE,
        created_at=datetime(2026, 9, 1, 12, 0),
        last_updated=datetime(2026, 9, 1, 12, 0),
        description=None,
        has_input=True,
        has_files=True,
    )
    refused = [httpx.ConnectError("[Errno 111] Connection refused")] * api_request.RETRY_ATTEMPTS
    asked = api_server(httpx.Response(200, json=project.model_dump(mode="json")), *refused)

    with patch("sys.argv", ["aristotle", "continue", project_id, "Go on", "--json"]):
        assert main() == 3

    assert [request.method for request in asked] == ["GET"] + ["POST"] * api_request.RETRY_ATTEMPTS
    error = (
        f"ConnectError on POST /project/{project_id}/ask after 5 attempts over 0 s: [Errno 111] Connection refused. "
        "Aristotle didn't act on it. The prompt was not sent, so it is safe to send it again."
    )
    assert caplog.messages[-1] == error
    assert json.loads(capsys.readouterr().out) == {"outcome": "not_processed", "error": error}


@pytest.mark.parametrize("flags", [["--json"], ["--non-interactive"], []], ids=["--json", "--non-interactive", "text"])
def test_a_continue_refused_for_too_many_runs_exits_6_and_says_the_prompt_was_not_sent(
    flags, api_server, caplog, capsys
):
    project_id = "3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f"
    project = mockable_project(project_id)
    refused = httpx.Response(429, json={"detail": "Too many inflight requests. Please wait for them to finish."})
    requests = api_server(httpx.Response(200, json=project.model_dump(mode="json")), refused)

    with patch("sys.argv", ["aristotle", "continue", project_id, "Go on", *flags]):
        assert main() == 6

    assert [request.method for request in requests] == ["GET", "POST"]
    error = f"{api_request.PROJECT_LIMIT_FALLBACK} The prompt was not sent, so it is safe to send it again."
    assert caplog.messages[-1] == error
    out = capsys.readouterr().out
    assert (json.loads(out) if flags else out) == ({"outcome": "not_processed", "error": error} if flags else "")


@pytest.mark.usefixtures("frozen_api_clock", "project_lookup")
@pytest.mark.parametrize(("failure", "code", "message"), LOOKUP_FAILURES)
@pytest.mark.parametrize(
    ("reference", "request_line"),
    [
        ("3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f", "GET /project/3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f"),
        ("FIFO overflow", "GET /project"),
    ],
    ids=["by id", "by name"],
)
@pytest.mark.parametrize("json_flag", [True, False], ids=["json", "text"])
def test_a_continue_whose_lookup_failed_says_the_prompt_was_not_sent(
    failure, code, message, reference, request_line, json_flag, api_server, caplog, capsys
):
    requests = api_server(*[failure] * api_request.RETRY_ATTEMPTS)

    with patch("sys.argv", ["aristotle", "continue", reference, "Go on", *(["--json"] if json_flag else [])]):
        assert main() == code

    assert [request.method for request in requests] == ["GET"] * api_request.RETRY_ATTEMPTS
    error = f"{message.format(request=request_line)} The prompt was not sent, so it is safe to send it again."
    assert caplog.messages[-1] == error
    out = capsys.readouterr().out
    if json_flag:
        assert json.loads(out) == {"outcome": "not_processed", "error": error}
    else:
        assert out == ""


def test_a_continue_to_a_project_not_found_is_no_outcome(api_server, caplog, capsys):
    project_id = "3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f"
    api_server(httpx.Response(404, json={"detail": "Not found"}))

    with patch("sys.argv", ["aristotle", "continue", project_id, "Go on", "--json"]):
        assert main() == 4

    assert caplog.messages[-1] == f"Project {project_id} not found or not accessible."
    assert capsys.readouterr().out == ""


def test_a_continue_whose_response_is_lost_exits_7_with_the_error_as_json(api_server, caplog, capsys):
    project_id = "3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f"
    project = Project(
        project_id=project_id,
        status=ProjectStatus.IDLE,
        created_at=datetime(2026, 9, 1, 12, 0),
        last_updated=datetime(2026, 9, 1, 12, 0),
        description=None,
        has_input=True,
        has_files=True,
    )
    asked = api_server(
        httpx.Response(200, json=project.model_dump(mode="json")),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
    )

    with patch("sys.argv", ["aristotle", "continue", project_id, "Go on", "--json"]):
        assert main() == 7

    assert [request.method for request in asked] == ["GET", "POST"]
    error = (
        f"RemoteProtocolError on POST /project/{project_id}/ask: Server disconnected without sending a response. "
        "Aristotle may have acted on it, so it was not sent again. If it did, the prompt was sent, though its task may "
        f"take a few minutes to show up: check `aristotle tasks {project_id}` before sending it again."
    )
    assert caplog.messages[-1] == error
    assert json.loads(capsys.readouterr().out) == {"outcome": "unknown", "error": error}


@pytest.mark.parametrize(
    ("argv", "setting", "hint"),
    [
        ([], AgentQuestionsSetting.TIMEOUT_15_MIN, True),
        (["--no-questions"], AgentQuestionsSetting.DISABLED, False),
        (["--json"], AgentQuestionsSetting.TIMEOUT_15_MIN, True),
    ],
)
async def test_a_continue_that_is_not_waited_on_can_be_asked_and_says_how_to_answer(argv, setting, hint, caplog):
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    mock_project = mockable_project()
    mock_project.status = ProjectStatus.IDLE
    mock_project.ask = AsyncMock(return_value=_queued_task())

    with (
        patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=mock_project),
        caplog.at_level(logging.INFO),
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", "Go on", *argv])) is None

    assert mock_project.ask.call_args.kwargs["agent_questions_setting"] == setting
    answer_hint = "If Aristotle asks anything, answer with:\n  aristotle questions project-123"
    assert (answer_hint in caplog.messages) is hint


async def test_no_questions_holds_for_a_waited_continue_too():
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    task = _queued_task()
    mock_project = Mock(spec=Project, description=None)
    mock_project.status = ProjectStatus.IDLE
    mock_project.ask = AsyncMock(return_value=task)

    with (
        patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=mock_project),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[])),
    ):
        await action.run_action(action.parser.parse_args(["project-123", "Go on", "--wait", "--no-questions"]))

    assert mock_project.ask.call_args.kwargs["agent_questions_setting"] == AgentQuestionsSetting.DISABLED


async def test_no_questions_on_a_running_project_warns_that_the_running_task_keeps_its_setting(caplog):
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    mock_project = Mock(spec=Project, description=None)
    mock_project.status = ProjectStatus.RUNNING
    mock_project.ask = AsyncMock(return_value=_queued_task())

    with (
        patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=mock_project),
        patch("aristotlelib.cli.command.continue_.Question.list_for_task", AsyncMock(return_value=[])),
        caplog.at_level(logging.INFO),
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", "Go on", "--no-questions"])) is None

    [warning] = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert warning.message == (
        "WARNING: --no-questions applies only when the prompt starts a new task; the task running on project "
        "project-123 keeps the setting it started with."
    )
    assert "If Aristotle asks anything, answer with:\n  aristotle questions project-123" in caplog.messages


async def test_an_interrupted_continue_wait_json_still_prints_the_task_as_it_last_stood(properties, caplog, capsys):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    task = _queued_task()
    mock_project = mockable_project(description="fifo proofs")
    mock_project.ask = AsyncMock(return_value=task)

    with (
        patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=mock_project),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=KeyboardInterrupt)) as wait,
        pytest.raises(KeyboardInterrupt),
    ):
        await action.run_action(action.parser.parse_args(["project-123", "Go on", "--wait", "--json"]))

    assert wait.await_args.kwargs["title"] == "fifo proofs"
    assert f"Prompt submitted to project project-123, task {task.agent_task_id}" in caplog.messages
    result = json.loads(capsys.readouterr().out)
    assert result["task"]["agent_task_id"] == task.agent_task_id
    assert result["property_counts"] is None and result["properties"] is None
    properties.assert_not_awaited()
    # Idle when it was fetched, it runs the task the prompt started, which says so without another request.
    assert (result["task"]["status"], result["project"]["status_name"]) == ("QUEUED", "RUNNING")
    assert result["project"]["latest_run_status"] == "QUEUED"
    mock_project.refresh.assert_not_awaited()


async def test_continue_json_prints_the_project_as_it_stands_once_the_prompt_went_in(properties, capsys):
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    project = mockable_project()

    async def refresh() -> None:
        project.status = ProjectStatus.RUNNING

    project.refresh = AsyncMock(side_effect=refresh)
    project.ask = AsyncMock(return_value=_queued_task())

    with patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=project):
        assert await action.run_action(action.parser.parse_args(["project-123", "Go on", "--json"])) is None

    result = json.loads(capsys.readouterr().out)
    # Idle when the prompt went in, which started a task.
    assert (result["project"]["status_name"], result["task"]["status"]) == ("RUNNING", "QUEUED")


def _waited_on(project, task: AgentTask, status_after: TaskStatus):
    """Patches for a `continue --wait` on ``project`` whose prompt went to ``task``, which ends in status_after."""

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

    project.ask = AsyncMock(return_value=task)
    return (
        patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=project),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)),
    )


async def test_continue_wait_json_prints_the_project_as_it_stands_once_the_wait_ends(properties, capsys):
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    project = mockable_project(status=ProjectStatus.RUNNING)

    async def refresh() -> None:
        project.status = ProjectStatus.IDLE

    project.refresh = AsyncMock(side_effect=refresh)
    found, waited = _waited_on(project, _queued_task(), TaskStatus.COMPLETE)

    with found, waited:
        assert await action.run_action(action.parser.parse_args(["project-123", "Go on", "--wait", "--json"])) == 0

    result = json.loads(capsys.readouterr().out)
    assert (result["task"]["status"], result["project"]["status_name"]) == ("COMPLETE", "IDLE")


async def test_an_interrupt_during_the_refresh_after_a_continue_wait_still_prints_the_task(properties, capsys):
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    project = mockable_project(status=ProjectStatus.RUNNING)
    project.refresh = AsyncMock(side_effect=KeyboardInterrupt)
    found, waited = _waited_on(project, _queued_task(), TaskStatus.COMPLETE)

    with found, waited, pytest.raises(KeyboardInterrupt):
        await action.run_action(action.parser.parse_args(["project-123", "Go on", "--wait", "--json"]))

    out = capsys.readouterr().out
    result = json.loads(out)
    # As for any wait cut short: the task as it last stood, no counts, and the project's run as the task has it.
    assert (result["task"]["status"], result["property_counts"]) == ("COMPLETE", None)
    assert (result["project"]["status_name"], result["project"]["latest_run_status"]) == ("IDLE", "COMPLETE")
    assert out.count('"web_url"') == 1


async def test_a_project_that_cant_be_refreshed_after_the_prompt_still_has_its_run_as_the_task_has_it(
    properties, capsys
):
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    project = mockable_project()
    project.latest_run_status = TaskStatus.COMPLETE
    project.refresh = AsyncMock(side_effect=AristotleAPIError("unavailable", status_code=503))
    project.ask = AsyncMock(return_value=_queued_task())

    with patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=project):
        await action.run_action(action.parser.parse_args(["project-123", "Go on", "--json"]))

    result = json.loads(capsys.readouterr().out)
    # Not idle with the run before's status beside the task the prompt started.
    assert (result["project"]["status_name"], result["project"]["latest_run_status"]) == ("RUNNING", "QUEUED")


@pytest.mark.parametrize("status_code", [503, 429], ids=["server error", "rate limited"])
async def test_continue_wait_json_nulls_the_counts_and_exits_3_when_the_tasks_cannot_be_listed(
    status_code, properties, capsys, caplog
):
    # Exit 3 even for a 429: the task has finished, and exit 6 says to retry, which would send the prompt again.
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    properties.return_value = [
        _property("Spec.Holds", DeclarationStatus.PROVED).model_copy(
            update={"last_updated": datetime(2026, 9, 1, 11, 0)}
        )
    ]
    project = mockable_project(has_files=False)
    project.get_tasks = AsyncMock(side_effect=AristotleAPIError("Service unavailable", status_code=status_code))
    found, waited = _waited_on(project, _queued_task(), TaskStatus.COMPLETE)

    with found, waited:
        code = await action.run_action(action.parser.parse_args(["project-123", "Go on", "--wait", "--json"]))

    assert code == 3
    result = json.loads(capsys.readouterr().out)
    assert result["task"]["status"] == "COMPLETE"
    # Every count and the properties are null; the rest of the result stands.
    assert (result["property_counts"], result["requested_counts"], result["properties"]) == (None, None, None)
    assert result["project"]["project_id"] == "project-123"
    assert "aristotle show project-123 --json" in caplog.text


async def test_continue_json_without_wait_nulls_properties_it_cannot_judge_and_still_exits_0(
    properties, capsys, caplog
):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    properties.return_value = [
        _property("Spec.Holds", DeclarationStatus.PROVED).model_copy(
            update={"last_updated": datetime(2026, 9, 1, 11, 0)}
        )
    ]
    project = mockable_project()
    project.ask = AsyncMock(return_value=_queued_task())
    project.get_tasks = AsyncMock(side_effect=AristotleAPIError("Service unavailable", status_code=503))

    with patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=project):
        code = await action.run_action(action.parser.parse_args(["project-123", "Go on", "--json"]))

    assert code is None
    result = json.loads(capsys.readouterr().out)
    assert (result["property_counts"], result["requested_counts"], result["properties"]) == (None, None, None)
    assert any(
        r.levelno == logging.WARNING and "aristotle show project-123 --json" in r.getMessage() for r in caplog.records
    )


@pytest.mark.parametrize("argv", [["--files", "lemma.lean", "Prove it"], ["--wait", "--files", "Prove it"]])
async def test_a_prompt_after_files_is_a_usage_error_that_says_to_give_it_first(
    argv, tmp_path, monkeypatch, capsys, resolve_project
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "lemma.lean").write_text("theorem t : True := trivial\n")
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()

    with pytest.raises(SystemExit) as exited:
        action.preflight(action.parser.parse_args(["project-123", *argv]))

    assert exited.value.code == 2
    resolve_project.assert_not_awaited()
    assert capsys.readouterr().err.endswith(
        "error: --files took 'Prove it' as a file, so no prompt was given. Give the prompt before --files, which "
        "takes every argument after it.\n"
    )


async def test_files_without_a_prompt_still_ask_for_one(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "lemma.lean").write_text("theorem t : True := trivial\n")
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()

    with pytest.raises(SystemExit) as exited:
        action.preflight(action.parser.parse_args(["project-123", "--files", "lemma.lean"]))

    assert exited.value.code == 2
    assert capsys.readouterr().err.endswith("error: a prompt is required, as an argument or with --prompt-file\n")


def _asked(response: str | None = None) -> Question:
    return Question(
        question_id="q-1",
        project_id="project-123",
        agent_run_id="task-456",
        content="Which clock?",
        response=response,
        created_at=datetime(2026, 9, 1, 12, 1),
        updated_at=datetime(2026, 9, 1, 12, 1),
    )


WAITING = "Aristotle is waiting for answers. Answer with:\n  aristotle questions project-123"
IF_ASKED = "If Aristotle asks anything, answer with:\n  aristotle questions project-123"


@pytest.mark.parametrize(
    ("listed", "hint"),
    [
        (AsyncMock(return_value=[_asked()]), WAITING),
        (AsyncMock(return_value=[_asked(response="Active low")]), IF_ASKED),
        (AsyncMock(side_effect=AristotleAPIError("Internal Server Error", 500)), IF_ASKED),
    ],
    ids=["a question pending", "every question answered", "the listing fails"],
)
async def test_a_prompt_to_a_running_task_says_whether_aristotle_is_waiting_for_answers(listed, hint, caplog):
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    project = mockable_project(status=ProjectStatus.RUNNING)
    project.ask = AsyncMock(return_value=_queued_task())

    with (
        patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=project),
        patch("aristotlelib.cli.command.continue_.Question.list_for_task", listed),
        caplog.at_level(logging.INFO),
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", "Go on"])) is None

    listed.assert_awaited_once_with("task-456", run_live=True)
    assert caplog.messages[-1] == hint


async def test_a_prompt_to_an_idle_project_lists_no_questions(caplog):
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    project = mockable_project()
    project.ask = AsyncMock(return_value=_queued_task())

    with (
        patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=project),
        patch("aristotlelib.cli.command.continue_.Question.list_for_task") as listed,
        caplog.at_level(logging.INFO),
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", "Go on"])) is None

    listed.assert_not_called()
    assert caplog.messages[-1] == IF_ASKED


async def test_a_questions_listing_that_outlasts_its_time_gives_the_general_hint(caplog, monkeypatch):
    monkeypatch.setattr("aristotlelib.cli.command.continue_.QUESTIONS_HINT_SECONDS", 0.01)
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    project = mockable_project(status=ProjectStatus.RUNNING)
    project.ask = AsyncMock(return_value=_queued_task())

    async def slow(agent_task_id: str, run_live: bool | None = None) -> list[Question]:
        await asyncio.sleep(5)
        return [_asked()]

    with (
        patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=project),
        patch("aristotlelib.cli.command.continue_.Question.list_for_task", side_effect=slow),
        caplog.at_level(logging.INFO),
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", "Go on"])) is None

    assert caplog.messages[-1] == IF_ASKED


async def test_a_failing_questions_listing_retries_quietly_and_gives_the_general_hint(api_server, caplog):
    requests = api_server(*[httpx.Response(503)] * api_request.RETRY_ATTEMPTS)
    parser = argparse.ArgumentParser()
    action = ContinueAction(parser.add_subparsers())
    action.add_action_arguments()
    project = mockable_project(status=ProjectStatus.RUNNING)
    project.ask = AsyncMock(return_value=_queued_task())

    with (
        patch("aristotlelib.cli.command.continue_.Project.from_id", return_value=project),
        caplog.at_level(logging.DEBUG),
    ):
        assert await action.run_action(action.parser.parse_args(["project-123", "Go on"])) is None

    assert len(requests) == api_request.RETRY_ATTEMPTS
    assert all(request.url.path.endswith("/task/task-456/questions") for request in requests)
    assert [r.message for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert any(r.message.startswith("Retrying in") for r in caplog.records if r.levelno == logging.DEBUG)
    assert caplog.messages[-1] == IF_ASKED
