"""Tests for aristotlelib.cli.continue_ module."""

import argparse
import asyncio
import io
import json
import logging
import os
import re
import sys
import tarfile
from datetime import datetime
from pathlib import Path
from unittest.mock import ANY, AsyncMock, Mock, patch

import httpx
import pytest

from aristotlelib import api_request, local_file_utils
from aristotlelib.agent_task import AgentTask, TaskStatus
from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.command.continue_ import ContinueAction
from aristotlelib.cli.controller import main
from aristotlelib.declaration import (
    Declaration,
    DeclarationEvidence,
    DeclarationFailureReason,
    DeclarationSource,
    DeclarationStatus,
    DeclarationType,
)
from aristotlelib.project import ARCHIVE_FILES_HEADER, AgentQuestionsSetting, Project, ProjectStatus
from aristotlelib.question import Question
from tests.cli.conftest import LOOKUP_FAILURES, mockable_project
from tests.conftest import BUY_CREDITS, OBJECT_REFUSALS, OUT_OF_CREDITS, SIGNUP_GRANT_PENDING, multipart_parts


@pytest.fixture(autouse=True)
def properties():
    """Patch the properties fetch a JSON result makes; empty by default."""
    with patch("aristotlelib.cli.api_action.Declaration.list_all_for_project", return_value=[]) as mock:
        yield mock


def _property(
    name: str,
    status: DeclarationStatus,
    reason: DeclarationFailureReason | None = None,
    *,
    evidence: DeclarationEvidence | None = None,
) -> Declaration:
    return Declaration(
        project_id="project-123",
        declaration_id=f"id-{name}",
        declaration_type=DeclarationType.PROPERTY,
        source=DeclarationSource.USER,
        name=name,
        status=status,
        evidence=evidence,
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
        agent_questions_setting=AgentQuestionsSetting.TIMEOUT_15_MIN,
        project_dir=None,
        send_folder=False,
        include_ignored=False,
        block=None,
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
        agent_questions_setting=AgentQuestionsSetting.TIMEOUT_15_MIN,
        project_dir=None,
        send_folder=False,
        include_ignored=False,
        block=None,
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
        agent_questions_setting=AgentQuestionsSetting.TIMEOUT_15_MIN,
        project_dir=None,
        send_folder=False,
        include_ignored=False,
        block=None,
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
            "files_applied",
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
        "error: Files can't be added to project project-123 while it runs a task. Send the prompt without --files and "
        "--project-dir to reach that task: its agent reads it right away. Or wait for the task to finish (`aristotle "
        "show project-123 --wait`), then send the prompt with --files. Cancelling the task first would lose the "
        "prompts it was given, except the project's first.\n"
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
        "files_applied",
        "property_counts",
        "requested_counts",
        "properties",
    ]
    assert result["project_id"] == result["project"]["project_id"] == "project-123"
    assert result["files_applied"] is None
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
            "evidence": None,
            "evidence_message": None,
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
            "evidence": None,
            "evidence_message": None,
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
        return [_property("Spec.Fails", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE)]

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
    assert result["property_counts"]["refuted"] == 1
    assert [p["status"] for p in result["properties"]] == [2]


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
        "Properties: 1 proved · 0 refuted · 0 in progress · 1 unresolved",
        "The proof of 1 property no longer checks, so it isn't verified; re-run to re-prove it.",
        DOWNLOAD_HINT,
    ]
    assert capsys.readouterr().out == ""


async def test_continue_wait_counts_the_proofs_a_task_out_of_budget_left_unchecked(properties, caplog):
    caplog.set_level(logging.INFO)
    properties.return_value = [_property("Spec.Holds", DeclarationStatus.PROVED)]

    code, _ = await _continue_and_wait([], TaskStatus.OUT_OF_BUDGET)

    assert code == 1
    assert "Properties: 0 proved · 0 refuted · 0 in progress · 1 unchecked" in caplog.messages


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

    assert caplog.messages[-1] == "Properties: 1 proved · 0 refuted · 0 in progress"


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


@pytest.mark.parametrize(
    ("detail", "status", "reason", "code"),
    [
        (OUT_OF_CREDITS, 402, f"{OUT_OF_CREDITS['message']} {BUY_CREDITS}", 6),
        (SIGNUP_GRANT_PENDING, 409, SIGNUP_GRANT_PENDING["message"], 3),
    ],
    ids=["out of credits", "still setting up"],
)
@pytest.mark.parametrize("flags", [["--json"], []], ids=["--json", "text"])
def test_a_continue_billing_refuses_says_the_prompt_was_not_sent(
    detail, status, reason, code, flags, api_server, caplog, capsys
):
    project_id = "3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f"
    project = mockable_project(project_id)
    refused = httpx.Response(status, json={"detail": detail})
    requests = api_server(httpx.Response(200, json=project.model_dump(mode="json")), refused)

    with patch("sys.argv", ["aristotle", "continue", project_id, "Go on", *flags]):
        assert main() == code

    assert [request.method for request in requests] == ["GET", "POST"]
    error = f"{reason} The prompt was not sent, so it is safe to send it again."
    assert caplog.messages[-1] == error
    out = capsys.readouterr().out
    assert (json.loads(out) if flags else out) == ({"outcome": "not_processed", "error": error} if flags else "")


@pytest.mark.parametrize(
    "detail",
    ["Forbidden", "Cannot continue a project owned by a different organization"],
    ids=["someone else's project", "a project of another organization"],
)
def test_a_continue_refused_for_its_project_says_why_and_exits_4(detail, api_server, caplog):
    project_id = "3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f"
    project = mockable_project(project_id)
    refused = httpx.Response(403, json={"detail": detail})
    requests = api_server(httpx.Response(200, json=project.model_dump(mode="json")), refused)

    with patch("sys.argv", ["aristotle", "continue", project_id, "Go on"]):
        assert main() == 4

    assert [request.method for request in requests] == ["GET", "POST"]
    assert caplog.messages[-1] == OBJECT_REFUSALS[detail]


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


FOLDER_PROJECT = "3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f"
FOLDER_TASK = "5d6e7f80-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
NOT_APPLIED = (
    f"Aristotle didn't apply {{folder}} to project {FOLDER_PROJECT}: the server doesn't take a folder with a follow-up, "
    f"so task {FOLDER_TASK} started without the folder's files. Cancel it with:\n  aristotle cancel {FOLDER_TASK}"
)


def _folder_project(status: ProjectStatus = ProjectStatus.IDLE) -> httpx.Response:
    project = Project(
        project_id=FOLDER_PROJECT,
        status=status,
        created_at=datetime(2026, 9, 1, 12, 0),
        last_updated=datetime(2026, 9, 1, 12, 0),
        description=None,
        has_input=True,
        has_files=True,
    )
    return httpx.Response(200, json=project.model_dump(mode="json"))


def _follow_up(applied: str | None) -> httpx.Response:
    """A follow-up's answer: its task, and ARCHIVE_FILES_HEADER at ``applied`` unless None, as an older server answers."""
    task = _queued_task().model_copy(update={"project_id": FOLDER_PROJECT, "agent_task_id": FOLDER_TASK})
    headers = {} if applied is None else {ARCHIVE_FILES_HEADER: applied}
    return httpx.Response(200, json=task.model_dump(mode="json"), headers=headers)


def _archive_in(request: httpx.Request, field: str = "archive") -> dict[str, bytes | None]:
    """The members of the archive ``request`` sent as ``field``: each file's content, or None for any other member."""
    (archive,) = [content for name, _, _, content in multipart_parts(request) if name == field]
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz", encoding="utf-8") as tar:
        return {m.name: (f.read() if (f := tar.extractfile(m)) else None) for m in tar.getmembers()}


def _rtl(tmp_path: Path) -> Path:
    """A folder whose fifo.v, reports/timing.rpt and .gitignore go up, and whose build.log, which an ignore rule
    matches, .env and earlier result archive stay out."""
    rtl = tmp_path / "rtl"
    (rtl / "reports").mkdir(parents=True)
    (rtl / "fifo.v").write_bytes(b"module fifo; endmodule\n")
    (rtl / "reports" / "timing.rpt").write_bytes(b"slack 0.12\n")
    (rtl / ".gitignore").write_bytes(b"*.log\n")
    (rtl / "build.log").write_bytes(b"built\n")
    (rtl / ".env").write_bytes(b"TOKEN=never-sent\n")
    (rtl / f"{FOLDER_PROJECT}-aristotle.tar.gz").write_bytes(b"an earlier result")
    return rtl


def _continue(*argv: str) -> int | None:
    with patch("sys.argv", ["aristotle", "continue", FOLDER_PROJECT, *argv]):
        return main()


def test_a_folder_goes_up_with_the_prompt_as_submit_would_upload_it(tmp_path, api_server):
    rtl = _rtl(tmp_path)
    (tmp_path / "outside.v").write_bytes(b"module outside; endmodule\n")
    try:
        (rtl / "fifo_link.v").symlink_to("fifo.v")
        (rtl / "outside_link.v").symlink_to(tmp_path / "outside.v")
        links = True
    except OSError:
        links = False
    requests = api_server(_folder_project(), _folder_project(), _folder_project(), _follow_up("4" if links else "3"))

    with patch("sys.argv", ["aristotle", "submit", "Prove it", "--project-dir", str(rtl)]):
        assert main() == 0
    assert _continue("Use the new reports", "--project-dir", str(rtl), "--send-folder") == 0

    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/api/v3/project"),
        ("GET", f"/api/v3/project/{FOLDER_PROJECT}"),
        ("GET", f"/api/v3/project/{FOLDER_PROJECT}"),
        ("POST", f"/api/v3/project/{FOLDER_PROJECT}/ask"),
    ]
    submitted = _archive_in(requests[0], field="input")
    assert _archive_in(requests[3]) == submitted
    # A link inside the folder goes up as its file's content; one leading out of it stays out.
    expected = [".gitignore", "fifo.v", *(["fifo_link.v"] if links else []), "reports/timing.rpt"]
    assert sorted(submitted) == expected
    assert [part[1:3] for part in multipart_parts(requests[3]) if part[0] == "archive"] == [
        ("rtl.tar.gz", "application/gzip")
    ]


def test_a_folders_hard_links_go_up_as_files_of_their_own(tmp_path, api_server):
    rtl = _rtl(tmp_path)
    try:
        os.link(rtl / "fifo.v", rtl / "fifo_copy.v")
    except (AttributeError, OSError):
        pytest.skip("this platform cannot create hard links")
    try:
        (rtl / "fifo_link.v").symlink_to("fifo.v")
        linked = ["fifo_link.v"]
    except OSError:
        linked = []
    requests = api_server(_folder_project(), _folder_project(), _follow_up(str(4 + len(linked))))

    assert _continue("Use the new reports", "--project-dir", str(rtl), "--send-folder") == 0

    (archive,) = [content for name, _, _, content in multipart_parts(requests[2]) if name == "archive"]
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        # The server applies a hard link under its first name only, so each name holds its own copy.
        assert {m.name: (m.isreg(), m.size) for m in tar.getmembers() if m.name.startswith("fifo")} == {
            name: (True, len(b"module fifo; endmodule\n")) for name in ["fifo.v", "fifo_copy.v", *linked]
        }


@pytest.mark.parametrize("json_flag", [False, True], ids=["text", "json"])
def test_the_count_aristotle_applied_is_said_and_given_as_files_applied(
    json_flag, tmp_path, api_server, caplog, capsys
):
    caplog.set_level(logging.INFO)
    rtl = _rtl(tmp_path)
    # The project, its check that it is idle, the follow-up, and with --json its refresh before the result.
    api_server(_folder_project(), _folder_project(), _follow_up("3"), _folder_project())

    assert (
        _continue("Use the new reports", "--project-dir", str(rtl), "--send-folder", *(["--json"] if json_flag else []))
        == 0
    )

    assert caplog.messages[-4:] == [
        "Uploading 3 files; 1 left out because it matches an ignore rule.",
        f"Prompt submitted to project {FOLDER_PROJECT}, task {FOLDER_TASK}",
        f"Aristotle applied 3 files from {rtl} to the project.",
        f"If Aristotle asks anything, answer with:\n  aristotle questions {FOLDER_PROJECT}",
    ]
    out = capsys.readouterr().out
    if json_flag:
        assert json.loads(out)["files_applied"] == 3
    else:
        assert out == ""


def test_one_file_applied_is_said_as_one(tmp_path, api_server, caplog):
    caplog.set_level(logging.INFO)
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "fifo.v").write_bytes(b"module fifo; endmodule\n")
    api_server(_folder_project(), _folder_project(), _follow_up("1"))

    assert _continue("Use it", "--project-dir", str(tmp_path / "rtl"), "--send-folder") == 0

    assert f"Aristotle applied 1 file from {tmp_path / 'rtl'} to the project." in caplog.messages


@pytest.mark.parametrize(
    "flags", [[], ["--wait"], ["--json"], ["--non-interactive"]], ids=["plain", "--wait", "--json", "--non-interactive"]
)
@pytest.mark.parametrize("header", [None, "", "three", "-1"], ids=["no header", "empty", "not a number", "negative"])
def test_a_folder_the_server_did_not_apply_names_the_task_to_cancel_and_exits_3_without_waiting(
    flags, header, tmp_path, api_server, caplog, capsys
):
    caplog.set_level(logging.INFO)
    rtl = _rtl(tmp_path)
    as_json = "--json" in flags or "--non-interactive" in flags
    # No wait: nothing after the follow-up but the refresh a --json result makes.
    requests = api_server(_folder_project(), _folder_project(), _follow_up(header), _folder_project())

    assert _continue("Use the new reports", "--project-dir", str(rtl), "--send-folder", *flags) == 3

    assert [request.method for request in requests] == ["GET", "GET", "POST", *(["GET"] if as_json else [])]
    assert caplog.records[-1].levelno == logging.ERROR
    assert caplog.messages[-2:] == [
        f"Prompt submitted to project {FOLDER_PROJECT}, task {FOLDER_TASK}",
        NOT_APPLIED.format(folder=rtl),
    ]
    out = capsys.readouterr().out
    if as_json:
        result = json.loads(out)
        assert result["files_applied"] is None
        assert result["task"]["agent_task_id"] == FOLDER_TASK
    else:
        assert out == ""


@pytest.mark.parametrize("json_flag", [False, True], ids=["text", "json"])
def test_files_under_project_dir_go_up_alone_at_their_paths(json_flag, tmp_path, api_server, caplog, capsys):
    caplog.set_level(logging.INFO)
    rtl = _rtl(tmp_path)
    # The project, its check that it is idle, the follow-up, answered as for no folder, and with --json its refresh.
    requests = api_server(_folder_project(), _folder_project(), _follow_up(None), _folder_project())
    flags = ["--json"] if json_flag else []

    assert (
        _continue("Use these", "--project-dir", str(rtl), "--files", str(rtl / "reports" / "timing.rpt"), *flags) == 0
    )

    sent = [(name, filename) for name, filename, _, _ in multipart_parts(requests[2])]
    assert sent == [("prompt", None), ("agent_questions_setting", None), ("files", "reports/timing.rpt")]
    assert "Uploaded 1 file (11 bytes) with the prompt:\n  reports/timing.rpt" in caplog.messages
    assert not [message for message in caplog.messages if message.startswith("Aristotle applied")]
    out = capsys.readouterr().out
    assert (json.loads(out)["files_applied"] if json_flag else out) == (None if json_flag else "")


@pytest.mark.parametrize("status", [ProjectStatus.IDLE, ProjectStatus.RUNNING], ids=["idle", "running"])
def test_project_dir_with_files_naming_none_is_only_a_root_and_the_prompt_goes_alone(status, tmp_path, api_server):
    rtl = _rtl(tmp_path)
    # The project, the follow-up, and for a running task the listing of its questions.
    unlisted = httpx.Response(404, json={"detail": "Not Found"})
    requests = api_server(_folder_project(status), _follow_up(None), unlisted)

    assert _continue("Use these", "--project-dir", str(rtl), "--files") == 0

    assert [request.method for request in requests][:2] == ["GET", "POST"]
    assert not [part for part in multipart_parts(requests[1]) if part[0] in ("files", "archive")]


@pytest.mark.parametrize("include_ignored", [False, True], ids=["default", "--include-ignored"])
def test_a_dry_run_lists_the_folder_says_each_file_replaces_the_projects_and_needs_no_key(
    include_ignored, tmp_path, api_server, monkeypatch, caplog, capsys
):
    caplog.set_level(logging.INFO)
    rtl = _rtl(tmp_path)
    requests = api_server()
    monkeypatch.setattr(api_request, "API_KEY", None)
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    monkeypatch.setattr("aristotlelib.cli.api_action.get_api_key", Mock(side_effect=AssertionError("no key needed")))
    flags = ["--include-ignored"] if include_ignored else []

    assert _continue("Use the new reports", "--project-dir", str(rtl), "--send-folder", *flags, "--dry-run") == 0

    assert requests == []
    listing = (
        [
            "The folder's 4 files that go up:\n  .gitignore\n  build.log  (--include-ignored)\n  fifo.v\n  reports/timing.rpt"
        ]
        if include_ignored
        else [
            "The folder's 3 files that go up:\n  .gitignore\n  fifo.v\n  reports/timing.rpt",
            "Left out, since an ignore rule matches it:\n  build.log",
        ]
    )
    assert caplog.messages[-len(listing) - 3 : -1] == [
        *listing,
        "Each file that goes up replaces the project's file at the same path, even one Aristotle has edited since, or "
        "is added; the project's other files stay.",
        "The prompt, as it would be sent:\n  Use the new reports",
    ]
    counts = "1 of them matches an ignore rule" if include_ignored else "1 left out because it matches an ignore rule"
    assert re.fullmatch(
        rf"Dry run: would upload {4 if include_ignored else 3} files \(\d+ bytes\); {counts}\.", caplog.messages[-1]
    )
    assert capsys.readouterr().out == ""


def test_a_dry_run_with_json_prints_what_would_go_up(tmp_path, api_server, capsys):
    rtl = _rtl(tmp_path)
    requests = api_server()

    assert _continue("Use the new reports", "--project-dir", str(rtl), "--send-folder", "--dry-run", "--json") == 0

    assert requests == []
    result = json.loads(capsys.readouterr().out)
    assert {key: value for key, value in result.items() if key != "size_bytes"} == {
        "files": 3,
        "paths": [".gitignore", "fifo.v", "reports/timing.rpt"],
        "left_out": ["build.log"],
        "prompt": "Use the new reports",
        "block": None,
    }
    assert result["size_bytes"] > 0


@pytest.mark.parametrize("dry_run", [False, True], ids=["continue", "--dry-run"])
def test_a_folder_over_the_upload_limit_exits_2_with_nothing_sent(dry_run, tmp_path, api_server, monkeypatch, caplog):
    monkeypatch.setattr(local_file_utils, "MAX_FILE_SIZE_BYTES", 1024 * 1024)
    big = tmp_path / "big"
    big.mkdir()
    (big / "trace.vcd").write_bytes(os.urandom(2 * 1024 * 1024))
    requests = api_server(_folder_project())

    assert (
        _continue("Use the trace", "--project-dir", str(big), "--send-folder", *(["--dry-run"] if dry_run else [])) == 2
    )

    # Only the project's lookup, before the folder is read.
    assert [request.method for request in requests] == ([] if dry_run else ["GET"])
    assert caplog.messages[-1] == (
        "The upload is 2.0 MB compressed, over the 1 MB limit. Exclude large files with .gitignore or submit a "
        "smaller folder."
    )
    assert not [message for message in caplog.messages if message.startswith("Uploading")]


@pytest.mark.parametrize("dry_run", [False, True], ids=["continue", "--dry-run"])
@pytest.mark.parametrize(
    ("aristotle_f", "sources"),
    [
        (b"fifo.v\n", "its .v and .sv files"),
        (
            b"--top fifo\n--blackbox sram\nfifo.v\nvendor/sram.v\n",
            "its .v and .sv files that its black boxes don't leave out",
        ),
    ],
    ids=["plain", "black-boxed"],
)
def test_a_folder_whose_verilog_an_ignore_rule_leaves_out_suggests_only_include_ignored(
    aristotle_f, sources, dry_run, tmp_path, api_server, caplog
):
    rtl = tmp_path / "rtl"
    (rtl / "vendor").mkdir(parents=True)
    (rtl / ".gitignore").write_bytes(b"fifo.v\n")
    (rtl / "fifo.v").write_bytes(b"module fifo; sram u(); endmodule\n")
    (rtl / "vendor" / "sram.v").write_bytes(b"module sram; endmodule\n")
    (rtl / "aristotle.f").write_bytes(aristotle_f)
    if b"--blackbox" not in aristotle_f:
        (rtl / ".gitignore").write_bytes(b"*.v\n")
    requests = api_server(_folder_project())

    assert (
        _continue(
            "Use the new sources", "--project-dir", str(rtl), "--send-folder", *(["--dry-run"] if dry_run else [])
        )
        == 2
    )

    assert [request.method for request in requests] == ([] if dry_run else ["GET"])
    ignored = "  fifo.v\n" if b"--blackbox" in aristotle_f else "  fifo.v\n  vendor/sram.v\n"
    assert caplog.messages[-1] == (
        f"No Verilog source would go up from {rtl}: an ignore rule matches each of {sources}:\n"
        f"{ignored}"
        "No .c or .rs file would go up either, so none of the project's sources would be replaced. To upload "
        "them, pass --include-ignored (from Python, the include_ignored argument), which uploads the files ignore "
        "rules keep out."
    )


FILELIST_NOTE = (
    "aristotle.f goes up as written, in place of the project's, even one submit generated: a follow-up takes only its "
    "black boxes from it, so a file it names goes up only from inside the folder, and if an ignore rule matches it, only "
    "with --include-ignored (from Python, the include_ignored argument)."
)


def _block_folder(tmp_path: Path) -> Path:
    """A folder whose aristotle.f names top.v beside it, gen/fifo_gen.v, which an ignore rule matches, and ip/core.v
    outside it."""
    rtl = tmp_path / "rtl"
    (rtl / "gen").mkdir(parents=True)
    (tmp_path / "ip").mkdir()
    (tmp_path / "ip" / "core.v").write_bytes(b"module core; endmodule\n")
    (rtl / "top.v").write_bytes(b"module top; core u(); fifo_gen f(); endmodule\n")
    (rtl / "gen" / "fifo_gen.v").write_bytes(b"module fifo_gen; endmodule\n")
    (rtl / ".gitignore").write_bytes(b"gen/\n")
    (rtl / "aristotle.f").write_bytes(b"--top top\ntop.v\ngen/fifo_gen.v\n../ip/core.v\n")
    return rtl


def test_an_aristotle_f_goes_up_as_written_and_names_nothing_more(tmp_path, api_server, caplog):
    caplog.set_level(logging.INFO)
    rtl = _block_folder(tmp_path)
    requests = api_server(_folder_project(), _folder_project(), _follow_up("3"))

    assert _continue("Use the new sources", "--project-dir", str(rtl), "--send-folder") == 0

    assert _archive_in(requests[2]) == {
        ".gitignore": b"gen/\n",
        "aristotle.f": b"--top top\ntop.v\ngen/fifo_gen.v\n../ip/core.v\n",
        "top.v": ANY,
    }
    uploading = caplog.messages.index("Uploading 3 files; 1 left out because it matches an ignore rule.")
    assert caplog.messages[uploading + 1] == FILELIST_NOTE


@pytest.mark.parametrize("dry_run", [False, True], ids=["continue", "--dry-run"])
def test_an_aristotle_f_an_ignore_rule_keeps_out_gets_no_note(dry_run, tmp_path, api_server, caplog):
    caplog.set_level(logging.INFO)
    rtl = _block_folder(tmp_path)
    (rtl / ".gitignore").write_bytes(b"gen/\naristotle.f\n")
    requests = api_server(_folder_project(), _folder_project(), _follow_up("2"))

    assert (
        _continue(
            "Use the new sources", "--project-dir", str(rtl), "--send-folder", *(["--dry-run"] if dry_run else [])
        )
        == 0
    )

    if not dry_run:
        assert sorted(_archive_in(requests[2])) == [".gitignore", "top.v"]
    assert not [message for message in caplog.messages if "aristotle.f goes up" in message]


def test_a_dry_run_of_a_folder_with_an_aristotle_f_says_what_becomes_of_it(tmp_path, api_server, caplog):
    caplog.set_level(logging.INFO)
    rtl = _block_folder(tmp_path)
    requests = api_server()

    assert _continue("Use the new sources", "--project-dir", str(rtl), "--send-folder", "--dry-run") == 0

    assert requests == []
    assert caplog.messages[-5:-2] == [
        "The folder's 3 files that go up:\n  .gitignore\n  aristotle.f\n  top.v",
        "Left out, since an ignore rule matches it:\n  gen/fifo_gen.v",
        "Each file that goes up replaces the project's file at the same path, even one Aristotle has edited since, or "
        f"is added; the project's other files stay.\n{FILELIST_NOTE}",
    ]


def _soc(tmp_path: Path) -> Path:
    """docs/verilog.md's black-box example: soc/aristotle.f black-boxes sram_1p, which vendor/sram_1p.v alone declares,
    beside the design under rtl/."""
    soc = tmp_path / "soc"
    (soc / "rtl").mkdir(parents=True)
    (soc / "vendor").mkdir()
    (soc / "rtl" / "soc_top.v").write_bytes(
        b"module soc_top(input clk, output q);\n  sram_1p u(.clk(clk), .q(q));\nendmodule\n"
    )
    (soc / "rtl" / "soc.f").write_bytes(b"soc_top.v\n")
    (soc / "vendor" / "sram_1p.v").write_bytes(b"module sram_1p(input clk, output q);\n  assign q = clk;\nendmodule\n")
    (soc / "aristotle.f").write_bytes(b"--top soc_top\n--blackbox sram_1p\n-F rtl/soc.f\nvendor/sram_1p.v\n")
    (soc / "README.md").write_bytes(b"notes\n")
    return soc


SOC_BLACKBOXES = (
    "Black boxes, which aristotle.f names, and the files left out for them:\n"
    f"  sram_1p ({os.path.join('soc', 'aristotle.f')}:2): {os.path.join('soc', 'vendor', 'sram_1p.v')}"
)


def test_a_model_the_folders_aristotle_f_black_boxes_stays_out_as_it_does_of_submit(
    tmp_path, api_server, monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    soc = _soc(tmp_path)
    monkeypatch.chdir(tmp_path)
    requests = api_server(_folder_project(), _folder_project(), _folder_project(), _follow_up("4"))

    with patch("sys.argv", ["aristotle", "submit", "Prove it", "--project-dir", "soc"]):
        assert main() == 0
    assert _continue("Use the new sources", "--project-dir", "soc", "--send-folder") == 0

    submitted, sent = _archive_in(requests[0], field="input"), _archive_in(requests[3])
    assert sorted(sent) == sorted(submitted) == ["README.md", "aristotle.f", "rtl/soc.f", "rtl/soc_top.v"]
    # submit sends the aristotle.f it generates; continue, the one in the folder.
    assert sent | {"aristotle.f": None} == submitted | {"aristotle.f": None}
    assert sent["aristotle.f"] == (soc / "aristotle.f").read_bytes()
    assert caplog.messages.count(SOC_BLACKBOXES) == 1


@pytest.mark.parametrize(
    ("change", "flags", "withheld"),
    [
        pytest.param(
            {"rtl/sram_copy.v": b"module sram_1p(input clk, output q);\nendmodule\n"},
            [],
            ["rtl/sram_copy.v", "vendor/sram_1p.v"],
            id="an unnamed file declaring only the black box",
        ),
        pytest.param(
            {
                "aristotle.f": b"--top soc_top\n-F rtl/soc.f\nvendor/sram_1p.v\n",
                "rtl/soc.f": b"--blackbox sram_1p\nsoc_top.v\n",
            },
            [],
            ["vendor/sram_1p.v"],
            id="a black box a nested filelist names",
        ),
        pytest.param(
            {".gitignore": b"gen/\n", "gen/sram_gen.v": b"module sram_1p(input clk, output q);\nendmodule\n"},
            ["--include-ignored"],
            ["gen/sram_gen.v", "vendor/sram_1p.v"],
            id="an ignored copy, with --include-ignored",
        ),
    ],
)
def test_continue_withholds_what_submit_withholds_for_a_black_box(
    change, flags, withheld, tmp_path, api_server, monkeypatch
):
    soc = _soc(tmp_path)
    for name, content in change.items():
        (soc / name).parent.mkdir(parents=True, exist_ok=True)
        (soc / name).write_bytes(content)
    monkeypatch.chdir(tmp_path)
    requests = api_server(_folder_project(), _folder_project(), _folder_project(), _follow_up("1"))

    with patch("sys.argv", ["aristotle", "submit", "Prove it", "--project-dir", "soc", *flags]):
        assert main() == 0
    assert _continue("Use the new sources", "--project-dir", "soc", "--send-folder", *flags) == 0

    submitted, sent = set(_archive_in(requests[0], field="input")), set(_archive_in(requests[3]))
    files = {p.relative_to(soc).as_posix() for p in soc.rglob("*") if p.is_file()}
    assert sorted(files - submitted - {".gitignore"}) == sorted(files - sent - {".gitignore"}) == withheld


def test_a_dry_run_lists_what_the_folders_black_boxes_leave_out(tmp_path, api_server, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _soc(tmp_path)
    monkeypatch.chdir(tmp_path)
    requests = api_server()

    assert _continue("Use the new sources", "--project-dir", "soc", "--send-folder", "--dry-run") == 0

    assert requests == []
    listed = caplog.messages.index(SOC_BLACKBOXES)
    assert caplog.messages[listed + 1] == (
        "The folder's 4 files that go up:\n  README.md\n  aristotle.f\n  rtl/soc.f\n  rtl/soc_top.v"
    )


@pytest.mark.parametrize("dry_run", [False, True], ids=["continue", "--dry-run"])
@pytest.mark.parametrize(
    ("change", "error"),
    [
        pytest.param(
            {"aristotle.f": b"--top soc_top\n--blackbox sram_1p\n-F rtl/missing.f\n"},
            f"{os.path.join('soc', 'aristotle.f')}:3: {os.path.join('soc', 'rtl', 'missing.f')}: no such file",
            id="unreadable aristotle.f",
        ),
        pytest.param(
            {"rtl/both.v": b"module sram_1p; endmodule\nmodule helper; endmodule\n"},
            None,
            id="a black box beside another module",
        ),
    ],
)
def test_a_black_box_submit_would_refuse_stops_continue_with_its_message(
    change, error, dry_run, tmp_path, api_server, monkeypatch, caplog
):
    soc = _soc(tmp_path)
    for name, content in change.items():
        (soc / name).write_bytes(content)
    monkeypatch.chdir(tmp_path)
    requests = api_server(_folder_project())

    with patch("sys.argv", ["aristotle", "submit", "Prove it", "--project-dir", "soc", "--dry-run"]):
        assert main() == 2
    refused = caplog.messages[-1]
    assert (
        _continue("Use the new sources", "--project-dir", "soc", "--send-folder", *(["--dry-run"] if dry_run else []))
        == 2
    )

    # Only the project's lookup: nothing went up.
    assert [request.method for request in requests] == ([] if dry_run else ["GET"])
    assert caplog.messages[-1] == refused
    if error is not None:
        assert refused == error


@pytest.mark.parametrize("dry_run", [False, True], ids=["continue", "--dry-run"])
def test_a_follow_up_takes_the_black_boxes_of_an_aristotle_f_that_names_no_source(
    dry_run, tmp_path, api_server, monkeypatch, caplog
):
    """submit refuses it, as its prompt would point Aristotle at it; a follow-up sends it only as a folder file."""
    caplog.set_level(logging.INFO)
    soc = _soc(tmp_path)
    (soc / "aristotle.f").write_bytes(b"--blackbox sram_1p\n")
    (soc / ".gitignore").write_bytes(b"aristotle.f\n")
    monkeypatch.chdir(tmp_path)
    requests = api_server(_folder_project(), _folder_project(), _follow_up("4"))

    with patch("sys.argv", ["aristotle", "submit", "Prove it", "--project-dir", "soc", "--dry-run"]):
        assert main() == 2
    assert caplog.messages[-1].startswith(f"{os.path.join('soc', 'aristotle.f')} names no source file")
    assert (
        _continue("Use the new sources", "--project-dir", "soc", "--send-folder", *(["--dry-run"] if dry_run else []))
        == 0
    )

    expected = [".gitignore", "README.md", "rtl/soc.f", "rtl/soc_top.v"]
    if dry_run:
        assert requests == []
        assert "The folder's 4 files that go up:\n  " + "\n  ".join(expected) in caplog.messages
    else:
        assert sorted(_archive_in(requests[2])) == expected


@pytest.mark.parametrize("dry_run", [False, True], ids=["continue", "--dry-run"])
def test_a_folder_file_that_cant_be_read_exits_3_with_nothing_sent(dry_run, tmp_path, api_server, caplog):
    rtl = _rtl(tmp_path)
    gone = FileNotFoundError(2, "No such file or directory", str(rtl / "fifo.v"))
    requests = api_server(_folder_project())

    with patch("aristotlelib.project._build_archive", side_effect=gone):
        code = _continue(
            "Use the new reports", "--project-dir", str(rtl), "--send-folder", *(["--dry-run"] if dry_run else [])
        )

    assert code == 3
    # Only the project's lookup: the folder is archived before Project.ask's own request.
    assert [request.method for request in requests] == ([] if dry_run else ["GET"])
    assert caplog.messages[-1] == f"Could not archive {rtl / 'fifo.v'}: No such file or directory. Try again."


@pytest.mark.parametrize(
    ("answer", "code", "error"),
    [
        pytest.param(
            httpx.Response(422, json={"detail": "File must be a valid gzipped tar file (.tar.gz)"}),
            2,
            "API request failed with status 422: File must be a valid gzipped tar file (.tar.gz)",
            id="not gzip",
        ),
        pytest.param(
            httpx.Response(422, json={"detail": "The upload is not a readable tar archive."}),
            2,
            "API request failed with status 422: The upload is not a readable tar archive.",
            id="unreadable tar",
        ),
        pytest.param(
            httpx.Response(400, json={"detail": "Cannot add files to a running project"}),
            2,
            "API request failed with status 400: Cannot add files to a running project",
            id="running since the check",
        ),
        pytest.param(
            httpx.Response(400, json={"detail": "Too many files. Maximum number of files is 10, plus one archive."}),
            2,
            "API request failed with status 400: Too many files. Maximum number of files is 10, plus one archive.",
            id="too many files",
        ),
        pytest.param(
            httpx.Response(500, json={"detail": "Internal Server Error"}),
            7,
            f"POST /project/{FOLDER_PROJECT}/ask: API request failed with status 500: Internal Server Error. Aristotle "
            "may have acted on it, so it was not sent again. If it did, the prompt was sent, though its task may take a "
            f"few minutes to show up: check `aristotle tasks {FOLDER_PROJECT}` before sending it again.",
            id="truncated gzip",
        ),
    ],
)
def test_a_folder_the_server_refuses_exits_as_any_refused_follow_up(answer, code, error, tmp_path, api_server, caplog):
    rtl = _rtl(tmp_path)
    requests = api_server(_folder_project(), _folder_project(), answer)

    assert _continue("Use the new reports", "--project-dir", str(rtl), "--send-folder") == code

    assert [request.method for request in requests] == ["GET", "GET", "POST"]
    assert caplog.messages[-1] == error
    assert not [message for message in caplog.messages if message.startswith(("Prompt submitted", "Aristotle applied"))]


@pytest.mark.parametrize("flags", [["--json"], ["--non-interactive"], []], ids=["--json", "--non-interactive", "text"])
def test_a_folder_refused_for_too_many_runs_exits_6_and_says_the_prompt_was_not_sent(
    flags, tmp_path, api_server, caplog, capsys
):
    rtl = _rtl(tmp_path)
    refused = httpx.Response(429, json={"detail": "Too many inflight requests. Please wait for them to finish."})
    requests = api_server(_folder_project(), _folder_project(), refused)

    assert _continue("Use the new reports", "--project-dir", str(rtl), "--send-folder", *flags) == 6

    assert [request.method for request in requests] == ["GET", "GET", "POST"]
    assert set(_archive_in(requests[2])) == {".gitignore", "fifo.v", "reports/timing.rpt"}
    error = f"{api_request.PROJECT_LIMIT_FALLBACK} The prompt was not sent, so it is safe to send it again."
    assert caplog.messages[-1] == error
    out = capsys.readouterr().out
    assert (json.loads(out) if flags else out) == ({"outcome": "not_processed", "error": error} if flags else "")


PROJECT_DIR_ALONE = (
    "--project-dir needs --send-folder, to send the whole folder, or --files, to send some of its files. To send the "
    "prompt alone, drop --project-dir."
)


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["--send-folder"], "--send-folder needs --project-dir, the folder to send."),
        (
            ["--project-dir", "{rtl}", "--send-folder", "--files", "{file}"],
            "--send-folder sends the whole folder, so it takes no --files.",
        ),
        (
            ["--project-dir", "{rtl}", "--send-folder", "--files"],
            "--send-folder sends the whole folder, so it takes no --files.",
        ),
        (
            ["--project-dir", "{rtl}", "--include-ignored"],
            "--include-ignored needs --send-folder, which sends the folder whose ignored files it uploads.",
        ),
        (
            ["--project-dir", "{rtl}", "--files", "{file}", "--include-ignored"],
            "--include-ignored needs --send-folder, which sends the folder whose ignored files it uploads.",
        ),
        (
            ["--project-dir", "{rtl}", "--dry-run"],
            "--dry-run needs --send-folder, which sends the folder whose files it lists.",
        ),
        (["--project-dir", "{rtl}"], PROJECT_DIR_ALONE),
        (["--project-dir", "{rtl}", "--json"], PROJECT_DIR_ALONE),
        (["--project-dir", "{rtl}", "--non-interactive"], PROJECT_DIR_ALONE),
        (
            ["--project-dir", "{rtl}", "--send-folder", "--dry-run", "--wait"],
            "--dry-run sends nothing, so there is nothing to wait for.",
        ),
        (
            ["--project-dir", "{rtl}", "--send-folder", "--dry-run", "--non-interactive"],
            "--dry-run sends nothing, so there is nothing to wait for.",
        ),
        (["--project-dir", "{file}", "--send-folder"], "--project-dir must be a valid directory: {file}"),
        (["--project-dir", "{missing}", "--send-folder"], "--project-dir must be a valid directory: {missing}"),
        (
            ["--project-dir", "", "--send-folder"],
            "--project-dir: Project directory cannot be empty; use . for the current directory",
        ),
    ],
)
def test_a_folder_option_misused_is_a_usage_error_before_any_request(argv, error, tmp_path, api_server, capsys):
    rtl = _rtl(tmp_path)
    notes = tmp_path / "notes.md"
    notes.write_bytes(b"# Notes\n")
    paths = {"rtl": rtl, "notes": notes, "file": rtl / "fifo.v", "missing": rtl / "missing"}
    requests = api_server()

    with pytest.raises(SystemExit) as exited:
        _continue("Use the new reports", *[arg.format(**paths) for arg in argv])

    assert exited.value.code == 2
    assert requests == []
    assert capsys.readouterr().err.endswith(f"error: {error.format(**paths)}\n")


def test_project_dir_alone_is_refused_before_the_key_is_read(tmp_path, api_server, monkeypatch, capsys):
    rtl = _rtl(tmp_path)
    requests = api_server()
    monkeypatch.setattr(api_request, "API_KEY", None)
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    monkeypatch.setattr("aristotlelib.cli.api_action.get_api_key", Mock(side_effect=AssertionError("no key needed")))

    with pytest.raises(SystemExit) as exited:
        _continue("Use the new reports", "--project-dir", str(rtl))

    assert exited.value.code == 2
    assert requests == []
    assert capsys.readouterr().err.endswith(f"error: {PROJECT_DIR_ALONE}\n")


IGNORED_TOO = ["--project-dir", "{rtl}", "--send-folder", "--include-ignored"]


@pytest.mark.parametrize(
    ("argv", "without", "named"),
    [
        (["--project-dir", "{rtl}", "--send-folder"], "--send-folder and --project-dir", "--send-folder"),
        (IGNORED_TOO, "--send-folder, --include-ignored and --project-dir", "--send-folder"),
        (["--files", "{notes}", "--project-dir", "{rtl}"], "--files and --project-dir", "--files"),
        (["--files", "{notes}"], "--files", "--files"),
    ],
    ids=["folder", "folder with ignored files", "files under the folder", "files under the current folder"],
)
def test_a_folder_for_a_running_project_is_refused_before_the_upload(
    argv, without, named, tmp_path, api_server, monkeypatch, capsys
):
    rtl = _rtl(tmp_path)
    monkeypatch.chdir(rtl)
    notes = rtl / "notes.md"
    notes.write_bytes(b"# Notes\n")
    requests = api_server(_folder_project(ProjectStatus.RUNNING))

    with pytest.raises(SystemExit) as exited:
        _continue("Use the new reports", *[arg.format(rtl=rtl, notes=notes) for arg in argv])

    assert exited.value.code == 2
    assert [request.method for request in requests] == ["GET"]
    assert capsys.readouterr().err.endswith(
        f"error: Files can't be added to project {FOLDER_PROJECT} while it runs a task. Send the prompt without "
        f"{without} to reach that task: its agent reads it right away. Or wait for the task to finish (`aristotle show "
        f"{FOLDER_PROJECT} --wait`), then send the prompt with {named}. Cancelling the task first would lose the "
        "prompts it was given, except the project's first.\n"
    )


@pytest.mark.parametrize(
    ("argv", "without", "named"),
    [
        (["--project-dir", "{rtl}", "--send-folder"], "--send-folder and --project-dir", "--send-folder"),
        (IGNORED_TOO, "--send-folder, --include-ignored and --project-dir", "--send-folder"),
        (["--files", "{notes}", "--project-dir", "{rtl}"], "--files and --project-dir", "--files"),
    ],
    ids=["folder", "folder with ignored files", "files under the folder"],
)
def test_a_task_started_while_the_folder_is_archived_is_refused_with_nothing_sent(
    argv, without, named, tmp_path, api_server, capsys
):
    rtl = _rtl(tmp_path)
    notes = rtl / "notes.md"
    notes.write_bytes(b"# Notes\n")
    # Idle at the command's check, running at the follow-up's own.
    requests = api_server(_folder_project(), _folder_project(ProjectStatus.RUNNING))

    with pytest.raises(SystemExit) as exited:
        _continue("Use the new reports", *[arg.format(rtl=rtl, notes=notes) for arg in argv])

    assert exited.value.code == 2
    assert [request.method for request in requests] == ["GET", "GET"]
    assert capsys.readouterr().err.endswith(
        f"error: Files can't be added to project {FOLDER_PROJECT} while it runs a task. Send the prompt without "
        f"{without} to reach that task: its agent reads it right away. Or wait for the task to finish (`aristotle show "
        f"{FOLDER_PROJECT} --wait`), then send the prompt with {named}. Cancelling the task first would lose the "
        "prompts it was given, except the project's first.\n"
    )


def test_a_reply_that_cant_be_read_after_a_folder_went_up_may_have_sent_the_prompt(
    tmp_path, api_server, monkeypatch, capsys, caplog
):
    monkeypatch.setattr(api_request, "getproxies", dict)
    rtl = _rtl(tmp_path)
    unreadable = httpx.Response(200, content=b"not JSON", headers={"content-type": "application/json"})
    requests = api_server(_folder_project(), _folder_project(), unreadable)

    assert _continue("Use the new reports", "--project-dir", str(rtl), "--send-folder") == 7

    assert [request.method for request in requests] == ["GET", "GET", "POST"]
    assert caplog.messages[-1].startswith(
        f"POST /project/{FOLDER_PROJECT}/ask: the answer from https://aristotle.harmonic.fun was 200 OK with "
        "application/json that isn't JSON, not Aristotle's: a captive portal or a proxy may have answered instead. "
        "Aristotle may have acted on it, so it was not sent again."
    )
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.usefixtures("frozen_api_clock")
def test_a_lookup_answered_by_json_this_version_cant_read_sends_no_prompt(api_server, monkeypatch, capsys):
    monkeypatch.setattr(api_request, "getproxies", dict)
    requests = api_server(*(lambda request: httpx.Response(200, json={}) for _ in range(api_request.RETRY_ATTEMPTS)))

    assert _continue("Use the new reports", "--json") == 3

    assert [request.method for request in requests] == ["GET"] * api_request.RETRY_ATTEMPTS
    printed = json.loads(capsys.readouterr().out)
    assert printed["outcome"] == "not_processed"
    assert printed["error"].startswith(
        f"GET /project/{FOLDER_PROJECT} after 5 attempts over 0 s: the answer from https://aristotle.harmonic.fun was "
        "200 OK with JSON this version of aristotlelib can't read ("
    )
    assert printed["error"].endswith("The prompt was not sent, so it is safe to send it again.")


def test_a_folder_with_nothing_to_upload_exits_2_with_nothing_sent(tmp_path, api_server, caplog):
    (tmp_path / "empty").mkdir()
    requests = api_server(_folder_project())

    assert _continue("Use it", "--project-dir", str(tmp_path / "empty"), "--send-folder") == 2

    assert [request.method for request in requests] == ["GET"]
    assert caplog.messages[-1] == f"Nothing to upload: {tmp_path / 'empty'} is empty."
