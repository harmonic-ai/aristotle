"""Tests for aristotlelib.cli.submit module."""

import argparse
import asyncio
import errno
import json
import logging
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import ANY, AsyncMock, Mock, patch

import httpx
import pytest

from aristotlelib import api_request, local_file_utils
from aristotlelib.agent_task import AgentTask, TaskStatus
from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.api_action import resume_hint
from aristotlelib.cli.command.submit import SubmitAction
from aristotlelib.cli.controller import main
from aristotlelib.declaration import Declaration, DeclarationSource, DeclarationStatus, DeclarationType
from aristotlelib.local_file_utils import LeanProjectError
from aristotlelib.project import AgentQuestionsSetting, Project, ProjectStatus
from aristotlelib.question import Question
from aristotlelib.shell import shell_hint
from aristotlelib.verilog import filelist
from tests.ci.conftest import git
from tests.conftest import FAKE_KEY, NEVER_READ, UNREACHABLE_HOST, WILDCARD_FOLDERS, fast_api_clock, shown_key_runs

PROJECT_ID = "3f1002f7-0b6c-4c1e-9d8e-2a4b5c6d7e8f"
RENAME_BY_ID = "aristotlelib.project.Project.rename_by_id"
SPACED = "my results/run 1.tar.gz"
"""A --destination the shell must quote."""
NOT_SAVED = "The project's files were not saved, since the command was interrupted."


def _download(destination: str | None = SPACED) -> list[str]:
    """The command that saves the project's files to ``destination``, else into the current directory."""
    return ["aristotle", "download", PROJECT_ID, *([] if destination is None else [f"--destination={destination}"])]


@pytest.fixture(autouse=True)
def default_web_origin(monkeypatch):
    monkeypatch.delenv("ARISTOTLE_WEB_URL", raising=False)


@pytest.fixture(autouse=True)
def properties():
    """Patch the properties fetch a JSON result makes; empty by default."""
    with patch("aristotlelib.cli.api_action.Declaration.list_all_for_project", return_value=[]) as mock:
        yield mock


def _property(name: str, status: DeclarationStatus) -> Declaration:
    return Declaration(
        project_id=PROJECT_ID,
        declaration_id=f"id-{name}",
        declaration_type=DeclarationType.PROPERTY,
        source=DeclarationSource.USER,
        name=name,
        status=status,
        filename="Spec/Property.lean",
        created_at=datetime(2026, 9, 1, 12, 0),
        last_updated=datetime(2026, 9, 1, 12, 0),
    )


async def test_submit_action_without_project_dir(caplog):
    """Test submitting a project without a directory."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = SubmitAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["Test prompt"])

    # Mock Project.create
    mock_project = Project(
        project_id="test-123",
        status=ProjectStatus.RUNNING,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=None,
        has_input=False,
        has_files=False,
    )

    with patch("aristotlelib.cli.command.submit.Project.create", return_value=mock_project) as mock_create:
        with caplog.at_level(logging.INFO):
            await action.run_action(args)

    # Verify Project.create was called with correct prompt
    mock_create.assert_called_once()
    call_args = mock_create.call_args
    assert call_args[1]["prompt"] == "Test prompt"
    assert "Open in Aristotle Web: https://aristotle.harmonic.fun/projects/test-123" in caplog.messages


async def test_submit_sends_a_prompt_from_a_file_longer_than_one_argument_may_be(tmp_path):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    # Linux takes at most 128 KiB in one argument.
    prompt = "Reply with the single word OK. " + "Context. " * 20_000
    path = tmp_path / "prompt.txt"
    path.write_text(prompt + "\n", encoding="utf-8")
    project = Project(
        project_id="test-123",
        status=ProjectStatus.RUNNING,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=None,
        has_input=False,
        has_files=False,
    )

    with patch("aristotlelib.cli.command.submit.Project.create", return_value=project) as create:
        args = action.parser.parse_args(["--prompt-file", str(path)])
        assert action.preflight(args) is None
        await action.run_action(args)

    assert len(prompt.encode()) > 128 * 1024
    assert create.call_args.kwargs["prompt"] == prompt


async def test_submit_action_rejects_empty_prompt():
    """Test that submit rejects an empty prompt before any API call."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = SubmitAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args([""])

    with patch("aristotlelib.cli.command.submit.Project.create") as mock_create:
        with pytest.raises(SystemExit) as exc_info:
            action.preflight(args)

    assert exc_info.value.code == 2
    mock_create.assert_not_called()


async def test_submit_action_rejects_whitespace_prompt(capsys):
    """Test that submit rejects a whitespace-only prompt before any API call."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = SubmitAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["   \n\t"])

    with patch("aristotlelib.cli.command.submit.Project.create") as mock_create:
        with pytest.raises(SystemExit) as exc_info:
            action.preflight(args)

    assert exc_info.value.code == 2
    assert "Prompt cannot be empty" in capsys.readouterr().err
    mock_create.assert_not_called()


async def test_submit_action_with_project_dir():
    """Test submitting a project with a directory."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        action = SubmitAction(subparsers)
        action.add_action_arguments()

        args = action.parser.parse_args(["Test prompt", "--project-dir", str(temp_path)])

        mock_project = Project(
            project_id="test-456",
            status=ProjectStatus.RUNNING,
            created_at=datetime.now(),
            last_updated=datetime.now(),
            description=None,
            has_input=True,
            has_files=False,
        )

        # Mock the validation functions to avoid prompts (used inside Project.create_from_directory)
        with (
            patch(
                "aristotlelib.project.local_file_utils.has_any_lean_files",
                return_value=False,
            ),
            patch(
                "aristotlelib.cli.command.submit.Project.create_from_directory",
                return_value=mock_project,
            ) as mock_create,
        ):
            await action.run_action(args)

        mock_create.assert_called_once()
        call_args = mock_create.call_args
        assert call_args[1]["prompt"] == "Test prompt"
        assert call_args[1]["project_dir"] == temp_path
        assert call_args[1]["include_ignored"] is False


async def test_submit_action_with_invalid_project_dir(capsys):
    """Test that submitting with invalid project dir is a usage error."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = SubmitAction(subparsers)
    action.add_action_arguments()

    with tempfile.NamedTemporaryFile() as f:
        args = action.parser.parse_args(["Test prompt", "--project-dir", f.name])

        with pytest.raises(SystemExit) as exc_info:
            action.preflight(args)

        assert exc_info.value.code == 2
        assert "must be a valid directory" in capsys.readouterr().err


def test_a_project_dir_with_nothing_to_upload_exits_2_before_any_request(api_server, caplog, tmp_path):
    requests = api_server()

    with patch("sys.argv", ["aristotle", "submit", "Prove it", "--project-dir", str(tmp_path)]):
        assert main() == 2

    assert requests == []
    assert caplog.messages[-1] == f"Nothing to upload: {tmp_path} is empty."


@pytest.mark.parametrize(
    "where",
    [
        "a git directory",
        "a .git folder",
        "a bare repository",
        "a folder that holds credentials",
        "a link into credentials",
    ],
)
@pytest.mark.parametrize("how", ["--project-dir", "block alone"])
def test_a_folder_inside_git_or_credentials_is_refused_before_any_request(
    api_server, caplog, tmp_path, monkeypatch, where: str, how: str
):
    """Below such a folder, its files would go up under names no rule withholds, as .git/config does."""
    requests = api_server()
    if where == "a git directory":
        git(tmp_path, "init", "-q", "repo")
        folder, reason = tmp_path / "repo" / ".git", "is a git directory"
    elif where == "a .git folder":
        git(tmp_path, "init", "-q", "repo")
        folder, reason = tmp_path / "repo" / ".git" / "hooks", "is inside a .git folder"
    elif where == "a bare repository":
        git(tmp_path, "init", "-q", "--bare", "lib.git")
        folder, reason = tmp_path / "lib.git" / "hooks", "is inside a git directory"
    elif where == "a link into credentials":
        if os.name == "nt":
            pytest.skip("symlinks need privileges on Windows")
        (tmp_path / ".docker" / "proj").mkdir(parents=True)
        (tmp_path / "proj").symlink_to(tmp_path / ".docker" / "proj")
        folder, reason = tmp_path / "proj", "is in a folder that holds credentials"
    else:
        folder, reason = tmp_path / ".docker" / "proj", "is in a folder that holds credentials"
        folder.mkdir(parents=True)
    (folder / "top.v").write_text("module top;\nendmodule\n")
    (folder / "config.json").write_text('{"auths": {"placeholder": {}}}\n')
    (folder / "list.f").write_text("top.v\nconfig.json\n")
    if how == "--project-dir":
        argv = ["aristotle", "submit", "Prove it", "--project-dir", str(folder)]
    else:
        monkeypatch.chdir(folder)
        argv = ["aristotle", "submit", "Prove it", "--top", "top", "-F", "list.f"]

    with patch("sys.argv", argv):
        assert main() == 2

    assert requests == []
    assert reason in caplog.messages[-1]


def test_an_empty_project_dir_is_a_usage_error_not_the_current_directory(capsys):
    with (
        patch("sys.argv", ["aristotle", "submit", "Test prompt", "--project-dir", ""]),
        patch("aristotlelib.cli.command.submit.Project.create_from_directory") as mock_create,
        pytest.raises(SystemExit) as exited,
    ):
        main()

    assert exited.value.code == 2
    assert (
        "argument --project-dir: Project directory cannot be empty; use . for the current directory"
        in capsys.readouterr().err
    )
    mock_create.assert_not_called()


def _unreadable_parent(tmp_path: Path) -> str:
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("needs a POSIX user that file permissions apply to")
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o000)
    return str(locked / "project")


@pytest.mark.parametrize(
    "project_dir",
    [lambda tmp_path: "d" * 300, _unreadable_parent],
    ids=["name too long", "parent unreadable"],
)
async def test_a_project_dir_that_cannot_be_examined_is_a_usage_error(project_dir, tmp_path, capsys):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    path = project_dir(tmp_path)

    try:
        with (
            patch("aristotlelib.cli.command.submit.Project.create_from_directory") as mock_create,
            pytest.raises(SystemExit) as exited,
        ):
            action.preflight(action.parser.parse_args(["Test prompt", "--project-dir", path]))
    finally:
        if (tmp_path / "locked").exists():
            os.chmod(tmp_path / "locked", 0o700)

    assert exited.value.code == 2
    assert "--project-dir must be a valid directory" in capsys.readouterr().err
    mock_create.assert_not_called()


def test_submit_names_the_credential_files_it_left_out(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("ARISTOTLE_API_KEY", "test-key")
    caplog.set_level(logging.INFO)
    (tmp_path / "top.sv").write_bytes(b"module top; endmodule\n")
    (tmp_path / ".netrc").write_bytes(b"machine example.invalid login x password y\n")
    (tmp_path / "SERVER.PEM").write_bytes(b"not a key\n")
    uploaded: list[str] = []

    async def create(*, tar_file_path, **kwargs):
        with tarfile.open(tar_file_path) as tar:
            uploaded.extend(tar.getnames())
        return _project(None)

    with (
        patch("sys.argv", ["aristotle", "submit", "Test prompt", "--project-dir", str(tmp_path)]),
        patch("aristotlelib.cli.command.submit.Project.create", side_effect=create),
    ):
        assert main() == 0

    assert "Excluded 2 file(s) matching secret patterns: .netrc, SERVER.PEM" in caplog.messages
    assert "Uploading 1 file." in caplog.messages
    assert uploaded == ["top.sv"]


@pytest.mark.parametrize(
    ("file_name", "message"),
    [
        (None, "is a directory. Did you mean to use --project-dir?"),
        ("Main.lean", "is a file. Did you mean to use --prompt-file?"),
    ],
    ids=["directory", "file"],
)
async def test_a_prompt_that_names_a_path_is_a_usage_error(file_name, message, tmp_path, capsys):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    path = tmp_path
    if file_name:
        path = tmp_path / file_name
        path.touch()

    with (
        patch("aristotlelib.cli.command.submit.Project.create") as mock_create,
        pytest.raises(SystemExit) as exc_info,
    ):
        action.preflight(action.parser.parse_args([str(path)]))

    assert exc_info.value.code == 2
    assert f"The prompt '{path}' {message}" in capsys.readouterr().err
    mock_create.assert_not_called()


@pytest.mark.parametrize(
    "prompt",
    ["Prove it. " * 30, "ü" * 128, "1/2 + " * 700],
    ids=["300 characters", "128 two-byte characters", "4200 bytes with slashes"],
)
async def test_a_prompt_too_long_to_be_a_path_is_submitted(prompt):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()

    with patch("aristotlelib.cli.command.submit.Project.create", return_value=_project(None)) as mock_create:
        args = action.parser.parse_args([prompt])
        assert action.preflight(args) is None
        await action.run_action(args)

    assert mock_create.call_args.kwargs["prompt"] == prompt


async def test_a_project_aristotle_cannot_take_is_one_error_line_and_exit_2(tmp_path, caplog):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    refused = LeanProjectError("Aristotle does not support projects with local dependencies.")

    with patch("aristotlelib.cli.command.submit.Project.create_from_directory", side_effect=refused):
        code = await action.run_action(action.parser.parse_args(["Prove it", "--project-dir", str(tmp_path)]))

    assert code == 2
    assert caplog.messages == ["Aristotle does not support projects with local dependencies."]


def test_a_folder_over_the_upload_limit_exits_2_saying_how_large_it_is_and_what_to_do(
    tmp_path, api_server, monkeypatch, caplog
):
    monkeypatch.setattr(local_file_utils, "MAX_FILE_SIZE_BYTES", 1024 * 1024)
    big = tmp_path / "big"
    big.mkdir()
    (big / "trace.vcd").write_bytes(os.urandom(2 * 1024 * 1024))
    requests = api_server()

    with patch("sys.argv", ["aristotle", "submit", "Prove it", "--project-dir", str(big)]):
        assert main() == 2

    assert requests == []
    assert caplog.messages[-1] == (
        "The upload is 2.0 MB compressed, over the 1 MB limit. Exclude large files with .gitignore or submit a "
        "smaller folder."
    )
    assert not [message for message in caplog.messages if message.startswith("Uploading")]


async def test_submit_action_with_wait():
    """Test submitting a project with --wait flag."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = SubmitAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["Test prompt", "--wait"])

    mock_project = Mock(spec=Project, description=None)
    mock_project.project_id = "test-789"
    mock_project.has_files = True

    mock_task = _real_task(TaskStatus.COMPLETE)

    mock_project.get_tasks = AsyncMock(return_value=([mock_task], None))
    mock_project.refresh = AsyncMock()
    mock_project.get_files = AsyncMock()

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=mock_project),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[])) as wait,
    ):
        assert await action.run_action(args) == 0

    mock_project.get_tasks.assert_called_once_with(limit=1)
    wait.assert_awaited_once()
    mock_project.get_files.assert_called_once()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (TaskStatus.COMPLETE_WITH_ERRORS, 1),
        (TaskStatus.OUT_OF_BUDGET, 1),
        (TaskStatus.FAILED, 3),
        (TaskStatus.CANCELED, 3),
    ],
)
async def test_submit_wait_exits_on_how_the_task_ended(status: TaskStatus, code: int):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()

    mock_project = Mock(spec=Project, description=None)
    mock_project.project_id = "test-789"
    mock_project.has_files = True
    mock_task = _real_task(status)
    mock_project.get_tasks = AsyncMock(return_value=([mock_task], None))
    mock_project.refresh = AsyncMock()
    mock_project.get_files = AsyncMock()

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=mock_project),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[])),
    ):
        assert await action.run_action(action.parser.parse_args(["Test prompt", "--wait"])) == code

    # The files are still downloaded; the exit code reports the outcome.
    mock_project.get_files.assert_called_once()


WAITS = [["--wait"], ["--wait", "--json"], ["--non-interactive"]]
"""Each way submit waits, as the `show` command that waits the same way takes it."""


def _check_unwaited(out: str, flags: list[str]) -> None:
    """Check ``out``: under --json, the new project with no task, counts or properties; otherwise nothing."""
    if flags == ["--wait"]:
        assert out == ""
        return
    result = json.loads(out)
    assert result["project"]["project_id"] == PROJECT_ID
    assert result["web_url"] == f"https://aristotle.harmonic.fun/projects/{PROJECT_ID}"
    assert [result[key] for key in ("task", "result_path", "property_counts", "requested_counts", "properties")] == [
        None
    ] * 5


@pytest.mark.parametrize("flags", WAITS, ids=" ".join)
async def test_a_submit_wait_whose_project_lists_no_task_exits_3_and_says_how_to_wait(flags, capsys, caplog):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=_project("Test prompt")),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([], None))),
        patch.object(AgentTask, "wait_for_completion", AsyncMock()) as wait,
    ):
        assert await action.run_action(action.parser.parse_args(["Test prompt", *flags])) == 3

    wait.assert_not_awaited()
    assert caplog.messages[-1] == (
        f"Project {PROJECT_ID} was created, but Aristotle listed no task for it, so the wait couldn't start.\n"
        + shell_hint("Wait for it with", ["aristotle", "show", PROJECT_ID, *flags])
    )
    _check_unwaited(capsys.readouterr().out, flags)


@pytest.mark.usefixtures("frozen_api_clock")
@pytest.mark.parametrize("flags", WAITS, ids=" ".join)
def test_a_submit_wait_whose_task_cannot_be_listed_exits_3_and_says_how_to_wait(flags, api_server, caplog, capsys):
    unavailable = [httpx.Response(503, text="Service Unavailable")] * api_request.RETRY_ATTEMPTS
    requests = api_server(httpx.Response(200, json=_project(None).model_dump(mode="json")), *unavailable)

    with patch("sys.argv", ["aristotle", "submit", "Prove it", *flags, "--destination", SPACED]):
        assert main() == 3

    assert [request.method for request in requests] == ["POST"] + ["GET"] * api_request.RETRY_ATTEMPTS
    show = ["aristotle", "show", PROJECT_ID, *flags, f"--destination={SPACED}"]
    assert caplog.messages[-1] == (
        f"Project {PROJECT_ID} was created, but listing its task failed, so the wait couldn't start: GET /project/"
        f"{PROJECT_ID}/tasks after 5 attempts over 0 s: API request failed with status 503: Service Unavailable\n"
        + shell_hint("Wait for it with", show)
    )
    _check_unwaited(capsys.readouterr().out, flags)


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, asyncio.CancelledError])
@pytest.mark.parametrize("flags", WAITS, ids=" ".join)
async def test_a_submit_wait_interrupted_before_its_task_is_listed_still_prints_the_project(
    flags, interrupt, capsys, caplog
):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=_project("Test prompt")),
        patch.object(Project, "get_tasks", AsyncMock(side_effect=interrupt)),
        pytest.raises(interrupt),
    ):
        await action.run_action(action.parser.parse_args(["Test prompt", *flags]))

    assert caplog.messages[-1] == (
        "Interrupted before the wait started: the project's run goes on.\n"
        f"{shell_hint('Wait for it with', ['aristotle', 'show', PROJECT_ID, *flags])}\n"
        f"Cancel it with: aristotle cancel {PROJECT_ID}"
    )
    _check_unwaited(capsys.readouterr().out, flags)


async def test_submit_action_with_wait_and_destination():
    """Test submitting a project with --wait and --destination flags."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = SubmitAction(subparsers)
    action.add_action_arguments()

    destination = "/tmp/output.tar.gz"
    args = action.parser.parse_args(["Test prompt", "--wait", "--destination", destination])

    mock_project = Mock(spec=Project, description=None)
    mock_project.project_id = "test-dest"
    mock_project.has_files = True

    mock_task = _real_task(TaskStatus.COMPLETE)

    mock_project.get_tasks = AsyncMock(return_value=([mock_task], None))
    mock_project.refresh = AsyncMock()
    mock_project.get_files = AsyncMock()

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=mock_project),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[])) as wait,
    ):
        await action.run_action(args)

    wait.assert_awaited_once()
    mock_project.get_files.assert_called_once_with(destination=destination)


async def test_submit_action_without_wait():
    """Test submitting a project without --wait flag doesn't call wait_for_completion."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = SubmitAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["Test prompt"])

    mock_project = Mock(spec=Project, description=None)
    mock_project.project_id = "test-nowait"
    mock_project.get_tasks = AsyncMock()

    with patch("aristotlelib.cli.command.submit.Project.create", return_value=mock_project):
        await action.run_action(args)

    mock_project.get_tasks.assert_not_called()


@pytest.mark.parametrize(
    ("argv", "setting", "hint"),
    [
        ([], AgentQuestionsSetting.TIMEOUT_15_MIN, True),
        (["--no-questions"], AgentQuestionsSetting.DISABLED, False),
        (["--json"], AgentQuestionsSetting.TIMEOUT_15_MIN, True),
    ],
)
async def test_a_submit_that_is_not_waited_on_can_be_asked_and_says_how_to_answer(argv, setting, hint, caplog):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=_project(None)) as create,
        patch.object(Project, "get_tasks", AsyncMock(return_value=([], None))),
        caplog.at_level(logging.INFO),
    ):
        assert await action.run_action(action.parser.parse_args(["Test prompt", *argv])) is None

    assert create.call_args.kwargs["agent_questions_setting"] == setting
    answer_hint = f"If Aristotle asks anything, answer with:\n  aristotle questions {PROJECT_ID}"
    assert (answer_hint in caplog.messages) is hint


async def test_no_questions_holds_for_a_waited_submit_too(caplog):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=_project(None)) as create,
        patch.object(Project, "get_tasks", AsyncMock(return_value=([_real_task(TaskStatus.COMPLETE)], None))),
        patch.object(Project, "refresh", AsyncMock()),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[])),
        caplog.at_level(logging.INFO),
    ):
        await action.run_action(action.parser.parse_args(["Test prompt", "--wait", "--no-questions"]))

    assert create.call_args.kwargs["agent_questions_setting"] == AgentQuestionsSetting.DISABLED
    assert not [message for message in caplog.messages if "aristotle questions" in message]


async def test_submit_action_with_correct_toolchain():
    """Test submitting with correct lean-toolchain proceeds without prompt."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        action = SubmitAction(subparsers)
        action.add_action_arguments()

        args = action.parser.parse_args(["Test prompt", "--project-dir", str(temp_path)])

        mock_project = Project(
            project_id="test-toolchain",
            status=ProjectStatus.RUNNING,
            created_at=datetime.now(),
            last_updated=datetime.now(),
            description=None,
            has_input=True,
            has_files=False,
        )

        with (
            patch(
                "aristotlelib.project.local_file_utils.has_any_lean_files",
                return_value=True,
            ),
            patch(
                "aristotlelib.project.local_file_utils.get_lean_toolchain_version",
                return_value="leanprover/lean4:v4.24.0",
            ),
            patch(
                "aristotlelib.project.local_file_utils.get_expected_toolchain",
                new=AsyncMock(return_value="leanprover/lean4:v4.24.0"),
            ),
            patch(
                "aristotlelib.cli.command.submit.Project.create_from_directory",
                return_value=mock_project,
            ) as mock_create,
            patch("builtins.input") as mock_input,
        ):
            await action.run_action(args)

        # Should not prompt the user when toolchain is correct
        mock_input.assert_not_called()
        mock_create.assert_called_once()


async def test_submit_action_with_missing_toolchain_user_cancels():
    """Test submitting with missing toolchain: code logs warning and proceeds (no prompt)."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        action = SubmitAction(subparsers)
        action.add_action_arguments()

        args = action.parser.parse_args(["Test prompt", "--project-dir", str(temp_path)])

        mock_project = Project(
            project_id="test-missing-toolchain",
            status=ProjectStatus.RUNNING,
            created_at=datetime.now(),
            last_updated=datetime.now(),
            description=None,
            has_input=True,
            has_files=False,
        )
        with (
            patch(
                "aristotlelib.project.local_file_utils.has_any_lean_files",
                return_value=True,
            ),
            patch(
                "aristotlelib.project.local_file_utils.get_lean_toolchain_version",
                return_value=None,
            ),
            patch(
                "aristotlelib.project.local_file_utils.get_expected_toolchain",
                new=AsyncMock(return_value="leanprover/lean4:v4.24.0"),
            ),
            patch(
                "aristotlelib.cli.command.submit.Project.create_from_directory",
                return_value=mock_project,
            ) as mock_create,
            patch("builtins.input", return_value="n"),
        ):
            await action.run_action(args)

        # Code warns but does not prompt; create_from_directory is called
        mock_create.assert_called_once()


async def test_submit_action_with_missing_toolchain_user_proceeds():
    """Test submitting with missing toolchain when user proceeds."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        action = SubmitAction(subparsers)
        action.add_action_arguments()

        args = action.parser.parse_args(["Test prompt", "--project-dir", str(temp_path)])

        mock_project = Project(
            project_id="test-proceed",
            status=ProjectStatus.RUNNING,
            created_at=datetime.now(),
            last_updated=datetime.now(),
            description=None,
            has_input=True,
            has_files=False,
        )

        with (
            patch(
                "aristotlelib.project.local_file_utils.has_any_lean_files",
                return_value=True,
            ),
            patch(
                "aristotlelib.project.local_file_utils.get_lean_toolchain_version",
                return_value=None,
            ),
            patch(
                "aristotlelib.project.local_file_utils.get_expected_toolchain",
                new=AsyncMock(return_value="leanprover/lean4:v4.24.0"),
            ),
            patch(
                "aristotlelib.cli.command.submit.Project.create_from_directory",
                return_value=mock_project,
            ) as mock_create,
            patch("builtins.input", return_value="y"),
        ):
            await action.run_action(args)

        # Should create project when user proceeds
        mock_create.assert_called_once()


async def test_submit_action_with_wrong_toolchain_user_cancels():
    """Test submitting with wrong toolchain: code logs warning and proceeds (no prompt)."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        action = SubmitAction(subparsers)
        action.add_action_arguments()

        args = action.parser.parse_args(["Test prompt", "--project-dir", str(temp_path)])

        mock_project = Project(
            project_id="test-wrong-toolchain",
            status=ProjectStatus.RUNNING,
            created_at=datetime.now(),
            last_updated=datetime.now(),
            description=None,
            has_input=True,
            has_files=False,
        )
        with (
            patch(
                "aristotlelib.project.local_file_utils.has_any_lean_files",
                return_value=True,
            ),
            patch(
                "aristotlelib.project.local_file_utils.get_lean_toolchain_version",
                return_value="leanprover/lean4:v4.23.0",
            ),
            patch(
                "aristotlelib.project.local_file_utils.get_expected_toolchain",
                new=AsyncMock(return_value="leanprover/lean4:v4.24.0"),
            ),
            patch(
                "aristotlelib.cli.command.submit.Project.create_from_directory",
                return_value=mock_project,
            ) as mock_create,
            patch("builtins.input", return_value="n"),
        ):
            await action.run_action(args)

        # Code warns but does not prompt; create_from_directory is called
        mock_create.assert_called_once()


async def test_submit_action_with_wrong_toolchain_user_proceeds():
    """Test submitting with wrong toolchain version when user proceeds."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        action = SubmitAction(subparsers)
        action.add_action_arguments()

        args = action.parser.parse_args(["Test prompt", "--project-dir", str(temp_path)])

        mock_project = Project(
            project_id="test-wrong-proceed",
            status=ProjectStatus.RUNNING,
            created_at=datetime.now(),
            last_updated=datetime.now(),
            description=None,
            has_input=True,
            has_files=False,
        )

        with (
            patch(
                "aristotlelib.project.local_file_utils.has_any_lean_files",
                return_value=True,
            ),
            patch(
                "aristotlelib.project.local_file_utils.get_lean_toolchain_version",
                return_value="leanprover/lean4:v4.23.0",
            ),
            patch(
                "aristotlelib.project.local_file_utils.get_expected_toolchain",
                new=AsyncMock(return_value="leanprover/lean4:v4.24.0"),
            ),
            patch(
                "aristotlelib.cli.command.submit.Project.create_from_directory",
                return_value=mock_project,
            ) as mock_create,
            patch("builtins.input", return_value="yes"),
        ):
            await action.run_action(args)

        # Should create project when user proceeds with "yes"
        mock_create.assert_called_once()


async def test_submit_action_without_lean_files_no_prompt():
    """Test submitting without .lean files doesn't trigger toolchain check."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        action = SubmitAction(subparsers)
        action.add_action_arguments()

        args = action.parser.parse_args(["Test prompt", "--project-dir", str(temp_path)])

        mock_project = Project(
            project_id="test-no-lean",
            status=ProjectStatus.RUNNING,
            created_at=datetime.now(),
            last_updated=datetime.now(),
            description=None,
            has_input=True,
            has_files=False,
        )

        with (
            patch(
                "aristotlelib.project.local_file_utils.has_any_lean_files",
                return_value=False,
            ),
            patch(
                "aristotlelib.cli.command.submit.Project.create_from_directory",
                return_value=mock_project,
            ) as mock_create,
            patch("builtins.input") as mock_input,
        ):
            await action.run_action(args)

        # Should not prompt the user when there are no lean files
        mock_input.assert_not_called()
        mock_create.assert_called_once()


def _project(description: str | None) -> Project:
    return Project(
        project_id=PROJECT_ID,
        status=ProjectStatus.RUNNING,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=description,
        has_input=False,
        has_files=False,
    )


async def test_submit_with_name_renames_the_new_project(caplog):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = SubmitAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["Test prompt", "--name", "  solana proj "])

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=_project("Test prompt")),
        patch(RENAME_BY_ID, return_value=_project("solana proj")) as mock_rename,
        caplog.at_level(logging.INFO),
    ):
        await action.run_action(args)

    mock_rename.assert_called_once_with(PROJECT_ID, "solana proj")
    assert caplog.messages[-4:] == [
        f"Project created: {PROJECT_ID}",
        f"Open in Aristotle Web: https://aristotle.harmonic.fun/projects/{PROJECT_ID}",
        "Project name: solana proj",
        f"If Aristotle asks anything, answer with:\n  aristotle questions {PROJECT_ID}",
    ]


async def test_submit_sends_a_name_that_is_not_utf8_escaped():
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()

    args = action.parser.parse_args(["Test prompt", "--name", "r\udce9sum\udce9"])

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=_project("Test prompt")),
        patch(RENAME_BY_ID, return_value=_project("r\\xe9sum\\xe9")) as mock_rename,
    ):
        await action.run_action(args)

    mock_rename.assert_called_once_with(PROJECT_ID, "r\\xe9sum\\xe9")


async def test_submit_with_name_and_project_dir_renames_the_new_project():
    with tempfile.TemporaryDirectory() as temp_dir:
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        action = SubmitAction(subparsers)
        action.add_action_arguments()

        args = action.parser.parse_args(["Test prompt", "--project-dir", temp_dir, "--name", "solana proj"])

        with (
            patch(
                "aristotlelib.cli.command.submit.Project.create_from_directory",
                return_value=_project(Path(temp_dir).name),
            ) as mock_create,
            patch(RENAME_BY_ID, return_value=_project("solana proj")) as mock_rename,
        ):
            await action.run_action(args)

    mock_create.assert_called_once()
    mock_rename.assert_called_once_with(PROJECT_ID, "solana proj")


async def test_submit_without_name_does_not_rename():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = SubmitAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["Test prompt"])

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=_project("Test prompt")),
        patch(RENAME_BY_ID) as mock_rename,
    ):
        await action.run_action(args)

    mock_rename.assert_not_called()


@pytest.mark.parametrize("name", ["", "  \t"])
def test_submit_rejects_an_empty_name_before_creating_the_project(name: str, capsys):
    with (
        patch("sys.argv", ["aristotle", "submit", "Test prompt", "--name", name]),
        patch("aristotlelib.cli.command.submit.Project.create") as mock_create,
        pytest.raises(SystemExit) as exited,
    ):
        main()

    assert exited.value.code == 2
    assert "argument --name: Project name cannot be empty" in capsys.readouterr().err
    mock_create.assert_not_called()


@pytest.mark.parametrize("name", ["Bob's proj", "-v2", "--wip", "=v2", "a b", "-", "$HOME `id` !!", "--%"])
def test_submit_keeps_the_project_when_naming_it_fails(name: str, caplog, monkeypatch):
    monkeypatch.setenv("ARISTOTLE_API_KEY", "test-key")
    caplog.set_level(logging.INFO)

    server_error = AristotleAPIError("API request failed with status 500: Internal Server Error", status_code=500)
    with (
        patch("sys.argv", ["aristotle", "submit", "Test prompt", f"--name={name}"]),
        patch("aristotlelib.cli.command.submit.Project.create", return_value=_project("Test prompt")),
        patch(RENAME_BY_ID, side_effect=server_error),
    ):
        assert main() == 0

    assert f"Project created: {PROJECT_ID}" in caplog.messages
    assert f"Open in Aristotle Web: https://aristotle.harmonic.fun/projects/{PROJECT_ID}" in caplog.messages
    [warning] = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert f"Project {PROJECT_ID} was created, but naming it failed: {server_error}" in warning.message

    command = warning.message.splitlines()[-1]
    if os.name != "nt":
        # zsh replaces a bare word that starts with = with a program's path.
        assert " =" not in command
    # PowerShell hands on the array @('--%') as the one argument --%, which it drops when quoted.
    words = shlex.split(command.replace("@('--%')", "'--%'"))
    with (
        patch("sys.argv", words),
        patch(RENAME_BY_ID, return_value=_project(name)) as mock_rename,
    ):
        main()

    mock_rename.assert_called_once_with(PROJECT_ID, name)


async def test_submit_still_waits_when_naming_fails():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = SubmitAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["Test prompt", "--name", "solana proj", "--wait"])

    mock_project = Mock(spec=Project)
    mock_project.project_id = PROJECT_ID
    mock_project.has_files = False
    # A server that doesn't take a name on create names the project after the prompt.
    mock_project.description = "Test prompt"
    mock_project.rename = AsyncMock(side_effect=AristotleAPIError("timed out"))

    mock_task = _real_task(TaskStatus.COMPLETE)

    mock_project.get_tasks = AsyncMock(return_value=([mock_task], None))
    mock_project.refresh = AsyncMock()

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=mock_project),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[])) as wait,
    ):
        assert await action.run_action(args) == 0

    mock_project.rename.assert_called_once_with("solana proj")
    wait.assert_awaited_once()


@pytest.mark.parametrize(("named", "renamed"), [("FIFO overflow", False), ("Prove it", True)], ids=["taken", "ignored"])
async def test_submit_names_the_project_on_create_and_renames_it_only_if_the_server_ignored_the_name(
    named, renamed, caplog
):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    project = _project(named)
    renamed_project = _project("FIFO overflow")

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=project) as create,
        patch(RENAME_BY_ID, AsyncMock(return_value=renamed_project)) as rename,
    ):
        assert await action.run_action(action.parser.parse_args(["Prove it", "--name", "FIFO overflow"])) is None

    assert create.call_args.kwargs["name"] == "FIFO overflow"
    assert rename.await_count == int(renamed)
    assert "Project name: FIFO overflow" in caplog.messages


def _real_task(status: TaskStatus) -> AgentTask:
    return AgentTask(
        project_id=PROJECT_ID,
        agent_task_id="task-456",
        status=status,
        created_at=datetime(2026, 9, 1, 12, 0),
        last_updated_at=datetime(2026, 9, 1, 13, 0),
        percent_complete=None,
        file_name=None,
        description="Test prompt",
        output_summary=None,
    )


async def test_submit_json_prints_the_project_and_its_task(properties, capsys):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    project = _project("Test prompt")

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=project),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([_real_task(TaskStatus.QUEUED)], None))),
    ):
        assert await action.run_action(action.parser.parse_args(["Test prompt", "--json"])) is None

    properties.assert_awaited_once_with(PROJECT_ID, declaration_type=DeclarationType.PROPERTY)
    result = json.loads(capsys.readouterr().out)
    assert list(result) == [
        "project",
        "task",
        "web_url",
        "result_path",
        "property_counts",
        "requested_counts",
        "properties",
    ]
    assert result["project"]["project_id"] == PROJECT_ID
    assert result["task"]["status"] == "QUEUED"
    assert result["web_url"] == f"https://aristotle.harmonic.fun/projects/{PROJECT_ID}"
    assert result["result_path"] is None
    assert result["property_counts"] is None
    assert result["properties"] == []


@pytest.mark.parametrize(
    ("status", "code", "counts"),
    [
        (TaskStatus.COMPLETE, 0, {"proved": 1, "negated": 1, "unchecked": 0}),
        (TaskStatus.OUT_OF_BUDGET, 1, {"proved": 0, "negated": 0, "unchecked": 2}),
    ],
)
async def test_submit_wait_json_waits_quietly_and_prints_the_finished_run(
    status, code, counts, properties, tmp_path, capsys
):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    project = _project("Test prompt")
    task = _real_task(TaskStatus.QUEUED)
    archive = tmp_path / "result.tar.gz"

    async def finish(
        num_events: int = 3,
        interactive: bool = True,
        stop_on_questions: bool = False,
        title: str | None = None,
        carry_on: str | None = None,
        resume: list[list[str]] | None = None,
    ) -> None:
        task.status = status

    async def refresh() -> None:
        project.has_files = True

    async def listed(project_id: str, declaration_type: DeclarationType) -> list[Declaration]:
        assert task.status == status
        fails = _property("Spec.Fails", DeclarationStatus.NEGATED).model_copy(
            update={"artifact_paths": ["sim/cex.vcd"]}
        )
        return [_property("Spec.Holds", DeclarationStatus.PROVED), fails]

    properties.side_effect = listed
    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=project),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([task], None))),
        patch.object(Project, "refresh", AsyncMock(side_effect=refresh)),
        patch.object(Project, "get_files", AsyncMock(return_value=archive)) as get_files,
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)) as wait,
    ):
        argv = ["Test prompt", "--wait", "--json", "--destination", str(archive)]
        assert await action.run_action(action.parser.parse_args(argv)) == code

    wait.assert_awaited_once_with(interactive=False, stop_on_questions=False, title=ANY, carry_on=ANY, resume=ANY)
    get_files.assert_awaited_once_with(destination=str(archive))
    # Broken off, it names the wait that carries on the same way, saving to the same place.
    resumed = f"aristotle show {PROJECT_ID} --task task-456 --wait --json --destination="
    assert resumed in wait.await_args.kwargs["carry_on"]
    # Ended badly, it names the follow-up, then the same wait, as `continue` takes no --destination.
    follow_up, wait_again = wait.await_args.kwargs["resume"]
    assert follow_up == ["aristotle", "continue", PROJECT_ID, "Continue"]
    assert wait_again[:-1] == ["aristotle", "show", PROJECT_ID, "--wait", "--json"]
    assert wait_again[-1].startswith("--destination=")
    result = json.loads(capsys.readouterr().out)
    assert result["task"]["status"] == status.value
    assert result["result_path"] == str(archive)
    disproved = counts["negated"]
    assert result["property_counts"] == {
        "counterexample": 0,
        "in_progress": 0,
        "unresolved": 0,
        "disproved": disproved,
        **counts,
    }
    checked = counts["unchecked"] == 0
    assert result["properties"] == [
        {
            "declaration_id": "id-Spec.Holds",
            "name": "Spec.Holds",
            "status": 3,
            "failure_reason": None,
            "unchecked": not checked,
            "status_name": "PROVED",
            "failure_reason_name": None,
            "source": 2,
            "source_name": "USER",
            "artifact_paths": [],
            "outcome": "proved" if checked else "unchecked",
        },
        {
            "declaration_id": "id-Spec.Fails",
            "name": "Spec.Fails",
            "status": 2,
            "failure_reason": None,
            "unchecked": not checked,
            "status_name": "NEGATED",
            "failure_reason_name": None,
            "source": 2,
            "source_name": "USER",
            "artifact_paths": ["sim/cex.vcd"],
            "outcome": "disproved" if checked else "unchecked",
        },
    ]


@pytest.mark.parametrize("wait_flag", [[], ["--wait"]])
async def test_submit_json_properties_are_null_when_they_cannot_be_fetched(wait_flag, properties, capsys):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    properties.side_effect = AristotleAPIError("bad gateway", status_code=502)

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=_project("Test prompt")),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([_real_task(TaskStatus.COMPLETE)], None))),
        patch.object(Project, "refresh", AsyncMock()),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[])),
    ):
        code = await action.run_action(action.parser.parse_args(["Test prompt", "--json", *wait_flag]))

    assert code == (0 if wait_flag else None)
    result = json.loads(capsys.readouterr().out)
    assert result["project"]["project_id"] == PROJECT_ID
    assert (result["property_counts"], result["properties"]) == (None, None)


@pytest.mark.parametrize("destination", [None, SPACED], ids=["no destination", "destination"])
@pytest.mark.parametrize("status", [TaskStatus.IN_PROGRESS, TaskStatus.QUEUED])
@pytest.mark.parametrize("json_flag", [[], ["--json"]])
async def test_a_wait_that_ends_before_the_task_downloads_nothing(status, json_flag, destination, capsys, caplog):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    project = _project("Test prompt")
    task = _real_task(status)

    async def refresh() -> None:
        project.has_files = True

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=project),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([task], None))),
        patch.object(Project, "refresh", AsyncMock(side_effect=refresh)),
        patch.object(Project, "get_files", AsyncMock()) as get_files,
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[])),
    ):
        argv = ["Test prompt", "--wait", *json_flag, *([] if destination is None else ["--destination", destination])]
        assert await action.run_action(action.parser.parse_args(argv)) == 3

    get_files.assert_not_awaited()
    assert (
        "Nothing was downloaded, since the task has not finished. "
        + shell_hint("Once it has, get its files with", _download(destination))
    ) in caplog.messages
    out = capsys.readouterr().out
    if json_flag:
        result = json.loads(out)
        assert result["task"]["status"] == status.value
        assert result["result_path"] is None
    else:
        assert out == ""


@pytest.mark.parametrize("wait_flag", ["--wait", "--non-interactive"])
@pytest.mark.parametrize("destination", ["notes.txt/result.tar.gz", "notes.txt/new/dir/", "notes.txt/"])
async def test_submit_to_a_destination_through_a_file_exits_2_without_creating_a_project(
    wait_flag, destination, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_text("notes")
    (tmp_path / "rtl").mkdir()
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    argv = ["Test prompt", "--project-dir", "rtl", wait_flag, "--destination", destination]

    with (
        patch("aristotlelib.cli.command.submit.Project.create") as create,
        patch("aristotlelib.cli.command.submit.Project.create_from_directory") as create_from_directory,
        pytest.raises(SystemExit) as exited,
    ):
        action.preflight(action.parser.parse_args(argv))

    assert exited.value.code == 2
    create.assert_not_called()
    create_from_directory.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"Cannot save the archive to {destination}: notes.txt is not a directory" in captured.err


DESTINATION_IGNORED = (
    "WARNING: --destination needs --wait or --non-interactive, so it is ignored, and 4.0 will refuse it. Once the run "
    "has finished, `aristotle download <project-id> --destination <path>` saves the result archive."
)


async def test_submit_without_wait_warns_that_the_destination_is_ignored_and_does_not_check_it(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_text("notes")
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()

    with patch("aristotlelib.cli.command.submit.Project.create", return_value=_project("Test prompt")) as create:
        argv = ["Test prompt", "--destination", "notes.txt/result.tar.gz"]
        args = action.parser.parse_args(argv)
        assert action.preflight(args) is None
        [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
        create.assert_not_called()
        assert await action.run_action(args) is None

    assert warning.message == DESTINATION_IGNORED
    create.assert_called_once()


@pytest.mark.parametrize("argv", [["--wait"], ["--non-interactive"], []], ids=["--wait", "--non-interactive", "none"])
async def test_only_a_destination_without_a_wait_is_warned_of(argv, tmp_path, caplog):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    destination = [] if not argv else ["--destination", str(tmp_path / "result.tar.gz")]

    assert action.preflight(action.parser.parse_args(["Test prompt", *argv, *destination])) is None

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_submit_wait_saves_into_a_destination_directory_under_the_default_name(tmp_path, capsys):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    project = _project("Test prompt")
    project.has_files = True
    archive = Mock()
    archive.headers = {"content-disposition": f'attachment; filename="{PROJECT_ID}-aristotle.tar.gz"'}
    archive.content = b"archive"
    client = AsyncMock()
    client.get = AsyncMock(return_value=archive)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=project),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([_real_task(TaskStatus.COMPLETE)], None))),
        patch.object(Project, "refresh", AsyncMock()),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[])),
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
    ):
        argv = ["Test prompt", "--wait", "--json", "--destination", str(tmp_path)]
        assert await action.run_action(action.parser.parse_args(argv)) == 0

    saved = tmp_path / f"{PROJECT_ID}-aristotle.tar.gz"
    assert saved.read_bytes() == b"archive"
    assert json.loads(capsys.readouterr().out)["result_path"] == str(saved)


@pytest.mark.parametrize("status", [TaskStatus.COMPLETE, TaskStatus.OUT_OF_BUDGET])
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            OSError(errno.ENOSPC, "No space left on device", "result.tar.gz"),
            "[Errno 28] No space left on device: 'result.tar.gz'",
        ),
        (AristotleAPIError("Request failed: timed out"), "Request failed: timed out"),
    ],
)
async def test_a_save_that_fails_after_the_run_still_prints_the_json_and_exits_3(
    status, error, reason, tmp_path, monkeypatch, capsys, caplog
):
    monkeypatch.chdir(tmp_path)
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    project = _project("Test prompt")
    project.has_files = True

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=project),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([_real_task(status)], None))),
        patch.object(Project, "refresh", AsyncMock()),
        patch.object(Project, "get_files", AsyncMock(side_effect=error)),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[])),
    ):
        argv = ["Test prompt", "--wait", "--json", "--destination", SPACED]
        assert await action.run_action(action.parser.parse_args(argv)) == 3

    result = json.loads(capsys.readouterr().out)
    assert result["task"]["status"] == status.value
    assert result["result_path"] is None
    assert caplog.records[-1].levelno == logging.ERROR
    assert (
        caplog.messages[-1]
        == f"The project's files were not saved: {reason}\n{shell_hint('Get them with', _download())}"
    )


@pytest.mark.parametrize("status", [TaskStatus.COMPLETE, TaskStatus.IN_PROGRESS], ids=["finished", "still running"])
async def test_a_refresh_that_fails_after_the_wait_still_prints_the_json_and_exits_3(status, capsys, caplog):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    project = _project("Test prompt")
    project.has_files = True
    unavailable = AristotleAPIError("GET /project/p after 5 attempts over 21 s: Service Unavailable", 503)

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=project),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([_real_task(status)], None))),
        patch.object(Project, "refresh", AsyncMock(side_effect=unavailable)),
        patch.object(Project, "get_files", AsyncMock()) as get_files,
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[])),
    ):
        argv = ["Test prompt", "--wait", "--json", "--destination", SPACED]
        assert await action.run_action(action.parser.parse_args(argv)) == 3

    result = json.loads(capsys.readouterr().out)
    assert result["task"]["status"] == status.value
    assert result["result_path"] is None
    get_files.assert_not_awaited()
    if status == TaskStatus.COMPLETE:
        assert caplog.messages[-1] == (
            f"The project's files were not saved, since the project could not be refreshed: {unavailable}\n"
            + shell_hint("Get them with", _download())
        )


def test_a_wait_whose_event_stream_keeps_answering_503_prints_the_task_as_it_last_stood(
    api_server, monkeypatch, capsys
):
    fast_api_clock(monkeypatch)
    task = _real_task(TaskStatus.IN_PROGRESS).model_dump(mode="json")
    project = _project("Prove it").model_dump(mode="json")
    streams: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/events-stream"):
            streams.append(request)
            return httpx.Response(503, json={"detail": "Service Unavailable"})
        if path.endswith("/tasks"):
            return httpx.Response(200, json={"agent_tasks": [task], "pagination_key": None})
        if path.endswith("/questions"):
            return httpx.Response(200, json=[])
        if "/task/" in path:
            return httpx.Response(200, json=task)
        return httpx.Response(200, json=project)

    api_server(*[serve] * 120)

    with patch("sys.argv", ["aristotle", "submit", "Prove it", "--wait", "--json"]):
        assert main() == 3

    result = json.loads(capsys.readouterr().out)
    assert result["task"]["status"] == "IN_PROGRESS"
    assert result["result_path"] is None
    # The run is live, so the stream rides out ten minutes of refusals, backing off to 30 s, before it gives up.
    assert len(streams) == 24


async def test_submit_wait_says_where_it_saved_the_archive(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    project = _project("Test prompt")
    project.has_files = True
    archive = tmp_path / f"{PROJECT_ID}-aristotle.tar.gz"

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=project),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([_real_task(TaskStatus.COMPLETE)], None))),
        patch.object(Project, "refresh", AsyncMock()),
        patch.object(Project, "get_files", AsyncMock(return_value=archive)),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[])),
    ):
        assert await action.run_action(action.parser.parse_args(["Test prompt", "--wait"])) == 0

    assert caplog.messages[-1] == f"Saved the project's files to {archive}"


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
async def test_submit_wait_counts_the_properties_before_saying_where_it_saved_the_archive(
    json_flag, properties, tmp_path, caplog, capsys
):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    project = _project("Test prompt")
    project.has_files = True
    task = _real_task(TaskStatus.IN_PROGRESS)
    archive = tmp_path / f"{PROJECT_ID}-aristotle.tar.gz"

    async def finish(
        num_events: int = 3,
        interactive: bool = True,
        stop_on_questions: bool = False,
        title: str | None = None,
        carry_on: str | None = None,
        resume: list[list[str]] | None = None,
    ) -> list:
        task.status = TaskStatus.COMPLETE
        return []

    async def listed(project_id: str, declaration_type: DeclarationType) -> list[Declaration]:
        assert task.status == TaskStatus.COMPLETE
        return [_property("Spec.Holds", DeclarationStatus.PROVED), _property("Spec.Fails", DeclarationStatus.NEGATED)]

    properties.side_effect = listed
    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=project),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([task], None))),
        patch.object(Project, "refresh", AsyncMock()),
        patch.object(Project, "get_files", AsyncMock(return_value=archive)),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)),
    ):
        assert await action.run_action(action.parser.parse_args(["Test prompt", "--wait", *json_flag])) == 0

    saved = f"Saved the project's files to {archive}"
    if json_flag:
        assert json.loads(capsys.readouterr().out)["property_counts"]["negated"] == 1
        assert caplog.messages[-1] == saved
        assert not [message for message in caplog.messages if message.startswith("Properties:")]
    else:
        assert caplog.messages[-2:] == ["Properties: 1 proved · 1 disproved · 0 in progress", saved]


async def test_submit_wait_without_json_keeps_the_interactive_display():
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    task = _real_task(TaskStatus.COMPLETE)

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=_project("Test prompt")),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([task], None))),
        patch.object(Project, "refresh", AsyncMock()),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[])) as wait,
    ):
        await action.run_action(action.parser.parse_args(["Test prompt", "--wait"]))

    wait.assert_awaited_once_with(interactive=True, stop_on_questions=False, title=ANY, carry_on=ANY, resume=ANY)
    assert wait.await_args.kwargs["carry_on"].endswith(f"aristotle show {PROJECT_ID} --task task-456 --wait")
    assert wait.await_args.kwargs["resume"] == [["aristotle", "continue", PROJECT_ID, "Continue", "--wait"]]


async def test_submit_json_still_prints_the_project_when_listing_its_task_fails(capsys, caplog):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    throttled = AristotleAPIError("Too many requests to Aristotle at once. Wait a few seconds and try again.", 429)

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=_project("Test prompt")),
        patch.object(Project, "get_tasks", AsyncMock(side_effect=throttled)),
    ):
        assert await action.run_action(action.parser.parse_args(["Test prompt", "--json"])) is None

    result = json.loads(capsys.readouterr().out)
    assert result["project"]["project_id"] == PROJECT_ID
    assert result["task"] is None
    assert result["property_counts"] is None
    assert caplog.records[-1].levelno == logging.WARNING
    assert f"Project {PROJECT_ID} was created, but listing its task failed" in caplog.messages[-1]


def _pending(number: int) -> Question:
    return Question(
        question_id=f"q-{number}",
        event_id=f"event-{number}",
        project_id=PROJECT_ID,
        agent_run_id="task-456",
        content=f"Question {number}?",
        suggestions=["Yes", "No"],
        created_at=datetime(2026, 9, 1, 12, number),
        updated_at=datetime(2026, 9, 1, 12, number),
    )


async def test_non_interactive_submit_stops_for_questions_and_exits_10(properties, capsys, caplog):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    task = _real_task(TaskStatus.IN_PROGRESS)
    questions = [_pending(1), _pending(2)]

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=_project("Test prompt")) as create,
        patch.object(Project, "get_tasks", AsyncMock(return_value=([task], None))),
        patch.object(Project, "get_files", AsyncMock()) as get_files,
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=questions)) as wait,
        caplog.at_level(logging.INFO),
    ):
        assert await action.run_action(action.parser.parse_args(["Test prompt", "--non-interactive"])) == 10

    assert create.call_args.kwargs["agent_questions_setting"] == AgentQuestionsSetting.TIMEOUT_15_MIN
    wait.assert_awaited_once_with(interactive=False, stop_on_questions=True, title=ANY, carry_on=ANY, resume=ANY)
    assert f"aristotle show {PROJECT_ID} --task task-456 --non-interactive" in wait.await_args.kwargs["carry_on"]
    assert wait.await_args.kwargs["resume"] == [["aristotle", "continue", PROJECT_ID, "Continue", "--non-interactive"]]
    get_files.assert_not_awaited()
    properties.assert_not_awaited()
    pending = json.loads(capsys.readouterr().out)
    assert pending["project_id"] == PROJECT_ID
    assert pending["task"]["agent_task_id"] == "task-456"
    assert [q["question_id"] for q in pending["questions"]] == ["q-1", "q-2"]
    assert caplog.messages[-1] == (
        "Aristotle is waiting for answers. Answer with:\n"
        f"  aristotle questions {PROJECT_ID} --task task-456 --answer <question-id>=<answer>\n"
        "then carry on waiting with:\n"
        f"  aristotle show {PROJECT_ID} --task task-456 --non-interactive"
    )


@pytest.mark.parametrize(
    "destination", ["out/", "my results/run 1.tar.gz", "-out/"], ids=["plain", "needs quoting", "starts with a dash"]
)
async def test_a_destination_carries_into_the_command_that_resumes_the_wait(destination, tmp_path, caplog):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=_project("Test prompt")),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([_real_task(TaskStatus.IN_PROGRESS)], None))),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[_pending(1)])),
        caplog.at_level(logging.INFO),
    ):
        argv = ["Test prompt", "--non-interactive", f"--destination={destination}"]
        assert await action.run_action(action.parser.parse_args(argv)) == 10

    assert caplog.messages[-1].endswith(resume_hint(_real_task(TaskStatus.IN_PROGRESS), destination))
    assert f"aristotle show {PROJECT_ID} --task task-456 --non-interactive" in caplog.messages[-1]


async def test_non_interactive_submit_without_questions_prints_the_finished_run(capsys):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    task = _real_task(TaskStatus.IN_PROGRESS)

    async def finish(
        num_events: int = 3,
        interactive: bool = True,
        stop_on_questions: bool = False,
        title: str | None = None,
        carry_on: str | None = None,
        resume: list[list[str]] | None = None,
    ) -> list:
        task.status = TaskStatus.COMPLETE
        return []

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=_project("Test prompt")),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([task], None))),
        patch.object(Project, "refresh", AsyncMock()),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)),
    ):
        assert await action.run_action(action.parser.parse_args(["Test prompt", "--non-interactive"])) == 0

    assert json.loads(capsys.readouterr().out)["task"]["status"] == "COMPLETE"


def _per_second_limit() -> httpx.Response:
    return httpx.Response(429, json={"detail": "Too many requests."})


def test_a_submit_refused_by_the_per_second_limit_is_sent_again_and_creates_the_project(api_server, caplog):
    caplog.set_level(logging.INFO)
    requests = api_server(_per_second_limit(), httpx.Response(200, json=_project(None).model_dump(mode="json")))

    with patch("sys.argv", ["aristotle", "submit", "Prove it"]):
        assert main() == 0

    assert len(requests) == 2
    assert f"Project created: {PROJECT_ID}" in caplog.messages


def _found_by_name(name: str) -> str:
    """The command that finds a project by its name, quoted for this machine's shells, and how to tell it apart."""
    return (
        shell_hint("find it by its name with", ["aristotle", "show", "--", name])
        + "\nwhich shows how old the project's run is: one older than this command belongs to another project of that "
        "name."
    )


@pytest.mark.parametrize("name", [None, "nightly", "Bob's proj"], ids=["unnamed", "plain", "apostrophe"])
@pytest.mark.parametrize("source", ["prompt", "--project-dir", "-F"])
@pytest.mark.parametrize("json_flag", [["--json"], []], ids=["--json", "no --json"])
def test_a_submit_whose_response_is_lost_exits_7_and_says_how_to_find_the_project(
    api_server, caplog, capsys, tmp_path, json_flag: list[str], source: str, name: str | None
):
    (tmp_path / "notes.md").write_text("# Notes\n")
    requests = api_server(lambda request: httpx.ReadTimeout(""))
    named = ["--name", name] if name else []
    folder = _source(tmp_path, source)

    with patch("sys.argv", ["aristotle", "submit", "Prove it", *named, *folder, *json_flag]):
        assert main() == 7

    assert len(requests) == 1
    found = f" Or {_found_by_name(name)}" if name else ""
    error = (
        "ReadTimeout on POST /project. Aristotle may have acted on it, so it was not sent again. "
        "If it did, the project was created, though it may take a few minutes to show up: check `aristotle list` "
        f"before submitting again.{found}"
    )
    assert caplog.messages[-1] == error
    out = capsys.readouterr().out
    assert (json.loads(out) if json_flag else out) == ({"outcome": "unknown", "error": error} if json_flag else "")


@pytest.mark.parametrize("json_flag", [["--json"], []], ids=["--json", "no --json"])
def test_a_submit_answered_by_a_page_that_isnt_aristotles_exits_7_as_its_answer_was_lost(
    api_server, caplog, capsys, monkeypatch, json_flag: list[str]
):
    monkeypatch.setattr(api_request, "getproxies", dict)
    requests = api_server(httpx.Response(200, html="<title>Hotel WiFi - Sign in</title>"))

    with patch("sys.argv", ["aristotle", "submit", "Prove it", *json_flag]):
        assert main() == 7

    assert len(requests) == 1
    error = (
        "POST /project: the answer from https://aristotle.harmonic.fun was 200 OK with text/html, not Aristotle's: a "
        "captive portal or a proxy may have answered instead. Aristotle may have acted on it, so it was not sent "
        "again. If it did, the project was created, though it may take a few minutes to show up: check `aristotle "
        "list` before submitting again."
    )
    assert caplog.messages[-1] == error
    out = capsys.readouterr().out
    assert (json.loads(out) if json_flag else out) == ({"outcome": "unknown", "error": error} if json_flag else "")


@pytest.mark.parametrize(
    ("refusal", "failure", "code"),
    [
        (
            lambda request: httpx.ConnectError("[Errno 111] Connection refused"),
            "ConnectError on POST /project after 5 attempts over 0 s: [Errno 111] Connection refused.",
            3,
        ),
        (
            lambda request: httpx.Response(429, json={"detail": "Too many requests."}),
            "POST /project after 5 attempts over 0 s: Too many requests to Aristotle at once. Wait a few seconds and "
            "try again.",
            6,
        ),
    ],
    ids=["connect error", "per-second 429"],
)
@pytest.mark.parametrize("json_flag", [["--json"], []], ids=["--json", "no --json"])
@pytest.mark.usefixtures("frozen_api_clock")
def test_a_submit_that_no_attempt_got_through_says_no_project_was_created(
    api_server, caplog, capsys, refusal, failure: str, code: int, json_flag: list[str]
):
    requests = api_server(*[refusal] * api_request.RETRY_ATTEMPTS)

    with patch("sys.argv", ["aristotle", "submit", "Prove it", *json_flag]):
        assert main() == code

    assert len(requests) == api_request.RETRY_ATTEMPTS
    error = f"{failure} Aristotle didn't act on it. No project was created, so it is safe to submit it again."
    assert caplog.messages[-1] == error
    out = capsys.readouterr().out
    assert (json.loads(out) if json_flag else out) == (
        {"outcome": "not_processed", "error": error} if json_flag else ""
    )


@pytest.mark.parametrize("json_flag", [["--json"], []], ids=["--json", "no --json"])
def test_a_submit_that_h11_refused_to_send_exits_3_without_quoting_the_header(
    api_server, caplog, capsys, json_flag: list[str]
):
    requests = api_server(httpx.LocalProtocolError(f"Illegal header value b'{FAKE_KEY}\\n'"))

    with patch("sys.argv", ["aristotle", "submit", "Prove it", *json_flag]):
        assert main() == 3

    assert len(requests) == 1
    error = (
        "LocalProtocolError on POST /project. Aristotle didn't act on it. No project was created, so it is safe to "
        "submit it again."
    )
    assert caplog.messages[-1] == error
    out = capsys.readouterr().out
    assert (json.loads(out) if json_flag else out) == (
        {"outcome": "not_processed", "error": error} if json_flag else ""
    )
    assert shown_key_runs(out + caplog.text, FAKE_KEY) == []


QUEUEING_LIMIT = (
    "Too many runs in progress: 1 can run at once and 0 more can wait in the queue. Wait for one to finish, or cancel a "
    "queued one."
)


@pytest.mark.parametrize(
    ("detail", "reason"),
    [
        ("Too many inflight requests. Please wait for them to finish.", api_request.PROJECT_LIMIT_FALLBACK),
        (QUEUEING_LIMIT, QUEUEING_LIMIT),
        ("Too many runs in progress", "Too many runs in progress."),
    ],
    ids=["legacy", "queueing", "no full stop"],
)
@pytest.mark.parametrize("flags", [["--json"], ["--non-interactive"], []], ids=["--json", "--non-interactive", "text"])
def test_a_submit_refused_for_too_many_runs_exits_6_and_says_no_project_was_created(
    api_server, caplog, capsys, detail: str, reason: str, flags: list[str]
):
    requests = api_server(httpx.Response(429, json={"detail": detail}))

    with patch("sys.argv", ["aristotle", "submit", "Prove it", *flags]):
        assert main() == 6

    assert len(requests) == 1
    error = f"{reason} No project was created, so it is safe to submit it again."
    assert caplog.messages[-1] == error
    out = capsys.readouterr().out
    assert (json.loads(out) if flags else out) == ({"outcome": "not_processed", "error": error} if flags else "")


def _source(folder: Path, source: str) -> list[str]:
    """The options that send the prompt alone, ``folder``, or the block a filelist in it describes, without a folder."""
    if source == "-F":
        (folder / "core.v").write_text("module core; endmodule\n")
        (folder / "core.f").write_text("core.v\n")
        return ["-F", str(folder / "core.f")]
    return ["--project-dir", str(folder)] if source == "--project-dir" else []


_INTERRUPTED = (
    "Interrupted during the create: the project may have been created and its run started, though it may take a few "
    "minutes to show up. Look in `aristotle list` for a project newer than this command"
)


@pytest.mark.parametrize("name", [None, "Bob's proj"], ids=["unnamed", "named"])
@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, asyncio.CancelledError])
@pytest.mark.parametrize("source", ["prompt", "--project-dir", "-F"])
async def test_an_interrupted_create_says_the_project_may_have_been_made_and_how_to_find_and_cancel_it(
    source, interrupt, name, api_server, caplog, tmp_path
):
    (tmp_path / "notes.md").write_text("# Notes\n")

    def upload_then_interrupt(request: httpx.Request) -> httpx.Response:
        raise interrupt

    requests = api_server(upload_then_interrupt)
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    argv = ["Prove it", *(["--name", name] if name else []), *_source(tmp_path, source)]

    with pytest.raises(interrupt):
        await action.run_action(action.parser.parse_args(argv))

    assert len(requests) == 1
    found = f", or {_found_by_name(name)}" if name else "."
    assert caplog.messages[-1] == f"{_INTERRUPTED}{found} Cancel it with `aristotle cancel <id>`."


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, asyncio.CancelledError])
async def test_an_interrupt_before_the_create_is_sent_says_nothing_was_sent(interrupt, api_server, caplog, tmp_path):
    (tmp_path / "notes.md").write_text("# Notes\n")
    requests = api_server()
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    argv = ["Prove it", "--name", "nightly", "--project-dir", str(tmp_path)]

    # While the archive is built, as for a large folder.
    with patch("aristotlelib.project.tarfile.open", side_effect=interrupt), pytest.raises(interrupt):
        await action.run_action(action.parser.parse_args(argv))

    assert requests == []
    assert caplog.messages[-1] == "Interrupted before the create was sent: no project was created."


async def test_a_ctrl_c_that_asyncio_defers_during_the_archive_lands_before_the_create_is_sent(
    api_server, caplog, tmp_path
):
    (tmp_path / "notes.md").write_text("# Notes\n")
    requests = api_server()
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    real_open = tarfile.open

    # From Python 3.11, asyncio.run takes a Ctrl-C as a cancel of the task, which lands at its next wait, as here.
    def cancelled_while_building(*args, **kwargs):
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        return real_open(*args, **kwargs)

    argv = ["Prove it", "--name", "nightly", "--project-dir", str(tmp_path)]
    with patch("aristotlelib.project.tarfile.open", side_effect=cancelled_while_building):
        with pytest.raises(asyncio.CancelledError):
            await action.run_action(action.parser.parse_args(argv))

    assert requests == []
    assert caplog.messages[-1] == "Interrupted before the create was sent: no project was created."


def test_a_submit_interrupted_during_the_create_exits_130_with_the_hint(api_server, caplog):
    def upload_then_interrupt(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    api_server(upload_then_interrupt)

    with patch("sys.argv", ["aristotle", "submit", "Prove it", "--name", "nightly"]):
        assert main() == 130

    assert (
        caplog.messages[-1] == f"{_INTERRUPTED}, or {_found_by_name('nightly')} Cancel it with `aristotle cancel <id>`."
    )


@pytest.mark.parametrize("cut_short", ["wait", "download", "counts"])
async def test_an_interrupted_submit_wait_json_still_prints_the_run_as_it_last_stood(
    cut_short, properties, tmp_path, capsys, caplog
):
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    project = _project("Test prompt")
    project.description = "fifo proofs"
    task = _real_task(TaskStatus.IN_PROGRESS)

    async def finish(
        num_events: int = 3,
        interactive: bool = True,
        stop_on_questions: bool = False,
        title: str | None = None,
        carry_on: str | None = None,
        resume: list[list[str]] | None = None,
    ) -> None:
        if cut_short == "wait":
            raise KeyboardInterrupt
        task.status = TaskStatus.COMPLETE

    async def refresh() -> None:
        project.has_files = True

    archive = tmp_path / "result.tar.gz"
    saved = AsyncMock(return_value=archive) if cut_short == "counts" else AsyncMock(side_effect=asyncio.CancelledError)
    if cut_short == "counts":
        properties.side_effect = KeyboardInterrupt
    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=project),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([task], None))),
        patch.object(Project, "refresh", AsyncMock(side_effect=refresh)),
        patch.object(Project, "get_files", saved),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)) as wait,
        pytest.raises((KeyboardInterrupt, asyncio.CancelledError)),
    ):
        argv = ["Test prompt", "--wait", "--json", "--destination", SPACED]
        await action.run_action(action.parser.parse_args(argv))

    assert wait.await_args.kwargs["title"] == "fifo proofs"
    out = capsys.readouterr().out
    result = json.loads(out)
    assert result["project"]["project_id"] == PROJECT_ID
    assert result["task"]["status"] == ("IN_PROGRESS" if cut_short == "wait" else "COMPLETE")
    # The project's run, as its task has it.
    assert result["project"]["latest_run_status"] == result["task"]["status"]
    assert result["project"]["status_name"] == ("RUNNING" if cut_short == "wait" else "IDLE")
    # An archive saved before the cut stays named.
    assert result["result_path"] == (str(archive) if cut_short == "counts" else None)
    assert result["property_counts"] is None and result["properties"] is None
    # Only a cut between the run's end and the save leaves files to save.
    not_saved = [m for m in caplog.messages if m.startswith(NOT_SAVED)]
    assert not_saved == (
        [f"{NOT_SAVED}\n{shell_hint('Get them with', _download())}"] if cut_short == "download" else []
    )


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, asyncio.CancelledError])
@pytest.mark.parametrize("destination", [SPACED, None], ids=["destination", "no destination"])
@pytest.mark.parametrize("cut_short", ["counts", "refresh", "download"])
async def test_a_submit_wait_cut_short_once_the_run_has_finished_says_the_files_were_not_saved(
    cut_short, destination, interrupt, properties, caplog
):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    project = _project("Test prompt")
    project.has_files = True
    task = _real_task(TaskStatus.IN_PROGRESS)

    async def finish(**_: object) -> list:
        task.status = TaskStatus.COMPLETE
        return []

    if cut_short == "counts":
        properties.side_effect = interrupt
    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=project),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([task], None))),
        patch.object(Project, "refresh", AsyncMock(side_effect=interrupt if cut_short == "refresh" else None)),
        patch.object(Project, "get_files", AsyncMock(side_effect=interrupt)) as get_files,
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)),
        pytest.raises(interrupt),
    ):
        argv = ["Test prompt", "--wait", *([] if destination is None else ["--destination", destination])]
        await action.run_action(action.parser.parse_args(argv))

    assert get_files.await_count == (cut_short == "download")
    assert caplog.records[-1].levelno == logging.WARNING
    assert caplog.messages[-1] == f"{NOT_SAVED}\n{shell_hint('Get them with', _download(destination))}"


@pytest.mark.parametrize("flags", [["--wait"], ["--wait", "--json"], ["--non-interactive"]], ids=" ".join)
async def test_an_interrupted_wait_names_the_wait_that_carries_on_and_saves_to_the_destination(flags, caplog):
    """While the run goes on, nothing is due yet: the command that waits on saves the files where this one would."""
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=_project("Test prompt")),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([_real_task(TaskStatus.IN_PROGRESS)], None))),
        patch("aristotlelib.agent_task.Question.list_for_task", AsyncMock(side_effect=KeyboardInterrupt)),
        patch("aristotlelib.progress_utils.print_progress_lines"),
        pytest.raises(KeyboardInterrupt),
    ):
        await action.run_action(action.parser.parse_args(["Test prompt", *flags, "--destination", SPACED]))

    show = ["aristotle", "show", PROJECT_ID, "--task", "task-456", *flags, f"--destination={SPACED}"]
    assert caplog.messages[-1] == (
        f"Task task-456 is still running.\n{shell_hint('Carry on waiting with', show)}\n"
        f"Cancel it with: aristotle cancel {PROJECT_ID}"
    )


def test_a_submit_wait_interrupted_in_its_download_exits_130_and_says_how_to_save_the_files_there(
    api_server, tmp_path, monkeypatch, caplog
):
    monkeypatch.chdir(tmp_path)
    project = _project("Prove it")
    project.has_files = True
    task = _real_task(TaskStatus.COMPLETE).model_dump(mode="json")

    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/result"):
            raise KeyboardInterrupt
        if request.url.path.endswith("/tasks"):
            return httpx.Response(200, json={"agent_tasks": [task], "pagination_key": None})
        return httpx.Response(200, json=project.model_dump(mode="json"))

    requests = api_server(*[serve] * 10)

    with (
        patch.object(AgentTask, "wait_for_completion", AsyncMock(return_value=[])),
        patch("sys.argv", ["aristotle", "submit", "Prove it", "--wait", "--destination", SPACED]),
    ):
        assert main() == 130

    assert requests[-1].url.path.endswith("/result")
    assert caplog.messages[-1] == f"{NOT_SAVED}\n{shell_hint('Get them with', _download())}"
    assert list(tmp_path.iterdir()) == []


async def test_submit_wait_json_nulls_the_counts_and_exits_3_when_the_tasks_cannot_be_listed(
    properties, capsys, caplog
):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()
    project = _project("Test prompt")
    task = _real_task(TaskStatus.QUEUED)
    earlier = _property("Spec.Holds", DeclarationStatus.PROVED).model_copy(
        update={"last_updated": datetime(2026, 9, 1, 11, 0)}
    )
    properties.return_value = [earlier]

    async def finish(
        num_events: int = 3,
        interactive: bool = True,
        stop_on_questions: bool = False,
        title=None,
        carry_on=None,
        resume=None,
    ) -> None:
        task.status = TaskStatus.COMPLETE

    async def tasks(limit: int, **kwargs):
        if limit == 1:
            return [task], None
        raise AristotleAPIError("Service unavailable", status_code=503)

    with (
        patch("aristotlelib.cli.command.submit.Project.create", return_value=project),
        patch.object(Project, "get_tasks", AsyncMock(side_effect=tasks)),
        patch.object(Project, "from_id", AsyncMock(return_value=project)),
        patch.object(Project, "refresh", AsyncMock()),
        patch.object(AgentTask, "wait_for_completion", AsyncMock(side_effect=finish)),
    ):
        code = await action.run_action(action.parser.parse_args(["Test prompt", "--wait", "--json"]))

    assert code == 3
    result = json.loads(capsys.readouterr().out)
    assert result["task"]["status"] == "COMPLETE"
    # Every count and the properties are null; the rest of the result stands.
    assert (result["property_counts"], result["requested_counts"], result["properties"]) == (None, None, None)
    assert result["project"]["project_id"] == PROJECT_ID
    assert f"aristotle show {PROJECT_ID} --json" in caplog.text


def _submit_and_capture(project_dir: Path, monkeypatch) -> tuple[int | None, dict[str, bytes], Mock]:
    """Run ``aristotle submit`` on ``project_dir`` with Project.create stubbed; the exit code and each file it sent."""
    monkeypatch.setenv("ARISTOTLE_API_KEY", "test-key")
    uploaded: dict[str, bytes] = {}

    async def create(*, tar_file_path, **kwargs):
        with tarfile.open(tar_file_path, encoding="utf-8") as tar:
            for member in tar.getmembers():
                extracted = tar.extractfile(member)
                assert extracted is not None
                uploaded[member.name] = extracted.read()
        return _project(None)

    with (
        patch("sys.argv", ["aristotle", "submit", "Prove it", "--project-dir", str(project_dir)]),
        patch("aristotlelib.cli.command.submit.Project.create", side_effect=create) as mock_create,
    ):
        code = main()
    return code, uploaded, mock_create


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())
    return path


def test_submit_uploads_the_block_its_aristotle_f_describes(tmp_path, monkeypatch, caplog):
    """Files outside the folder go up under _ext/, and a generated aristotle.f in the server's options replaces the
    folder's own."""
    caplog.set_level(logging.INFO)
    wa = tmp_path / "wa"
    _write(wa / "inc" / "defs.vh", "`define W 8\n")
    _write(wa / "ip" / "core.v", "module core; endmodule\n")
    _write(wa / "ip" / "ip.f", "core.v\n")
    block = tmp_path / "block"
    _write(block / "top.sv", '`include "defs.vh"\nmodule top; core u(); endmodule\n')
    _write(block / "README.md", "notes\n")
    _write(block / "aristotle.f", "+incdir+$WA/inc\n-F $WA/ip/ip.f\n--top top\n-sv\ntop.sv\n")
    monkeypatch.setenv("WA", str(wa))

    code, uploaded, _ = _submit_and_capture(block, monkeypatch)

    assert code == 0
    assert sorted(uploaded) == ["README.md", "_ext/WA/inc/defs.vh", "_ext/WA/ip/core.v", "aristotle.f", "top.sv"]
    assert uploaded["_ext/WA/ip/core.v"] == b"module core; endmodule\n"
    assert uploaded["aristotle.f"].decode() == (
        "# Generated by the aristotle CLI from aristotle.f, in the options Aristotle reads.\n"
        "--top top\n--single-unit\n-I _ext/WA/inc\n_ext/WA/ip/core.v\ntop.sv\n"
    )
    assert any(m.startswith("A generated aristotle.f goes up in place of the folder's own") for m in caplog.messages)
    # The folder's aristotle.f may be someone else's: what it takes from outside is a warning, by where each file is.
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        w.startswith(f"WARNING: Uploading 2 files from outside {block}:")
        and f"\n  _ext/WA/ip/core.v   <- {wa / 'ip' / 'core.v'}" in w
        for w in warnings
    )
    assert (
        "WARNING: The filelists take $WA from this machine's environment: the files it names go up, and the values it "
        "sets with them." in warnings
    )
    assert str(wa) not in warnings[-1]
    assert any(
        m.startswith("Ignored -sv (") and m.endswith("aristotle.f:4): a simulator option") for m in caplog.messages
    )
    assert "Uploading 5 files." in caplog.messages


def test_an_aristotle_f_in_the_servers_options_goes_up_as_committed(tmp_path, monkeypatch):
    committed = "--top top\n-I rtl\nrtl/top.sv\n"
    _write(tmp_path / "rtl" / "top.sv", "module top; endmodule\n")
    _write(tmp_path / "aristotle.f", committed)

    code, uploaded, _ = _submit_and_capture(tmp_path, monkeypatch)

    assert code == 0
    assert sorted(uploaded) == ["aristotle.f", "rtl/top.sv"]
    assert uploaded["aristotle.f"].decode() == committed


def test_a_file_the_block_needs_goes_up_though_gitignore_excludes_it(tmp_path, monkeypatch, caplog):
    """Generated RTL is usually ignored; a reached file goes up anyway, and the log names it."""
    caplog.set_level(logging.INFO)
    _write(tmp_path / ".gitignore", "gen/\n")
    _write(tmp_path / "gen" / "out.v", "module out; endmodule\n")
    _write(tmp_path / "gen" / "unused.v", "module unused; endmodule\n")
    _write(tmp_path / "aristotle.f", "gen/out.v\n")

    code, uploaded, _ = _submit_and_capture(tmp_path, monkeypatch)

    assert code == 0
    assert sorted(uploaded) == [".gitignore", "aristotle.f", "gen/out.v"]
    assert "Uploading 1 file an ignore rule matches, since the Verilog block needs it: gen/out.v" in caplog.messages
    assert "Uploading 3 files; 1 left out because it matches an ignore rule." in caplog.messages


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_file_the_block_reaches_through_a_linked_folder_goes_up_at_the_linked_path(tmp_path, monkeypatch):
    _write(tmp_path / "shared" / "common.v", "module common; endmodule\n")
    block = tmp_path / "block"
    (block / "rtl").mkdir(parents=True)
    (block / "rtl" / "common").symlink_to(tmp_path / "shared")
    _write(block / "aristotle.f", "rtl/common/common.v\n")

    code, uploaded, _ = _submit_and_capture(block, monkeypatch)

    assert code == 0
    assert sorted(uploaded) == ["aristotle.f", "rtl/common/common.v"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_linked_ip_folder_goes_up_where_the_block_names_it_listed_by_where_it_is(tmp_path, monkeypatch, caplog):
    """Its files keep their linked places, so every include still reaches its header and nothing goes up twice; a
    committed aristotle.f's warning names where each really is."""
    _write(tmp_path / "shared" / "ip" / "inc" / "defs.vh", "`define W 8\n")
    _write(tmp_path / "shared" / "ip" / "rtl" / "core.v", '`include "../inc/defs.vh"\nmodule core; endmodule\n')
    block = tmp_path / "block"
    block.mkdir()
    (block / "ip").symlink_to(tmp_path / "shared" / "ip")
    _write(block / "top.v", '`include "ip/inc/defs.vh"\nmodule top; core c(); endmodule\n')
    _write(block / "aristotle.f", "--top top\ntop.v\nip/rtl/core.v\n")

    code, uploaded, _ = _submit_and_capture(block, monkeypatch)

    assert code == 0
    assert sorted(uploaded) == ["aristotle.f", "ip/inc/defs.vh", "ip/rtl/core.v", "top.v"]
    assert uploaded["top.v"].decode().startswith('`include "ip/inc/defs.vh"\n')
    assert uploaded["ip/rtl/core.v"].decode().startswith('`include "../inc/defs.vh"\n')
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(f"  ip/rtl/core.v  <- {tmp_path / 'shared' / 'ip' / 'rtl' / 'core.v'}" in w for w in warnings)


def test_an_aristotle_f_naming_a_missing_file_stops_before_any_request(tmp_path, monkeypatch, caplog):
    """A file the CLI can't read stops the command, rather than failing later in the run."""
    _write(tmp_path / "aristotle.f", "--top top\nmissing.v\n")

    code, _, mock_create = _submit_and_capture(tmp_path, monkeypatch)

    assert code == 2
    mock_create.assert_not_called()
    assert caplog.messages[-1] == f"{tmp_path / 'aristotle.f'}:2: {tmp_path / 'missing.v'}: no such file"


@pytest.mark.parametrize("how", ["unreadable", "linked into a folder it can't enter"])
def test_an_aristotle_f_that_cant_be_read_stops_naming_it_alone(tmp_path, monkeypatch, caplog, how: str):
    """It is the folder's, not the command line's."""
    f = tmp_path / "aristotle.f"
    locked = tmp_path / "locked"
    if how == "unreadable":
        _write(f, "top.v\n").chmod(0)
    else:
        if os.name == "nt":
            pytest.skip("POSIX permission bits")
        f.symlink_to(_write(locked / "top.f", "top.v\n"))
        locked.chmod(0o600)
    try:
        if os.access(f, os.R_OK):
            pytest.skip("running as a user who can read any file")
        code, _, mock_create = _submit_and_capture(tmp_path, monkeypatch)
    finally:
        if how == "unreadable":
            f.chmod(0o644)
        else:
            locked.chmod(0o700)

    assert code == 2
    mock_create.assert_not_called()
    assert caplog.messages[-1] == f"Can't read {filelist.shown(f)}: Permission denied"


def test_an_aristotle_f_with_an_unknown_option_stops(tmp_path, monkeypatch, caplog):
    _write(tmp_path / "aristotle.f", "-xprop-mode\n")

    code, _, mock_create = _submit_and_capture(tmp_path, monkeypatch)

    assert code == 2
    mock_create.assert_not_called()
    assert caplog.messages[-1].endswith("aristotle.f:1: -xprop-mode is not an option Aristotle knows")


def test_a_file_where_one_from_outside_would_go_stops(tmp_path, monkeypatch, caplog):
    _write(tmp_path / "wa" / "a.v")
    block = tmp_path / "block"
    _write(block / "_ext" / "wa" / "a.v", "an older copy\n")
    _write(block / "aristotle.f", f"{(tmp_path / 'wa' / 'a.v').as_posix()}\n")

    code, _, mock_create = _submit_and_capture(block, monkeypatch)

    assert code == 2
    mock_create.assert_not_called()
    assert caplog.messages[-1] == (
        f"{block} has a file at _ext/wa/a.v, where a file its aristotle.f names from outside it would go up. Rename "
        "that file or its folder."
    )


@pytest.mark.parametrize(
    ("local", "file", "inside"),
    [
        (Path("_ext", "wa", "top.v", "notes.txt"), "_ext/wa/top.v", "_ext/wa/top.v/notes.txt"),
        (Path("_ext", "wa"), "_ext/wa", "_ext/wa/top.v"),
    ],
    ids=["a folder where the file goes", "a file where its folder goes"],
)
def test_a_file_from_outside_that_would_share_a_path_with_a_folder_stops(
    tmp_path, monkeypatch, caplog, local: Path, file: str, inside: str
):
    """An archive with one path as both a file and a folder can't be extracted."""
    _write(tmp_path / "wa" / "top.v")
    block = tmp_path / "block"
    _write(block / local, "a local file\n")
    _write(block / "aristotle.f", f"{(tmp_path / 'wa' / 'top.v').as_posix()}\n")

    code, _, mock_create = _submit_and_capture(block, monkeypatch)

    assert code == 2
    mock_create.assert_not_called()
    assert caplog.messages[-1] == (
        f"{block}'s upload can't hold both {file} and {inside}, which needs {file} to be a folder. Rename one of them "
        "or its folder."
    )


def test_a_folder_with_its_own_ext_folder_still_uploads_files_from_outside(tmp_path, monkeypatch):
    _write(tmp_path / "wa" / "a.v")
    block = tmp_path / "block"
    _write(block / "_ext" / "notes.txt")
    _write(block / "aristotle.f", f"{(tmp_path / 'wa' / 'a.v').as_posix()}\n")

    code, uploaded, _ = _submit_and_capture(block, monkeypatch)

    assert code == 0
    assert sorted(uploaded) == ["_ext/notes.txt", "_ext/wa/a.v", "aristotle.f"]


def test_an_include_that_resolves_nowhere_is_a_warning(tmp_path, monkeypatch, caplog):
    _write(tmp_path / "top.v", '`include "gone.vh"\nmodule top; endmodule\n')
    _write(tmp_path / "aristotle.f", "top.v\n")

    code, uploaded, _ = _submit_and_capture(tmp_path, monkeypatch)

    assert code == 0
    assert sorted(uploaded) == ["aristotle.f", "top.v"]
    assert any(
        m.startswith('WARNING: `include "gone.vh" (')
        and m.endswith("top.v:1) names no file Aristotle can find, so none goes up.")
        for m in caplog.messages
    )


def test_a_committed_aristotle_f_goes_up_though_gitignore_excludes_it(tmp_path, monkeypatch):
    _write(tmp_path / ".gitignore", "*.f\n")
    _write(tmp_path / "top.v", "module top; endmodule\n")
    _write(tmp_path / "aristotle.f", "top.v\n")

    code, uploaded, _ = _submit_and_capture(tmp_path, monkeypatch)

    assert code == 0
    assert sorted(uploaded) == [".gitignore", "aristotle.f", "top.v"]


def test_a_relative_project_dir_uploads_its_committed_aristotle_f(tmp_path, monkeypatch):
    """The committed file is the folder's own, however --project-dir is spelled."""
    _write(tmp_path / "gen" / "types.vh")
    _write(tmp_path / "block" / "top.v", f'`include "{(tmp_path / "gen" / "types.vh").as_posix()}"\n')
    _write(tmp_path / "block" / "aristotle.f", "--top top\ntop.v\n")
    monkeypatch.chdir(tmp_path)

    code, uploaded, _ = _submit_and_capture(Path("block"), monkeypatch)

    assert code == 0
    assert sorted(uploaded) == ["_ext/gen/types.vh", "aristotle.f", "top.v"]


@pytest.mark.parametrize(("folder", "sibling"), WILDCARD_FOLDERS)
def test_a_project_dir_whose_path_holds_a_wildcard_uploads_only_its_own_block(tmp_path, monkeypatch, folder, sibling):
    """Not the block of a sibling folder that the path matches as a pattern."""
    for name in (folder, sibling):
        _write(tmp_path / name / "core.v", f"// {name}\nmodule core; endmodule\n")
        _write(tmp_path / name / "aristotle.f", "core.v\n")

    code, uploaded, _ = _submit_and_capture(tmp_path / folder, monkeypatch)

    assert code == 0
    assert uploaded == {"aristotle.f": b"core.v\n", "core.v": f"// {folder}\nmodule core; endmodule\n".encode()}


def test_an_ignored_file_where_one_from_outside_goes_is_not_a_clash(tmp_path, monkeypatch, caplog):
    """A stale copy .gitignore keeps out doesn't go up, so it doesn't stop the upload."""
    _write(tmp_path / "wa" / "a.v", "module a; endmodule\n")
    block = tmp_path / "block"
    _write(block / ".gitignore", "_ext/\n")
    _write(block / "_ext" / "wa" / "a.v", "an older copy\n")
    _write(block / "aristotle.f", f"{(tmp_path / 'wa' / 'a.v').as_posix()}\n")

    caplog.set_level(logging.INFO)
    code, uploaded, _ = _submit_and_capture(block, monkeypatch)

    assert code == 0
    assert uploaded["_ext/wa/a.v"] == b"module a; endmodule\n"
    assert "since the Verilog block needs" not in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_the_generated_file_names_only_include_folders_that_went_up(tmp_path, monkeypatch):
    """A linked folder the walk doesn't enter, and one .gitignore excludes, aren't named unless a file went up."""
    _write(tmp_path / "rtl" / "inc" / "x.vh")
    (tmp_path / "hdr").symlink_to(tmp_path / "rtl" / "inc")
    _write(tmp_path / "gen" / "regs.vh")
    _write(tmp_path / ".gitignore", "gen/\n")
    _write(tmp_path / "rtl" / "a.v", "module a; endmodule\n")
    _write(tmp_path / "aristotle.f", "-sv\n--top a\n-I hdr\n-I gen\n-I rtl/inc\nrtl/a.v\n")

    code, uploaded, _ = _submit_and_capture(tmp_path, monkeypatch)

    assert code == 0
    lines = uploaded["aristotle.f"].decode().splitlines()
    assert "-I rtl/inc" in lines
    assert "-I hdr" not in lines and "-I gen" not in lines


@pytest.mark.skipif(sys.platform in ("darwin", "win32"), reason="the file system needs to allow any byte in a name")
def test_a_folder_above_the_project_with_a_name_that_isnt_utf8_is_fine(tmp_path, monkeypatch):
    """Only the upload's own paths must be UTF-8."""
    block = tmp_path / os.fsdecode(b"caf\xe9") / "block"
    _write(block / "rtl" / "a.v", "module a; endmodule\n")
    _write(block / "aristotle.f", "-sv\nrtl/a.v\n")

    code, uploaded, _ = _submit_and_capture(block, monkeypatch)

    assert code == 0
    assert sorted(uploaded) == ["aristotle.f", "rtl/a.v"]


def test_a_tracked_file_the_block_needs_is_not_warned_about_as_held_back(tmp_path, monkeypatch, caplog):
    """A tracked file an ignore rule matches goes up when the block needs it, so the held-back warning leaves it out."""
    caplog.set_level(logging.INFO)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write(tmp_path / ".gitignore", "gen/\n")
    _write(tmp_path / "gen" / "out.v", "module out; endmodule\n")
    _write(tmp_path / "aristotle.f", "gen/out.v\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "gen/out.v", ".gitignore", "aristotle.f"], check=True)

    code, uploaded, _ = _submit_and_capture(tmp_path, monkeypatch)

    assert code == 0
    assert "gen/out.v" in uploaded
    assert "Not uploading" not in caplog.text


def test_an_ignored_committed_aristotle_f_is_not_warned_about_as_held_back(tmp_path, monkeypatch, caplog):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write(tmp_path / ".gitignore", "*.f\n")
    _write(tmp_path / "top.v", "module top; endmodule\n")
    _write(tmp_path / "aristotle.f", "top.v\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", ".gitignore", "aristotle.f", "top.v"], check=True)

    code, uploaded, _ = _submit_and_capture(tmp_path, monkeypatch)

    assert code == 0
    assert "aristotle.f" in uploaded
    assert "Not uploading" not in caplog.text


def test_submit_rewrites_an_absolute_include_in_the_upload_only(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    header = _write(tmp_path / "gen" / "common" / "math_types.vh", "`define W 8\n")
    text = f'`include "{header.as_posix()}"\nmodule alu; endmodule\n'
    source = _write(tmp_path / "block" / "alu.v", text)
    _write(tmp_path / "block" / "aristotle.f", "alu.v\n")

    code, uploaded, _ = _submit_and_capture(tmp_path / "block", monkeypatch)

    assert code == 0
    assert uploaded["alu.v"] == b'`include "_ext/common/math_types.vh"\nmodule alu; endmodule\n'
    assert uploaded["_ext/common/math_types.vh"] == b"`define W 8\n"
    assert source.read_text() == text
    assert f'  alu.v:1  "{header.as_posix()}" -> "_ext/common/math_types.vh"' in caplog.text


def test_a_rewritten_file_with_a_hard_link_goes_up_as_a_plain_file(tmp_path, monkeypatch):
    """A hard-linked header whose include is rewritten is a plain entry with its own bytes, and the archive stays
    readable to its end."""
    header = _write(tmp_path / "gen" / "types.vh", "`define W 8\n")
    _write(tmp_path / "block" / "a" / "h.vh", f'`include "{header.as_posix()}"\n')
    (tmp_path / "block" / "b").mkdir()
    os.link(tmp_path / "block" / "a" / "h.vh", tmp_path / "block" / "b" / "h.vh")
    _write(tmp_path / "block" / "top.v", '`include "a/h.vh"\n`include "b/h.vh"\n')
    _write(tmp_path / "block" / "aristotle.f", "top.v\n")

    code, uploaded, _ = _submit_and_capture(tmp_path / "block", monkeypatch)

    assert code == 0
    assert uploaded["a/h.vh"] == b'`include "../_ext/gen/types.vh"\n'
    assert uploaded["b/h.vh"] == b'`include "../_ext/gen/types.vh"\n'
    assert "aristotle.f" in uploaded and "_ext/gen/types.vh" in uploaded


def _run_submit(argv: list[str], monkeypatch, key: bool = True) -> tuple[int | None, dict[str, bytes], Mock]:
    """Run ``aristotle submit`` with ``argv`` and Project.create stubbed; the exit code and each file it sent."""
    if key:
        monkeypatch.setenv("ARISTOTLE_API_KEY", "test-key")
    else:
        monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    uploaded: dict[str, bytes] = {}

    async def create(*, tar_file_path, **kwargs):
        with tarfile.open(tar_file_path, encoding="utf-8") as tar:
            for member in tar.getmembers():
                extracted = tar.extractfile(member)
                assert extracted is not None
                uploaded[member.name] = extracted.read()
        return _project(None)

    with (
        patch("sys.argv", ["aristotle", "submit", "Prove it", *argv]),
        patch("aristotlelib.cli.command.submit.Project.create", side_effect=create) as mock_create,
    ):
        code = main()
    return code, uploaded, mock_create


def _ip(tmp_path: Path) -> Path:
    """A filelist, ip/ip.f, naming core.v beside it with a define."""
    _write(tmp_path / "ip" / "core.v", "module core; endmodule\n")
    return _write(tmp_path / "ip" / "ip.f", "+define+FROM_IP\ncore.v\n")


def test_submit_without_project_dir_uploads_only_the_block(tmp_path, monkeypatch):
    """--top and -F make the block, laid out from the current directory, and nothing else goes up."""
    _ip(tmp_path)
    _write(tmp_path / "unrelated.txt", "not sent\n")
    monkeypatch.chdir(tmp_path)

    code, uploaded, mock_create = _run_submit(["--top", "core", "-F", "ip/ip.f"], monkeypatch)

    assert code == 0
    assert sorted(uploaded) == ["aristotle.f", "ip/core.v"]
    assert uploaded["aristotle.f"].decode() == (
        "# Generated by the aristotle CLI from the command line, in the options Aristotle reads.\n"
        "--top core\n--single-unit\n-D FROM_IP\nip/core.v\n"
    )
    assert mock_create.call_args.kwargs["public_file_path"] == "core.tar.gz"
    assert mock_create.call_args.kwargs["prompt"] == "Prove it\n\nUse aristotle.f as the commands file.\n"


def test_an_error_after_archiving_is_not_called_an_archive_error(tmp_path, monkeypatch, caplog):
    """Only the archive step's errors are named as such; one creating the project is the command's own."""
    _ip(tmp_path)
    monkeypatch.chdir(tmp_path)

    monkeypatch.setenv("ARISTOTLE_API_KEY", "test-key")

    with (
        patch("sys.argv", ["aristotle", "submit", "Prove it", "--top", "core", "-F", "ip/ip.f"]),
        patch("aristotlelib.cli.command.submit.Project.create", side_effect=PermissionError(13, "Permission denied")),
    ):
        main()

    assert "Could not archive" not in caplog.text


@pytest.mark.parametrize("dry_run", [False, True], ids=["submit", "--dry-run"])
def test_a_block_file_gone_mid_upload_exits_3_with_one_line(tmp_path, monkeypatch, caplog, dry_run):
    _ip(tmp_path)
    monkeypatch.chdir(tmp_path)
    gone = FileNotFoundError(2, "No such file or directory", str(tmp_path / "ip" / "core.v"))
    argv = ["--top", "core", "-F", "ip/ip.f", *(["--dry-run"] if dry_run else [])]

    with patch("aristotlelib.project._build_archive", side_effect=gone):
        code, _, mock_create = _run_submit(argv, monkeypatch, key=not dry_run)

    assert code == 3
    mock_create.assert_not_called()
    assert (
        caplog.messages[-1] == f"Could not archive {tmp_path / 'ip' / 'core.v'}: No such file or directory. Try again."
    )


def test_submit_keeps_the_order_of_f_and_big_f(tmp_path, monkeypatch):
    """-f and -F apply in the order given, as lines of aristotle.f would."""
    _ip(tmp_path)
    _write(tmp_path / "first.f", "+define+FIRST\n")
    monkeypatch.chdir(tmp_path)

    code, uploaded, _ = _run_submit(["-F", "ip/ip.f", "--top", "core", "-f", "first.f"], monkeypatch)

    assert code == 0
    lines = uploaded["aristotle.f"].decode().splitlines()
    assert lines.index("-D FROM_IP") < lines.index("-D FIRST")


@pytest.mark.parametrize(("folder", "sibling"), WILDCARD_FOLDERS)
def test_a_filelist_named_by_a_path_holding_a_wildcard_is_the_only_one_read(tmp_path, monkeypatch, folder, sibling):
    for name, source in ((folder, "own.v"), (sibling, "other.v")):
        _write(tmp_path / source, "module core; endmodule\n")
        _write(tmp_path / name / "ip.f", f"../{source}\n")
    monkeypatch.chdir(tmp_path)

    code, uploaded, _ = _run_submit(["--top", "core", "-F", str(Path(folder, "ip.f"))], monkeypatch)

    assert code == 0
    assert uploaded.keys() == {"aristotle.f", "own.v"}


@pytest.mark.parametrize(("folder", "sibling"), WILDCARD_FOLDERS)
def test_a_source_whose_path_in_the_upload_holds_a_wildcard_stops_before_any_request(
    tmp_path, monkeypatch, caplog, folder, sibling
):
    """The generated aristotle.f would name it by a pattern, which the server could match to the sibling's file."""
    for name in (folder, sibling):
        _write(tmp_path / name / "core.v", "module core; endmodule\n")
    _write(tmp_path / folder / "ip.f", "core.v\n")
    monkeypatch.chdir(tmp_path)

    code, _, mock_create = _run_submit(["--top", "core", "-F", str(Path(folder, "ip.f"))], monkeypatch)

    assert code == 2
    mock_create.assert_not_called()
    wildcard = "..." if folder.startswith("...") else folder[1]
    assert caplog.messages[-1].startswith(f'{folder}/core.v ({Path(folder, "ip.f")}:1) has "{wildcard}" in its path')


def test_a_comma_in_a_filelists_path_on_the_command_line_is_part_of_its_name(tmp_path, monkeypatch):
    _write(tmp_path / "core.v", "module core; endmodule\n")
    _write(tmp_path / "a,b.f", "core.v\n")
    monkeypatch.chdir(tmp_path)

    code, uploaded, _ = _run_submit(["--top", "core", "-F", "a,b.f"], monkeypatch)

    assert code == 0
    assert uploaded.keys() == {"aristotle.f", "core.v"}


def test_an_include_folder_whose_path_in_the_upload_holds_a_comma_stops_before_any_request(
    tmp_path, monkeypatch, caplog
):
    """The generated aristotle.f would name it in an -I line, which the server splits at the comma."""
    _write(tmp_path / "x,y" / "include" / "defs.vh", "`define W 1\n")
    _write(tmp_path / "x,y" / "core.v", '`include "defs.vh"\nmodule core; endmodule\n')
    _write(tmp_path / "x,y" / "ip.f", "-I include\ncore.v\n")
    monkeypatch.chdir(tmp_path)

    code, _, mock_create = _run_submit(["--top", "core", "-F", str(Path("x,y", "ip.f"))], monkeypatch)

    assert code == 2
    mock_create.assert_not_called()
    assert caplog.messages[-1].startswith(f'x,y/include ({Path("x,y", "ip.f")}:1) has "," in its path')


def test_a_filelist_named_with_a_trailing_slash_stops_saying_to_drop_it(tmp_path, monkeypatch, caplog):
    _ip(tmp_path)
    monkeypatch.chdir(tmp_path)

    code, _, mock_create = _run_submit(["--top", "core", "-F", "ip/ip.f/"], monkeypatch)

    assert code == 2
    mock_create.assert_not_called()
    assert caplog.messages[-1] == f"the command line: ip/ip.f/: {Path('ip', 'ip.f')} is a file; drop the trailing slash"


def test_submit_flags_add_lines_to_the_folders_aristotle_f(tmp_path, monkeypatch):
    _ip(tmp_path)
    block = tmp_path / "block"
    _write(block / "top.v", "module top; core u(); endmodule\n")
    _write(block / "aristotle.f", "top.v\n")
    monkeypatch.chdir(tmp_path)

    code, uploaded, _ = _run_submit(["--project-dir", "block", "--top", "top", "-F", "ip/ip.f"], monkeypatch)

    assert code == 0
    assert sorted(uploaded) == ["_ext/ip/core.v", "aristotle.f", "top.v"]
    assert uploaded["aristotle.f"].decode().splitlines()[:2] == [
        "# Generated by the aristotle CLI from aristotle.f and the command line, in the options Aristotle reads.",
        "--top top",
    ]


@pytest.mark.parametrize("order", ["top first", "top last"])
def test_a_command_line_top_replaces_the_filelists_tops(order, tmp_path, monkeypatch, caplog):
    """Wherever it stands among -f and -F, and the replaced tops are listed."""
    caplog.set_level(logging.INFO)
    _write(tmp_path / "ip" / "core.v", "module core; endmodule\nmodule wrap; core u(); endmodule\n")
    _write(tmp_path / "ip" / "ip.f", "--top core\ncore.v\n")
    block = tmp_path / "block"
    _write(block / "aristotle.f", "--top core\n")
    monkeypatch.chdir(tmp_path)
    flags = ["--top", "wrap", "-F", "ip/ip.f"] if order == "top first" else ["-F", "ip/ip.f", "--top", "wrap"]

    code, uploaded, _ = _run_submit(["--project-dir", "block", *flags], monkeypatch)

    assert code == 0
    lines = uploaded["aristotle.f"].decode().splitlines()
    assert [line for line in lines if line.startswith("--top")] == ["--top wrap"]
    assert (
        f"Ignored --top core ({Path('block', 'aristotle.f')}:1): the command line's --top replaces it"
        in caplog.messages
    )
    assert f"Ignored --top core ({Path('ip', 'ip.f')}:1): the command line's --top replaces it" in caplog.messages


NO_SOURCE = (
    "The Verilog block names no source file. Pass a filelist that names its sources with -f or -F, or, with "
    "--project-dir, name the top module and any black box in the prompt instead, though a black box's model in the "
    "folder still goes up."
)
NO_SOURCE_IN_FILELIST = "The Verilog block names no source file. Pass a filelist that names its sources with -f or -F."


def test_submit_top_alone_names_no_source_file(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)

    code, _, mock_create = _run_submit(["--top", "core"], monkeypatch)

    assert code == 2
    mock_create.assert_not_called()
    assert caplog.messages[-1] == NO_SOURCE


@pytest.mark.parametrize("option", [["--top", "top"], ["--blackbox", "sram_1p"]], ids=["--top", "--blackbox"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["--dry-run", "run"])
def test_top_or_blackbox_on_a_folder_with_no_filelist_stops_before_any_request(
    tmp_path, monkeypatch, caplog, option: list[str], dry_run: bool
):
    """The folder's sources aren't a filelist, so the block names none."""
    _write(tmp_path / "rtl" / "top.v", "module top; sram_1p u(); endmodule\n")
    _write(tmp_path / "vendor" / "sram_1p.v", "module sram_1p; endmodule\n")
    monkeypatch.chdir(tmp_path)

    argv = ["--project-dir", ".", *option, *(["--dry-run"] if dry_run else [])]
    code, _, mock_create = _run_submit(argv, monkeypatch, key=not dry_run)

    assert code == 2
    mock_create.assert_not_called()
    assert caplog.messages[-1] == NO_SOURCE


@pytest.mark.parametrize(
    ("filelist", "argv"),
    [
        ("defs.f", ["-f", "defs.f", "--top", "top"]),
        ("aristotle.f", ["--top", "top"]),
    ],
    ids=["-f", "aristotle.f"],
)
def test_a_filelist_that_names_no_source_is_refused_without_the_prompt_route(
    tmp_path, monkeypatch, caplog, filelist: str, argv: list[str]
):
    """The prompt can't stand in for a filelist that names no source."""
    _write(tmp_path / "rtl" / "top.v", '`include "defs.vh"\nmodule top; endmodule\n')
    _write(tmp_path / "inc" / "defs.vh", "`define WIDE\n")
    _write(tmp_path / filelist, "+define+WIDE\n+incdir+inc\n")
    monkeypatch.chdir(tmp_path)

    code, _, mock_create = _run_submit(["--project-dir", ".", *argv, "--dry-run"], monkeypatch, key=False)

    assert code == 2
    mock_create.assert_not_called()
    assert caplog.messages[-1] == NO_SOURCE_IN_FILELIST


def test_a_filelist_kept_outside_a_folder_with_none_lets_a_black_box_leave_the_folders_model_out(tmp_path, monkeypatch):
    """Paths in -f are from the current directory, and nothing is written into the folder."""
    folder = tmp_path / "design"
    _write(folder / "rtl" / "top.v", "module top; sram_1p u(); endmodule\n")
    _write(folder / "vendor" / "sram_1p.v", "module sram_1p; endmodule\n")
    _write(tmp_path / "top.f", "rtl/top.v\n")
    monkeypatch.chdir(folder)
    before = sorted(folder.rglob("*"))

    argv = ["--project-dir", ".", "-f", str(Path("..", "top.f")), "--blackbox", "sram_1p"]
    code, uploaded, mock_create = _run_submit(argv, monkeypatch)

    assert code == 0
    assert sorted(uploaded) == ["aristotle.f", "rtl/top.v"]
    assert uploaded["aristotle.f"].decode().splitlines()[1:] == ["--single-unit", "rtl/top.v"]
    assert mock_create.call_args.kwargs["prompt"].endswith("its source isn't in the upload: sram_1p.\n")
    assert sorted(folder.rglob("*")) == before


@pytest.mark.parametrize("dry_run", [True, False], ids=["--dry-run", "run"])
def test_a_command_line_filelist_in_a_folder_that_holds_credentials_stops_before_it_is_read(
    tmp_path, monkeypatch, caplog, dry_run: bool
):
    _write(tmp_path / ".docker" / "defs.f", "+define+TOKEN=DUMMYVALUE\n")
    _write(tmp_path / "core.v", "module core; endmodule\n")
    monkeypatch.chdir(tmp_path)

    argv = ["-f", str(Path(".docker", "defs.f")), "--top", "core", *(["--dry-run"] if dry_run else [])]
    code, _, mock_create = _run_submit(argv, monkeypatch, key=not dry_run)

    assert code == 2
    mock_create.assert_not_called()
    assert (
        "is in a folder that holds credentials, and such files are never uploaded. Leave it out of the block."
        in caplog.messages[-1]
    )
    assert "DUMMYVALUE" not in caplog.text


@pytest.mark.parametrize("value", NEVER_READ.values(), ids=NEVER_READ.keys())
@pytest.mark.parametrize("route", ["aristotle.f", "include", "-f"])
def test_a_path_never_read_refuses_stops_submit_before_anything_looks_it_up(
    tmp_path, monkeypatch, caplog, no_lookup_of, value: str, route: str
):
    """Before any request: a repository someone else wrote can name one in its aristotle.f or an include."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "top.v", f'`include "{value}"\n' if route == "include" else "module top; endmodule\n")
    if route == "-f":
        argv, expected = (
            ["--top", "top", "-f", value],
            f"the command line: the filelist named is {filelist.never_read(value)}",
        )
    else:
        _write(tmp_path / "aristotle.f", "top.v\n" if route == "include" else f"top.v\n'{value}'\n")
        argv = ["--project-dir", str(tmp_path)]
        expected = (
            f"top.v:1 includes {filelist.never_read(value)}."
            if route == "include"
            else f"aristotle.f:2: the file named is {filelist.never_read(value)}"
        )
    no_lookup_of(UNREACHABLE_HOST)

    code, _, mock_create = _run_submit(argv, monkeypatch)

    assert code == 2
    mock_create.assert_not_called()
    assert caplog.messages[-1] == expected


def test_a_dry_run_lists_the_block_sends_nothing_and_needs_no_key(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _ip(tmp_path)
    monkeypatch.chdir(tmp_path)

    with patch("aristotlelib.cli.api_action.get_api_key", side_effect=ValueError("no key")) as get_api_key:
        code, _, mock_create = _run_submit(["--top", "core", "-F", "ip/ip.f", "--dry-run"], monkeypatch, key=False)

    assert code == 0
    mock_create.assert_not_called()
    get_api_key.assert_not_called()
    assert (
        f"The Verilog block's 1 file:\n  ip/core.v  (named at {Path('ip', 'ip.f')}:2)\naristotle.f, as it goes up:"
        in caplog.text
    )
    assert any(m.startswith("Dry run: would upload 2 files (") for m in caplog.messages)
    assert "The prompt, as it would be sent:\n  Prove it\n  \n  Use aristotle.f as the commands file." in caplog.text


def test_a_dry_run_with_json_prints_the_block(tmp_path, monkeypatch, capsys):
    _ip(tmp_path)
    monkeypatch.chdir(tmp_path)

    code, _, _ = _run_submit(["--top", "core", "-F", "ip/ip.f", "--dry-run", "--json"], monkeypatch, key=False)

    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["files"] == 2
    assert result["block"]["files"] == [
        {"path": "ip/core.v", "source": "ip/core.v", "reason": f"named at {Path('ip', 'ip.f')}:2"}
    ]
    assert result["block"]["filelist"].splitlines()[1] == "--top core"
    assert result["prompt"] == "Prove it\n\nUse aristotle.f as the commands file.\n"


def _blackboxed(tmp_path: Path, named: bool = True) -> Path:
    """A folder whose aristotle.f names its top and black-boxes a vendor model, vendor/sram_1p.v, which it names too
    when ``named``."""
    block = tmp_path / "block"
    _write(block / "rtl" / "top.v", "module top; sram_1p u(); endmodule\n")
    _write(block / "vendor" / "sram_1p.v", "module sram_1p; endmodule\n")
    _write(block / "aristotle.f", "--top top\n--blackbox sram_1p\nrtl/top.v\n" + "vendor/sram_1p.v\n" * named)
    return block


NAMED = pytest.mark.parametrize("named", [True, False], ids=["named", "in no filelist"])


@NAMED
def test_a_black_box_leaves_its_file_out_of_the_folder_and_is_named_in_the_prompt(
    tmp_path, monkeypatch, caplog, named: bool
):
    """The folder goes up whole but for the model, and the aristotle.f that goes up names neither."""
    caplog.set_level(logging.INFO)
    block = _blackboxed(tmp_path, named)

    code, uploaded, mock_create = _submit_and_capture(block, monkeypatch)

    assert code == 0
    assert sorted(uploaded) == ["aristotle.f", "rtl/top.v"]
    assert uploaded["aristotle.f"].decode().splitlines()[1:] == ["--top top", "--single-unit", "rtl/top.v"]
    assert mock_create.call_args.kwargs["prompt"] == (
        "Prove it\n\nUse aristotle.f as the commands file.\n"
        "Treat this module as a black box with unconstrained outputs; its source isn't in the upload: sram_1p.\n"
    )
    assert [m for m in caplog.messages if m.startswith("Black boxes")] == [
        "Black boxes, which the prompt names, and the files left out for them:\n"
        f"  sram_1p ({block / 'aristotle.f'}:2): {block / 'vendor' / 'sram_1p.v'}"
    ]


@NAMED
def test_verilog_that_spells_coqs_words_still_leaves_its_black_box_out(tmp_path, monkeypatch, named: bool):
    """A generate block named Proof, and a hierarchical reference into it spaced as Proof. q, make no Coq file."""
    block = tmp_path / "block"
    _write(block / "rtl" / "top.v", "module top(input clk, output y); ram u(.clk(clk), .y(y)); endmodule\n")
    _write(
        block / "rtl" / "ram.v",
        "module ram(input clk, output y);\n"
        "  generate if (1) begin : Proof\n    reg q;\n    always @(posedge clk) q <= !q;\n  end endgenerate\n"
        "  assign y = Proof. q;\nendmodule\n",
    )
    _write(block / "aristotle.f", "--top top\n--blackbox ram\nrtl/top.v\n" + "rtl/ram.v\n" * named)

    code, uploaded, _ = _submit_and_capture(block, monkeypatch)

    assert code == 0
    assert sorted(uploaded) == ["aristotle.f", "rtl/top.v"]


def test_submit_blackbox_adds_to_the_block_as_top_f_and_big_f_do(tmp_path, monkeypatch):
    _write(tmp_path / "ip" / "core.v", "module core; sram_1p u(); rom_8k r(); endmodule\n")
    _write(tmp_path / "ip" / "sram_1p.v", "module sram_1p; endmodule\n")
    _write(tmp_path / "ip" / "ip.f", "core.v\nsram_1p.v\n")
    monkeypatch.chdir(tmp_path)

    code, uploaded, mock_create = _run_submit(
        ["--blackbox", "sram_1p", "--top", "core", "-F", "ip/ip.f", "--blackbox", "rom_8k"], monkeypatch
    )

    assert code == 0
    assert sorted(uploaded) == ["aristotle.f", "ip/core.v"]
    assert mock_create.call_args.kwargs["prompt"].endswith(
        "\nTreat these modules as black boxes with unconstrained outputs; their sources aren't in the upload: "
        "sram_1p, rom_8k.\n"
    )


@NAMED
def test_a_dry_run_lists_each_black_box_with_its_files_and_the_prompt(
    tmp_path, monkeypatch, caplog, capsys, named: bool
):
    caplog.set_level(logging.INFO)
    _blackboxed(tmp_path, named)
    monkeypatch.chdir(tmp_path)

    code, _, mock_create = _run_submit(
        ["--project-dir", "block", "--blackbox", "rom_8k", "--dry-run", "--json"], monkeypatch, key=False
    )

    assert code == 0
    mock_create.assert_not_called()
    sram, at = str(Path("block", "vendor", "sram_1p.v")), f"{Path('block', 'aristotle.f')}:2"
    assert (
        "Black boxes, which the prompt names, and the files left out for them:\n"
        f"  sram_1p ({at}): {sram}\n"
        "  rom_8k (the command line): no file in the upload declares it"
    ) in caplog.messages
    line = "Treat these modules as black boxes with unconstrained outputs; their sources aren't in the upload: "
    assert f"The prompt, as it would be sent:\n  Prove it\n  \n  Use aristotle.f as the commands file.\n  {line}" in (
        caplog.text
    )
    result = json.loads(capsys.readouterr().out)
    assert result["paths"] == ["aristotle.f", "rtl/top.v"]
    assert result["block"]["blackboxes"] == [
        {"module": "sram_1p", "at": at, "left_out": [sram]},
        {"module": "rom_8k", "at": "the command line", "left_out": []},
    ]
    assert result["prompt"].endswith(f"{line}sram_1p, rom_8k.\n")


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@NAMED
def test_a_link_in_the_folder_to_a_file_a_black_box_leaves_out_stays_out(tmp_path, monkeypatch, caplog, named: bool):
    """As any link whose target doesn't go up."""
    block = _blackboxed(tmp_path, named)
    (block / "alias.v").symlink_to(Path("vendor", "sram_1p.v"))

    code, uploaded, _ = _submit_and_capture(block, monkeypatch)

    assert code == 0
    assert sorted(uploaded) == ["aristotle.f", "rtl/top.v"]
    assert local_file_utils.skipped_symlinks_warning({Path("alias.v"): "target not uploaded"}) in caplog.text


@pytest.mark.parametrize(
    ("named", "reason", "fix"),
    [
        (True, "named at {f}:4", "or black-box those too"),
        (
            False,
            "goes up with the folder",
            "black-box those too, or keep the file out of the upload with a rule in {g}",
        ),
    ],
    ids=["named", "in no filelist"],
)
def test_a_black_box_sharing_a_file_with_another_module_stops_before_any_request(
    tmp_path, monkeypatch, caplog, named: bool, reason: str, fix: str
):
    block = _blackboxed(tmp_path, named)
    _write(block / "vendor" / "sram_1p.v", "module sram_1p; endmodule\nmodule sram_ctrl; endmodule\n")

    code, _, mock_create = _submit_and_capture(block, monkeypatch)

    assert code == 2
    mock_create.assert_not_called()
    assert caplog.messages[-1] == (
        f"{block / 'vendor' / 'sram_1p.v'} ({reason.format(f=block / 'aristotle.f')}) can't be left out for --blackbox "
        f"sram_1p ({block / 'aristotle.f'}:2): it also declares sram_ctrl. Move sram_1p to a file of its own, "
        f"{fix.format(g=block / '.gitignore')}."
    )


def test_a_file_in_the_folder_that_isnt_verilog_is_checked_for_a_credentials_name_once(tmp_path, monkeypatch):
    """By the archive, and not before it by the black boxes' scan, which reads only Verilog."""
    block = _blackboxed(tmp_path, named=False)
    _write(block / "vendor" / "notes.txt", "datasheet\n")
    checked: list[Path] = []
    is_secret_path = local_file_utils.is_secret_path
    monkeypatch.setattr(
        local_file_utils, "is_secret_path", lambda path, root=None: checked.append(path) or is_secret_path(path, root)
    )

    code, uploaded, _ = _submit_and_capture(block, monkeypatch)

    assert code == 0
    assert "vendor/notes.txt" in uploaded
    assert checked.count(Path("vendor", "notes.txt")) == 1


def test_a_file_in_no_filelist_that_an_ignore_rule_keeps_out_is_not_read_for_black_boxes(tmp_path, monkeypatch):
    """It doesn't go up, so a black box it shares with another module doesn't stop the command."""
    block = _blackboxed(tmp_path, named=False)
    _write(block / "vendor" / "sram_1p.v", "module sram_1p; endmodule\nmodule sram_ctrl; endmodule\n")
    _write(block / ".gitignore", "vendor/\n")

    code, uploaded, _ = _submit_and_capture(block, monkeypatch)

    assert code == 0
    assert sorted(uploaded) == [".gitignore", "aristotle.f", "rtl/top.v"]


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--dry-run"], "--dry-run needs --project-dir, -f or -F: without them only the prompt is sent."),
        (["--dry-run", "--wait", "-f", "x.f"], "--dry-run sends nothing, so there is nothing to wait for."),
    ],
)
def test_a_dry_run_that_cant_mean_anything_is_a_usage_error(argv, message, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exited:
        _run_submit(argv, monkeypatch, key=False)

    assert exited.value.code == 2
    assert message in capsys.readouterr().err


def test_submit_reads_a_filelist_spelled_in_any_case(tmp_path, monkeypatch):
    _write(tmp_path / "top.v", "module top; endmodule\n")
    _write(tmp_path / "Aristotle.f", "-sv\ntop.v\n")

    code, uploaded, _ = _submit_and_capture(tmp_path, monkeypatch)

    assert code == 0
    assert sorted(uploaded) == ["Aristotle.f", "top.v"]
    assert uploaded["Aristotle.f"].decode().startswith("# Generated by the aristotle CLI from Aristotle.f")


def test_a_dry_run_lists_the_folders_other_files_and_those_an_ignore_rule_leaves_out(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _write(tmp_path / ".gitignore", "build/\n*.log\n")
    _write(tmp_path / "build" / "out.bin", "x")
    _write(tmp_path / "sim.log", "x")
    _write(tmp_path / "notes.md", "x")
    _write(tmp_path / "top.v", "module top; endmodule\n")
    _write(tmp_path / "aristotle.f", "-sv\ntop.v\n")

    code, _, _ = _run_submit(["--project-dir", str(tmp_path), "--dry-run"], monkeypatch, key=False)

    assert code == 0
    assert "The folder's 2 other files that go up:\n  .gitignore\n  notes.md" in caplog.messages
    assert "Left out, since an ignore rule matches them:\n  build/out.bin\n  sim.log" in caplog.messages
    assert any(
        m.startswith("Dry run: would upload 4 files (")
        and m.endswith("; 2 left out because they match an ignore rule.")
        for m in caplog.messages
    )


def test_a_dry_run_names_one_other_file_in_the_singular(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _write(tmp_path / "notes.md", "x")
    _write(tmp_path / "top.v", "module top; endmodule\n")
    _write(tmp_path / "aristotle.f", "-sv\ntop.v\n")

    code, _, _ = _run_submit(["--project-dir", str(tmp_path), "--dry-run"], monkeypatch, key=False)

    assert code == 0
    assert "The folder's 1 other file that goes up:\n  notes.md" in caplog.messages


@pytest.mark.parametrize("in_git", [False, True], ids=["outside git", "untracked"])
def test_a_dry_run_says_the_folders_own_aristotle_f_goes_up_as_it_is(tmp_path, monkeypatch, caplog, in_git):
    caplog.set_level(logging.INFO)
    if in_git:
        git(tmp_path, "init", "-q")
    _write(tmp_path / "top.v", "module top; endmodule\n")
    _write(tmp_path / "aristotle.f", "--top top\ntop.v\n")

    code, _, _ = _run_submit(["--project-dir", str(tmp_path), "--dry-run"], monkeypatch, key=False)

    assert code == 0
    assert any(m.endswith("\naristotle.f goes up as it is.") for m in caplog.messages)
    assert "committed" not in caplog.text


def test_a_dry_run_of_a_folder_with_no_block_lists_its_files(tmp_path, monkeypatch, caplog, capsys):
    caplog.set_level(logging.INFO)
    _write(tmp_path / ".gitignore", "scratch/\n")
    _write(tmp_path / "Main.lean", "theorem t : True := trivial\n")
    _write(tmp_path / "scratch" / "notes.txt", "x")

    code, _, _ = _run_submit(["--project-dir", str(tmp_path), "--dry-run", "--json"], monkeypatch, key=False)

    assert code == 0
    assert "The folder's 2 files that go up:\n  .gitignore\n  Main.lean" in caplog.messages
    result = json.loads(capsys.readouterr().out)
    assert result["paths"] == [".gitignore", "Main.lean"]
    assert result["left_out"] == ["scratch/notes.txt"]
    assert result["block"] is None


@pytest.mark.parametrize("dry_run", [True, False], ids=["--dry-run", "run"])
def test_a_lake_manifest_that_cant_be_read_is_warned_of_once(tmp_path, monkeypatch, caplog, dry_run: bool):
    _write(tmp_path / "lake-manifest.json", "{not json")
    _write(tmp_path / "notes.txt", "x")

    code, _, _ = _run_submit(
        ["--project-dir", str(tmp_path), *(["--dry-run"] if dry_run else [])], monkeypatch, key=not dry_run
    )

    assert code == 0
    assert sum(m.startswith("Failed to read lake-manifest.json") for m in caplog.messages) == 1


def test_a_dry_run_refuses_local_lean_dependencies_as_submit_does(tmp_path, monkeypatch, caplog):
    _write(tmp_path / "lake-manifest.json", '{"packages": [{"type": "path", "name": "local"}]}')

    code, _, _ = _run_submit(["--project-dir", str(tmp_path), "--dry-run"], monkeypatch, key=False)

    assert code == 2
    assert "does not support projects with local dependencies" in caplog.text


def test_a_dry_run_of_a_folder_with_nothing_to_upload_says_why_as_submit_does(tmp_path, monkeypatch, caplog):
    _write(tmp_path / ".DS_Store", "x")

    code, _, _ = _run_submit(["--project-dir", str(tmp_path), "--dry-run"], monkeypatch, key=False)

    assert code == 2
    assert (
        caplog.messages[-1] == f"Nothing to upload from {tmp_path}: its only file, .DS_Store, is one Aristotle skips."
    )


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permission bits, which root bypasses")
@pytest.mark.parametrize("dry_run", [True, False], ids=["--dry-run", "run"])
@pytest.mark.parametrize("block", [[], ["--top", "core"], ["-F", "proj/core.f"]], ids=["no block", "--top", "-F"])
@pytest.mark.parametrize("mode", [0o000, 0o600], ids=["none", "not enterable"])
def test_a_folder_that_cant_be_read_is_said_so_before_its_block_is_read(
    tmp_path, monkeypatch, caplog, dry_run: bool, block: list[str], mode: int
):
    """A run and a dry run stop on it first, whatever the block options."""
    folder = tmp_path / "proj"
    folder.mkdir()
    _write(folder / "core.v", "module core; endmodule\n")
    _write(folder / "core.f", "core.v\n")
    monkeypatch.chdir(tmp_path)
    folder.chmod(mode)
    try:
        argv = ["--project-dir", str(folder), *block, *(["--dry-run"] if dry_run else [])]
        code, _, create = _run_submit(argv, monkeypatch, key=not dry_run)
    finally:
        folder.chmod(0o700)

    assert code == 2
    create.assert_not_called()
    assert caplog.messages[-1] == f"Nothing to upload: {folder} can't be read (Permission denied)."


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permission bits, which root bypasses")
@pytest.mark.parametrize("dry_run", [True, False], ids=["--dry-run", "run"])
@pytest.mark.parametrize(
    ("locked", "mode", "code", "message"),
    [
        (".", 0o600, 2, "Nothing to upload: {proj} can't be read (Permission denied)."),
        ("src", 0o600, 3, "Could not archive {proj}/src/Main.lean: Permission denied."),
        ("src/Main.lean", 0o000, 3, "Could not archive {proj}/src/Main.lean: Permission denied."),
    ],
    ids=["the folder not enterable", "a folder in it not enterable", "a file in it not readable"],
)
def test_a_lean_folder_or_a_path_in_it_that_cant_be_read_stops_before_any_request(
    api_server, tmp_path, caplog, dry_run: bool, locked: str, mode: int, code: int, message: str
):
    """Exit 2 for the folder, as for one that can't be listed; 3 for a path in it, as for any file that can't be archived."""
    requests = api_server()
    proj = tmp_path / "proj"
    _write(proj / "Notes.lean", "-- notes\n")
    _write(proj / "src" / "Main.lean", "-- a file\n")
    (proj / locked).chmod(mode)
    try:
        argv = ["aristotle", "submit", "Prove it", "--project-dir", str(proj), *(["--dry-run"] if dry_run else [])]
        with patch("sys.argv", argv):
            assert main() == code
    finally:
        (proj / locked).chmod(0o700)

    assert requests == []
    assert caplog.messages[-1] == message.format(proj=proj)


def _templates(folder: Path) -> None:
    """A repository whose Verilog is generated from tracked templates and ignored."""
    git(folder, "init", "-q")
    _write(folder / ".gitignore", "*.v\n")
    _write(folder / "fifo.vp", "//; my $depth = parameter(Name=>'Depth', Val=>8);\nmodule `mname`; endmodule\n")
    _write(folder / "top.vp", "module `mname`; endmodule\n")
    _write(folder / "Makefile", "gen:\n\tGenesis2.pl -generate -top top -input fifo.vp top.vp\n")
    git(folder, "add", ".gitignore", "fifo.vp", "top.vp", "Makefile")
    _write(folder / "fifo.v", "module fifo; endmodule\n")
    _write(folder / "top.v", "module top; fifo q(); endmodule\n")


def _no_verilog(folder: Path, files: list[str], more: int = 0) -> str:
    """The error for ``folder`` when an ignore rule leaves out ``files``, as listed, and ``more``."""
    return "\n".join(
        [
            f"No Verilog source would go up from {folder}: an ignore rule matches each of its .v and .sv files:",
            *(f"  {line}" for line in files),
            *([f"  and {more} more"] if more else []),
            "No .lean, .c or .rs file would go up either, so the run would have no design to verify. To upload them, "
            "pass --include-ignored (from Python, the include_ignored argument), which uploads the files ignore rules "
            "keep out, or describe the design with a filelist, which uploads every file it names, even one an ignore "
            f"rule matches: {folder / 'aristotle.f'}, -F <filelist>, or --top <module> with -f <filelist> (from "
            "Python, the block argument).",
        ]
    )


@pytest.mark.parametrize(
    "flags", [[], ["--dry-run"], ["--json"], ["--non-interactive"], ["--dry-run", "--json"]], ids=" ".join
)
def test_an_ignore_rule_that_leaves_out_every_verilog_source_stops_before_any_request(
    api_server, caplog, capsys, tmp_path, flags: list[str]
):
    requests = api_server()
    _templates(tmp_path)

    with patch("sys.argv", ["aristotle", "submit", "Prove it", "--project-dir", str(tmp_path), *flags]):
        assert main() == 2

    assert requests == []
    assert capsys.readouterr().out == ""
    assert caplog.messages[-1] == _no_verilog(tmp_path, ["fifo.v  (.gitignore:1: *.v)", "top.v  (.gitignore:1: *.v)"])


def test_a_filelist_naming_the_ignored_verilog_uploads_it(tmp_path, monkeypatch, caplog):
    _templates(tmp_path)
    _write(tmp_path / "sim.f", "fifo.v\ntop.v\n")
    monkeypatch.chdir(tmp_path)

    code, uploaded, _ = _run_submit(["--project-dir", ".", "-F", "sim.f"], monkeypatch)

    assert code == 0
    assert {"fifo.v", "top.v"} <= set(uploaded)
    assert "No Verilog source" not in caplog.text


@pytest.mark.parametrize("dry_run", [False, True], ids=["run", "--dry-run"])
@pytest.mark.parametrize("source", ["Main.lean", "lakefile.lean", "src/fir.c", "src/lib.rs"])
def test_ignored_verilog_beside_lean_c_or_rust_is_a_warning(tmp_path, monkeypatch, caplog, source: str, dry_run: bool):
    """As in a build folder: the Lean, C or Rust files still go up."""
    git(tmp_path, "init", "-q")
    _write(tmp_path / ".gitignore", "build/\n")
    _write(tmp_path / source, "x\n")
    _write(tmp_path / "build" / "fir.v", "module fir; endmodule\n")

    argv = ["--project-dir", str(tmp_path), *(["--dry-run"] if dry_run else [])]
    with patch("aristotlelib.project.local_file_utils.get_expected_toolchain", AsyncMock(return_value=None)):
        code, uploaded, create = _run_submit(argv, monkeypatch, key=not dry_run)

    assert code == 0
    assert create.called is not dry_run
    assert dry_run or sorted(uploaded) == [".gitignore", source]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert (
        f"WARNING: No Verilog source goes up from {tmp_path}: an ignore rule matches each of its .v and .sv files:\n"
        "  build/fir.v  (.gitignore:1: build/)\n"
        "To verify them too, pass --include-ignored (from Python, the include_ignored argument), which uploads the "
        "files ignore rules keep out, or describe the design with a filelist, which uploads every file it names, even "
        f"one an ignore rule matches: {tmp_path / 'aristotle.f'}, -F <filelist>, or --top <module> with -f <filelist> "
        "(from Python, the block argument)."
    ) in warnings


def test_one_verilog_source_that_goes_up_is_enough(tmp_path, monkeypatch, caplog):
    git(tmp_path, "init", "-q")
    _write(tmp_path / ".gitignore", "gen/\n")
    _write(tmp_path / "top.sv", "module top; regs r(); endmodule\n")
    _write(tmp_path / "gen" / "regs.v", "module regs; endmodule\n")

    code, uploaded, _ = _submit_and_capture(tmp_path, monkeypatch)

    assert code == 0
    assert sorted(uploaded) == [".gitignore", "top.sv"]
    assert "No Verilog source" not in caplog.text


@pytest.mark.parametrize(
    ("tracked", "ignored", "code"),
    [("defs.vh", "top.v", 2), ("top.vp", "defs.svh", 0), ("include/fir.h", "fir.v", 2)],
    ids=["a Verilog header goes up", "a Verilog header left out", "a C header goes up"],
)
def test_a_header_is_not_a_source(tmp_path, monkeypatch, caplog, tracked: str, ignored: str, code: int):
    git(tmp_path, "init", "-q")
    _write(tmp_path / ".gitignore", f"{ignored}\n")
    _write(tmp_path / tracked, "`define W 8\n")
    _write(tmp_path / ignored, "`define W 8\n")

    assert _submit_and_capture(tmp_path, monkeypatch)[0] == code
    assert ("No Verilog source would go up" in caplog.text) is (code == 2)


def test_the_error_lists_five_and_counts_the_rest_without_a_rule_git_cant_name(api_server, caplog, tmp_path):
    """Outside a repository, only the folder's .gitignore applies, which git can't name the rule of."""
    requests = api_server()
    _write(tmp_path / ".gitignore", "gen/\n")
    _write(tmp_path / "notes.md", "x")
    for i in range(7):
        _write(tmp_path / "gen" / f"m{i}.v", f"module m{i}; endmodule\n")

    with patch("sys.argv", ["aristotle", "submit", "Prove it", "--project-dir", str(tmp_path)]):
        assert main() == 2

    assert requests == []
    assert caplog.messages[-1] == _no_verilog(tmp_path, [f"gen/m{i}.v" for i in range(5)], more=2)


def test_a_folder_of_ignored_verilog_alone_says_how_to_upload_it(api_server, caplog, tmp_path):
    """Rather than that nothing would go up, which names no way to upload the files."""
    requests = api_server()
    git(tmp_path, "init", "-q")
    _write(tmp_path / ".gitignore", "rtl/*.v\n")
    _write(tmp_path / "rtl" / "top.v", "module top; endmodule\n")

    with patch("sys.argv", ["aristotle", "submit", "Prove it", "--project-dir", str(tmp_path / "rtl")]):
        assert main() == 2

    assert requests == []
    assert caplog.messages[-1] == _no_verilog(tmp_path / "rtl", ["top.v  (.gitignore:1: rtl/*.v)"])


def test_in_a_folder_git_ignores_the_error_names_the_folders_own_gitignore(api_server, caplog, tmp_path):
    """Only that file applies there, not the rule that ignores the folder."""
    requests = api_server()
    git(tmp_path, "init", "-q")
    _write(tmp_path / ".gitignore", "build/\n")
    _write(tmp_path / "build" / "rtl" / ".gitignore", "*.v\n")
    _write(tmp_path / "build" / "rtl" / "top.vp", "module `mname`; endmodule\n")
    _write(tmp_path / "build" / "rtl" / "top.v", "module top; endmodule\n")
    folder = tmp_path / "build" / "rtl"

    with patch("sys.argv", ["aristotle", "submit", "Prove it", "--project-dir", str(folder)]):
        assert main() == 2

    assert requests == []
    assert caplog.messages[-1] == _no_verilog(folder, ["top.v  (build/rtl/.gitignore)"])


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permission bits, which root bypasses")
def test_a_folder_that_cant_be_read_is_named_rather_than_the_ignored_verilog(api_server, caplog, tmp_path):
    """Its files may be Verilog sources no rule ignores."""
    requests = api_server()
    git(tmp_path, "init", "-q")
    (tmp_path / ".git" / "info" / "exclude").write_text("*.v\n")
    _write(tmp_path / "top.v", "module top; endmodule\n")
    _write(tmp_path / "src" / "core.sv", "module core; endmodule\n")
    (tmp_path / "src").chmod(0o000)
    try:
        with patch("sys.argv", ["aristotle", "submit", "Prove it", "--project-dir", str(tmp_path)]):
            assert main() == 2
    finally:
        (tmp_path / "src").chmod(0o700)

    assert requests == []
    assert (
        caplog.messages[-1]
        == f"Nothing to upload from {tmp_path}: its folder src can't be read, and nothing else in it goes up."
    )


INCLUDE_IGNORED_HINT = (
    "pass --include-ignored (from Python, the include_ignored argument), which uploads the files ignore rules keep out"
)


def test_include_ignored_is_off_unless_given_and_its_help_says_what_still_stays_out(monkeypatch):
    # Wide enough that argparse wraps no line, as textwrap may break one after a hyphen.
    monkeypatch.setenv("COLUMNS", "2000")
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()

    assert action.parser.parse_args(["Prove it"]).include_ignored is False
    assert action.parser.parse_args(["Prove it", "--include-ignored"]).include_ignored is True
    help_text = " ".join(action.parser.format_help().split())
    assert "Files git ignores (unless --include-ignored), files named like credentials" in help_text
    assert (
        "--include-ignored Also upload the files in --project-dir that git ignores, such as sources a build generates: "
        "those a .gitignore, .git/info/exclude or the global excludes file matches, or, outside a git repository or in "
        "a folder git ignores, the folder's top-level .gitignore. Files named like credentials or kept in a folder "
        "that holds them, .git folders, result archives an earlier run saved and files whose names aren't UTF-8 still "
        "stay out, and --dry-run marks each file it adds. Only the command line turns it on, never an aristotle.f."
    ) in help_text


def test_include_ignored_uploads_generated_verilog_but_no_credential_or_result_archive(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _templates(tmp_path)
    _write(tmp_path / "macros" / "defs.vh", "`define W 8\n")
    _write(tmp_path / ".gitignore", "*.v\n.env\n*.tar.gz\n")
    git(tmp_path, "add", ".gitignore", "macros/defs.vh")
    _write(tmp_path / ".env", "TOKEN=x\n")
    _write(tmp_path / f"{PROJECT_ID}-aristotle.tar.gz", "an earlier run's result\n")

    code, uploaded, _ = _run_submit(["--project-dir", str(tmp_path), "--include-ignored"], monkeypatch)

    assert code == 0
    assert sorted(uploaded) == [".gitignore", "Makefile", "fifo.v", "fifo.vp", "macros/defs.vh", "top.v", "top.vp"]
    assert uploaded["fifo.v"] == b"module fifo; endmodule\n"
    assert "Excluded 1 file(s) matching secret patterns: .env" in caplog.messages
    assert any(m.startswith("Not uploading 1 result archive an earlier run saved;") for m in caplog.messages)
    assert "Uploading 7 files; 2 of them match an ignore rule." in caplog.messages
    assert "No Verilog source" not in caplog.text


def test_a_dry_run_with_include_ignored_marks_each_file_it_adds(tmp_path, monkeypatch, caplog, capsys):
    caplog.set_level(logging.INFO)
    _write(tmp_path / ".gitignore", "build/\n*.log\n")
    _write(tmp_path / "build" / "out.bin", "x")
    _write(tmp_path / "sim.log", "x")
    _write(tmp_path / "notes.md", "x")
    _write(tmp_path / "top.v", "module top; endmodule\n")
    _write(tmp_path / "aristotle.f", "-sv\ntop.v\n")

    argv = ["--project-dir", str(tmp_path), "--dry-run", "--json", "--include-ignored"]
    code, _, _ = _run_submit(argv, monkeypatch, key=False)

    assert code == 0
    assert (
        "The folder's 4 other files that go up:\n  .gitignore\n  build/out.bin  (--include-ignored)\n  notes.md\n"
        "  sim.log  (--include-ignored)"
    ) in caplog.messages
    assert "Left out, since" not in caplog.text
    assert any(
        m.startswith("Dry run: would upload 6 files (") and m.endswith("; 2 of them match an ignore rule.")
        for m in caplog.messages
    )
    result = json.loads(capsys.readouterr().out)
    assert {"build/out.bin", "sim.log"} <= set(result["paths"])
    assert result["left_out"] == []


def test_dry_runs_help_says_its_json_doesnt_mark_what_include_ignored_adds(monkeypatch):
    monkeypatch.setenv("COLUMNS", "2000")
    parser = argparse.ArgumentParser()
    action = SubmitAction(parser.add_subparsers())
    action.add_action_arguments()

    help_text = " ".join(action.parser.format_help().split())

    assert (
        "With --json, print that as one JSON object, which doesn't mark the files --include-ignored adds." in help_text
    )


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_dry_run_with_include_ignored_marks_a_link_to_a_file_it_adds(tmp_path, monkeypatch, caplog):
    """The link goes up only with its target, which only the option adds; the summary counts what an ignore rule
    matches."""
    caplog.set_level(logging.INFO)
    _write(tmp_path / ".gitignore", "gen/\n")
    _write(tmp_path / "gen" / "out.v", "module out; endmodule\n")
    (tmp_path / "alias.v").symlink_to(Path("gen", "out.v"))

    argv = ["--project-dir", str(tmp_path), "--dry-run", "--include-ignored"]
    code, _, _ = _run_submit(argv, monkeypatch, key=False)

    assert code == 0
    assert (
        "The folder's 3 files that go up:\n  .gitignore\n  alias.v  (--include-ignored)\n  gen/out.v  (--include-ignored)"
    ) in caplog.messages
    assert any(m.endswith("; 1 of them matches an ignore rule.") for m in caplog.messages)


def test_include_ignored_with_a_filelist_uploads_the_folders_other_ignored_files_too(tmp_path, monkeypatch, caplog):
    """The block's file goes up as before and is listed with it; only the other is marked as the option's."""
    caplog.set_level(logging.INFO)
    _write(tmp_path / ".gitignore", "gen/\n")
    _write(tmp_path / "gen" / "out.v", "module out; endmodule\n")
    _write(tmp_path / "gen" / "unused.v", "module unused; endmodule\n")
    _write(tmp_path / "aristotle.f", "gen/out.v\n")

    code, uploaded, _ = _run_submit(["--project-dir", str(tmp_path), "--include-ignored"], monkeypatch)

    assert code == 0
    assert sorted(uploaded) == [".gitignore", "aristotle.f", "gen/out.v", "gen/unused.v"]
    assert "Uploading 1 file an ignore rule matches, since the Verilog block needs it: gen/out.v" in caplog.messages
    assert "Uploading 4 files; 2 of them match an ignore rule." in caplog.messages
    caplog.clear()

    argv = ["--project-dir", str(tmp_path), "--include-ignored", "--dry-run"]
    code, _, _ = _run_submit(argv, monkeypatch, key=False)

    assert code == 0
    assert "  gen/out.v  (named at" in caplog.text
    assert (
        "The folder's 2 other files that go up:\n  .gitignore\n  gen/unused.v  (--include-ignored)" in caplog.messages
    )


def test_without_include_ignored_both_warnings_name_it_and_with_it_neither_fires(tmp_path, monkeypatch, caplog):
    """The held-back warning for a tracked file, and the one for Verilog an ignore rule leaves out beside Lean."""
    git(tmp_path, "init", "-q")
    _write(tmp_path / ".gitignore", "build/\n")
    _write(tmp_path / "Main.lean", "theorem t : True := trivial\n")
    _write(tmp_path / "build" / "fir.v", "module fir; endmodule\n")
    _write(tmp_path / "build" / "keep.h", "#define KEEP 1\n")
    git(tmp_path, "add", "-f", ".gitignore", "Main.lean", "build/keep.h")
    toolchain = patch("aristotlelib.project.local_file_utils.get_expected_toolchain", AsyncMock(return_value=None))

    with toolchain:
        code, uploaded, _ = _run_submit(["--project-dir", str(tmp_path)], monkeypatch)

    assert code == 0
    assert sorted(uploaded) == [".gitignore", "Main.lean"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    held_back = local_file_utils.held_back_warning({Path("build/keep.h"): ".gitignore:1: build/"}, include_ignored=True)
    assert held_back in warnings
    assert held_back.endswith(f" then `!/build/gen/keep.h`. Or {INCLUDE_IGNORED_HINT}.")
    (no_verilog,) = [w for w in warnings if w.startswith("WARNING: No Verilog source goes up")]
    assert f"\nTo verify them too, {INCLUDE_IGNORED_HINT}, or describe the design with a filelist" in no_verilog
    caplog.clear()

    with toolchain:
        code, uploaded, _ = _run_submit(["--project-dir", str(tmp_path), "--include-ignored"], monkeypatch)

    assert code == 0
    assert sorted(uploaded) == [".gitignore", "Main.lean", "build/fir.v", "build/keep.h"]
    assert "Not uploading" not in caplog.text
    assert "No Verilog source" not in caplog.text


@pytest.mark.parametrize(
    "argv",
    [
        ["--include-ignored"],
        ["--include-ignored", "--top", "core", "-F", "ip/ip.f"],
        ["--include-ignored", "-F", "ip/ip.f", "--dry-run"],
    ],
    ids=["the prompt alone", "a block", "a dry run of a block"],
)
def test_include_ignored_without_a_project_dir_is_a_usage_error(argv, tmp_path, monkeypatch, capsys):
    _ip(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exited:
        _run_submit(argv, monkeypatch, key=False)

    assert exited.value.code == 2
    assert (
        "--include-ignored needs --project-dir, the folder whose ignored files it uploads." in capsys.readouterr().err
    )


def test_an_aristotle_f_cannot_turn_on_include_ignored(tmp_path, monkeypatch, caplog):
    _templates(tmp_path)
    _write(tmp_path / "aristotle.f", "--include-ignored\n--top top\ntop.v\n")

    code, _, create = _run_submit(["--project-dir", str(tmp_path)], monkeypatch)

    assert code == 2
    create.assert_not_called()
    assert caplog.messages[-1] == f"{tmp_path / 'aristotle.f'}:1: --include-ignored is not an option Aristotle knows"


def test_with_include_ignored_a_file_that_declares_only_a_black_box_still_stays_out(tmp_path, monkeypatch):
    block = _blackboxed(tmp_path, named=False)
    _write(block / ".gitignore", "vendor/\n")
    _write(block / "vendor" / "notes.txt", "datasheet\n")

    code, uploaded, _ = _run_submit(["--project-dir", str(block), "--include-ignored"], monkeypatch)

    assert code == 0
    assert sorted(uploaded) == [".gitignore", "aristotle.f", "rtl/top.v", "vendor/notes.txt"]


def test_with_include_ignored_a_black_box_beside_another_module_stops_without_naming_gitignore(
    tmp_path, monkeypatch, caplog
):
    """The file goes up with the folder, so it is read for black boxes, and an ignore rule can't keep it out."""
    block = _blackboxed(tmp_path, named=False)
    _write(block / ".gitignore", "vendor/\n")
    _write(block / "vendor" / "sram_1p.v", "module sram_1p; endmodule\nmodule sram_ctrl; endmodule\n")

    code, _, create = _run_submit(["--project-dir", str(block), "--include-ignored"], monkeypatch)

    assert code == 2
    create.assert_not_called()
    assert caplog.messages[-1] == (
        f"{block / 'vendor' / 'sram_1p.v'} (goes up with the folder) can't be left out for --blackbox sram_1p "
        f"({block / 'aristotle.f'}:2): it also declares sram_ctrl. Move sram_1p to a file of its own, or black-box "
        "those too."
    )


def test_with_include_ignored_an_ignored_file_where_one_from_outside_goes_stops(tmp_path, monkeypatch, caplog):
    """It would go up too, where the file the block names from outside goes."""
    _write(tmp_path / "wa" / "a.v", "module a; endmodule\n")
    block = tmp_path / "block"
    _write(block / ".gitignore", "_ext/\n")
    _write(block / "_ext" / "wa" / "a.v", "an older copy\n")
    _write(block / "aristotle.f", f"{(tmp_path / 'wa' / 'a.v').as_posix()}\n")

    code, _, create = _run_submit(["--project-dir", str(block), "--include-ignored"], monkeypatch)

    assert code == 2
    create.assert_not_called()
    assert caplog.messages[-1] == (
        f"{block} has a file at _ext/wa/a.v, where a file its aristotle.f names from outside it would go up. Rename "
        "that file or its folder."
    )


def test_with_include_ignored_the_upload_limit_still_holds_with_its_message(tmp_path, api_server, monkeypatch, caplog):
    monkeypatch.setattr(local_file_utils, "MAX_FILE_SIZE_BYTES", 1024 * 1024)
    _write(tmp_path / ".gitignore", "*.vcd\n")
    _write(tmp_path / "top.v", "module top; endmodule\n")
    (tmp_path / "trace.vcd").write_bytes(os.urandom(2 * 1024 * 1024))
    requests = api_server()

    with patch("sys.argv", ["aristotle", "submit", "Prove it", "--project-dir", str(tmp_path), "--include-ignored"]):
        assert main() == 2

    assert requests == []
    assert caplog.messages[-1] == (
        "The upload is 2.0 MB compressed, over the 1 MB limit. Exclude large files with .gitignore or submit a "
        "smaller folder."
    )
