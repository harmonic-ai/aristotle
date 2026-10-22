"""Tests for aristotlelib.cli.ci_setup: ``aristotle ci setup``."""

import argparse
import asyncio
import itertools
import logging
import os
import random
import re
import subprocess
import tarfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aristotlelib import api_request
from aristotlelib.agent_task import AgentTask, TaskStatus
from aristotlelib.api_request import AristotleAPIError
from aristotlelib.ci import context, manifest, runner
from aristotlelib.cli.ci_setup import (
    SETUP_PROMPT,
    SetupAction,
    _assertion,
    _code,
    _code_values,
    _endings,
    _in_repository,
    _name_fits,
    _path_arg,
    _without_attributes,
)
from aristotlelib.cli.command.ci import CIAction
from aristotlelib.declaration import DeclarationStatus, DeclarationType
from aristotlelib.event import EventType
from aristotlelib.exit_codes import (
    EXIT_INFRASTRUCTURE_ERROR,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    EXIT_VERIFICATION_FAILED,
)
from aristotlelib.project import AgentQuestionsSetting, Project, ProjectStatus, create_sent
from aristotlelib.shell import shell_commands
from tests.ci.conftest import make_declaration, refusing_git, without_git


def _line(*words: str) -> str:
    """The one line ``words`` make as this platform's shell reads them, where no shell needs a form of its own."""
    [(shell, [line])] = shell_commands(words)
    assert shell is None
    return line


PROJECT_ID = "2fa30605-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_request, "API_KEY", "test-key")


@pytest.fixture(autouse=True)
def no_aristotle_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run with no ARISTOTLE_* variable from the shell, which ARISTOTLE_FROM or ARISTOTLE_MANIFEST would redirect."""
    for name in list(os.environ):
        if name.startswith("ARISTOTLE_"):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def at_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aristotlelib.cli.ci_setup.can_prompt", lambda: True)


@pytest.fixture(autouse=True)
def rename():
    """Rename projects without a request, outside any CI system and with no ARISTOTLE_NAME."""
    with (
        patch.object(Project, "rename_by_id", AsyncMock(return_value=_project())) as rename,
        patch.object(context, "detect", side_effect=lambda: context.CIContext(context.CISystem.LOCAL, Path.cwd())),
        patch.dict(os.environ, {"ARISTOTLE_NAME": ""}),
    ):
        yield rename


def _init_repo(root: Path) -> None:
    """A git repository at ``root`` with fifo.v and a committed .env."""

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (root / "fifo.v").write_text("module fifo;\nendmodule\n")
    (root / ".env").write_text("SECRET=hunter2\n")
    git("add", "-A", "-f")
    git("commit", "-qm", "initial")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _parse(argv: list[str]) -> tuple[argparse.Namespace, SetupAction]:
    """Parse ``aristotle ci ...`` with the real ci parser, returning the namespace and the setup action."""
    parser = argparse.ArgumentParser()
    CIAction(parser.add_subparsers()).add_arguments()
    args = parser.parse_args(["ci", *argv], argparse.Namespace(api_key=None))
    return args, args.func.__self__


def _project() -> Project:
    return Project(
        project_id=PROJECT_ID,
        status=ProjectStatus.RUNNING,
        created_at=datetime.now(),
        last_updated=datetime.now(),
        description=None,
        has_input=True,
        has_files=False,
    )


def _proposal(declaration_id: str, description: str | None, minute: int, **kwargs):
    declaration = make_declaration(name=f"Fifo.{declaration_id}", description=description, **kwargs)
    return declaration.model_copy(
        update={"declaration_id": declaration_id, "created_at": datetime(2026, 1, 1, 0, minute)}
    )


def _task(
    status: TaskStatus = TaskStatus.COMPLETE,
    live: bool = False,
    wait: AsyncMock | None = None,
    took: timedelta = timedelta(minutes=10),
) -> MagicMock:
    task = MagicMock()
    task.status = status
    task.created_at = datetime(2000, 1, 1)  # Before any proposal here, so each is this run's verdict.
    task.live = live
    task.wait_for_completion = wait or AsyncMock(return_value=[])
    task.created_at = datetime(2026, 1, 1)
    # Rating or renaming the run later moves its last update, but not its newest event.
    task.last_updated_at = task.created_at + took + timedelta(days=1)
    task.get_events = AsyncMock(return_value=([_event(EventType.SUMMARY, task.created_at + took)], None))
    return task


def _event(event_type: EventType, created_at: datetime) -> MagicMock:
    return MagicMock(event_type=event_type, created_at=created_at)


async def _setup(argv: list[str], declarations, task: MagicMock | None = None, ci_argv: tuple[str, ...] = ()):
    """Run setup through preflight and run_action with the API patched out; returns (code, create mock)."""
    args, action = _parse([*ci_argv, "setup", *argv])
    code = action.preflight(args)
    if code is not None:
        return code, None
    with (
        patch.object(Project, "create", AsyncMock(return_value=_project())) as create,
        patch.object(Project, "get_tasks", AsyncMock(return_value=([task or _task()], None))),
        patch.object(Project, "get_declarations", AsyncMock(return_value=declarations)),
    ):
        return await action.run_action(args), create


async def test_setup_writes_the_proposed_properties_as_a_valid_manifest(repo: Path, caplog):
    declarations = [
        _proposal("never_overflows", "The FIFO never accepts a write while full.", 1, source_file="fifo.v"),
        _proposal("empty_after_reset", "After reset the FIFO is empty.", 2),
    ]

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup([], declarations)

    assert code == EXIT_SUCCESS
    written = manifest.load(repo / "aristotle.toml")
    assert [(p.id, p.kind, p.statement) for p in written.properties] == [
        (
            "never_overflows",
            manifest.Kind.NATURAL_LANGUAGE,
            "The FIFO never accepts a write while full. (Module `fifo` in `fifo.v`.)",
        ),
        ("empty_after_reset", manifest.Kind.NATURAL_LANGUAGE, "After reset the FIFO is empty."),
    ]
    assert written.max_wall_time_seconds == 3600
    text = (repo / "aristotle.toml").read_text()
    assert f"/projects/{PROJECT_ID}" in text
    assert "# Fifo.never_overflows (fifo.v)" in text
    assert "Wrote aristotle.toml with 2 properties:" in caplog.text
    assert "  never_overflows: The FIFO never accepts a write while full. (Module `fifo` in `fifo.v`.)" in caplog.text
    assert "git add -- aristotle.toml" in caplog.text


async def test_setup_asks_for_proposals_with_questions_on_and_uploads_no_secret(repo: Path, caplog):
    uploaded: list[str] = []
    sizes: list[int] = []

    async def create(**kwargs):
        with tarfile.open(kwargs["tar_file_path"]) as tar:
            uploaded.extend(tar.getnames())
        sizes.append(Path(kwargs["tar_file_path"]).stat().st_size)
        return _project()

    args, action = _parse(["setup"])
    assert action.preflight(args) is None
    with (
        patch.object(Project, "create", AsyncMock(side_effect=create)) as create_mock,
        patch.object(Project, "get_tasks", AsyncMock(return_value=([_task()], None))),
        patch.object(Project, "get_declarations", AsyncMock(return_value=[_proposal("p", "It holds.", 1)])),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        await action.run_action(args)

    assert f"Uploading {runner.describe_upload(1, sizes[0])}." in caplog.messages
    kwargs = create_mock.await_args.kwargs
    assert kwargs["prompt"] == SETUP_PROMPT
    assert kwargs["agent_questions_setting"] == AgentQuestionsSetting.TIMEOUT_15_MIN
    assert uploaded == ["fifo.v"]


async def test_a_repository_folder_whose_name_is_not_utf8_goes_up_under_an_ascii_name(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("Windows file names are Unicode")
    root = tmp_path / os.fsdecode(b"r\xe9sum\xe9")
    try:
        root.mkdir()
    except OSError:
        pytest.skip("this file system takes only UTF-8 names")
    _init_repo(root)
    monkeypatch.chdir(root)

    code, create = await _setup([], [_proposal("p", "It holds.", 1)])

    assert code == EXIT_SUCCESS
    assert re.fullmatch(r"r-sum [0-9a-f]{12}\.tar\.gz", create.await_args.kwargs["public_file_path"])


async def test_the_setup_project_is_named_after_the_repository_and_the_commit(repo: Path, rename: AsyncMock):
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    branch = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    code, _ = await _setup([], [_proposal("p", "It holds.", 1)])

    assert code == EXIT_SUCCESS
    rename.assert_awaited_once_with(PROJECT_ID, f"{repo.name}: ci setup @ {branch} {commit}")


@pytest.mark.parametrize("after_setup", [True, False], ids=["after setup", "before setup"])
async def test_a_given_name_replaces_the_default_on_either_side_of_setup(repo: Path, rename: AsyncMock, after_setup):
    option = ("--name", "fifo setup")

    code, _ = await _setup(
        list(option) if after_setup else [], [_proposal("p", "It holds.", 1)], ci_argv=() if after_setup else option
    )

    assert code == EXIT_SUCCESS
    rename.assert_awaited_once_with(PROJECT_ID, "fifo setup")


async def test_a_failed_rename_does_not_stop_setup(repo: Path, rename: AsyncMock, caplog):
    rename.side_effect = AristotleAPIError("API request failed with status 500: boom", 500)

    code, _ = await _setup([], [_proposal("p", "It holds.", 1)])

    assert code == EXIT_SUCCESS
    assert "Could not name the project" in caplog.text
    assert (repo / "aristotle.toml").exists()


async def test_ids_that_are_not_manifest_ids_are_rewritten_and_kept_unique(repo: Path):
    declarations = [
        _proposal("fifo.never overflows", "One.", 1),
        _proposal("fifo_never_overflows", "Two.", 2),
        _proposal("...", "Three.", 3),
    ]

    code, _ = await _setup([], declarations)

    assert code == EXIT_SUCCESS
    ids = [p.id for p in manifest.load(repo / "aristotle.toml").properties]
    assert ids == ["fifo_never_overflows", "fifo_never_overflows_2", "property"]


async def test_withdrawn_properties_and_definitions_are_left_out(repo: Path):
    declarations = [
        _proposal("kept", "Kept.", 1),
        _proposal("withdrawn", "Withdrawn.", 2, status=DeclarationStatus.REMOVED),
        _proposal("definition", "A definition.", 3, declaration_type=DeclarationType.DEFINITION),
    ]

    await _setup([], declarations)

    assert [p.id for p in manifest.load(repo / "aristotle.toml").properties] == ["kept"]


async def test_a_property_without_a_description_is_stated_by_its_name(repo: Path):
    await _setup([], [_proposal("bare", None, 1)])

    assert manifest.load(repo / "aristotle.toml").properties[0].statement == "Fifo.bare"


async def test_properties_are_written_in_the_order_proposed(repo: Path):
    await _setup([], [_proposal("second", "B.", 2), _proposal("first", "A.", 1)])

    assert [p.id for p in manifest.load(repo / "aristotle.toml").properties] == ["first", "second"]


async def test_no_proposals_write_nothing_and_exit_1(repo: Path, caplog):
    code, _ = await _setup([], [_proposal("d", "A definition.", 1, declaration_type=DeclarationType.DEFINITION)])

    assert code == EXIT_VERIFICATION_FAILED
    assert not (repo / "aristotle.toml").exists()
    assert "proposed no properties" in caplog.text


@pytest.mark.parametrize("status", [TaskStatus.FAILED, TaskStatus.CANCELED])
async def test_a_run_that_did_not_finish_writes_nothing(repo: Path, status: TaskStatus, caplog):
    """And says how to write it, not to `continue` the run, which would write no manifest."""
    task = _task(status=status)
    code, _ = await _setup([], [_proposal("p", "It holds.", 1)], task=task)

    assert code == EXIT_INFRASTRUCTURE_ERROR
    assert not (repo / "aristotle.toml").exists()
    assert task.wait_for_completion.await_args.kwargs["resumable"] is False
    assert (
        "The setup run did not finish, so nothing was written. Run `aristotle ci setup` again, or write "
        f"aristotle.toml from what it did with:\n  aristotle ci setup --from {PROJECT_ID}"
    ) in caplog.messages


@pytest.mark.parametrize(
    ("status", "live"), [(TaskStatus.IN_PROGRESS, True), (TaskStatus.UNKNOWN, False)], ids=["still running", "unknown"]
)
async def test_a_wait_that_ends_early_says_how_to_write_the_manifest_later(
    repo: Path, caplog, status: TaskStatus, live: bool
):
    code, _ = await _setup([], [_proposal("p", "It holds.", 1)], task=_task(status=status, live=live))

    assert code == EXIT_INFRASTRUCTURE_ERROR
    assert not (repo / "aristotle.toml").exists()
    assert f"aristotle ci setup --from {PROJECT_ID}" in caplog.text


async def test_ctrl_c_during_the_wait_says_how_to_write_the_manifest_later(repo: Path, caplog):
    task = _task(wait=AsyncMock(side_effect=KeyboardInterrupt))

    with caplog.at_level(logging.INFO, logger="aristotle"), pytest.raises(KeyboardInterrupt):
        await _setup([], [], task=task)

    assert f"aristotle ci setup --from {PROJECT_ID}" in caplog.text


@pytest.mark.parametrize(
    ("status", "refresh"),
    [
        (TaskStatus.QUEUED, AsyncMock()),
        (TaskStatus.QUEUED, AsyncMock(side_effect=AristotleAPIError("unavailable", status_code=503))),
        (TaskStatus.IN_PROGRESS, AsyncMock()),
    ],
    ids=["confirmed queued", "refresh fails", "running"],
)
async def test_ctrl_c_during_the_wait_gives_the_cancel_hint_once(
    repo: Path, caplog, status: TaskStatus, refresh: AsyncMock
):
    task = AgentTask(
        project_id=PROJECT_ID,
        agent_task_id="task-queued",
        status=status,
        created_at=datetime.now(),
        last_updated_at=datetime.now(),
        percent_complete=None,
        file_name=None,
        description=None,
        output_summary=None,
    )

    with (
        patch("aristotlelib.agent_task.Question.list_for_task", new=AsyncMock(side_effect=KeyboardInterrupt)),
        patch.object(AgentTask, "refresh", refresh),
        patch("aristotlelib.progress_utils.print_progress_lines"),
        caplog.at_level(logging.INFO, logger="aristotle"),
        pytest.raises(KeyboardInterrupt),
    ):
        await _setup([], [], task=task)

    assert caplog.text.count(f"aristotle cancel {PROJECT_ID}") == 1
    assert f"aristotle ci setup --from {PROJECT_ID}" in caplog.text


async def _interrupt_the_create(interrupt: type[BaseException], sent: bool) -> tuple[AsyncMock, MagicMock]:
    """Run setup, whose create is interrupted by ``interrupt`` once sent if ``sent``, with its project then listed as
    queued; returns the listing mock and the run's task."""
    uploaded_as: list[str] = []

    async def create(**kwargs):
        uploaded_as.append(kwargs["public_file_path"].removesuffix(".tar.gz"))
        if sent:
            create_sent.set(True)
        raise interrupt

    async def listing(**kwargs):
        project = _project().model_copy(update={"description": uploaded_as[0], "latest_run_status": TaskStatus.QUEUED})
        return [project], None

    task = _task(status=TaskStatus.QUEUED)
    task.project_id = PROJECT_ID
    task.cancel = AsyncMock()
    args, action = _parse(["setup"])
    assert action.preflight(args) is None
    with (
        patch.object(Project, "create", AsyncMock(side_effect=create)),
        patch.object(Project, "list_projects", AsyncMock(side_effect=listing)) as listed,
        patch.object(Project, "get_tasks", AsyncMock(return_value=([task], None))),
        pytest.raises(interrupt),
    ):
        await action.run_action(args)
    return listed, task


@pytest.mark.parametrize("interrupt", [asyncio.CancelledError, KeyboardInterrupt])
async def test_an_interrupt_once_the_create_was_sent_cancels_the_setup_run(repo: Path, caplog, interrupt):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        _, task = await _interrupt_the_create(interrupt, sent=True)

    task.cancel.assert_awaited_once()
    assert caplog.messages[-2:] == [
        f"Found it; cancelling the run: https://aristotle.harmonic.fun/projects/{PROJECT_ID}",
        "The run was cancelled.",
    ]


async def test_an_interrupt_before_the_setup_create_is_sent_says_no_project_was_created(repo: Path, caplog):
    listed, task = await _interrupt_the_create(KeyboardInterrupt, sent=False)

    listed.assert_not_awaited()
    task.cancel.assert_not_awaited()
    assert caplog.messages[-1] == "Interrupted before the run was submitted: no project was created."


async def test_an_existing_manifest_is_refused_before_anything_is_uploaded(repo: Path, caplog):
    (repo / "aristotle.toml").write_text("# mine\n")

    code, create = await _setup([], [])

    assert code == EXIT_USAGE_ERROR
    assert create is None
    assert (repo / "aristotle.toml").read_text() == "# mine\n"
    assert "already exists. Pass --force to replace it." in caplog.text


CI_HINT = "In CI, `aristotle ci` starts from the checkout's top level, so the job reads this manifest with:"


def _subfolders(repo: Path, *folders: str) -> None:
    """Each of ``folders`` in ``repo``, holding a tracked Verilog file."""
    for folder in folders:
        (repo / folder).mkdir(parents=True, exist_ok=True)
        (repo / folder / "block.v").write_text("module block;\nendmodule\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)


async def test_a_manifest_written_in_a_subfolder_says_what_ci_needs_to_read_it(repo: Path, monkeypatch, caplog):
    _subfolders(repo, "hw")
    monkeypatch.chdir(repo / "hw")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup([], [_proposal("p", "It holds.", 1)])

    assert code == EXIT_SUCCESS
    assert (repo / "hw" / "aristotle.toml").exists()
    assert f"{CI_HINT}\n  aristotle ci --directory hw" in caplog.text


async def test_from_below_a_subprojects_manifest_setup_refuses_to_hide_it(repo: Path, monkeypatch, caplog):
    _subfolders(repo, "hw/rtl")
    (repo / "hw" / "aristotle.toml").write_text("# hw\n")
    monkeypatch.chdir(repo / "hw" / "rtl")

    code, create = await _setup([], [])

    assert code == EXIT_USAGE_ERROR
    assert create is None
    assert not (repo / "hw" / "rtl" / "aristotle.toml").exists()
    assert (
        f"{repo / 'hw' / 'aristotle.toml'} already exists, and `aristotle ci` run here reads it. Pass --force to "
        "replace it, or --directory . to set up this folder on its own."
    ) in caplog.text


async def test_force_from_below_a_subprojects_manifest_replaces_that_one(repo: Path, monkeypatch, caplog):
    _subfolders(repo, "hw/rtl")
    (repo / "hw" / "aristotle.toml").write_text("# hw\n")
    monkeypatch.chdir(repo / "hw" / "rtl")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, create = await _setup(["--force"], [_proposal("p", "It holds.", 1)])

    assert code == EXIT_SUCCESS
    assert [p.id for p in manifest.load(repo / "hw" / "aristotle.toml").properties] == ["p"]
    assert not (repo / "hw" / "rtl" / "aristotle.toml").exists()
    assert f"{CI_HINT}\n  aristotle ci --directory hw" in caplog.text
    assert (
        f"Setting up {repo / 'hw'}, whose aristotle.toml `aristotle ci` run here reads, and replacing it. Pass "
        "--directory . to set up this folder on its own."
    ) in caplog.messages
    assert "aristotle ci --dry-run --directory .." in caplog.text


async def test_force_says_nothing_of_replacing_when_a_later_check_refuses(repo: Path, monkeypatch, caplog):
    _subfolders(repo, "hw/rtl")
    (repo / "hw" / "aristotle.toml").write_text("# hw\n")
    monkeypatch.chdir(repo / "hw" / "rtl")
    monkeypatch.setattr("aristotlelib.cli.ci_setup.can_prompt", lambda: False)

    code, create = await _setup(["--force"], [])

    assert code == EXIT_USAGE_ERROR
    assert create is None
    assert "runs only at a terminal" in caplog.text
    assert "Setting up" not in caplog.text


async def test_directory_dot_sets_up_a_folder_below_a_manifest_on_its_own(repo: Path, monkeypatch):
    (repo / "aristotle.toml").write_text("# top\n")
    _subfolders(repo, "docs")
    monkeypatch.chdir(repo / "docs")

    code, _ = await _setup(["--directory", "."], [_proposal("p", "It holds.", 1)])

    assert code == EXIT_SUCCESS
    assert (repo / "aristotle.toml").read_text() == "# top\n"
    assert [p.id for p in manifest.load(repo / "docs" / "aristotle.toml").properties] == ["p"]


async def test_a_manifest_in_another_checkout_from_the_folder_set_up_gets_no_ci_line(
    repo: Path, tmp_path_factory, caplog
):
    outside = tmp_path_factory.mktemp("outside")
    _subfolders(repo, "hw")

    with (
        patch.object(Project, "_resolve", AsyncMock(return_value=(PROJECT_ID, None))),
        patch.object(Project, "from_id", AsyncMock(return_value=_project())),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        code, _ = await _setup(
            ["--from", PROJECT_ID, "--directory", str(outside), "--manifest", "hw/aristotle.toml"],
            [_proposal("p", "It holds.", 1)],
            task=_task(),
        )

    assert code == EXIT_SUCCESS
    assert (repo / "hw" / "aristotle.toml").exists()
    assert CI_HINT not in caplog.text


@pytest.mark.parametrize(
    ("argv", "written"),
    [([], "aristotle.toml"), (["--directory", "hw"], "hw/aristotle.toml")],
    ids=["top level", "--directory from the top"],
)
async def test_no_ci_hint_when_the_dry_run_command_already_reads_the_manifest(repo: Path, argv, written, caplog):
    _subfolders(repo, "hw")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup(argv, [_proposal("p", "It holds.", 1)])

    assert code == EXIT_SUCCESS
    assert (repo / written).exists()
    assert CI_HINT not in caplog.text


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
async def test_a_dangling_symlink_counts_as_an_existing_manifest(repo: Path):
    (repo / "aristotle.toml").symlink_to(repo / "missing.toml")

    code, _ = await _setup([], [])

    assert code == EXIT_USAGE_ERROR


async def test_force_replaces_an_existing_manifest(repo: Path):
    (repo / "aristotle.toml").write_text("# mine\n")

    code, _ = await _setup(["--force"], [_proposal("p", "It holds.", 1)])

    assert code == EXIT_SUCCESS
    assert [p.id for p in manifest.load(repo / "aristotle.toml").properties] == ["p"]


async def test_a_manifest_created_during_the_run_is_not_replaced(repo: Path, caplog):
    async def wait(**_kwargs):
        (repo / "aristotle.toml").write_text("# meanwhile\n")
        return []

    code, _ = await _setup([], [_proposal("p", "It holds.", 1)], task=_task(wait=AsyncMock(side_effect=wait)))

    assert code == EXIT_USAGE_ERROR
    assert (repo / "aristotle.toml").read_text() == "# meanwhile\n"
    assert f"aristotle ci setup --from {PROJECT_ID} --force" in caplog.text


async def test_a_write_error_says_how_to_write_the_manifest_once_fixed(repo: Path, caplog):
    args, action = _parse(["setup"])
    assert action.preflight(args) is None

    with (
        patch.object(Project, "create", AsyncMock(return_value=_project())),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([_task()], None))),
        patch.object(Project, "get_declarations", AsyncMock(return_value=[_proposal("p", "It holds.", 1)])),
        patch("aristotlelib.cli.ci_setup.open", side_effect=PermissionError(13, "Permission denied"), create=True),
    ):
        code = await action.run_action(args)

    assert code == EXIT_INFRASTRUCTURE_ERROR
    assert "Permission denied" in caplog.text
    assert f"aristotle ci setup --from {PROJECT_ID}" in caplog.text


async def test_a_manifest_path_in_a_missing_directory_is_refused(repo: Path, caplog):
    code, create = await _setup(["--manifest", "nowhere/aristotle.toml"], [])

    assert code == EXIT_USAGE_ERROR
    assert create is None
    assert f"Cannot write {Path('nowhere', 'aristotle.toml')}: nowhere does not exist; make it first." in caplog.text


async def test_a_manifest_path_under_a_file_is_refused_as_not_a_directory(repo: Path, caplog):
    (repo / "notes").write_text("not a folder\n")

    code, create = await _setup(["--manifest", "notes/aristotle.toml"], [])

    assert code == EXIT_USAGE_ERROR
    assert create is None
    assert f"Cannot write {Path('notes', 'aristotle.toml')}: notes is not a directory." in caplog.text


async def test_a_manifest_outside_any_repository_gets_no_git_commands(repo: Path, tmp_path_factory, caplog):
    outside = tmp_path_factory.mktemp("outside") / "aristotle.toml"

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup(["--manifest", str(outside)], [_proposal("p", "The FIFO never overflows.", 1)])

    assert code == EXIT_SUCCESS
    assert outside.exists()
    assert "git add" not in caplog.text and "git -C" not in caplog.text
    assert f"{outside} is outside any git repository, so `aristotle ci` reads it only with --manifest." in caplog.text


@pytest.mark.parametrize("git_fails", [without_git, refusing_git], ids=["missing", "refusing"])
def test_a_checkout_git_cannot_read_still_counts_as_a_repository(
    repo: Path, monkeypatch: pytest.MonkeyPatch, git_fails
):
    # So that ci setup --from gives the git commands to commit its manifest, not the advice to pass --manifest.
    git_fails(monkeypatch, repo)

    assert _in_repository(repo)


async def _setup_from(repo: Path, files: dict[str, str], declarations) -> str:
    """Track ``files`` in ``repo``, run ``ci setup --from`` on ``declarations`` and return the manifest it wrote."""
    for name, text in files.items():
        (repo / name).write_text(text)
    if files:
        subprocess.run(["git", "-C", str(repo), "add", *files], check=True)
    with (
        patch.object(Project, "_resolve", AsyncMock(return_value=(PROJECT_ID, None))),
        patch.object(Project, "from_id", AsyncMock(return_value=_project())),
    ):
        code, _ = await _setup(["--from", PROJECT_ID], declarations, task=_task())
    assert code == EXIT_SUCCESS
    return (repo / "aristotle.toml").read_text()


@pytest.mark.parametrize(
    ("description", "reason"),
    [
        (
            "The arbiter grants per the input contract.",
            'it says "the input contract", which may refer to the conversation of the run it was copied from',
        ),
        ("As agreed, a request is held until granted.", 'it says "As agreed"'),
        (
            "With CREDITS fixed to 4, the counter never exceeds CREDITS.",
            'it says "CREDITS fixed to 4", and the code sets CREDITS only to 2 in credit.v',
        ),
        (
            "The counter never exceeds CREDITS. Assumes: as agreed, grant is low in reset.",
            'its assumptions say "as agreed"',
        ),
        (
            "The counter never exceeds CREDITS. Assumes: CREDITS is 4.",
            'its assumptions say "CREDITS is 4", and the code sets CREDITS only to 2 in credit.v',
        ),
    ],
)
async def test_from_flags_a_property_that_leans_on_the_run_it_copies(repo: Path, caplog, description: str, reason: str):
    declarations = [_proposal("leans", description, 1), _proposal("stands", "With CREDITS = 2, count stays low.", 2)]

    with caplog.at_level(logging.INFO, logger="aristotle"):
        text = await _setup_from(
            repo, {"credit.v": "module credit #(parameter CREDITS = 2) (input clk);\nendmodule\n"}, declarations
        )

    assert f"; check it before you commit: {reason}" in text
    assert text.count("check it before you commit") == 1
    assert "Check leans before you commit" in caplog.text


@pytest.mark.parametrize(
    ("files", "description"),
    [
        ({"defs.vh": "`define DATA_WIDTH 32 // bits\n"}, "DATA_WIDTH is 32."),
        ({"queue.rs": "const MAX_LEN: usize = 64;\n"}, "The queue holds at most MAX_LEN = 64 items."),
        (
            {"buf.h": "#define BUF_SIZE 256U\n#define SLOTS (8)\n"},
            "With BUF_SIZE of 256 and SLOTS of 8, nothing drops.",
        ),
        ({"fifo.v": "module fifo #(parameter DEPTH = 8'd16) ();\nendmodule\n"}, "DEPTH is 16."),
        (
            {"fifo.v": "module fifo #(parameter DEPTH = 4) ();\nendmodule\n", "top.v": "fifo #(.DEPTH(16)) u ();\n"},
            "DEPTH is 16.",
        ),
        ({"fifo.v": "module fifo #(parameter DEPTH = 2 ** AW) ();\nendmodule\n"}, "DEPTH is 8."),
        ({"fifo.v": "module fifo (input RST);\nendmodule\n"}, "When RST is 1, the FIFO of 4 entries empties."),
        ({"read.c": "int n;\n"}, "read returns as many bytes as requested within this session."),
        (
            {
                "defs.vh": "`ifndef DEFS_VH\n`define DEFS_VH\n\n`define DATA_WIDTH 32\n`endif\n",
                "fifo.v": "module fifo #(parameter DATA_WIDTH = 8) ();\nendmodule\n",
            },
            "A word holds DATA_WIDTH bits. Assumes: DATA_WIDTH is 32.",
        ),
        ({"uart.h": "#define UART_CR (*(volatile unsigned *)0x4000C030)\n"}, "UART_CR is 1 after reset."),
        ({"fsm.c": "switch (s) { case IDLE: n = 2; }\n"}, "IDLE is 0 at reset."),
        ({"count.sv": "module c; logic [WIDTH:0] count = 0; endmodule\n"}, "WIDTH is 4."),
        ({"defs.vh": "// d\xe9finitions\n`define DEPTH 16\n", "fifo.v": "localparam DEPTH = 4;\n"}, "DEPTH is 16."),
        ({"fifo.v": "localparam MSB = 7;\nlocalparam TIMEOUT = 2;\n"}, "Bit MSB of 8-bit count; TIMEOUT is 1.5 ms."),
        ({"fifo.v": "localparam DEPTH = 16;\n"}, "DEPTH is " + "9" * 5000 + "."),
        ({"fifo.v": "localparam WIDTH = 16;\nlocalparam DEPTH = 16;\n"}, "`WIDTH` is `4`-bit; `DEPTH` is `4`.5."),
    ],
)
async def test_from_leaves_a_value_the_code_may_set_and_ordinary_wording_alone(
    repo: Path, files: dict[str, str], description: str
):
    for name, text in files.items():
        # Latin-1, as an older header may be.
        (repo / name).write_bytes(text.encode("latin-1"))
    subprocess.run(["git", "-C", str(repo), "add", *files], check=True)

    text = await _setup_from(repo, {}, [_proposal("p", description, 1)])

    assert "check it before you commit" not in text


@pytest.mark.parametrize(
    ("description", "quoted"),
    [
        ("When DEPTH is 2 or more, the FIFO never overflows.", "DEPTH is 2"),
        ("With `DEPTH` fixed to `4`, count stays below it.", "`DEPTH` fixed to `4`"),
    ],
)
async def test_from_quotes_the_value_it_flags(repo: Path, description: str, quoted: str):
    text = await _setup_from(repo, {"fifo.v": "localparam DEPTH = 16;\n"}, [_proposal("p", description, 1)])

    assert f'check it before you commit: it says "{quoted}", and the code sets DEPTH only to 16 in fifo.v' in text


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root reads any directory")
async def test_from_skips_a_file_it_cannot_reach(repo: Path):
    (repo / "locked").mkdir()
    (repo / "locked" / "defs.vh").write_text("`define DEPTH 16\n")
    subprocess.run(["git", "-C", str(repo), "add", "locked/defs.vh"], check=True)
    (repo / "locked").chmod(0)
    try:
        text = await _setup_from(repo, {"fifo.v": "localparam DEPTH = 4;\n"}, [_proposal("p", "DEPTH is 8.", 1)])
    finally:
        (repo / "locked").chmod(0o755)

    assert 'it says "DEPTH is 8", and the code sets DEPTH only to 4 in fifo.v' in text


def test_from_reads_a_long_line_in_linear_time(repo: Path):
    (repo / "data.c").write_text("AB: " * 50_000 + "(*p) " * 40_000 + ".A(" * 50_000 + "\n")
    # An unpaired quote, then quotes escaped up to a last backslash with no newline after it.
    (repo / "quotes.h").write_text("'\"'\n" + "  X('\\\"') \\\n" * 16_000 + "\\")
    subprocess.run(["git", "-C", str(repo), "add", "data.c", "quotes.h"], check=True)
    started = time.monotonic()

    _code_values(repo)

    # A scan from each `NAME:`, `(*` or `.NAME(` to the end of the line took minutes.
    assert time.monotonic() - started < 10


def test_attributes_are_removed_as_the_regex_removed_them():
    """Every line of up to 8 of `(`, `*`, `)` and `a`, then longer random ones, against the regex it replaced."""
    regex = re.compile(r"\(\*.*?\*\)")
    lines = ["".join(chars) for size in range(9) for chars in itertools.product("(*)a", repeat=size)]
    rng = random.Random(3383)
    lines += ["".join(rng.choices("(*) a", k=rng.randrange(300))) for _ in range(2000)]

    for line in lines:
        assert _without_attributes(line) == regex.sub("", line), line


def test_a_line_of_attributes_never_closed_is_read_in_linear_time():
    line = "(*" * 60_000
    started = time.monotonic()

    assert _code((line,)) == [line]
    # The regex took a minute: from each `(*`, it searched the rest of the line for a `*)`.
    assert time.monotonic() - started < 1


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_from_reads_regular_source_files_only(repo: Path):
    os.mkfifo(repo / "pipe")
    (repo / "model.v").symlink_to(repo / "pipe")
    (repo / "fifo.v").write_text("module fifo #(parameter DEPTH = 4) ();\nendmodule\n")
    subprocess.run(["git", "-C", str(repo), "add", "model.v", "fifo.v"], check=True)
    values: dict[str, dict[int | None, str]] = {}

    # Reading the pipe would block until something writes to it.
    reader = threading.Thread(target=lambda: values.update(_code_values(repo)), daemon=True)
    reader.start()
    reader.join(30)

    assert not reader.is_alive()
    assert values == {"DEPTH": {4: "fifo.v"}}


async def test_from_warns_of_nothing_it_would_not_upload(repo: Path, caplog):
    (repo / "notes.v").write_text("module notes; endmodule\n")
    subprocess.run(["git", "-C", str(repo), "add", "notes.v"], check=True)
    (repo / ".gitignore").write_text("notes.v\n")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        await _setup_from(
            repo, {"fifo.v": "module fifo #(parameter DEPTH = 4) ();\nendmodule\n"}, [_proposal("p", "DEPTH is 8.", 1)]
        )

    assert "Not uploading" not in caplog.text


async def test_without_a_terminal_setup_refuses_and_points_at_from(repo: Path, monkeypatch, caplog):
    monkeypatch.setattr("aristotlelib.cli.ci_setup.can_prompt", lambda: False)

    code, create = await _setup([], [])

    assert code == EXIT_USAGE_ERROR
    assert create is None
    assert "runs only at a terminal" in caplog.text
    assert "--from <project>" in caplog.text


async def test_a_directory_that_is_not_a_repository_creates_no_project(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    code, create = await _setup([], [])

    assert code == EXIT_USAGE_ERROR
    create.assert_not_awaited()


async def test_from_writes_an_existing_projects_properties_without_a_run_or_a_terminal(
    repo: Path, monkeypatch, resolve_project
):
    monkeypatch.setattr("aristotlelib.cli.ci_setup.can_prompt", lambda: False)
    args, action = _parse(["setup", "--from", "FIFO run"])
    resolve_project.side_effect = lambda reference: (PROJECT_ID, None)

    assert action.preflight(args) is None
    with (
        patch.object(Project, "from_id", AsyncMock(return_value=_project())) as from_id,
        patch.object(Project, "create", AsyncMock()) as create,
        patch.object(Project, "get_tasks", AsyncMock(return_value=([_task()], None))),
        patch.object(Project, "get_declarations", AsyncMock(return_value=[_proposal("p", "It holds.", 1)])),
    ):
        code = await action.run_action(args)

    assert code == EXIT_SUCCESS
    resolve_project.assert_awaited_once_with("FIFO run")
    from_id.assert_awaited_once_with(PROJECT_ID)
    create.assert_not_awaited()
    assert [p.id for p in manifest.load(repo / "aristotle.toml").properties] == ["p"]


async def test_from_writes_the_properties_of_the_project_its_lookup_listed_without_fetching_it_again(
    repo: Path, monkeypatch, resolve_project
):
    monkeypatch.setattr("aristotlelib.cli.ci_setup.can_prompt", lambda: False)
    args, action = _parse(["setup", "--from", "FIFO run"])
    resolve_project.side_effect = lambda reference: (PROJECT_ID, _project())

    assert action.preflight(args) is None
    with (
        patch.object(Project, "from_id", AsyncMock(return_value=_project())) as from_id,
        patch.object(Project, "get_tasks", AsyncMock(return_value=([_task()], None))),
        patch.object(Project, "get_declarations", AsyncMock(return_value=[_proposal("p", "It holds.", 1)])),
    ):
        code = await action.run_action(args)

    assert code == EXIT_SUCCESS
    from_id.assert_not_awaited()
    assert [p.id for p in manifest.load(repo / "aristotle.toml").properties] == ["p"]


async def test_from_is_read_from_aristotle_from(repo: Path, monkeypatch, resolve_project, caplog):
    monkeypatch.setenv("ARISTOTLE_FROM", "FIFO run")
    monkeypatch.setattr("aristotlelib.cli.ci_setup.can_prompt", lambda: False)
    args, action = _parse(["setup"])
    resolve_project.side_effect = lambda reference: (PROJECT_ID, None)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        assert action.preflight(args) is None
    with (
        patch.object(Project, "from_id", AsyncMock(return_value=_project())),
        patch.object(Project, "create", AsyncMock()) as create,
        patch.object(Project, "get_tasks", AsyncMock(return_value=([_task()], None))),
        patch.object(Project, "get_declarations", AsyncMock(return_value=[_proposal("p", "It holds.", 1)])),
    ):
        code = await action.run_action(args)

    assert code == EXIT_SUCCESS
    resolve_project.assert_awaited_once_with("FIFO run")
    create.assert_not_awaited()
    assert "Writing the manifest from the project ARISTOTLE_FROM names: FIFO run" in caplog.messages


def test_an_empty_aristotle_from_is_unset(repo: Path, monkeypatch, caplog):
    monkeypatch.setenv("ARISTOTLE_FROM", "")
    monkeypatch.setattr("aristotlelib.cli.ci_setup.can_prompt", lambda: False)
    args, action = _parse(["setup"])

    assert action.preflight(args) == EXIT_USAGE_ERROR

    assert args.from_project is None
    assert "runs only at a terminal" in caplog.text


def test_from_wins_over_aristotle_from(repo: Path, monkeypatch, caplog):
    monkeypatch.setenv("ARISTOTLE_FROM", "env project")
    args, action = _parse(["setup", "--from", "flag project"])

    with caplog.at_level(logging.INFO, logger="aristotle"):
        assert action.preflight(args) is None

    assert args.from_project == "flag project"
    assert "ARISTOTLE_FROM" not in caplog.text


@pytest.mark.parametrize(
    ("took", "budget"),
    [
        (timedelta(minutes=10), 3600),
        (timedelta(minutes=214), 321 * 60),
        (timedelta(minutes=214, seconds=1), 322 * 60),
        (timedelta(minutes=20, seconds=0.3), 3600),
        (timedelta(days=8), manifest.MAX_WALL_TIME_SECONDS),
        (timedelta(seconds=-5), 3600),
    ],
    ids=[
        "a short run gets the hour",
        "half as long again",
        "rounded up to a minute",
        "a fraction of a second over",
        "capped at a week",
        "no time",
    ],
)
async def test_the_time_budget_allows_half_as_long_again_as_the_run_took(
    repo: Path, caplog, took: timedelta, budget: int
):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup([], [_proposal("p", "It holds.", 1)], task=_task(took=took))

    assert code == EXIT_SUCCESS
    assert manifest.load(repo / "aristotle.toml").max_wall_time_seconds == budget
    minutes = max(round(took.total_seconds() / 60), 1)
    text = (repo / "aristotle.toml").read_text()
    assert (
        f"[check]\n# The run this was written from took {minutes} min. The budget allows half as long again, and at "
        f"least an hour.\nmax_wall_time_seconds = {budget}\n"
    ) in text
    longer = " Give the CI job longer than that." if budget > 3600 else ""
    assert (
        f"Its time budget is {budget // 60} minutes; the run it was written from took {minutes}.{longer}" in caplog.text
    )


async def test_a_question_asked_after_the_run_does_not_stretch_it(repo: Path, caplog):
    task = _task(took=timedelta(minutes=20))
    asked = _event(EventType.QUESTION, task.created_at + timedelta(days=2))
    ended = _event(EventType.SUMMARY, task.created_at + timedelta(minutes=20))
    task.get_events = AsyncMock(return_value=([asked, ended], None))

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup([], [_proposal("p", "It holds.", 1)], task=task)

    assert code == EXIT_SUCCESS
    assert manifest.load(repo / "aristotle.toml").max_wall_time_seconds == 3600
    assert "Its time budget is 60 minutes; the run it was written from took 20." in caplog.text


@pytest.mark.parametrize(
    "status", [DeclarationStatus.PROVED, DeclarationStatus.IN_PROGRESS, DeclarationStatus.FAILED], ids=str
)
async def test_from_takes_its_budget_from_the_run_that_set_the_properties_not_a_later_follow_up(
    repo: Path, caplog, status: DeclarationStatus
):
    """A short follow-up after the run would otherwise cut the budget to its length, verdict or not."""
    run = _task(took=timedelta(minutes=214))
    run.agent_task_id = "run-1"
    follow_up = _task(TaskStatus.CANCELED, took=timedelta(minutes=2))
    follow_up.agent_task_id = "follow-up-2"
    follow_up.created_at = datetime(2026, 1, 3)  # After the verdicts' last update, 2026-01-02.
    args, action = _parse(["setup", "--from", PROJECT_ID])
    assert action.preflight(args) is None

    with (
        patch.object(Project, "_resolve", AsyncMock(return_value=(PROJECT_ID, None))),
        patch.object(Project, "from_id", AsyncMock(return_value=_project())),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([follow_up, run], None))),
        patch.object(
            Project, "get_declarations", AsyncMock(return_value=[_proposal("p", "It holds.", 1, status=status)])
        ),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        assert await action.run_action(args) == EXIT_SUCCESS

    assert manifest.load(repo / "aristotle.toml").max_wall_time_seconds == 321 * 60
    assert (
        "# The run that set these properties, task run-1, took 214 min. The budget allows half as long again, and at "
        "least an hour." in (repo / "aristotle.toml").read_text()
    )
    assert "Its time budget is 321 minutes; the run that set its properties, task run-1, took 214." in caplog.text
    follow_up.get_events.assert_not_awaited()


async def test_from_a_run_it_cannot_attribute_says_only_how_long_the_newest_took(repo: Path, caplog):
    follow_up = _task(TaskStatus.CANCELED, took=timedelta(minutes=2))
    follow_up.created_at = datetime(2026, 1, 3)  # After the property's last update, and the only task listed.
    follow_up.get_events = AsyncMock(
        return_value=([_event(EventType.SUMMARY, follow_up.created_at + timedelta(minutes=2))], None)
    )
    args, action = _parse(["setup", "--from", PROJECT_ID])
    assert action.preflight(args) is None

    with (
        patch.object(Project, "_resolve", AsyncMock(return_value=(PROJECT_ID, None))),
        patch.object(Project, "from_id", AsyncMock(return_value=_project())),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([follow_up], None))),
        patch.object(Project, "get_declarations", AsyncMock(return_value=[_proposal("p", "It holds.", 1)])),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        assert await action.run_action(args) == EXIT_SUCCESS

    assert "# The run this was written from took 2 min." in (repo / "aristotle.toml").read_text()
    assert "the run it was written from took 2." in caplog.text


@pytest.mark.parametrize(
    "events",
    [
        AsyncMock(return_value=([], None)),
        AsyncMock(return_value=([_event(EventType.QUESTION, datetime(2026, 1, 3))], None)),
        AsyncMock(side_effect=AristotleAPIError("Bad gateway", 502)),
    ],
    ids=["no events", "only a question", "events unavailable"],
)
async def test_a_run_of_unknown_length_gets_the_default_budget_without_a_note(repo: Path, caplog, events):
    task = _task(took=timedelta(minutes=214))
    task.get_events = events

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup([], [_proposal("p", "It holds.", 1)], task=task)

    assert code == EXIT_SUCCESS
    assert manifest.load(repo / "aristotle.toml").max_wall_time_seconds == 3600
    assert "The run this was written from took" not in (repo / "aristotle.toml").read_text()
    assert "Its time budget is" not in caplog.text


@pytest.mark.parametrize(
    "argv",
    [["setup", "--manifest", "x.toml", "--directory", "hw"], ["--manifest", "x.toml", "--directory", "hw", "setup"]],
    ids=["after setup", "before setup"],
)
def test_manifest_and_directory_are_read_on_either_side_of_setup(argv: list[str]):
    args, action = _parse(argv)

    assert isinstance(action, SetupAction)
    assert args.manifest == Path("x.toml")
    assert args.directory == Path("hw")


def test_ci_without_setup_still_runs_ci():
    args, action = _parse(["--dry-run"])

    assert isinstance(action, CIAction)
    assert args.ci_command is None


def test_an_existing_manifest_exits_2_before_the_api_key_is_looked_for(repo: Path, monkeypatch):
    (repo / "aristotle.toml").write_text("# mine\n")
    monkeypatch.setattr(api_request, "API_KEY", None)
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    args, action = _parse(["setup"])

    with patch.object(SetupAction, "validate", side_effect=AssertionError("must not look for a key")):
        assert action.run(args) == EXIT_USAGE_ERROR


@pytest.mark.parametrize(
    ("status", "live"), [(TaskStatus.IN_PROGRESS, True), (TaskStatus.UNKNOWN, False)], ids=["still running", "unknown"]
)
async def test_from_refuses_a_project_whose_run_is_not_known_to_have_finished(
    repo: Path, caplog, status: TaskStatus, live: bool
):
    args, action = _parse(["setup", "--from", PROJECT_ID])

    assert action.preflight(args) is None
    with (
        patch.object(Project, "from_id", AsyncMock(return_value=_project())),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([_task(status, live=live)], None))),
        patch.object(Project, "get_declarations", AsyncMock(side_effect=AssertionError("must not read them"))),
    ):
        code = await action.run_action(args)

    assert code == EXIT_USAGE_ERROR
    assert not (repo / "aristotle.toml").exists()
    assert f"is not known to have finished ({status.name})" in caplog.text


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], f"aristotle ci setup --from {PROJECT_ID}"),
        (["--directory", "sub dir"], _line("aristotle", "ci", "setup", "--from", PROJECT_ID, "--directory", "sub dir")),
        (
            ["--manifest", "ci/a.toml", "--force"],
            f"aristotle ci setup --from {PROJECT_ID} --manifest ci/a.toml --force",
        ),
    ],
    ids=["default", "directory", "manifest and force"],
)
async def test_the_recovery_command_repeats_where_the_manifest_goes(repo: Path, caplog, argv: list[str], expected: str):
    (repo / "sub dir").mkdir()
    (repo / "sub dir" / "alu.v").write_text("module alu;\nendmodule\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    (repo / "ci").mkdir()
    task = _task(status=TaskStatus.IN_PROGRESS, live=True)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup(argv, [], task=task)

    assert code == EXIT_INFRASTRUCTURE_ERROR
    assert caplog.messages[-1].splitlines()[-1] == f"  {expected}"


def test_dry_run_before_setup_is_refused_before_anything_runs(repo: Path, capsys):
    args, action = _parse(["--dry-run", "setup"])

    with pytest.raises(SystemExit) as exited:
        action.preflight(args)

    assert exited.value.code == EXIT_USAGE_ERROR
    assert capsys.readouterr().err.endswith(
        "error: --dry-run is an option of `aristotle ci`, not of `aristotle ci setup`.\n"
    )


async def test_each_property_notes_whether_it_was_already_proved_or_disproved(repo: Path, caplog):
    declarations = [
        _proposal("holds", "It holds.", 1, status=DeclarationStatus.PROVED, source_file="fifo.v"),
        _proposal("broken", "It is broken.", 2, status=DeclarationStatus.COUNTEREXAMPLE),
        _proposal("open", "Not checked.", 3, status=DeclarationStatus.IN_PROGRESS),
    ]

    code, _ = await _setup([], declarations)

    assert code == EXIT_SUCCESS
    text = (repo / "aristotle.toml").read_text()
    assert "# Fifo.holds (fifo.v); proved when proposed\n" in text
    assert "# Fifo.broken; disproved by counterexample when proposed, so CI fails on it as it stands\n" in text
    assert "# Fifo.open\n" in text
    assert "Aristotle already disproved broken, so `aristotle ci` fails on it as it stands." in caplog.text


async def test_an_out_of_budget_setup_run_notes_its_verdicts_unchecked(repo: Path, caplog):
    declarations = [
        _proposal("holds", "It holds.", 1, status=DeclarationStatus.PROVED, source_file="fifo.v"),
        _proposal("broken", "It is broken.", 2, status=DeclarationStatus.COUNTEREXAMPLE),
    ]

    code, _ = await _setup([], declarations, task=_task(TaskStatus.OUT_OF_BUDGET))

    assert code == EXIT_SUCCESS
    text = (repo / "aristotle.toml").read_text()
    assert "# Fifo.holds (fifo.v); proved when proposed, unchecked\n" in text
    assert "# Fifo.broken; disproved by counterexample when proposed, unchecked\n" in text
    assert "already disproved" not in caplog.text


async def test_an_unchecked_verdict_settles_no_expected_failure_or_deliberate_counterexample(repo: Path, caplog):
    declarations = [
        _proposal(
            "wraps",
            "The count wraps at 16. Expected to fail: it saturates at 15.",
            1,
            status=DeclarationStatus.COUNTEREXAMPLE,
        ),
        _proposal(
            "demo",
            "The count resets to 0. Deliberate counterexample: reset is synchronous.",
            2,
            status=DeclarationStatus.PROVED,
        ),
    ]

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup([], declarations, task=_task(TaskStatus.OUT_OF_BUDGET))

    assert code == EXIT_SUCCESS
    text = (repo / "aristotle.toml").read_text()
    assert (
        "# Fifo.wraps; disproved by counterexample when proposed, unchecked; Aristotle expects it to be disproved: it saturates at 15.\n"
        in text
    )
    assert (
        "# Fifo.demo; proved when proposed, unchecked; a deliberate counterexample, Aristotle says: reset is "
        "synchronous.\n" in text
    )
    assert [p.expect_disproved for p in manifest.load(repo / "aristotle.toml").properties] == [False, True]
    assert (
        "Aristotle expects wraps to be disproved, so `aristotle ci` would fail on it. A disproved property"
        in caplog.text
    )
    assert 'Aristotle says demo shows a counterexample on purpose, so it is written with expect = "disproved"' in (
        caplog.text
    )
    assert "already disproved" not in caplog.text


async def test_from_a_failed_run_notes_its_proofs_unchecked_and_keeps_its_refutations(repo: Path, caplog):
    declarations = [
        _proposal("holds", "It holds.", 1, status=DeclarationStatus.PROVED, source_file="fifo.v"),
        _proposal("broken", "It is broken.", 2, status=DeclarationStatus.COUNTEREXAMPLE),
    ]
    args, action = _parse(["setup", "--from", PROJECT_ID])

    assert action.preflight(args) is None
    with (
        patch.object(Project, "from_id", AsyncMock(return_value=_project())),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([_task(TaskStatus.FAILED)], None))),
        patch.object(Project, "get_declarations", AsyncMock(return_value=declarations)),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        code = await action.run_action(args)

    assert code == EXIT_SUCCESS
    text = (repo / "aristotle.toml").read_text()
    assert "# Fifo.holds (fifo.v); proved when proposed, unchecked\n" in text
    assert "# Fifo.broken; disproved by counterexample when proposed, so CI fails on it as it stands\n" in text
    assert "Aristotle already disproved broken, so `aristotle ci` fails on it as it stands." in caplog.text


async def test_from_a_project_whose_earlier_runs_cannot_be_listed_writes_nothing(repo: Path, caplog):
    # The proof was last updated on 2026-01-02, before the follow-up: the run before it set it, and can't be listed.
    declarations = [_proposal("holds", "It holds.", 1, status=DeclarationStatus.PROVED, source_file="fifo.v")]
    follow_up = _task(TaskStatus.COMPLETE)
    follow_up.created_at = datetime(2026, 1, 3)

    async def tasks(limit: int, **kwargs):
        if limit == 1:
            return [follow_up], None
        raise AristotleAPIError("Service unavailable", status_code=503)

    args, action = _parse(["setup", "--from", PROJECT_ID])

    assert action.preflight(args) is None
    with (
        patch.object(Project, "from_id", AsyncMock(return_value=_project())),
        patch.object(Project, "get_tasks", AsyncMock(side_effect=tasks)),
        patch.object(Project, "get_declarations", AsyncMock(return_value=declarations)),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        code = await action.run_action(args)

    assert code == EXIT_INFRASTRUCTURE_ERROR
    assert not (repo / "aristotle.toml").exists()
    assert "tasks couldn't be listed, so its proofs can't be told apart" in caplog.text
    assert f"aristotle ci setup --from {PROJECT_ID}" in caplog.text


@pytest.mark.parametrize(
    ("first", "note"),
    [(TaskStatus.COMPLETE, "proved when proposed"), (TaskStatus.CANCELED, "proved when proposed, unchecked")],
    ids=["set by a run that finished", "set by a cancelled run"],
)
async def test_from_a_project_with_a_cancelled_follow_up_notes_a_proof_by_the_run_that_set_it(
    repo: Path, first: TaskStatus, note: str
):
    # Last updated on 2026-01-02, by the first run; the cancelled follow-up was created the day after.
    declarations = [_proposal("holds", "It holds.", 1, status=DeclarationStatus.PROVED, source_file="fifo.v")]
    follow_up = _task(TaskStatus.CANCELED)
    follow_up.created_at = datetime(2026, 1, 3)
    args, action = _parse(["setup", "--from", PROJECT_ID])

    assert action.preflight(args) is None
    with (
        patch.object(Project, "from_id", AsyncMock(return_value=_project())),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([follow_up, _task(first)], None))),
        patch.object(Project, "get_declarations", AsyncMock(return_value=declarations)),
    ):
        code = await action.run_action(args)

    assert code == EXIT_SUCCESS
    assert f"# Fifo.holds (fifo.v); {note}\n" in (repo / "aristotle.toml").read_text()


FIFO_RTL = """\
module fifo #(parameter DEPTH = 4) (input clk, input rst, input push, input pop, output full);
  reg [2:0] count;
  a_no_overflow: assert property (@(posedge clk) disable iff (rst) !(full && push && !pop));
  a_no_underflow: assert property (@(posedge clk) disable iff (rst) (pop && count == 0) |=> count == 0);
  assert property (@(posedge clk) count <= DEPTH);
endmodule

module fifo_ctrl (input clk);
endmodule
"""

NO_OVERFLOW_ASSERTION = "a_no_overflow: assert property (@(posedge clk) disable iff (rst) !(full && push && !pop));"

NO_UNDERFLOW_ASSERTION = (
    "a_no_underflow: assert property (@(posedge clk) disable iff (rst) (pop && count == 0) |=> count == 0);"
)


@pytest.fixture
def rtl(repo: Path) -> Path:
    (repo / "rtl").mkdir()
    (repo / "rtl" / "fifo.v").write_text(FIFO_RTL)
    return repo


async def test_statements_lose_the_prompts_numbering_and_name_their_module_and_file(rtl: Path):
    in_fifo = {"source_file": "rtl/fifo.v", "source_anchor": "reg [2:0] count;"}
    declarations = [
        _proposal("no_overflow", "(1) No overflow: count never exceeds DEPTH.", 1, **in_fifo),
        _proposal("named", "2. In module `fifo` of `rtl/fifo.v`, full is high when count is DEPTH.", 2, **in_fifo),
        _proposal("only_file", "3) In `rtl/fifo.v`, count resets to 0.", 3, **in_fifo),
        _proposal("only_module", "[4] In fifo, pop on empty is ignored.", 4, **in_fifo),
        _proposal(
            "ctrl",
            "#5 The controller has no input but clk.",
            5,
            source_file="rtl/fifo.v",
            source_anchor="module fifo_ctrl (input clk);",
        ),
        _proposal("two_modules", "The file holds two modules.", 6, source_file="rtl/fifo.v"),
        _proposal("c_model", "The C model never overflows.", 7, source_file="model/fifo.c"),
        _proposal("unplaced", "1.5 V is never driven.", 8),
        _proposal("colon", "9: In module `fifo` of `rtl/fifo.v`, count is never negative.", 9, **in_fifo),
        _proposal("suggested", "Suggested: in module `fifo` of `rtl/fifo.v`, push on full is ignored.", 10, **in_fifo),
        _proposal("claim", "Claim that in module `fifo` of `rtl/fifo.v`, count wraps.", 11, **in_fifo),
        _proposal("numbered_claim", "12. Claim that in fifo, empty is low after a push.", 12, **in_fifo),
        _proposal("signal", "claim that is granted stays high until ack.", 13),
        _proposal("suggested_numbered", "Suggested: (2) In module `fifo`, count is never negative.", 14),
    ]

    code, _ = await _setup([], declarations)

    assert code == EXIT_SUCCESS
    assert [p.statement for p in manifest.load(rtl / "aristotle.toml").properties] == [
        "No overflow: count never exceeds DEPTH. (Module `fifo` in `rtl/fifo.v`.)",
        "In module `fifo` of `rtl/fifo.v`, full is high when count is DEPTH.",
        "In `rtl/fifo.v`, count resets to 0. (Module `fifo`.)",
        "In fifo, pop on empty is ignored. (In `rtl/fifo.v`.)",
        "The controller has no input but clk. (Module `fifo_ctrl` in `rtl/fifo.v`.)",
        "The file holds two modules. (In `rtl/fifo.v`.)",
        "The C model never overflows. (In `model/fifo.c`.)",
        "1.5 V is never driven.",
        "In module `fifo` of `rtl/fifo.v`, count is never negative.",
        "in module `fifo` of `rtl/fifo.v`, push on full is ignored.",
        "in module `fifo` of `rtl/fifo.v`, count wraps.",
        "in fifo, empty is low after a push. (In `rtl/fifo.v`.)",
        "claim that is granted stays high until ack.",
        "In module `fifo`, count is never negative.",
    ]


async def test_a_property_that_names_the_labelled_assertion_it_is_on_is_written_as_that_assertion(rtl: Path, caplog):
    on_overflow = {"source_file": "rtl/fifo.v", "source_anchor": NO_OVERFLOW_ASSERTION}
    on_underflow = {"source_file": "rtl/fifo.v", "source_anchor": NO_UNDERFLOW_ASSERTION}
    declarations = [
        _proposal(
            "no_underflow",
            "(2) The SystemVerilog assertion `a_no_underflow` in module `fifo` holds: a pop when empty leaves it empty.",
            2,
            **on_underflow,
        ),
        _proposal("a_no_overflow", "A push when full is held off.", 3, **on_overflow),
        _proposal("again", "The assertion a_no_overflow holds.", 5, **on_overflow),
        _proposal(
            "bounded",
            "count never exceeds DEPTH.",
            6,
            source_file="rtl/fifo.v",
            source_anchor="assert property (@(posedge clk) count <= DEPTH);",
        ),
    ]

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup([], declarations)

    assert code == EXIT_SUCCESS
    in_fifo = "(Module `fifo` in `rtl/fifo.v`.)"
    assert [(p.id, p.kind, p.statement) for p in manifest.load(rtl / "aristotle.toml").properties] == [
        ("no_underflow", manifest.Kind.SVA, "fifo.a_no_underflow"),
        ("a_no_overflow", manifest.Kind.SVA, "fifo.a_no_overflow"),
        ("again", manifest.Kind.NATURAL_LANGUAGE, f"The assertion a_no_overflow holds. {in_fifo}"),
        ("bounded", manifest.Kind.NATURAL_LANGUAGE, f"count never exceeds DEPTH. {in_fifo}"),
    ]
    assert (
        "# Fifo.no_underflow (rtl/fifo.v); proved when proposed; Aristotle describes it as: The SystemVerilog "
        "assertion `a_no_underflow` in module `fifo` holds: a pop when empty leaves it empty.\n"
        "[requested_properties.no_underflow]\n"
    ) in (rtl / "aristotle.toml").read_text()
    assert "  no_underflow (sva): fifo.a_no_underflow\n" in caplog.text


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("The assertion a_no_overflow holds.", "fifo.a_no_overflow"),
        ("The SystemVerilog assertion `fifo.a_no_overflow` holds: a full FIFO takes no push.", "fifo.a_no_overflow"),
        (
            "The SystemVerilog assertion `a_no_overflow` in module `fifo` holds: a full FIFO takes no push.",
            "fifo.a_no_overflow",
        ),
        ("The assertion `a_no_overflow` in module `fifo` of `rtl/fifo.v` holds.", "fifo.a_no_overflow"),
        ("top.u_fifo.a_no_overflow holds.", "fifo.a_no_overflow"),
        ("a_no_overflow holds only while rst is low.", None),
        ("a_no_overflow is not vacuous: full and push are both high in some cycle.", None),
        ("The assertion a_no_overflow holds even without its disable iff.", None),
        ("Only while rst is low, a_no_overflow holds.", None),
        ("Even without its disable iff, the assertion a_no_overflow holds.", None),
        ("Assuming a_no_overflow holds: count never exceeds DEPTH.", None),
        ("a_no_overflow holds; consequently count never exceeds DEPTH.", None),
        ("A full FIFO takes no push.", None),
    ],
)
def test_a_property_on_an_assertion_is_that_assertion_only_when_it_says_it_holds(
    description: str, expected: str | None
):
    declaration = make_declaration(
        name="Fifo.p", description=description, source_file="rtl/fifo.v", source_anchor=NO_OVERFLOW_ASSERTION
    )

    assert _assertion(declaration, "fifo", description) == expected


async def test_the_module_is_the_one_open_at_the_property_line(repo: Path):
    (repo / "top.sv").write_text(
        "module stub; endmodule\n`define V 4\n(* keep *) module top (input clk);\n"
        "  wire w; /* nor endmodule here */\n  reg r;\nendmodule\n`define W 8\n"
    )
    declarations = [
        _proposal("no_ports", "It has no ports.", 1, source_file="top.sv", source_anchor="module stub; endmodule"),
        _proposal("v", "V is 4.", 2, source_file="top.sv", source_anchor="`define V 4"),
        _proposal("r_held", "r is held.", 3, source_file="top.sv", source_anchor="reg r;"),
        _proposal("width", "W is 8.", 4, source_file="top.sv", source_anchor="`define W 8"),
    ]

    code, _ = await _setup([], declarations)

    assert code == EXIT_SUCCESS
    assert [p.statement for p in manifest.load(repo / "aristotle.toml").properties] == [
        "It has no ports. (Module `stub` in `top.sv`.)",
        "V is 4. (In `top.sv`.)",
        "r is held. (Module `top` in `top.sv`.)",
        "W is 8. (In `top.sv`.)",
    ]


async def test_module_headers_and_terminators_in_block_comments_are_not_code(repo: Path):
    (repo / "actual.sv").write_text(
        "module actual (input clk);\n/* For instance:\nmodule obsolete;\n*/\n"
        "  a_ok: assert property (@(posedge clk) 1);\n/*\nendmodule\n*/\n"
        "  /* b_old: assert property (@(posedge clk) 0);\n  b_old: assert property (@(posedge clk) 0); */\n"
        "  reg r;\nendmodule\n"
    )
    declarations = [
        _proposal(
            "ok",
            "The assertion a_ok holds.",
            1,
            source_file="actual.sv",
            source_anchor="a_ok: assert property (@(posedge clk) 1);",
        ),
        _proposal("r_held", "r is held.", 2, source_file="actual.sv", source_anchor="reg r;"),
        _proposal(
            "old",
            "The assertion b_old holds.",
            3,
            source_file="actual.sv",
            source_anchor="b_old: assert property (@(posedge clk) 0); */",
        ),
    ]

    code, _ = await _setup([], declarations)

    assert code == EXIT_SUCCESS
    assert [(p.kind, p.statement) for p in manifest.load(repo / "aristotle.toml").properties] == [
        (manifest.Kind.SVA, "actual.a_ok"),
        (manifest.Kind.NATURAL_LANGUAGE, "r is held. (Module `actual` in `actual.sv`.)"),
        (manifest.Kind.NATURAL_LANGUAGE, "The assertion b_old holds. (Module `actual` in `actual.sv`.)"),
    ]


async def test_a_directory_named_like_the_module_does_not_name_it(repo: Path):
    (repo / "rtl" / "fifo").mkdir(parents=True)
    (repo / "rtl" / "fifo" / "fifo.v").write_text("module fifo;\nendmodule\n")

    code, _ = await _setup(
        [], [_proposal("p", "In `rtl/fifo/fifo.v`, count never exceeds DEPTH.", 1, source_file="rtl/fifo/fifo.v")]
    )

    assert code == EXIT_SUCCESS
    assert [p.statement for p in manifest.load(repo / "aristotle.toml").properties] == [
        "In `rtl/fifo/fifo.v`, count never exceeds DEPTH. (Module `fifo`.)"
    ]


async def test_a_property_aristotle_expects_to_fail_is_marked_and_warned_about(repo: Path, caplog):
    declarations = [
        _proposal(
            "full_flag",
            "In module `fifo` of `fifo.v`, full rises with the push that fills the FIFO. Expected to fail: full is "
            "registered, so it rises a cycle late.",
            1,
            status=DeclarationStatus.IN_PROGRESS,
            source_file="fifo.v",
        ),
        _proposal(
            "failover",
            "In module `fifo` of `fifo.v`, a stalled read is expected to fail over to the spare port.",
            2,
            status=DeclarationStatus.IN_PROGRESS,
            source_file="fifo.v",
        ),
    ]

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup([], declarations)

    assert code == EXIT_SUCCESS
    assert [p.statement for p in manifest.load(repo / "aristotle.toml").properties] == [
        "In module `fifo` of `fifo.v`, full rises with the push that fills the FIFO.",
        "In module `fifo` of `fifo.v`, a stalled read is expected to fail over to the spare port.",
    ]
    text = (repo / "aristotle.toml").read_text()
    assert (
        "# Fifo.full_flag (fifo.v); Aristotle expects it to be disproved: full is registered, so it rises a cycle late.\n"
    ) in text
    assert "# Fifo.failover (fifo.v)\n" in text
    assert (
        "Aristotle expects full_flag to be disproved, so `aristotle ci` would fail on it. A disproved property means the code "
        "or the property is wrong" in caplog.text
    )
    assert [p.expect_disproved for p in manifest.load(repo / "aristotle.toml").properties] == [False, False]


async def test_a_deliberate_counterexample_is_marked_unless_a_checked_proof_settles_it(repo: Path, caplog):
    shows = "A full FIFO takes no write. Deliberate counterexample: it overwrites."
    declarations = [
        _proposal("demo", shows, 1, status=DeclarationStatus.NEGATED),
        _proposal("fixed", shows, 2, status=DeclarationStatus.PROVED),
        _proposal("pending", shows, 3, status=DeclarationStatus.IN_PROGRESS),
    ]

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup([], declarations)

    assert code == EXIT_SUCCESS
    assert [p.expect_disproved for p in manifest.load(repo / "aristotle.toml").properties] == [True, False, True]
    text = (repo / "aristotle.toml").read_text()
    assert "# Fifo.demo; disproved when proposed, a deliberate counterexample: it overwrites.\n" in text
    assert "# Fifo.fixed; proved when proposed\n" in text
    assert "# Fifo.pending; a deliberate counterexample, Aristotle says: it overwrites.\n" in text
    assert 'unless it is marked expect = "disproved", and on a proved one that is.' in text.splitlines()[2]
    assert "already disproved" not in caplog.text and "expects" not in caplog.text
    assert (
        'Aristotle says demo, pending show counterexamples on purpose, so they are written with expect = "disproved"'
        in caplog.text
    )
    assert "Make sure each one is deliberate: if not, remove that line." in caplog.text


@pytest.mark.parametrize(
    "description",
    [
        "Full is set. Expected to fail: it is late. Deliberate counterexample: a demo.",
        "Full is set. Deliberate counterexample: a demo. Expected to fail: it is late.",
    ],
)
def test_an_expectation_and_a_deliberate_counterexample_split_in_either_order(description: str):
    assert _endings(description) == ("Full is set.", None, "it is late.", "a demo.")


async def test_a_deliberate_counterexample_keeps_what_it_assumes(repo: Path):
    description = "Full is set. Assumes: pop is low. Deliberate counterexample: the demo registers full."

    code, _ = await _setup([], [_proposal("full", description, 1, status=DeclarationStatus.IN_PROGRESS)])

    assert code == EXIT_SUCCESS
    [written] = manifest.load(repo / "aristotle.toml").properties
    assert (written.statement, written.assumptions, written.expect_disproved) == ("Full is set.", "pop is low.", True)


async def test_a_counterexample_aristotle_also_expects_to_fail_is_not_marked(repo: Path, caplog):
    description = "Full is set. Expected to fail: it is late. Deliberate counterexample: a demo."

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup([], [_proposal("full", description, 1, status=DeclarationStatus.IN_PROGRESS)])

    assert code == EXIT_SUCCESS
    assert [p.expect_disproved for p in manifest.load(repo / "aristotle.toml").properties] == [False]
    assert (
        "# Fifo.full; Aristotle expects it to be disproved: it is late; Aristotle also calls it a deliberate counterexample: "
        "a demo.\n" in (repo / "aristotle.toml").read_text()
    )
    assert "Aristotle expects full to be disproved, so `aristotle ci` would fail on it." in caplog.text
    assert "on purpose" not in caplog.text


async def test_what_a_property_assumes_goes_under_assumptions_for_either_kind(rtl: Path, caplog):
    on_overflow = {"source_file": "rtl/fifo.v", "source_anchor": NO_OVERFLOW_ASSERTION}
    in_fifo = {"source_file": "rtl/fifo.v", "source_anchor": "reg [2:0] count;"}
    declarations = [
        _proposal(
            "a_no_overflow",
            "The assertion a_no_overflow holds. Assumes: push is held low while rst is high.",
            1,
            **on_overflow,
        ),
        _proposal(
            "bounded",
            "In module `fifo` of `rtl/fifo.v`, count never exceeds DEPTH. assumes: DEPTH is 4. Expected to fail: count "
            "is three bits wide.",
            2,
            status=DeclarationStatus.IN_PROGRESS,
            **in_fifo,
        ),
        _proposal("plain", "In module `fifo` of `rtl/fifo.v`, count resets to 0.", 3, **in_fifo),
    ]

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup([], declarations)

    assert code == EXIT_SUCCESS
    assert [(p.kind, p.statement, p.assumptions) for p in manifest.load(rtl / "aristotle.toml").properties] == [
        (manifest.Kind.SVA, "fifo.a_no_overflow", "push is held low while rst is high."),
        (manifest.Kind.NATURAL_LANGUAGE, "In module `fifo` of `rtl/fifo.v`, count never exceeds DEPTH.", "DEPTH is 4."),
        (manifest.Kind.NATURAL_LANGUAGE, "In module `fifo` of `rtl/fifo.v`, count resets to 0.", None),
    ]
    assert "  a_no_overflow (sva): fifo.a_no_overflow Assumes: push is held low while rst is high.\n" in caplog.text
    assert "Aristotle expects bounded to be disproved, so `aristotle ci` would fail on it." in caplog.text


async def test_assumes_and_expected_to_fail_split_in_either_order(rtl: Path):
    in_fifo = {
        "source_file": "rtl/fifo.v",
        "source_anchor": "reg [2:0] count;",
        "status": DeclarationStatus.IN_PROGRESS,
    }
    declarations = [
        _proposal(
            "bounded",
            "Count never exceeds DEPTH. Expected to fail: count is 3 bits. Assumes: DEPTH is 8.",
            1,
            **in_fifo,
        ),
        _proposal(
            "full",
            "Full is set with the push that fills the FIFO. Assumes: no pop. Expected to fail: late.",
            2,
            **in_fifo,
        ),
    ]

    code, _ = await _setup([], declarations)

    assert code == EXIT_SUCCESS
    assert [(p.id, p.assumptions) for p in manifest.load(rtl / "aristotle.toml").properties] == [
        ("bounded", "DEPTH is 8."),
        ("full", "no pop."),
    ]
    text = (rtl / "aristotle.toml").read_text()
    assert "Aristotle expects it to be disproved: count is 3 bits.\n" in text
    assert "Aristotle expects it to be disproved: late.\n" in text


class _FullDisk:
    """A file whose writes fail as on a full disk."""

    def __init__(self, f):
        self.f = f

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.f.close()

    def write(self, _text):
        raise OSError(28, "No space left on device")


async def test_a_failed_write_leaves_no_partial_manifest_behind(repo: Path, caplog):
    real_open = open

    def full_disk(path, mode, **kwargs):
        return _FullDisk(real_open(path, mode, **kwargs))

    with patch("aristotlelib.cli.ci_setup.open", side_effect=full_disk, create=True):
        code, _ = await _setup([], [_proposal("p", "It holds.", 1)])

    assert code == EXIT_INFRASTRUCTURE_ERROR
    assert not (repo / "aristotle.toml").exists()
    assert "No space left on device" in caplog.text


async def test_a_failed_forced_write_keeps_the_old_manifest(repo: Path):
    (repo / "aristotle.toml").write_text("# mine\n")

    with patch("aristotlelib.cli.ci_setup.os.replace", side_effect=OSError(13, "Permission denied")):
        code, _ = await _setup(["--force"], [_proposal("p", "It holds.", 1)])

    assert code == EXIT_INFRASTRUCTURE_ERROR
    assert (repo / "aristotle.toml").read_text() == "# mine\n"
    assert sorted(p.name for p in repo.iterdir() if p.name.endswith(".tmp")) == []


async def test_an_api_error_after_the_run_says_how_to_write_the_manifest_later(repo: Path, caplog):
    args, action = _parse(["setup"])
    assert action.preflight(args) is None

    with (
        patch.object(Project, "create", AsyncMock(return_value=_project())),
        patch.object(Project, "get_tasks", AsyncMock(return_value=([_task()], None))),
        patch.object(Project, "get_declarations", AsyncMock(side_effect=AristotleAPIError("Bad Gateway", 502))),
    ):
        code = await action.run_action(args)

    assert code == EXIT_INFRASTRUCTURE_ERROR
    assert not (repo / "aristotle.toml").exists()
    assert f"aristotle ci setup --from {PROJECT_ID}" in caplog.text


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--manifest", "a" * 300 + "/aristotle.toml"], "is not a directory"),
        (["--manifest", "a" * 300 + ".toml"], "its name is longer than the file system takes"),
        (["--directory", "a" * 300], f"--directory: {'a' * 300} is not a directory."),
    ],
    ids=["manifest folder", "manifest name", "directory"],
)
async def test_a_path_too_long_for_the_file_system_is_refused(repo: Path, caplog, argv: list[str], expected: str):
    code, create = await _setup(argv, [])

    assert code == EXIT_USAGE_ERROR
    assert create is None
    assert expected in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="the limit a POSIX file system reports")
@pytest.mark.parametrize(("limit", "fits"), [(255, False), (400, True), (0, True)], ids=["255", "400", "unknown"])
def test_a_manifest_name_fits_within_the_limit_the_file_system_reports(limit: int, fits: bool, monkeypatch, tmp_path):
    monkeypatch.setattr("aristotlelib.cli.ci_setup.os.pathconf", lambda path, name: limit)

    assert _name_fits(tmp_path, "a" * 300 + ".toml") is fits


@pytest.mark.parametrize(
    ("name", "fits"),
    [("a" * 255, True), ("a" * 256, False), ("\U0001f600" * 128, False), ("a\udc80.toml", True)],
    ids=["255 units", "256 units", "128 emoji in 256 units", "lone surrogate"],
)
def test_a_manifest_name_fits_within_255_utf16_code_units_on_windows(name: str, fits: bool, tmp_path, monkeypatch):
    with monkeypatch.context() as windows:
        windows.setattr("aristotlelib.cli.ci_setup.os.name", "nt")
        result = _name_fits(tmp_path, name)

    assert result is fits


@pytest.mark.skipif(os.name == "nt", reason="Windows' 260-character path limit")
async def test_a_forced_write_takes_a_manifest_name_as_long_as_the_file_system_takes(repo: Path):
    name = "a" * 250 + ".toml"
    (repo / name).write_text("# mine\n")

    code, _ = await _setup(["--manifest", name, "--force"], [_proposal("p", "It holds.", 1)])

    assert code == EXIT_SUCCESS
    assert [p.id for p in manifest.load(repo / name).properties] == ["p"]


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permission bits, which root bypasses")
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--manifest", "locked/aristotle.toml"], "Cannot write locked/aristotle.toml: locked is not writable."),
        (["--manifest", "sealed/aristotle.toml"], "Cannot write sealed/aristotle.toml: sealed is not writable."),
        (["--directory", "sealed"], "--directory: sealed can't be read."),
    ],
    ids=["manifest in a read-only folder", "manifest in a folder that can't be entered", "directory"],
)
async def test_a_path_that_cannot_be_written_or_entered_is_refused_before_the_run(
    repo: Path, caplog, argv: list[str], expected: str
):
    (repo / "locked").mkdir()
    (repo / "locked").chmod(0o555)
    (repo / "sealed").mkdir()
    (repo / "sealed").chmod(0o000)

    try:
        code, create = await _setup(argv, [])
    finally:
        (repo / "locked").chmod(0o755)
        (repo / "sealed").chmod(0o755)

    assert code == EXIT_USAGE_ERROR
    assert create is None
    assert expected in caplog.text


async def test_a_manifest_path_that_is_a_directory_is_refused_even_with_force(repo: Path, caplog):
    (repo / "ci").mkdir()

    code, create = await _setup(["--manifest", "ci", "--force"], [])

    assert code == EXIT_USAGE_ERROR
    assert create is None
    assert "ci is a directory" in caplog.text


async def test_the_next_steps_are_shell_quoted(repo: Path, caplog):
    (repo / "sub dir").mkdir()
    (repo / "sub dir" / "alu.v").write_text("module alu;\nendmodule\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup(["--directory", "sub dir"], [_proposal("p", "It holds.", 1)])

    assert code == EXIT_SUCCESS
    assert f"  {_line('aristotle', 'ci', '--dry-run', '--directory', 'sub dir')}\n" in caplog.text
    assert f"  {_line('git', '-C', 'sub dir', 'add', '--', 'aristotle.toml')}\n" in caplog.text
    assert f"  {_line('git', '-C', 'sub dir', 'commit', '-m', 'Add aristotle.toml')}" in caplog.text


async def test_the_check_step_repeats_both_the_directory_and_the_manifest(repo: Path, caplog):
    (repo / "hw").mkdir()
    (repo / "hw" / "alu.v").write_text("module alu;\nendmodule\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup(["--directory", "hw", "--manifest", "hw/ci.toml"], [_proposal("p", "It holds.", 1)])

    assert code == EXIT_SUCCESS
    assert "  aristotle ci --dry-run --directory hw --manifest hw/ci.toml\n" in caplog.text


async def test_a_manifest_outside_the_working_directory_is_committed_from_its_own_directory(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup(["--directory", str(repo)], [_proposal("p", "It holds.", 1)])

    assert code == EXIT_SUCCESS
    directory = _path_arg(repo.resolve())
    assert f"  {_line('git', '-C', directory, 'add', '--', 'aristotle.toml')}\n" in caplog.text
    assert f"  {_line('git', '-C', directory, 'commit', '-m', 'Add aristotle.toml')}" in caplog.text


@pytest.mark.parametrize(
    ("argv", "named"),
    [
        (["--timeout", "5", "setup"], "--timeout is an option"),
        (["--no-junit", "--fail-on", "never", "setup"], "--fail-on, --no-junit are options"),
        (["--project", "1a2b3c4d", "setup"], "--project is an option"),
    ],
)
def test_ci_options_before_setup_are_refused_rather_than_ignored(repo: Path, capsys, argv: list[str], named: str):
    args, action = _parse(argv)

    with pytest.raises(SystemExit) as exited:
        action.preflight(args)

    assert exited.value.code == EXIT_USAGE_ERROR
    assert f"error: {named} of `aristotle ci`, not of `aristotle ci setup`." in capsys.readouterr().err


def test_ci_defaults_from_the_environment_do_not_count_as_options_given(repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARISTOTLE_TIMEOUT", "45")
    monkeypatch.setenv("ARISTOTLE_FAIL_ON", "never")
    args, action = _parse(["setup"])

    assert action.preflight(args) is None


def test_printed_paths_use_forward_slashes_on_windows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("aristotlelib.cli.ci_setup.os.name", "nt")

    assert _path_arg("hw\\sub dir") == "hw/sub dir"


@pytest.mark.parametrize(
    "get_tasks",
    [AsyncMock(return_value=([], None)), AsyncMock(side_effect=AristotleAPIError("Internal Server Error", 500))],
    ids=["no task", "listing fails"],
)
async def test_a_run_that_cannot_be_followed_says_how_to_write_the_manifest_later(repo: Path, caplog, get_tasks):
    args, action = _parse(["setup"])
    assert action.preflight(args) is None

    with (
        patch.object(Project, "create", AsyncMock(return_value=_project())),
        patch.object(Project, "get_tasks", get_tasks),
    ):
        code = await action.run_action(args)

    assert code == EXIT_INFRASTRUCTURE_ERROR
    assert not (repo / "aristotle.toml").exists()
    assert caplog.messages[-1].endswith(f"aristotle ci setup --from {PROJECT_ID}")


async def test_an_option_like_manifest_name_is_never_printed_as_an_option(repo: Path, caplog):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        code, _ = await _setup(["--manifest=-A"], [_proposal("p", "It holds.", 1)])

    assert code == EXIT_SUCCESS
    assert (repo / "-A").is_file()
    assert "  aristotle ci --dry-run --manifest ./-A\n" in caplog.text
    assert "  git add -- ./-A\n" in caplog.text


def test_a_path_argument_never_starts_with_a_dash():
    assert _path_arg("-hw") == "./-hw"
    assert _path_arg("hw/-x") == "hw/-x"


def test_a_ci_option_given_with_its_default_value_is_still_refused(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setenv("ARISTOTLE_TIMEOUT", "45")
    args, action = _parse(["--timeout", "45", "--fail-on", "failure", "setup"])

    with pytest.raises(SystemExit) as exited:
        action.preflight(args)

    assert exited.value.code == EXIT_USAGE_ERROR
    assert "error: --timeout, --fail-on are options" in capsys.readouterr().err


async def test_an_upload_over_the_size_limit_is_refused_naming_the_repository(
    repo: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    monkeypatch.setattr("aristotlelib.ci.runner.local_file_utils.MAX_FILE_SIZE_BYTES", 100)

    with caplog.at_level(logging.ERROR, logger="aristotle"):
        code, create = await _setup([], [])

    assert code == EXIT_USAGE_ERROR
    assert create is not None
    create.assert_not_awaited()
    [message] = caplog.messages
    assert message.startswith(f"The upload of {repo.resolve()} is too large: ")
