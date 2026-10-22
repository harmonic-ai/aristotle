"""Tests for aristotlelib.ci.runner."""

import asyncio
import gzip
import logging
import os
import signal
import sys
import tarfile
import time
import types
from collections.abc import Callable
from datetime import datetime
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from aristotlelib import api_request, local_file_utils
from aristotlelib.agent_task import AgentTask, TaskStatus
from aristotlelib.api_request import AristotleAPIError, AristotleOutcomeUnknownError, AristotleProjectLimitError
from aristotlelib.ci import runner, scope
from aristotlelib.ci.manifest import Kind, Manifest, RequestedProperty
from aristotlelib.ci.record import RunRecord, files_digest
from aristotlelib.event import EventType
from aristotlelib.project import AgentQuestionsSetting, Project, ProjectStatus
from aristotlelib.verilog import bundle
from tests.ci.conftest import git, latin1_name
from tests.conftest import hard_link, swapped_for_a_link

NO_DEADLINE = float("inf")

FIFO = RequestedProperty(id="fifo_no_overflow", kind=Kind.NATURAL_LANGUAGE, statement="The FIFO never overflows.")


def _manifest(*properties: RequestedProperty) -> Manifest:
    """A manifest of ``properties``, or of FIFO alone."""
    return Manifest(path=Path("aristotle.toml"), properties=list(properties or [FIFO]))


def _members(tarball: Path) -> set[str]:
    with tarfile.open(tarball, "r:gz") as tar:
        return set(tar.getnames())


def test_the_tarball_contains_the_upload_set(git_repo: Path, tmp_path: Path):
    result = scope.resolve(git_repo, scope.Scope.ALL)
    destination = tmp_path / "out.tar.gz"

    runner.build_tarball(git_repo, result.upload_files, destination, result.focus_files)

    assert "rtl/fifo.v" in _members(destination)
    assert "README.md" in _members(destination)


def test_a_group_name_that_is_not_utf8_does_not_stop_a_utf8_name_going_up(git_repo: Path, tmp_path: Path, monkeypatch):
    (git_repo / "rtl" / "naïve.v").write_text("module naive; endmodule\n")
    # As under an ASCII locale: tarfile's default encoding is ASCII, and a UTF-8 group name decodes with surrogates.
    monkeypatch.setattr(tarfile.TarFile, "encoding", "ascii")
    monkeypatch.setattr(tarfile, "grp", Mock(getgrgid=lambda gid: ("dom\udcc3\udca4nen-benutzer",)))
    destination = tmp_path / "out.tar.gz"

    runner.build_tarball(git_repo, [Path("rtl/naïve.v")], destination, None)

    with tarfile.open(destination, encoding="utf-8") as tar:
        assert tar.getnames() == ["rtl/naïve.v"]


def test_secrets_are_excluded_even_when_tracked_in_git(git_repo: Path, tmp_path: Path):
    result = scope.resolve(git_repo, scope.Scope.ALL)
    destination = tmp_path / "out.tar.gz"

    upload = runner.build_tarball(git_repo, result.upload_files, destination, result.focus_files)

    assert ".env" not in _members(destination)
    assert upload.excluded == [Path(".env")]
    assert Path(".env") not in upload.uploaded


def test_every_credential_pattern_is_left_out_in_either_case(git_repo: Path, tmp_path: Path):
    names = [pattern.replace("*", "x") for pattern in local_file_utils.SECRET_FILE_PATTERNS]
    secrets = [
        Path(folder) / name
        for folder, spelled in ((".", names), ("lower", names), ("upper", [n.upper() for n in names]))
        for name in spelled
    ]
    for path in secrets:
        (git_repo / path).parent.mkdir(exist_ok=True)
        (git_repo / path).write_text("machine example.invalid login x password y\n")
    destination = tmp_path / "out.tar.gz"

    upload = runner.build_tarball(git_repo, [Path("rtl/fifo.v"), *secrets], destination, None)

    assert _members(destination) == {"rtl/fifo.v"}
    assert upload.excluded == secrets


def test_files_in_a_folder_that_holds_credentials_are_left_out_as_submit_leaves_them(
    git_repo: Path, tmp_path: Path, caplog
):
    secrets = [
        Path("config/.env/production.yaml"),
        Path("sub/.netrc/inner"),
        Path(".docker/config.json"),
        Path(".envrc"),
    ]
    for path in secrets:
        (git_repo / path).parent.mkdir(parents=True, exist_ok=True)
        (git_repo / path).write_text("machine example.invalid login x password y\n")
    result = scope.ScopeResult(upload_files=[Path("rtl/fifo.v"), *secrets], focus_files=[])
    destination = tmp_path / "out.tar.gz"

    with caplog.at_level(logging.INFO, logger="aristotle"):
        package = runner.package(git_repo, result, destination, manifest=_manifest())

    assert _members(destination) == {"rtl/fifo.v"}
    assert package.summary.startswith(f"{runner.describe_upload(1, destination.stat().st_size)};")
    assert (
        "Excluded 4 file(s) matching secret patterns: config/.env/production.yaml, sub/.netrc/inner, .docker/config.json, .envrc"
        in caplog.messages
    )


def test_a_tracked_path_that_is_not_a_file_is_named_rather_than_failing(git_repo: Path, tmp_path: Path, caplog):
    (git_repo / "vendor").mkdir()
    destination = tmp_path / "out.tar.gz"

    upload = runner.build_tarball(
        git_repo, [Path("rtl/fifo.v"), Path("does/not/exist.v"), Path("vendor")], destination, [Path("rtl/fifo.v")]
    )

    assert _members(destination) == {"rtl/fifo.v", runner.FOCUS_MANIFEST}
    assert upload.uploaded == [Path("rtl/fifo.v")]
    assert "Not uploading 2 tracked path(s) that are not files in the checkout" in caplog.text
    assert "does/not/exist.v, vendor" in caplog.text


def test_every_tracked_path_that_is_not_a_file_is_named(git_repo: Path, tmp_path: Path, caplog):
    missing = [Path(f"gone/{i}.v") for i in range(7)]

    runner.build_tarball(git_repo, [Path("rtl/fifo.v"), *missing], tmp_path / "out.tar.gz", [Path("rtl/fifo.v")])

    assert "".join(f"\n  gone/{i}.v" for i in range(7)) in caplog.text


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
def test_a_symlink_to_an_uploaded_file_goes_up_as_that_files_content(git_repo: Path, tmp_path: Path, caplog):
    (git_repo / "rtl" / "alias.v").symlink_to("fifo.v")
    (git_repo / "rtl" / "secret.v").symlink_to("../.env")
    (git_repo / "rtl" / "outside.v").symlink_to(tmp_path / "elsewhere.v")
    (tmp_path / "elsewhere.v").write_text("module elsewhere;\nendmodule\n")
    files = [Path(".env"), Path("rtl/alias.v"), Path("rtl/fifo.v"), Path("rtl/outside.v"), Path("rtl/secret.v")]
    destination = tmp_path / "out.tar.gz"

    upload = runner.build_tarball(git_repo, files, destination, [Path("rtl/alias.v"), Path("rtl/secret.v")])

    with tarfile.open(destination, "r:gz") as tar:
        alias = tar.getmember("rtl/alias.v")
        assert alias.isfile()
        content = tar.extractfile(alias)
        assert content is not None and content.read() == (git_repo / "rtl" / "fifo.v").read_bytes()
        manifest = tar.extractfile(runner.FOCUS_MANIFEST)
        assert manifest is not None and manifest.read() == b"rtl/alias.v\n"
        assert set(tar.getnames()) == {"rtl/alias.v", "rtl/fifo.v", runner.FOCUS_MANIFEST}
    assert sorted(upload.uploaded) == [Path("rtl/alias.v"), Path("rtl/fifo.v")]
    assert "Left out 2 symbolic links" in caplog.text
    assert "rtl/outside.v (target not uploaded)" in caplog.text
    assert "rtl/secret.v (target not uploaded)" in caplog.text


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
def test_each_name_of_a_file_goes_up_as_a_file_holding_its_content(
    git_repo: Path, tmp_path_factory: pytest.TempPathFactory, caplog
):
    """A hard link, and links to a file with two names and to one with one; links leading out of the checkout or
    nowhere, and a linked folder, stay out."""
    rtl = git_repo / "rtl"
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (elsewhere / "id_ed25519").write_text("private key\n")
    (rtl / "solo.v").write_text("module solo;\nendmodule\n")
    hard_link(rtl / "fifo.v", rtl / "copy.v")
    (rtl / "alias.v").symlink_to("fifo.v")
    (rtl / "solo_alias.v").symlink_to("solo.v")
    (rtl / "key").symlink_to(elsewhere / "id_ed25519")
    (rtl / "broken.v").symlink_to("missing.v")
    (git_repo / "linked").symlink_to("rtl", target_is_directory=True)
    names = ["rtl/fifo.v", "rtl/copy.v", "rtl/alias.v", "rtl/solo.v", "rtl/solo_alias.v", "rtl/key", "rtl/broken.v"]
    destination = elsewhere / "out.tar.gz"

    upload = runner.build_tarball(git_repo, [*map(Path, names), Path("linked")], destination, None)

    with tarfile.open(destination, "r:gz") as tar:
        members = {m.name: (m.type, tar.extractfile(m).read() if m.isfile() else None) for m in tar.getmembers()}
    fifo, solo = (tarfile.REGTYPE, b"module fifo;\nendmodule\n"), (tarfile.REGTYPE, b"module solo;\nendmodule\n")
    assert members == {
        "rtl/fifo.v": fifo,
        "rtl/copy.v": fifo,
        "rtl/alias.v": fifo,
        "rtl/solo.v": solo,
        "rtl/solo_alias.v": solo,
    }
    assert sorted(upload.uploaded) == sorted(map(Path, names[:5]))
    assert (
        local_file_utils.skipped_symlinks_warning(
            {
                Path("linked"): "target not uploaded",
                Path("rtl/broken.v"): "broken link",
                Path("rtl/key"): "target not uploaded",
            }
        )
        in caplog.messages
    )


def test_a_tracked_file_swapped_for_a_link_while_it_is_archived_stops_packaging(
    git_repo: Path, tmp_path_factory: pytest.TempPathFactory
):
    """The run is worth retrying, and the link isn't followed."""
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (elsewhere / "id_ed25519").write_text("private key\n")
    fifo = git_repo / "rtl" / "fifo.v"
    result = scope.ScopeResult(upload_files=[Path("rtl/fifo.v")], focus_files=[Path("rtl/fifo.v")])

    with swapped_for_a_link(fifo, elsewhere / "id_ed25519"), pytest.raises(runner.RunnerError) as raised:
        runner.package(git_repo, result, elsewhere / "out.tar.gz")

    assert str(raised.value) == f"Could not archive {fifo}: it changed while it was being uploaded. Try again."
    assert b"private key" not in gzip.decompress((elsewhere / "out.tar.gz").read_bytes())


def test_the_tarball_lists_every_file_under_verification(git_repo: Path, tmp_path: Path):
    focus = [Path(f"rtl/mod{i}.v") for i in range(runner.MAX_FOCUS_FILES_IN_PROMPT + 20)]
    destination = tmp_path / "out.tar.gz"

    for path in focus:
        (git_repo / path).write_text(f"module {path.stem};\nendmodule\n")

    runner.build_tarball(git_repo, [Path("rtl/fifo.v"), *focus], destination, focus)

    with tarfile.open(destination, "r:gz") as tar:
        manifest = tar.extractfile(runner.FOCUS_MANIFEST)
        assert manifest is not None
        assert manifest.read().decode().splitlines() == [p.as_posix() for p in focus]


def test_a_tracked_file_at_the_manifest_path_is_not_uploaded(git_repo: Path, tmp_path: Path, caplog):
    (git_repo / runner.FOCUS_MANIFEST).write_text("stale\n")
    destination = tmp_path / "out.tar.gz"

    runner.build_tarball(git_repo, [Path(runner.FOCUS_MANIFEST), Path("rtl/fifo.v")], destination, [Path("rtl/fifo.v")])

    with tarfile.open(destination, "r:gz") as tar:
        assert tar.getnames() == ["rtl/fifo.v", runner.FOCUS_MANIFEST]
        manifest = tar.extractfile(runner.FOCUS_MANIFEST)
        assert manifest is not None
        assert manifest.read() == b"rtl/fifo.v\n"
    assert f"Not uploading the tracked {runner.FOCUS_MANIFEST}" in caplog.text


def test_without_a_focus_list_a_tracked_file_at_the_manifest_path_goes_up_as_is(git_repo: Path, tmp_path: Path, caplog):
    (git_repo / runner.FOCUS_MANIFEST).write_bytes(b"notes\n")
    destination = tmp_path / "out.tar.gz"

    upload = runner.build_tarball(git_repo, [Path(runner.FOCUS_MANIFEST), Path("rtl/fifo.v")], destination, None)

    with tarfile.open(destination, "r:gz") as tar:
        assert tar.getnames() == [runner.FOCUS_MANIFEST, "rtl/fifo.v"]
        content = tar.extractfile(runner.FOCUS_MANIFEST)
        assert content is not None and content.read() == b"notes\n"
    assert upload.uploaded == [Path(runner.FOCUS_MANIFEST), Path("rtl/fifo.v")]
    assert "Not uploading the tracked" not in caplog.text


async def test_submit_names_only_the_files_to_verify_that_went_up(git_repo: Path, tmp_path: Path):
    result = scope.ScopeResult(
        upload_files=[Path("rtl/fifo.v"), Path("rtl/gone.v")], focus_files=[Path("rtl/fifo.v"), Path("rtl/gone.v")]
    )

    with patch.object(runner.Project, "create", AsyncMock()) as create:
        await runner.submit(git_repo, result, tmp_path / "out.tar.gz", resubmit_by=NO_DEADLINE)

    prompt = create.await_args.kwargs["prompt"]
    assert "- rtl/fifo.v" in prompt
    assert "rtl/gone.v" not in prompt


@pytest.mark.parametrize(
    ("count", "size", "described"),
    [
        (1, 412, "1 file (412 bytes)"),
        (2, 1023, "2 files (1023 bytes)"),
        (1, 1024, "1 file (1.0 KB)"),
        (3, 1536, "3 files (1.5 KB)"),
        (4, 1024 * 1024 - 1, "4 files (1.0 MB)"),
        (30, int(2.4 * 1024 * 1024), "30 files (2.4 MB)"),
    ],
)
def test_describe_upload_counts_files_and_sizes_the_archive_in_a_readable_unit(count, size, described):
    assert runner.describe_upload(count, size) == described


async def test_submit_says_how_many_files_go_up_and_how_big_the_archive_is(git_repo: Path, tmp_path: Path, caplog):
    result = scope.ScopeResult(upload_files=[Path("rtl/fifo.v")], focus_files=[Path("rtl/fifo.v")])
    tarball = tmp_path / "out.tar.gz"

    with patch.object(runner.Project, "create", AsyncMock()), caplog.at_level(logging.INFO):
        await runner.submit(git_repo, result, tarball, resubmit_by=NO_DEADLINE)

    size = tarball.stat().st_size
    assert size < 1024
    assert f"Uploading 1 file ({size} bytes); verifying 1" in caplog.messages


async def test_submit_refuses_a_run_with_nothing_to_verify(git_repo: Path, tmp_path: Path):
    result = scope.ScopeResult(upload_files=[Path("rtl/fifo.v"), Path("rtl/gone.v")], focus_files=[Path("rtl/gone.v")])

    with (
        patch.object(runner.Project, "create", AsyncMock()) as create,
        pytest.raises(runner.RunnerError, match="None of the files to verify could be uploaded"),
    ):
        await runner.submit(git_repo, result, tmp_path / "out.tar.gz", resubmit_by=NO_DEADLINE)

    create.assert_not_awaited()


async def test_submit_with_a_manifest_asks_for_its_properties_without_a_focus_list(
    git_repo: Path, tmp_path: Path, caplog
):
    result = scope.resolve_checkout(git_repo)
    manifest = _manifest()
    tarball = tmp_path / "out.tar.gz"

    with (
        patch.object(runner.Project, "create", AsyncMock()) as create,
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        await runner.submit(git_repo, result, tarball, resubmit_by=NO_DEADLINE, manifest=manifest)

    assert _members(tarball) == {"README.md", "rtl/driver.c", "rtl/fifo.v"}
    record = RunRecord.of_checkout(git_repo, result.upload_files, manifest)
    assert create.await_args.kwargs["prompt"] == (
        f"{runner.build_manifest_prompt(manifest, result)}\n\n{record.prompt_ending}"
    )
    assert create.await_args.kwargs["agent_questions_setting"] == AgentQuestionsSetting.DISABLED
    uploading = runner.describe_upload(3, tarball.stat().st_size)
    assert f"Uploading {uploading}; checking 1 property" in caplog.messages


def test_a_manifest_runs_record_is_of_the_files_it_archived_though_the_checkout_changes_after(
    git_repo: Path, tmp_path: Path
):
    result = scope.resolve_checkout(git_repo)
    archived = files_digest(git_repo, result.upload_files)
    build_tarball = runner.build_tarball

    def archive_then_edit(*args, **kwargs):
        upload = build_tarball(*args, **kwargs)
        # A save, as an editor or a generator may make, between the archive and the record.
        (git_repo / "rtl" / "driver.c").write_text("int main(void) { return 1; }\n")
        return upload

    with patch.object(runner, "build_tarball", archive_then_edit):
        package = runner.package(git_repo, result, tmp_path / "out.tar.gz", manifest=_manifest())

    assert package.record is not None
    assert package.record.files == archived != files_digest(git_repo, result.upload_files)


async def test_submit_with_a_manifest_needs_no_source_file(git_repo: Path, tmp_path: Path):
    result = scope.ScopeResult(upload_files=[Path("README.md")], focus_files=[])

    with patch.object(runner.Project, "create", AsyncMock()) as create:
        await runner.submit(git_repo, result, tmp_path / "out.tar.gz", resubmit_by=NO_DEADLINE, manifest=_manifest())

    create.assert_awaited_once()


async def test_submit_with_a_manifest_refuses_a_run_with_nothing_uploaded(git_repo: Path, tmp_path: Path):
    result = scope.ScopeResult(upload_files=[Path(".env"), Path("does/not/exist.v")], focus_files=[])

    with (
        patch.object(runner.Project, "create", AsyncMock()) as create,
        pytest.raises(runner.RunnerError, match="No file could be uploaded"),
    ):
        await runner.submit(git_repo, result, tmp_path / "out.tar.gz", resubmit_by=NO_DEADLINE, manifest=_manifest())

    create.assert_not_awaited()


def _created(description: str = "project") -> Project:
    """A project as Project.create returns it, named after the archive."""
    return Project(
        project_id="00000000-0000-0000-0000-000000000001",
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 1),
        description=description,
        status=ProjectStatus.RUNNING,
        has_input=True,
        has_files=False,
    )


NAME = "rtl/hw: ci @ main 1a2b3c4"


async def test_submit_names_the_project_it_created(git_repo: Path, tmp_path: Path, caplog):
    result = scope.ScopeResult(upload_files=[Path("rtl/fifo.v")], focus_files=[Path("rtl/fifo.v")])
    project = _created()

    with (
        patch.object(runner.Project, "create", AsyncMock(return_value=project)),
        patch.object(runner.Project, "rename_by_id", AsyncMock(return_value=_created(NAME))) as rename,
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        submitted = await runner.submit(git_repo, result, tmp_path / "out.tar.gz", resubmit_by=NO_DEADLINE, name=NAME)

    rename.assert_awaited_once_with(project.project_id, NAME)
    assert submitted is project and project.description == NAME
    assert caplog.messages[-1] == f"Project name: {NAME}"


async def test_submit_without_a_name_leaves_the_project_as_created(git_repo: Path, tmp_path: Path):
    result = scope.ScopeResult(upload_files=[Path("rtl/fifo.v")], focus_files=[Path("rtl/fifo.v")])

    with (
        patch.object(runner.Project, "create", AsyncMock(return_value=_created())),
        patch.object(runner.Project, "rename_by_id", AsyncMock()) as rename,
    ):
        await runner.submit(git_repo, result, tmp_path / "out.tar.gz", resubmit_by=NO_DEADLINE)

    rename.assert_not_awaited()


LOST = AristotleOutcomeUnknownError(
    "ReadTimeout on POST /project. Aristotle may have acted on it, so it was not sent again."
)


def test_the_archive_is_uploaded_under_the_checkouts_name_and_a_random_suffix(git_repo: Path, tmp_path: Path):
    assert runner.new_upload_name(git_repo / "hw rtl ✓") != runner.new_upload_name(git_repo / "hw rtl ✓")
    assert runner.new_upload_name(Path("/work/hw rtl ✓")).rsplit(" ", 1)[0] == "hw-rtl"
    assert runner.new_upload_name(Path("/")).rsplit(" ", 1)[0] == "project"


async def _lose_the_create(
    git_repo: Path, tmp_path: Path, *listings, lost: AristotleOutcomeUnknownError = LOST
) -> tuple[Project, AsyncMock, AsyncMock]:
    """submit, with the create's response lost and ``listings`` returned by Project.list_projects in turn."""
    result = scope.ScopeResult(upload_files=[Path("rtl/fifo.v")], focus_files=[Path("rtl/fifo.v")])
    with (
        patch.object(runner, "new_upload_name", return_value="repo 0123456789ab"),
        patch.object(runner.Project, "create", AsyncMock(side_effect=lost)) as create,
        patch.object(runner.Project, "list_projects", AsyncMock(side_effect=listings)) as listed,
        patch.object(runner.Project, "rename_by_id", AsyncMock(return_value=_created(NAME))),
        patch.object(runner.asyncio, "sleep", AsyncMock()),
    ):
        project = await runner.submit(git_repo, result, tmp_path / "out.tar.gz", resubmit_by=NO_DEADLINE, name=NAME)
    return project, create, listed


async def test_a_create_whose_response_was_lost_is_found_by_its_upload_name_and_followed(git_repo, tmp_path, caplog):
    ours = _created("repo 0123456789ab")

    with caplog.at_level(logging.INFO, logger="aristotle"):
        project, create, listed = await _lose_the_create(
            git_repo, tmp_path, ([_created("repo 0123456789ac"), ours, _created("repo")], None)
        )

    assert project is ours and project.description == NAME
    assert create.await_args.kwargs["public_file_path"] == "repo 0123456789ab.tar.gz"
    listed.assert_awaited_once_with(
        limit=runner.PROJECTS_SEARCHED_FOR_A_LOST_CREATE, latest_run_status=runner.STARTED_STATUSES
    )
    start = next(i for i, message in enumerate(caplog.messages) if message.startswith(str(LOST)))
    assert caplog.messages[start : start + 3] == [
        f"{LOST} Looking for the project by the name it was uploaded under, repo 0123456789ab.",
        f"Found it, so following {ours.project_id}.",
        f"Project name: {NAME}",
    ]


async def test_a_lost_create_is_logged_without_submits_advice_as_ci_looks_it_up(git_repo, tmp_path, caplog):
    """Project.create wraps the error with advice to check `aristotle list`, which ci then does itself."""
    wrapped = AristotleOutcomeUnknownError(
        f"{LOST} If it did, the project was created: check `aristotle list` before submitting again."
    )
    wrapped.__cause__ = LOST

    with caplog.at_level(logging.INFO, logger="aristotle"):
        await _lose_the_create(git_repo, tmp_path, ([_created("repo 0123456789ab")], None), lost=wrapped)

    assert f"{LOST} Looking for the project by the name it was uploaded under, repo 0123456789ab." in caplog.messages
    assert "before submitting again" not in caplog.text


async def test_a_lost_create_is_looked_for_again_before_it_counts_as_missing(git_repo, tmp_path):
    ours = _created("repo 0123456789ab")

    project, _, listed = await _lose_the_create(git_repo, tmp_path, ([], None), ([ours], None))

    assert project is ours
    assert listed.await_count == 2


async def test_a_lost_create_that_is_never_found_is_outcome_unknown(git_repo, tmp_path):
    with pytest.raises(AristotleOutcomeUnknownError) as raised:
        await _lose_the_create(git_repo, tmp_path, *[([_created("repo")], None)] * (runner.LOST_CREATE_LOOKUPS + 1))

    assert str(raised.value) == (
        "None of your newest projects is named repo 0123456789ab, so Aristotle either did not create it or has not "
        "yet. Check `aristotle list` before running this again."
    )


async def test_a_lost_create_whose_run_never_started_is_not_followed(git_repo, tmp_path):
    started = [([], None)] * runner.LOST_CREATE_LOOKUPS
    stuck = _created("repo 0123456789ab")

    with pytest.raises(AristotleOutcomeUnknownError) as raised:
        await _lose_the_create(git_repo, tmp_path, *started, ([stuck], None))

    assert str(raised.value) == (
        f"Project {stuck.project_id} is named repo 0123456789ab, but its run hasn't started, so there is nothing to "
        "follow yet. Check `aristotle list` before running this again."
    )


def test_a_started_run_is_any_status_but_unknown():
    assert set(runner.STARTED_STATUSES) == set(TaskStatus) - {TaskStatus.UNKNOWN}


async def test_a_lost_create_matching_several_projects_is_outcome_unknown(git_repo, tmp_path):
    twins = [_created("repo 0123456789ab"), _created("repo 0123456789ab")]

    with pytest.raises(AristotleOutcomeUnknownError, match="^2 of your projects are named repo 0123456789ab. "):
        await _lose_the_create(git_repo, tmp_path, (twins, None))


async def test_a_lost_create_lookup_that_keeps_failing_is_not_retried_beyond_the_request_layer(
    git_repo, tmp_path, api_server
):
    result = scope.ScopeResult(upload_files=[Path("rtl/fifo.v")], focus_files=[Path("rtl/fifo.v")])
    requests = api_server(*[_unavailable] * api_request.RETRY_ATTEMPTS)

    with (
        patch.object(runner.Project, "create", AsyncMock(side_effect=LOST)),
        patch.object(runner.asyncio, "sleep", AsyncMock()),
        pytest.raises(AristotleOutcomeUnknownError, match=f" after {api_request.RETRY_ATTEMPTS} attempts "),
    ):
        await runner.submit(git_repo, result, tmp_path / "out.tar.gz", resubmit_by=NO_DEADLINE)

    assert len(requests) == api_request.RETRY_ATTEMPTS


async def test_a_lost_create_that_cannot_be_looked_for_is_outcome_unknown(git_repo, tmp_path):
    refused = AristotleAPIError("API request failed with status 403: Forbidden", 403)

    with pytest.raises(AristotleOutcomeUnknownError, match="^Your projects could not be listed to find it: API"):
        await _lose_the_create(git_repo, tmp_path, refused)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            AristotleAPIError("API request failed with status 500: boom", 500),
            "API request failed with status 500: boom",
        ),
        (AristotleAPIError("Too many requests to Aristotle at once.", 429), "Too many requests to Aristotle at once."),
        (ValueError("unexpected reply"), "unexpected reply"),
        (RuntimeError(), "RuntimeError"),
    ],
    ids=["server error", "rate limited", "malformed reply", "no message"],
)
async def test_a_failed_rename_is_a_warning_and_the_run_goes_ahead(error: Exception, reason: str, caplog):
    project = _created()

    with patch.object(runner.Project, "rename_by_id", AsyncMock(side_effect=error)):
        await runner.name_project(project, NAME)

    assert project.description == "project"
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, f'Could not name the project "{NAME}", so it keeps the name "project": {reason}')
    ]


async def test_a_rename_that_does_not_answer_is_abandoned_after_the_timeout(monkeypatch, caplog):
    monkeypatch.setattr(runner, "NAME_TIMEOUT_SECONDS", 0.05)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def stall(*_args):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with patch.object(runner.Project, "rename_by_id", AsyncMock(side_effect=stall)):
        await runner.name_project(_created(), NAME)

    assert started.is_set() and cancelled.is_set()
    assert caplog.messages == [
        f'Could not name the project "{NAME}", so it keeps the name "project": no answer in 0.05 s'
    ]


async def test_a_cancellation_during_the_rename_is_not_swallowed():
    with (
        patch.object(runner.Project, "rename_by_id", AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        await runner.name_project(_created(), NAME)


LIMIT_ERROR = AristotleProjectLimitError("You have too many projects in progress.")


def _in_progress(*project_ids: str) -> tuple[list[Project], None]:
    projects = [
        Project(
            project_id=project_id,
            created_at=datetime(2026, 1, 1),
            last_updated=datetime(2026, 1, 1),
            description=None,
            status=ProjectStatus.RUNNING,
            has_input=True,
            has_files=False,
        )
        for project_id in project_ids
    ]
    return projects, None


@pytest.fixture
def clock(monkeypatch) -> list[float]:
    """A clock the patched sleep drives, with jitter pinned to the top of its range."""
    now = [0.0]

    async def advance(seconds):
        now[0] += seconds

    monkeypatch.setattr(runner.asyncio, "sleep", advance)
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(runner.random, "uniform", lambda low, high: high)
    return now


async def _submit(git_repo: Path, tmp_path: Path, resubmit_by: float = NO_DEADLINE) -> Project:
    result = scope.ScopeResult(upload_files=[Path("rtl/fifo.v")], focus_files=[Path("rtl/fifo.v")])
    return await runner.submit(git_repo, result, tmp_path / "out.tar.gz", resubmit_by=resubmit_by)


async def test_a_refused_run_waits_for_a_project_to_finish_then_submits(git_repo, tmp_path, clock, caplog):
    created = object()
    listings = [_in_progress("a"), _in_progress("a"), _in_progress("a"), _in_progress()]

    with (
        patch.object(runner.Project, "create", AsyncMock(side_effect=[LIMIT_ERROR, created])) as create,
        patch.object(runner.Project, "list_projects", AsyncMock(side_effect=listings)),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        assert await _submit(git_repo, tmp_path) is created

    assert create.await_count == 2
    assert "Waiting to submit: Aristotle refused the run: You have too many projects in progress." in caplog.text
    assert "/projects/a" in caplog.text
    assert "A project finished. Submitting again." in caplog.text


async def test_a_listing_that_fails_after_a_refusal_is_polled_past(git_repo, tmp_path, clock, caplog):
    created = object()
    unavailable = AristotleAPIError("GET /project after 5 attempts over 21 s: Service Unavailable", 503)

    with (
        patch.object(runner.Project, "create", AsyncMock(side_effect=[LIMIT_ERROR, created])) as create,
        patch.object(
            runner.Project, "list_projects", AsyncMock(side_effect=[unavailable, _in_progress(), _in_progress()])
        ),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        assert await _submit(git_repo, tmp_path) is created

    assert create.await_count == 2
    assert f"Your projects in progress or queued could not be listed: {unavailable}" in caplog.text


async def test_a_slot_taken_by_another_run_is_waited_for_again(git_repo, tmp_path, clock):
    created = object()
    events: list[str] = []

    async def create(**_kwargs):
        events.append("create")
        if events.count("create") < 3:
            raise LIMIT_ERROR
        return created

    # "a" finishes, "b" takes the slot first, then "b" finishes.
    listings = iter([_in_progress("a"), _in_progress(), _in_progress("b"), _in_progress("b"), _in_progress()])

    async def list_projects(**_kwargs):
        listing = next(listings)
        events.append("list " + ",".join(p.project_id for p in listing[0]))
        return listing

    with (
        patch.object(runner.Project, "create", side_effect=create),
        patch.object(runner.Project, "list_projects", side_effect=list_projects),
    ):
        assert await _submit(git_repo, tmp_path) is created

    assert events == ["create", "list a", "list ", "create", "list b", "list b", "list ", "create"]


@pytest.mark.parametrize(
    "listings",
    [[("a", "b"), ("b",)], [("a", "b"), ("b", "c"), ("c",)], [("a",), ("a", "b"), ("b",)]],
    ids=["one of two", "one of two replaced, then one leaves", "one more, then one leaves"],
)
async def test_fewer_projects_listed_than_at_the_most_since_the_refusal_frees_a_slot(
    git_repo, tmp_path, clock, listings
):
    created = object()
    listed = iter([_in_progress(*project_ids) for project_ids in listings])

    with (
        patch.object(runner.Project, "create", AsyncMock(side_effect=[LIMIT_ERROR, created])) as create,
        patch.object(runner.Project, "list_projects", side_effect=lambda **_kwargs: next(listed)),
    ):
        assert await _submit(git_repo, tmp_path, resubmit_by=600) is created

    assert create.await_count == 2
    assert clock[0] == runner.POLL_INTERVAL_SECONDS * 1.25 * (len(listings) - 1)


async def test_a_project_replaced_by_another_is_waited_on_without_uploading_again(git_repo, tmp_path, clock, caplog):
    created = object()
    # "a" finishes and "b" takes its slot before the next listing, then "b" is replaced by "c", then "c" finishes.
    listed = iter([_in_progress("a"), _in_progress("b"), _in_progress("c"), _in_progress()])

    with (
        patch.object(runner.Project, "create", AsyncMock(side_effect=[LIMIT_ERROR, created])) as create,
        patch.object(runner.Project, "list_projects", side_effect=lambda **_kwargs: next(listed)),
        caplog.at_level(logging.INFO, logger="aristotle"),
    ):
        assert await _submit(git_repo, tmp_path) is created

    assert create.await_count == 2
    assert clock[0] == runner.POLL_INTERVAL_SECONDS * 1.25 * 3
    assert caplog.text.count("Still waiting to submit: 1 of your projects in progress or queued.") == 2
    assert caplog.text.count("A project finished. Submitting again.") == 1


async def test_a_project_that_appears_while_waiting_is_waited_on(git_repo, tmp_path, clock, monkeypatch):
    monkeypatch.setattr(runner, "SLOT_RETRY_SECONDS", 600)
    created = object()
    listed = iter([_in_progress(), _in_progress("x"), _in_progress()])

    with (
        patch.object(runner.Project, "create", AsyncMock(side_effect=[LIMIT_ERROR, created])),
        patch.object(runner.Project, "list_projects", side_effect=lambda **_kwargs: next(listed)),
    ):
        assert await _submit(git_repo, tmp_path) is created

    assert clock[0] == runner.POLL_INTERVAL_SECONDS * 1.25 * 2


async def test_a_wait_that_reaches_resubmit_by_tries_once_more_then_raises(git_repo, tmp_path, clock):
    uploads: list[float] = []

    async def create(**_kwargs):
        uploads.append(clock[0])
        raise LIMIT_ERROR

    with (
        patch.object(runner.Project, "create", side_effect=create),
        patch.object(runner.Project, "list_projects", AsyncMock(return_value=_in_progress("a"))),
        pytest.raises(runner.SlotTimeoutError, match="nothing was verified") as exc_info,
    ):
        await _submit(git_repo, tmp_path, resubmit_by=600)

    assert uploads == [0, 600]
    assert str(exc_info.value).startswith("Aristotle refused the run: You have too many projects in progress.")
    assert "/projects/a" in str(exc_info.value)


async def test_a_slot_freed_by_an_unlisted_project_is_taken_at_resubmit_by(git_repo, tmp_path, clock):
    created = object()

    async def create(**_kwargs):
        # An unlisted project (another organization's) holds the second slot until t=60; "a" never leaves.
        if clock[0] < 60:
            raise LIMIT_ERROR
        return created

    with (
        patch.object(runner.Project, "create", side_effect=create),
        patch.object(runner.Project, "list_projects", AsyncMock(return_value=_in_progress("a"))),
    ):
        assert await _submit(git_repo, tmp_path, resubmit_by=600) is created

    assert clock[0] == 600


async def test_the_per_second_limit_is_not_waited_out(git_repo, tmp_path, clock):
    per_second = AristotleAPIError("Too many requests to Aristotle at once.", status_code=429)

    with (
        patch.object(runner.Project, "create", AsyncMock(side_effect=per_second)) as create,
        patch.object(runner.Project, "list_projects", AsyncMock()) as list_projects,
        pytest.raises(AristotleAPIError) as exc_info,
    ):
        await _submit(git_repo, tmp_path)

    assert exc_info.value is per_second
    create.assert_awaited_once()
    list_projects.assert_not_awaited()


async def test_a_refusal_with_nothing_listed_retries_on_a_growing_backoff(git_repo, tmp_path, clock, monkeypatch):
    monkeypatch.setattr(runner.random, "uniform", lambda low, high: low)
    uploads: list[float] = []

    async def create(**_kwargs):
        uploads.append(clock[0])
        raise LIMIT_ERROR

    with (
        patch.object(runner.Project, "create", side_effect=create),
        patch.object(runner.Project, "list_projects", AsyncMock(return_value=_in_progress())),
        pytest.raises(runner.SlotTimeoutError, match="ones in another organization count too"),
    ):
        await _submit(git_repo, tmp_path, resubmit_by=3600)

    gaps = [later - earlier for earlier, later in zip(uploads, uploads[1:])]
    assert gaps[:-1] == [30, 60, 120, 240, 480, 600, 600, 600, 600]
    assert uploads[-1] == 3600, "the last try is at resubmit_by"


async def test_a_client_error_while_waiting_ends_the_wait(git_repo, tmp_path, clock):
    forbidden = AristotleAPIError("Forbidden", status_code=403)

    with (
        patch.object(runner.Project, "create", AsyncMock(side_effect=LIMIT_ERROR)),
        patch.object(runner.Project, "list_projects", AsyncMock(side_effect=[_in_progress("a"), forbidden])),
        pytest.raises(AristotleAPIError) as exc_info,
    ):
        await _submit(git_repo, tmp_path)

    assert exc_info.value is forbidden


async def test_a_refusal_past_resubmit_by_fails_without_announcing_a_wait(git_repo, tmp_path, clock, caplog):
    with (
        patch.object(runner.Project, "create", AsyncMock(side_effect=LIMIT_ERROR)) as create,
        patch.object(runner.Project, "list_projects", AsyncMock(return_value=_in_progress("a"))),
        pytest.raises(runner.SlotTimeoutError, match="/projects/a"),
    ):
        await _submit(git_repo, tmp_path, resubmit_by=0)

    create.assert_awaited_once()
    assert "Waiting to submit" not in caplog.text


async def test_a_refusal_past_resubmit_by_is_a_slot_timeout_even_if_listing_fails(git_repo, tmp_path, clock):
    bad_gateway = AristotleAPIError("Bad gateway", status_code=502)

    with (
        patch.object(runner.Project, "create", AsyncMock(side_effect=LIMIT_ERROR)),
        patch.object(runner.Project, "list_projects", AsyncMock(side_effect=bad_gateway)) as list_projects,
        pytest.raises(runner.SlotTimeoutError, match="nothing was verified"),
    ):
        await _submit(git_repo, tmp_path, resubmit_by=0)

    list_projects.assert_awaited_once()
    assert clock[0] == 0


async def test_the_backoff_restarts_after_a_refusal_that_listed_projects(git_repo, tmp_path, clock):
    uploads: list[float] = []

    async def create(**_kwargs):
        uploads.append(clock[0])
        if len(uploads) < 6:
            raise LIMIT_ERROR
        return object()

    listed_a: list[bool] = []

    async def list_projects(**_kwargs):
        # Three refusals list nothing and grow the backoff; the fourth lists "a", which then leaves.
        if len(uploads) == 4 and not listed_a:
            listed_a.append(True)
            return _in_progress("a")
        return _in_progress()

    with (
        patch.object(runner.Project, "create", side_effect=create),
        patch.object(runner.Project, "list_projects", side_effect=list_projects),
    ):
        await _submit(git_repo, tmp_path)

    assert uploads[5] - uploads[4] <= runner.SLOT_RETRY_SECONDS * 1.5 + runner.POLL_INTERVAL_SECONDS * 1.25


async def test_a_transient_error_while_waiting_does_not_end_the_wait(git_repo, tmp_path, clock, caplog):
    created = object()
    listings = [_in_progress("a"), AristotleAPIError("Bad gateway", status_code=502), _in_progress()]

    with (
        patch.object(runner.Project, "create", AsyncMock(side_effect=[LIMIT_ERROR, created])),
        patch.object(runner.Project, "list_projects", AsyncMock(side_effect=listings)),
    ):
        assert await _submit(git_repo, tmp_path) is created

    assert "Could not list your projects in progress, retrying: Bad gateway" in caplog.text


def test_the_prompt_names_the_files_under_verification():
    result = scope.ScopeResult(
        upload_files=[Path("rtl/fifo.v"), Path("README.md")],
        focus_files=[Path("rtl/fifo.v")],
    )

    prompt = runner.build_prompt(result)

    assert "- rtl/fifo.v" in prompt
    assert "README.md" not in prompt
    assert "continuous integration" in prompt


def test_the_prompt_truncates_a_very_large_focus_set():
    focus = [Path(f"rtl/mod{i}.v") for i in range(runner.MAX_FOCUS_FILES_IN_PROMPT + 20)]
    result = scope.ScopeResult(upload_files=focus, focus_files=focus)

    prompt = runner.build_prompt(result)

    assert "...and 20 more files" in prompt
    assert f"The complete list is also in {runner.FOCUS_MANIFEST}" in prompt


def test_extra_instructions_are_appended():
    result = scope.ScopeResult(upload_files=[], focus_files=[Path("a.v")])

    prompt = runner.build_prompt(result, extra_instructions="Ignore the legacy bus.")

    assert prompt.endswith("Ignore the legacy bus.")


def test_the_prompt_names_a_canonical_filelist():
    result = scope.ScopeResult(
        upload_files=[Path("aristotle.f"), Path("rtl/fifo.v")],
        focus_files=[Path("rtl/fifo.v")],
        filelists=[Path("aristotle.f")],
    )

    assert "Use aristotle.f as the commands file." in runner.build_prompt(result)


def test_the_prompt_offers_other_filelists_without_asserting_which_is_right():
    result = scope.ScopeResult(
        upload_files=[Path("top.f"), Path("rtl/fifo.v")],
        focus_files=[Path("rtl/fifo.v")],
        filelists=[Path("top.f"), Path("rtl/core.f")],
    )

    prompt = runner.build_prompt(result)

    assert "appear to be Verilog filelists" in prompt
    assert "- top.f" in prompt
    assert "- rtl/core.f" in prompt
    assert "commands file" in prompt


def test_the_prompt_names_only_the_filelists_the_upload_holds(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / "fifo.v").write_text("module fifo;\nendmodule\n")
    (root / "rtl" / "core.f").write_text("fifo.v\n")
    # aristotle.f is tracked but missing from the checkout, so the upload leaves it out.
    result = scope.ScopeResult(
        upload_files=[Path("aristotle.f"), Path("rtl/core.f"), Path("rtl/fifo.v")],
        focus_files=[Path("rtl/fifo.v")],
        filelists=[Path("aristotle.f"), Path("rtl/core.f")],
    )

    prompt = runner.package(root, result, tmp_path / "out.tar.gz").prompt

    assert "aristotle.f" not in prompt
    assert "- rtl/core.f" in prompt


def test_the_prompt_names_a_blocks_black_boxes_after_its_commands_file(tmp_path: Path):
    (tmp_path / "top.v").write_text("module top; rom_8k r(); endmodule\n")
    (tmp_path / "aristotle.f").write_text("--blackbox rom_8k\ntop.v\n")
    block = bundle.read(tmp_path / "aristotle.f", tmp_path, environ={})
    result = scope.ScopeResult(upload_files=[Path("top.v")], focus_files=[Path("top.v")], block=block)

    assert (
        "\n\nUse aristotle.f as the commands file.\nTreat this module as a black box with unconstrained outputs; its "
        "source isn't in the upload: rom_8k.\n"
    ) in runner.build_prompt(result)


def test_a_block_file_edited_after_it_was_read_stops_packaging_with_one_line(tmp_path: Path):
    """Its include's rewrite no longer fits the file, so nothing goes up and the run is worth retrying."""
    header = tmp_path / "gen" / "types.vh"
    header.parent.mkdir()
    header.write_text("`define W 8\n")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "top.v").write_text(f'`include "{header.as_posix()}"\nmodule top; endmodule\n')
    (root / "aristotle.f").write_text("top.v\n")
    block = bundle.read(root / "aristotle.f", root, environ={})
    (root / "top.v").write_text(f'// edited\n`include "{header.as_posix()}"\nmodule top; endmodule\n')
    result = scope.ScopeResult(
        upload_files=[Path("aristotle.f"), Path("top.v")], focus_files=[Path("top.v")], block=block
    )

    with pytest.raises(runner.RunnerError, match=r"top.v changed while it was being uploaded. Try again.$"):
        runner.package(root, result, tmp_path / "out.tar.gz")


def test_a_block_file_gone_after_it_was_read_stops_packaging_with_one_line(tmp_path: Path):
    header = tmp_path / "gen" / "types.vh"
    header.parent.mkdir()
    header.write_text("`define W 8\n")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "top.v").write_text(f'`include "{header.as_posix()}"\nmodule top; endmodule\n')
    (root / "aristotle.f").write_text("top.v\n")
    block = bundle.read(root / "aristotle.f", root, environ={})
    header.unlink()
    result = scope.ScopeResult(
        upload_files=[Path("aristotle.f"), Path("top.v")], focus_files=[Path("top.v")], block=block
    )

    with pytest.raises(runner.RunnerError, match=r"^Could not archive .*types\.vh: .+\. Try again\.$"):
        runner.package(root, result, tmp_path / "out.tar.gz")


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="POSIX file modes, not root"
)
def test_a_tracked_file_that_cant_be_read_stops_packaging_without_advising_a_retry(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "top.v").write_text("module top; endmodule\n")
    (root / "other.v").write_text("module other; endmodule\n")
    (root / "other.v").chmod(0)
    result = scope.ScopeResult(upload_files=[Path("other.v"), Path("top.v")], focus_files=[Path("top.v")])

    try:
        with pytest.raises(runner.RunnerError, match=r"^Could not archive .*other.v: Permission denied.$"):
            runner.package(root, result, tmp_path / "out.tar.gz")
    finally:
        (root / "other.v").chmod(0o644)


def test_the_prompt_says_nothing_about_filelists_when_there_are_none():
    result = scope.ScopeResult(upload_files=[Path("rtl/fifo.v")], focus_files=[Path("rtl/fifo.v")])

    assert "commands file" not in runner.build_prompt(result)


def test_the_manifest_prompt_states_each_kind_under_its_exact_id():
    manifest = _manifest(
        RequestedProperty(id="FIFO-no_overflow-2", kind=Kind.NATURAL_LANGUAGE, statement="The FIFO never overflows."),
        RequestedProperty(id="handshake", kind=Kind.SVA, statement="u_top.a_handshake"),
        RequestedProperty(id="parse", kind=Kind.C_FUNCTION_NO_UB, statement="parse_header"),
    )

    prompt = runner.build_manifest_prompt(manifest, scope.ScopeResult(upload_files=[], focus_files=[]))

    assert (
        "\n\n- id: FIFO-no_overflow-2\n  Property: The FIFO never overflows.\n"
        "- id: handshake\n  Property: The SystemVerilog assertion u_top.a_handshake holds.\n"
        "- id: parse\n  Property: The C function parse_header has no undefined behavior.\n\n"
    ) in prompt
    assert runner.FOCUS_MANIFEST not in prompt
    assert "Nobody can reply to this run, so don't ask for a reply." in prompt


@pytest.mark.parametrize(
    ("path", "named"),
    [("aristotle.toml", "aristotle.toml"), ("gates/advisory.toml", "gates/advisory.toml")],
    ids=["default", "another manifest"],
)
def test_the_manifest_prompt_says_where_answers_go_by_the_manifest_the_run_reads(tmp_path: Path, path: str, named: str):
    checked = Manifest(
        path=tmp_path / path, properties=[RequestedProperty(id="a", kind=Kind.NATURAL_LANGUAGE, statement="Holds.")]
    )

    prompt = runner.build_manifest_prompt(checked, scope.ScopeResult(upload_files=[], focus_files=[]), tmp_path)

    assert f"say that the answer goes in {named}, in the property's statement" in prompt


def test_the_manifest_prompt_indents_the_continuation_of_a_multi_line_statement():
    multi_line = RequestedProperty(id="fifo", kind=Kind.NATURAL_LANGUAGE, statement="Never overflows.\nNor underflows.")

    prompt = runner.build_manifest_prompt(_manifest(multi_line), scope.ScopeResult(upload_files=[], focus_files=[]))

    assert "- id: fifo\n  Property: Never overflows.\n    Nor underflows.\n\n" in prompt


def test_the_manifest_prompt_sends_a_propertys_assumptions_with_it():
    assumed = RequestedProperty(
        id="ack",
        kind=Kind.SVA,
        statement="top.assert_ack",
        assumptions="assert_en is 1 after reset.\nreq is held until ack.",
    )

    prompt = runner.build_manifest_prompt(_manifest(assumed), scope.ScopeResult(upload_files=[], focus_files=[]))

    assert (
        "- id: ack\n  Property: The SystemVerilog assertion top.assert_ack holds.\n"
        "  Assumptions: assert_en is 1 after reset.\n    req is held until ack.\n\n"
    ) in prompt
    assert "check the property under them" in prompt


def test_the_manifest_prompt_asks_for_each_property_under_its_id_and_allows_suggestions():
    prompt = runner.build_manifest_prompt(_manifest(), scope.ScopeResult(upload_files=[], focus_files=[]))

    assert 'add_spec_declaration as a property with source "user", using its id above' in prompt
    assert "as the declaration_id" in prompt
    assert 'with source "agent" and ids not listed above' in prompt
    assert "reported as suggestions and do not affect the result" in prompt


def test_the_manifest_prompt_names_a_canonical_filelist():
    with_filelist = scope.ScopeResult(
        upload_files=[Path("aristotle.f"), Path("rtl/fifo.v")],
        focus_files=[Path("rtl/fifo.v")],
        filelists=[Path("aristotle.f")],
    )
    without = scope.ScopeResult(upload_files=[Path("rtl/fifo.v")], focus_files=[Path("rtl/fifo.v")])

    assert "Use aristotle.f as the commands file." in runner.build_manifest_prompt(_manifest(), with_filelist)
    assert "commands file" not in runner.build_manifest_prompt(_manifest(), without)


class _FakeTask:
    """A run that sits at one percentage forever."""

    def __init__(self):
        self.status = TaskStatus.IN_PROGRESS
        self.percent_complete = 1

    async def refresh(self):
        pass


class _ClimbingTask(_FakeTask):
    """A run that stays in progress while its percentage climbs at each refresh."""

    async def refresh(self):
        self.percent_complete += 1


STOP_MARKER = (
    "\n\n## Aristotle stopped before finishing\n\nIt needs decisions only you can make, listed in the summary above."
)


class _FakeProject:
    def __init__(self, task):
        self._task = task

    async def get_tasks(self, limit=1):
        return [self._task], None

    def web_url(self) -> str:
        return "https://aristotle.harmonic.fun/projects/fake"


@pytest.mark.asyncio
async def test_a_stalled_run_still_prints_a_heartbeat(monkeypatch, caplog):
    """A build log that goes quiet for an hour gets the job killed by the runner."""
    # A clock the patched sleep drives, so the test neither waits nor depends on wall time.
    clock = [0.0]

    async def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr(runner.asyncio, "sleep", advance)
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])

    with caplog.at_level(logging.INFO, logger="aristotle"):
        final_status = await runner.wait_for_completion(
            _FakeProject(_FakeTask()), timeout_seconds=runner.HEARTBEAT_SECONDS * 3, cancel_on_timeout=False
        )

    assert final_status is None
    beats = [line for line in caplog.messages if line.endswith("] IN_PROGRESS")]
    # One on the first poll, then one per heartbeat window until the budget runs out.
    assert len(beats) == 3, beats


@pytest.mark.asyncio
async def test_an_unchanged_status_is_not_printed_on_every_poll_however_its_percentage_moves(monkeypatch, caplog):
    """Only a change of status, or the heartbeat, prints a line, and no line shows the percentage."""
    clock = [0.0]

    async def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr(runner.asyncio, "sleep", advance)
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])

    with caplog.at_level(logging.INFO, logger="aristotle"):
        await runner.wait_for_completion(
            _FakeProject(_ClimbingTask()), timeout_seconds=runner.HEARTBEAT_SECONDS, cancel_on_timeout=False
        )

    polls = runner.HEARTBEAT_SECONDS / runner.POLL_INTERVAL_SECONDS
    lines = [line for line in caplog.messages if "IN_PROGRESS" in line]
    assert len(lines) < polls
    assert not [line for line in lines if "%" in line]


@pytest.mark.asyncio
async def test_a_failed_task_is_returned_rather_than_raised():
    task = _FakeTask()
    task.status = TaskStatus.FAILED

    assert await runner.wait_for_completion(_FakeProject(task), timeout_seconds=60) == TaskStatus.FAILED


async def _no_wait(_seconds):
    pass


class _FlakyTask(_FakeTask):
    """A run whose status endpoint fails once, then reports it finished."""

    def __init__(self, error):
        super().__init__()
        self._error = error

    async def refresh(self):
        if self._error:
            error, self._error = self._error, None
            raise error
        self.status = TaskStatus.COMPLETE


@pytest.mark.asyncio
async def test_a_transient_poll_error_is_retried_rather_than_failing_the_build(monkeypatch):
    monkeypatch.setattr(runner.asyncio, "sleep", _no_wait)
    task = _FlakyTask(AristotleAPIError("Bad gateway", status_code=502))

    assert await runner.wait_for_completion(_FakeProject(task), timeout_seconds=60) == TaskStatus.COMPLETE


@pytest.mark.asyncio
async def test_a_client_error_while_polling_is_not_retried(monkeypatch):
    monkeypatch.setattr(runner.asyncio, "sleep", _no_wait)
    task = _FlakyTask(AristotleAPIError("Not found", status_code=404))

    with pytest.raises(AristotleAPIError):
        await runner.wait_for_completion(_FakeProject(task), timeout_seconds=60)


def _unavailable(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, json={"detail": "Service unavailable"})


def _running_project() -> Project:
    return Project(
        project_id="2fa30605-173d-4e2b-8cad-849b55641da3",
        status=ProjectStatus.RUNNING,
        created_at=datetime(2026, 10, 1),
        last_updated=datetime(2026, 10, 1),
        description="fifo",
        has_input=True,
        has_files=False,
    )


@pytest.mark.asyncio
async def test_a_fetch_the_request_layer_retries_is_not_retried_again(api_server, monkeypatch):
    monkeypatch.setattr(runner.asyncio, "sleep", _no_wait)
    requests = api_server(*[_unavailable] * api_request.RETRY_ATTEMPTS)

    with pytest.raises(AristotleAPIError, match=f" after {api_request.RETRY_ATTEMPTS} attempts "):
        await runner.fetch_declarations(_running_project())

    assert len(requests) == api_request.RETRY_ATTEMPTS


@pytest.mark.asyncio
async def test_a_fetch_that_fails_once_is_retried_by_the_request_layer(api_server, monkeypatch):
    monkeypatch.setattr(runner.asyncio, "sleep", _no_wait)
    requests = api_server(_unavailable, httpx.Response(200, json={"declarations": [], "pagination_key": None}))

    assert await runner.fetch_declarations(_running_project()) == []
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_finding_the_run_is_not_retried_beyond_the_request_layer(api_server, monkeypatch):
    monkeypatch.setattr(runner.asyncio, "sleep", _no_wait)
    requests = api_server(*[_unavailable] * api_request.RETRY_ATTEMPTS)

    with pytest.raises(AristotleAPIError, match=f" after {api_request.RETRY_ATTEMPTS} attempts "):
        await runner.wait_for_completion(_running_project(), timeout_seconds=60)

    assert len(requests) == api_request.RETRY_ATTEMPTS


class _InterruptedTask(_FakeTask):
    """A run whose wait is interrupted at its first refresh, as a cancelled CI job's is."""

    def __init__(self, interrupt: BaseException):
        super().__init__()
        self._interrupt = interrupt
        self.cancelled = False
        self.refreshes = 0

    async def refresh(self):
        self.refreshes += 1
        if self.refreshes == 1:
            raise self._interrupt

    async def cancel(self):
        self.cancelled = True


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [asyncio.CancelledError(), KeyboardInterrupt()])
async def test_an_interrupted_wait_cancels_the_run(monkeypatch, interrupt):
    monkeypatch.setattr(runner.asyncio, "sleep", _no_wait)
    task = _InterruptedTask(interrupt)

    with pytest.raises(type(interrupt)):
        await runner.wait_for_completion(_FakeProject(task), timeout_seconds=60)

    assert task.cancelled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (None, "it didn't finish within 0.01 s"),
        (RuntimeError("503 Service Unavailable"), "503 Service Unavailable"),
        (ConnectionResetError(), "ConnectionResetError"),
    ],
    ids=["out of time", "an error", "an error with no message"],
)
async def test_a_cancel_on_interrupt_that_fails_says_why_and_how_to_cancel_by_hand(
    monkeypatch, caplog, failure, reason
):
    monkeypatch.setattr(runner.asyncio, "sleep", _no_wait)
    monkeypatch.setattr(runner, "INTERRUPT_CANCEL_SECONDS", 0.01)

    class _Task(_InterruptedTask):
        project_id = "p-1"

        async def cancel(self):
            if failure is None:
                await asyncio.Event().wait()
            raise failure

    with caplog.at_level(logging.INFO, logger="aristotle"), pytest.raises(KeyboardInterrupt):
        await runner.wait_for_completion(_FakeProject(_Task(KeyboardInterrupt())), timeout_seconds=60)

    assert f"Could not cancel the run: {reason}. Cancel it with:\n  aristotle cancel p-1" in caplog.messages


@pytest.mark.asyncio
async def test_a_cancel_after_an_interrupt_while_finding_the_run_that_runs_out_of_time_says_so(monkeypatch, caplog):
    monkeypatch.setattr(runner, "INTERRUPT_CANCEL_SECONDS", 0.01)

    class _Task(_FakeTask):
        project_id = "p-1"

        async def cancel(self):
            await asyncio.Event().wait()

    class _Project(_FakeProject):
        project_id = "p-1"
        lookups = 0

        async def get_tasks(self, limit=1):
            self.lookups += 1
            if self.lookups == 1:
                raise KeyboardInterrupt
            return [self._task], None

        def web_url(self) -> str:
            return "https://aristotle.harmonic.fun/projects/p-1"

    with caplog.at_level(logging.INFO, logger="aristotle"), pytest.raises(KeyboardInterrupt):
        await runner.wait_for_completion(_Project(_Task()), timeout_seconds=60)

    assert (
        "Could not cancel the run: it didn't finish within 0.01 s. Cancel it with:\n  aristotle cancel p-1"
        in caplog.messages
    )


@pytest.mark.asyncio
async def test_a_cancel_when_the_time_budget_runs_out_that_fails_says_why_and_how_to_cancel(monkeypatch, caplog):
    clock = [0.0]

    async def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr(runner.asyncio, "sleep", advance)
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])

    class _Task(_FakeTask):
        project_id = "p-1"

        async def cancel(self):
            raise AristotleAPIError("Too many requests. Wait a few seconds and try again.", 429)

    with caplog.at_level(logging.INFO, logger="aristotle"):
        await runner.wait_for_completion(_FakeProject(_Task()), timeout_seconds=60)

    assert (
        "Could not cancel the run: Too many requests. Wait a few seconds and try again. Cancel it with:\n"
        "  aristotle cancel p-1"
    ) in caplog.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True], ids=["the time budget runs out", "an interrupt"])
async def test_a_cancel_refused_because_the_run_had_finished_says_so_without_advice(monkeypatch, caplog, interrupted):
    clock = [0.0]

    async def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr(runner.asyncio, "sleep", advance)
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])

    class _Task(_InterruptedTask if interrupted else _FakeTask):
        project_id = "p-1"

        async def cancel(self):
            # As AgentTask.cancel does: the refresh after the refusal shows the run had finished.
            self.status = TaskStatus.COMPLETE
            raise AristotleAPIError("Task t-1 has already finished (COMPLETE), so it can't be cancelled.", 404)

    task = _Task(KeyboardInterrupt()) if interrupted else _Task()
    with caplog.at_level(logging.INFO, logger="aristotle"):
        if interrupted:
            with pytest.raises(KeyboardInterrupt):
                await runner.wait_for_completion(_FakeProject(task), timeout_seconds=60)
        else:
            await runner.wait_for_completion(_FakeProject(task), timeout_seconds=60)

    assert "The run had already finished (COMPLETE), so there was nothing to cancel." in caplog.messages
    assert not any("Could not cancel" in message for message in caplog.messages)


RUN_TASK = {
    "project_id": "p-1",
    "agent_task_id": "t-1",
    "status": "IN_PROGRESS",
    "created_at": "2026-10-10T00:00:00",
    "last_updated_at": "2026-10-10T00:00:00",
    "percent_complete": 40,
    "file_name": None,
    "description": None,
    "output_summary": None,
}


def _task_now(status: str) -> httpx.Response:
    return httpx.Response(200, json={**RUN_TASK, "status": status})


@pytest.mark.parametrize(
    ("last_seen", "finished"),
    [("IN_PROGRESS", "COMPLETE"), ("IN_PROGRESS", "OUT_OF_BUDGET"), ("QUEUED", "FAILED")],
    ids=["completed", "out of budget", "queued, then failed"],
)
async def test_a_run_the_cancel_finds_finished_is_reported_as_it_ended(api_server, clock, caplog, last_seen, finished):
    # The last poll is at the deadline; the run ends before the cancel arrives, so Aristotle refuses it.
    requests = api_server(
        _task_now(last_seen),
        _task_now(last_seen),
        httpx.Response(404, json={"detail": "Agent task cannot be canceled"}),
        _task_now(finished),
    )
    task = AgentTask.model_validate({**RUN_TASK, "status": last_seen})

    with caplog.at_level(logging.INFO, logger="aristotle"):
        status = await runner.wait_for_completion(_FakeProject(task), timeout_seconds=30)

    assert status == TaskStatus[finished]
    assert [(r.method, r.url.path) for r in requests] == [
        ("GET", "/api/v3/task/t-1"),
        ("GET", "/api/v3/task/t-1"),
        ("POST", "/api/v3/task/t-1/cancel"),
        ("GET", "/api/v3/task/t-1"),
    ]
    assert caplog.messages[-2:] == [
        f"The run had already finished ({finished}), so there was nothing to cancel.",
        f"Run finished: {finished}",
    ]
    assert not any("Could not cancel" in message for message in caplog.messages)


async def test_a_run_the_cancel_stops_is_still_reported_as_cut_short(api_server, clock):
    api_server(_task_now("IN_PROGRESS"), _task_now("IN_PROGRESS"), _task_now("CANCELED"))

    status = await runner.wait_for_completion(_FakeProject(AgentTask.model_validate(RUN_TASK)), timeout_seconds=30)

    assert status is None


# A rename runs under wait_for, so Ctrl-C reaches it as a cancellation: on 3.11+ directly, on 3.10 at shutdown.
async def test_an_interrupt_while_naming_the_project_cancels_the_run_and_prints_its_url(git_repo, tmp_path, caplog):
    result = scope.ScopeResult(upload_files=[Path("rtl/fifo.v")], focus_files=[Path("rtl/fifo.v")])
    project = _created()
    task = _InterruptedTask(KeyboardInterrupt())

    with (
        patch.object(runner.Project, "create", AsyncMock(return_value=project)),
        patch.object(runner.Project, "rename_by_id", AsyncMock(side_effect=asyncio.CancelledError)),
        patch.object(runner.Project, "get_tasks", AsyncMock(return_value=([task], None))),
        caplog.at_level(logging.INFO, logger="aristotle"),
        pytest.raises(asyncio.CancelledError),
    ):
        await runner.submit(git_repo, result, tmp_path / "out.tar.gz", resubmit_by=NO_DEADLINE, name=NAME)

    assert task.cancelled
    assert f"Interrupted; cancelling the run: {project.web_url()}" in caplog.messages


async def test_an_interrupt_while_naming_the_project_leaves_the_run_with_on_timeout_leave(git_repo, tmp_path, caplog):
    result = scope.ScopeResult(upload_files=[Path("rtl/fifo.v")], focus_files=[Path("rtl/fifo.v")])
    project = _created()
    task = _InterruptedTask(KeyboardInterrupt())

    with (
        patch.object(runner.Project, "create", AsyncMock(return_value=project)),
        patch.object(runner.Project, "rename_by_id", AsyncMock(side_effect=asyncio.CancelledError)),
        patch.object(runner.Project, "get_tasks", AsyncMock(return_value=([task], None))),
        caplog.at_level(logging.INFO, logger="aristotle"),
        pytest.raises(asyncio.CancelledError),
    ):
        await runner.submit(
            git_repo, result, tmp_path / "out.tar.gz", resubmit_by=NO_DEADLINE, name=NAME, cancel_on_interrupt=False
        )

    assert not task.cancelled
    assert f"Interrupted; the run carries on: {project.web_url()}" in caplog.messages


UPLOAD = "repo 0123456789ab"
PROJECT_URL = "https://aristotle.harmonic.fun/projects/p-1"
SLEEP = asyncio.sleep
"""asyncio.sleep before a test patches it, for a server that answers late."""
CANCEL_WAIT = 0.5
"""INTERRUPT_CANCEL_SECONDS for a cancel that answers late; frozen_runner_clock keeps a task lookup before it from
using it up."""


@pytest.fixture
def frozen_runner_clock(monkeypatch) -> None:
    """runner's clock, stopped, while asyncio's timeouts keep real time: finding the run takes none of the
    INTERRUPT_CANCEL_SECONDS that a cancel after an interrupt shares with it, however slow the runner."""
    monkeypatch.setattr(runner, "time", types.SimpleNamespace(monotonic=lambda: 0.0, time=time.time))


def _project_json(latest_run_status: str | None, description: str = UPLOAD) -> dict:
    """Project p-1 as Aristotle lists it, named ``description``, its run in ``latest_run_status``."""
    return {
        "project_id": "p-1",
        "created_at": "2026-10-19T00:00:00",
        "last_updated": "2026-10-19T00:00:00",
        "description": description,
        "status": 1,
        "latest_run_status": latest_run_status,
        "has_input": True,
        "has_files": False,
    }


def _listed(*statuses: str | None) -> httpx.Response:
    """The newest projects: another one, and p-1, uploaded as UPLOAD, for each of ``statuses``."""
    projects = [_project_json(status) for status in statuses]
    return httpx.Response(
        200, json={"projects": [*projects, _project_json("IN_PROGRESS", "repo")], "pagination_key": None}
    )


def _tasks(status: str = "QUEUED") -> httpx.Response:
    """The project's tasks: t-1 alone, in ``status``."""
    return httpx.Response(200, json={"agent_tasks": [{**RUN_TASK, "status": status}], "pagination_key": None})


def _raise(interrupt: BaseException):
    """A server outcome that interrupts the request it answers."""

    def outcome(request: httpx.Request) -> httpx.Response:
        raise interrupt

    return outcome


async def _answer_late(request: httpx.Request) -> httpx.Response:
    """A cancel that Aristotle answers after 95 s, as it does while it stops the run."""
    await SLEEP(95)
    return _task_now("CANCELED")


async def _signalled(signum: int, request: httpx.Request) -> httpx.Response:
    """A request during which ``signum`` arrives, through the handler then installed, and which Aristotle answers after
    95 s."""
    handler = signal.getsignal(signum)
    if not callable(handler) or signal.getsignal(signal.SIGINT) is not signal.getsignal(signal.SIGTERM):
        pytest.fail("No one handler takes a second SIGINT or SIGTERM.")
    handler(signum, None)
    await SLEEP(95)
    return _task_now("CANCELED")


SECOND_SIGNALS = pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])


async def _interrupt_the_create(git_repo: Path, tmp_path: Path, interrupt: type[BaseException], **kwargs) -> None:
    """submit, named NAME and uploaded as UPLOAD, interrupted by ``interrupt``."""
    result = scope.ScopeResult(upload_files=[Path("rtl/fifo.v")], focus_files=[Path("rtl/fifo.v")])
    with patch.object(runner, "new_upload_name", return_value=UPLOAD), pytest.raises(interrupt):
        await runner.submit(git_repo, result, tmp_path / "out.tar.gz", resubmit_by=NO_DEADLINE, name=NAME, **kwargs)


LOOKING = "Interrupted while creating the project; looking for it by the name it was uploaded under, repo 0123456789ab."


@pytest.mark.parametrize("interrupt", [asyncio.CancelledError, KeyboardInterrupt])
async def test_an_interrupt_before_the_create_is_sent_says_no_project_was_created(
    git_repo, tmp_path, api_server, caplog, interrupt
):
    requests = api_server()

    with patch.object(runner.Project, "create", AsyncMock(side_effect=interrupt)):
        await _interrupt_the_create(git_repo, tmp_path, interrupt)

    assert requests == []
    assert caplog.messages[-1] == "Interrupted before the run was submitted: no project was created."


async def test_an_interrupt_while_waiting_for_a_slot_says_no_project_was_created(git_repo, tmp_path, clock, caplog):
    async def refused(**kwargs):
        runner.create_sent.set(True)
        raise LIMIT_ERROR

    with (
        patch.object(runner.Project, "create", AsyncMock(side_effect=refused)),
        patch.object(
            runner.Project, "list_projects", AsyncMock(side_effect=[_in_progress("a"), KeyboardInterrupt])
        ) as listed,
    ):
        await _interrupt_the_create(git_repo, tmp_path, KeyboardInterrupt)

    assert listed.await_count == 2
    assert caplog.messages[-1] == "Interrupted before the run was submitted: no project was created."


@pytest.mark.parametrize("interrupt", [asyncio.CancelledError, KeyboardInterrupt])
async def test_an_interrupt_once_the_create_was_sent_finds_the_project_and_cancels_its_run_once_it_starts(
    git_repo, tmp_path, api_server, clock, caplog, interrupt
):
    requests = api_server(
        _raise(interrupt()), _listed(), _listed("NOT_STARTED"), _listed("QUEUED"), _tasks(), _task_now("CANCELED")
    )

    with caplog.at_level(logging.INFO, logger="aristotle"):
        await _interrupt_the_create(git_repo, tmp_path, interrupt)

    assert [(r.method, r.url.path) for r in requests] == [
        ("POST", "/api/v3/project"),
        *[("GET", "/api/v3/project")] * 3,
        ("GET", "/api/v3/project/p-1/tasks"),
        ("POST", "/api/v3/task/t-1/cancel"),
    ]
    assert caplog.messages[-3:] == [LOOKING, f"Found it; cancelling the run: {PROJECT_URL}", "The run was cancelled."]


async def test_an_interrupted_create_with_on_timeout_leave_names_the_project_without_waiting_for_its_run(
    git_repo, tmp_path, api_server, clock, caplog
):
    requests = api_server(_raise(KeyboardInterrupt()), _listed(), _listed("NOT_STARTED"))

    await _interrupt_the_create(git_repo, tmp_path, KeyboardInterrupt, cancel_on_interrupt=False)

    assert len(requests) == 3
    assert caplog.messages[-2:] == [LOOKING, f"Found it; the run carries on: {PROJECT_URL}"]


async def test_an_interrupted_create_whose_project_does_not_show_up_says_how_to_find_it(
    git_repo, tmp_path, api_server, clock, caplog
):
    lookups = runner.INTERRUPTED_CREATE_SECONDS // runner.INTERRUPTED_CREATE_LOOKUP_SECONDS
    requests = api_server(_raise(KeyboardInterrupt()), *[_listed()] * lookups)

    await _interrupt_the_create(git_repo, tmp_path, KeyboardInterrupt)

    assert len(requests) == 1 + lookups
    assert caplog.messages[-1] == (
        "None of your newest projects is named repo 0123456789ab after 10 s, so Aristotle either did not create it or "
        "has not yet. Look for it in `aristotle list`, and cancel it with `aristotle cancel <id>`."
    )


async def test_an_interrupted_create_whose_run_does_not_start_names_the_project_to_cancel(
    git_repo, tmp_path, api_server, clock, caplog
):
    lookups = runner.INTERRUPTED_CREATE_SECONDS // runner.INTERRUPTED_CREATE_LOOKUP_SECONDS
    api_server(_raise(KeyboardInterrupt()), *[_listed("NOT_STARTED")] * lookups)

    await _interrupt_the_create(git_repo, tmp_path, KeyboardInterrupt)

    assert caplog.messages[-1] == (
        "Found it, but its run hadn't started after 10 s, so it couldn't be cancelled. Once it starts, cancel it "
        "with:\n  aristotle cancel p-1"
    )


async def test_an_interrupted_create_whose_projects_cannot_be_listed_says_so(
    git_repo, tmp_path, api_server, clock, caplog
):
    lookups = runner.INTERRUPTED_CREATE_SECONDS // runner.INTERRUPTED_CREATE_LOOKUP_SECONDS
    refused = httpx.Response(400, json={"detail": "Bad request"})
    api_server(_raise(KeyboardInterrupt()), *[refused] * lookups)

    await _interrupt_the_create(git_repo, tmp_path, KeyboardInterrupt)

    assert caplog.messages[-1] == (
        "Your projects could not be listed to find it: API request failed with status 400: Bad request. Look for it "
        "in `aristotle list`, and cancel it with `aristotle cancel <id>`."
    )


@SECOND_SIGNALS
async def test_a_second_interrupt_ends_the_search_for_an_interrupted_creates_project(
    git_repo, tmp_path, api_server, clock, caplog, signum
):
    before = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    requests = api_server(_raise(KeyboardInterrupt()), _listed(), partial(_signalled, signum))

    await _interrupt_the_create(git_repo, tmp_path, KeyboardInterrupt)

    assert len(requests) == 3
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == before
    assert caplog.messages[-1] == (
        "Interrupted again before the project was found. Look for it in `aristotle list`, and cancel it with "
        "`aristotle cancel <id>`."
    )


async def test_an_interrupted_wait_whose_cancel_answers_late_is_cancelled_when_the_run_reads_so(
    api_server, monkeypatch, caplog
):
    monkeypatch.setattr(runner.asyncio, "sleep", _no_wait)
    monkeypatch.setattr(runner, "INTERRUPT_CANCEL_SECONDS", CANCEL_WAIT)
    requests = api_server(_tasks("IN_PROGRESS"), _raise(KeyboardInterrupt()), _answer_late, _task_now("CANCELED"))

    with caplog.at_level(logging.INFO, logger="aristotle"), pytest.raises(KeyboardInterrupt):
        await runner.wait_for_completion(_running_project(), timeout_seconds=60)

    assert [(r.method, r.url.path) for r in requests] == [
        ("GET", f"/api/v3/project/{_running_project().project_id}/tasks"),
        ("GET", "/api/v3/task/t-1"),
        ("POST", "/api/v3/task/t-1/cancel"),
        ("GET", "/api/v3/task/t-1"),
    ]
    assert caplog.messages[-2:] == ["Interrupted; cancelling the run.", "The run was cancelled."]


@pytest.mark.usefixtures("frozen_runner_clock")
async def test_an_interrupt_while_naming_the_project_whose_cancel_answers_late_is_cancelled_when_the_run_reads_so(
    git_repo, tmp_path, api_server, monkeypatch, caplog
):
    monkeypatch.setattr(runner, "INTERRUPT_CANCEL_SECONDS", CANCEL_WAIT)
    project = _created()
    created = httpx.Response(200, json={**_project_json("QUEUED"), "project_id": project.project_id})
    api_server(created, _raise(asyncio.CancelledError()), _tasks(), _answer_late, _task_now("CANCELED"))

    with caplog.at_level(logging.INFO, logger="aristotle"):
        await _interrupt_the_create(git_repo, tmp_path, asyncio.CancelledError)

    assert caplog.messages[-2:] == [f"Interrupted; cancelling the run: {project.web_url()}", "The run was cancelled."]


@SECOND_SIGNALS
@pytest.mark.usefixtures("frozen_runner_clock")
async def test_a_second_interrupt_while_cancelling_after_an_interrupted_rename_still_checks_the_run(
    git_repo, tmp_path, api_server, monkeypatch, caplog, signum
):
    monkeypatch.setattr(runner, "INTERRUPT_CANCEL_SECONDS", CANCEL_WAIT)
    project = _created()
    created = httpx.Response(200, json={**_project_json("QUEUED"), "project_id": project.project_id})
    api_server(created, _raise(asyncio.CancelledError()), _tasks(), partial(_signalled, signum), _task_now("CANCELED"))

    with caplog.at_level(logging.INFO, logger="aristotle"):
        await _interrupt_the_create(git_repo, tmp_path, asyncio.CancelledError)

    assert caplog.messages[-2:] == [f"Interrupted; cancelling the run: {project.web_url()}", "The run was cancelled."]


@pytest.mark.parametrize(
    ("refreshed", "returned", "said"),
    [
        ("CANCELED", None, "The run was cancelled."),
        ("COMPLETE", TaskStatus.COMPLETE, "The run had already finished (COMPLETE), so there was nothing to cancel."),
        (
            "IN_PROGRESS",
            None,
            f"Could not cancel the run: it didn't finish within {CANCEL_WAIT} s. Cancel it with:\n  aristotle cancel p-1",
        ),
    ],
    ids=["cancelled", "finished first", "still running"],
)
async def test_a_cancel_when_the_time_budget_runs_out_waits_for_its_answer_only_so_long_and_sends_it_once(
    api_server, monkeypatch, caplog, refreshed, returned, said
):
    monkeypatch.setattr(runner, "INTERRUPT_CANCEL_SECONDS", CANCEL_WAIT)
    requests = api_server(_tasks("IN_PROGRESS"), _answer_late, _task_now(refreshed))

    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger="aristotle"):
        assert await runner.wait_for_completion(_running_project(), timeout_seconds=0) == returned

    assert time.monotonic() - started < runner.CANCEL_CHECK_SECONDS
    assert [r.method for r in requests] == ["GET", "POST", "GET"]
    assert said in caplog.messages


@pytest.mark.parametrize(
    ("refreshed", "said"),
    [
        ("CANCELED", "The run was cancelled."),
        (
            "IN_PROGRESS",
            "Could not cancel the run: interrupted again before Aristotle answered. Cancel it with:\n  aristotle cancel p-1",
        ),
    ],
    ids=["reads cancelled", "still running"],
)
@SECOND_SIGNALS
async def test_a_second_interrupt_during_the_cancel_ends_its_wait_and_the_run_is_checked_once(
    api_server, monkeypatch, caplog, refreshed, said, signum
):
    monkeypatch.setattr(runner.asyncio, "sleep", _no_wait)
    monkeypatch.setattr(runner, "INTERRUPT_CANCEL_SECONDS", CANCEL_WAIT)
    requests = api_server(
        _tasks("IN_PROGRESS"), _raise(KeyboardInterrupt()), partial(_signalled, signum), _task_now(refreshed)
    )

    with caplog.at_level(logging.INFO, logger="aristotle"), pytest.raises(KeyboardInterrupt):
        await runner.wait_for_completion(_running_project(), timeout_seconds=60)

    assert [r.method for r in requests] == ["GET", "GET", "POST", "GET"]
    assert caplog.messages[-1] == said


def _ctrl_c(response: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    """A server outcome that answers ``response`` once Ctrl-C has arrived, really, during its request."""

    def outcome(request: httpx.Request) -> httpx.Response:
        signal.raise_signal(signal.SIGINT)
        return response

    return outcome


async def _during(signum: int, request: httpx.Request) -> httpx.Response:
    """A request during which ``signum`` arrives, really, and which Aristotle answers after 95 s."""
    signal.raise_signal(signum)
    await SLEEP(95)
    return _task_now("CANCELED")


def _listed_then_sigterm(request: httpx.Request) -> httpx.Response:
    """A listing without the project, after which SIGTERM arrives, really, before the next."""
    asyncio.get_running_loop().call_later(0.05, signal.raise_signal, signal.SIGTERM)
    return _listed()


INTERRUPTED_AGAIN = (
    "Could not cancel the run: interrupted again before Aristotle answered. Cancel it with:\n  aristotle cancel p-1"
)
NOT_FOUND_AGAIN = (
    "Interrupted again before the project was found. Look for it in `aristotle list`, and cancel it with "
    "`aristotle cancel <id>`."
)


@pytest.mark.skipif(sys.version_info < (3, 11), reason="asyncio.run takes Ctrl-C as a cancel from Python 3.11")
@pytest.mark.parametrize(
    ("create", "cancel", "outcomes", "said"),
    [
        (
            False,
            True,
            [_ctrl_c(_tasks("IN_PROGRESS")), partial(_during, signal.SIGTERM), _task_now("CANCELED")],
            "The run was cancelled.",
        ),
        (
            False,
            True,
            [_ctrl_c(_tasks("IN_PROGRESS")), partial(_during, signal.SIGTERM), partial(_during, signal.SIGTERM)],
            INTERRUPTED_AGAIN,
        ),
        (
            False,
            False,
            [_ctrl_c(_tasks("QUEUED")), partial(_during, signal.SIGTERM)],
            f"Interrupted; the run carries on: {_running_project().web_url()}",
        ),
        (True, True, [partial(_during, signal.SIGINT), partial(_during, signal.SIGTERM)], NOT_FOUND_AGAIN),
        (True, True, [partial(_during, signal.SIGINT), _listed_then_sigterm], NOT_FOUND_AGAIN),
        (
            True,
            True,
            [partial(_during, signal.SIGINT), _listed("QUEUED"), partial(_during, signal.SIGTERM)],
            INTERRUPTED_AGAIN,
        ),
    ],
    ids=[
        "the wait, during the cancel",
        "the wait, during the check after the cancel",
        "the wait left going, during the check it is queued",
        "the create, during a listing",
        "the create, between listings",
        "the create, during the task lookup",
    ],
)
def test_a_second_interrupt_still_ends_the_command_as_ctrl_c_does(
    git_repo, tmp_path, api_server, monkeypatch, caplog, create, cancel, outcomes, said
):
    monkeypatch.setattr(runner, "POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(runner, "INTERRUPT_CANCEL_SECONDS", CANCEL_WAIT)
    monkeypatch.setattr(runner, "INTERRUPTED_CREATE_LOOKUP_SECONDS", CANCEL_WAIT)
    monkeypatch.setattr(runner, "new_upload_name", lambda root: UPLOAD)
    api_server(*outcomes)
    result = scope.ScopeResult(upload_files=[Path("rtl/fifo.v")], focus_files=[Path("rtl/fifo.v")])
    command = (
        runner.submit(git_repo, result, tmp_path / "out.tar.gz", resubmit_by=NO_DEADLINE, cancel_on_interrupt=cancel)
        if create
        else runner.wait_for_completion(_running_project(), timeout_seconds=60, cancel_on_timeout=cancel)
    )
    # SIGTERM as the CLI sets it, so that one no handler takes ends the test, not the process, and SIGINT so that
    # asyncio.run takes Ctrl-C even where the tests started with it ignored.
    previous = [(sig, signal.signal(sig, signal.default_int_handler)) for sig in (signal.SIGINT, signal.SIGTERM)]
    try:
        with caplog.at_level(logging.INFO, logger="aristotle"), pytest.raises(KeyboardInterrupt):
            asyncio.run(command)
    finally:
        for sig, handler in previous:
            signal.signal(sig, handler)

    assert caplog.messages[-1] == said


@pytest.mark.asyncio
async def test_an_interrupt_while_finding_the_run_still_cancels_it(monkeypatch):
    monkeypatch.setattr(runner.asyncio, "sleep", _no_wait)
    task = _InterruptedTask(KeyboardInterrupt())
    lookups: list[int] = []

    class _Project(_FakeProject):
        async def get_tasks(self, limit=1):
            lookups.append(1)
            if len(lookups) == 1:
                raise KeyboardInterrupt
            return [task], None

        def web_url(self) -> str:
            return "https://aristotle.harmonic.fun/projects/p-1"

    with pytest.raises(KeyboardInterrupt):
        await runner.wait_for_completion(_Project(task), timeout_seconds=60)

    assert task.cancelled


@pytest.mark.asyncio
async def test_a_timeout_cancel_whose_reply_is_lost_is_not_reported_as_failed(api_server, monkeypatch, caplog):
    monkeypatch.setattr(runner.asyncio, "sleep", _no_wait)
    task = {
        "project_id": "00000000-0000-0000-0000-000000000001",
        "agent_task_id": "task-1",
        "status": "IN_PROGRESS",
        "created_at": "2026-10-01T00:00:00",
        "last_updated_at": "2026-10-01T00:05:00",
        "percent_complete": 20,
        "file_name": None,
        "description": None,
        "output_summary": None,
    }
    requests = api_server(
        httpx.Response(200, json={"agent_tasks": [task], "pagination_key": None}),
        httpx.ReadError(""),
        httpx.Response(404, json={"detail": "Agent task cannot be canceled"}),
        httpx.Response(200, json={**task, "status": "CANCELED"}),
    )

    with caplog.at_level(logging.INFO, logger="aristotle"):
        assert await runner.wait_for_completion(_created(), timeout_seconds=0) is None

    assert [request.method for request in requests] == ["GET", "POST", "POST", "GET"]
    assert not [message for message in caplog.messages if "Could not cancel" in message]


@pytest.mark.asyncio
async def test_an_interrupted_wait_leaves_the_run_with_on_timeout_leave(monkeypatch):
    monkeypatch.setattr(runner.asyncio, "sleep", _no_wait)
    task = _InterruptedTask(KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        await runner.wait_for_completion(_FakeProject(task), timeout_seconds=60, cancel_on_timeout=False)

    assert not task.cancelled


@pytest.mark.asyncio
async def test_an_interrupted_wait_left_running_says_the_run_carries_on(monkeypatch, caplog):
    monkeypatch.setattr(runner.asyncio, "sleep", _no_wait)
    project = _FakeProject(_InterruptedTask(KeyboardInterrupt()))

    with caplog.at_level(logging.INFO, logger="aristotle"), pytest.raises(KeyboardInterrupt):
        await runner.wait_for_completion(project, timeout_seconds=60, cancel_on_timeout=False)

    assert caplog.messages[-1] == f"Interrupted; the run carries on: {project.web_url()}"


class _QueuedTask(_FakeTask):
    """A run that never leaves the queue."""

    def __init__(self):
        super().__init__()
        self.status = TaskStatus.QUEUED
        self.percent_complete = None
        self.cancelled = False

    async def cancel(self):
        self.cancelled = True


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_on_timeout", [True, False])
async def test_a_budget_that_runs_out_before_the_run_starts_returns_queued(monkeypatch, caplog, cancel_on_timeout):
    clock = [0.0]

    async def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr(runner.asyncio, "sleep", advance)
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    task = _QueuedTask()

    with caplog.at_level(logging.INFO, logger="aristotle"):
        final_status = await runner.wait_for_completion(
            _FakeProject(task), timeout_seconds=60, cancel_on_timeout=cancel_on_timeout
        )

    assert final_status == TaskStatus.QUEUED
    assert task.cancelled is cancel_on_timeout
    assert "The time budget ran out before the run started: it was still queued." in caplog.messages


class _InterruptedQueuedTask(_InterruptedTask):
    """A queued run whose wait is interrupted on its first poll; the refreshes after that report ``after``."""

    def __init__(self, after: TaskStatus | Exception):
        super().__init__(KeyboardInterrupt())
        self.status = TaskStatus.QUEUED
        self._after = after
        self._refreshes = 0

    async def refresh(self):
        self._refreshes += 1
        if self._refreshes == 1:
            raise self._interrupt
        if isinstance(self._after, Exception):
            raise self._after
        self.status = self._after


@pytest.mark.asyncio
async def test_an_interrupted_wait_on_a_queued_run_cancels_it_at_once_without_a_refresh(monkeypatch, caplog):
    monkeypatch.setattr(runner.asyncio, "sleep", _no_wait)
    task = _InterruptedQueuedTask(TaskStatus.QUEUED)

    with caplog.at_level(logging.INFO, logger="aristotle"), pytest.raises(KeyboardInterrupt):
        await runner.wait_for_completion(_FakeProject(task), timeout_seconds=60)

    assert task.cancelled
    assert task._refreshes == 1
    assert caplog.messages == ["Interrupted; cancelling the run.", "The run was cancelled."]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("after", "never_started"),
    [
        (TaskStatus.QUEUED, True),
        (TaskStatus.IN_PROGRESS, False),
        (AristotleAPIError("unavailable", status_code=503), False),
    ],
    ids=["confirmed queued", "started since the last poll", "refresh fails"],
)
async def test_an_interrupted_wait_left_running_says_it_never_started_only_when_a_refresh_confirms_it(
    monkeypatch, caplog, after, never_started
):
    monkeypatch.setattr(runner.asyncio, "sleep", _no_wait)
    task = _InterruptedQueuedTask(after)

    with caplog.at_level(logging.INFO, logger="aristotle"), pytest.raises(KeyboardInterrupt):
        await runner.wait_for_completion(_FakeProject(task), timeout_seconds=60, cancel_on_timeout=False)

    assert not task.cancelled
    assert (
        "Interrupted before the run started: it is still queued, and will start later." in caplog.messages
    ) is never_started


class _QueuedTaskWhoseRefreshFails(_QueuedTask):
    """A run last seen queued, whose every later refresh fails transiently."""

    async def refresh(self):
        raise AristotleAPIError("unavailable", status_code=503)


@pytest.mark.asyncio
async def test_a_budget_that_runs_out_after_a_failed_refresh_does_not_claim_the_run_never_started(monkeypatch, caplog):
    clock = [0.0]

    async def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr(runner.asyncio, "sleep", advance)
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    task = _QueuedTaskWhoseRefreshFails()

    with caplog.at_level(logging.INFO, logger="aristotle"):
        final_status = await runner.wait_for_completion(_FakeProject(task), timeout_seconds=60)

    assert final_status is None
    assert "The time budget ran out before the run finished." in caplog.messages
    assert task.cancelled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "summary", "expected"),
    [
        (
            TaskStatus.COMPLETE_WITH_ERRORS,
            "Proved the read property.\n\nOpen questions:\n1. Which reset polarity?" + STOP_MARKER,
            "Proved the read property.\n\nOpen questions:\n1. Which reset polarity?",
        ),
        (TaskStatus.COMPLETE_WITH_ERRORS, STOP_MARKER.lstrip("\n"), "Aristotle gave no summary of the run."),
        (TaskStatus.COMPLETE_WITH_ERRORS, "Proved the read property.", None),
        (TaskStatus.COMPLETE_WITH_ERRORS, None, None),
        (TaskStatus.COMPLETE, "Done." + STOP_MARKER, None),
        (
            TaskStatus.COMPLETE_WITH_ERRORS,
            "A summary that mentions ## Aristotle stopped before finishing mid-line.",
            None,
        ),
    ],
    ids=["summary-before-the-marker", "marker-only", "no-marker", "no-summary", "complete", "not-a-heading"],
)
async def test_the_stopped_summary_is_the_summary_before_aristotles_marker(
    status: TaskStatus, summary: str | None, expected: str | None
):
    task = types.SimpleNamespace(status=status, output_summary=summary)

    assert await runner.fetch_stopped_summary(_FakeProject(task)) == expected


def test_a_name_that_is_not_ascii_goes_up_under_its_utf8_name(git_repo: Path, tmp_path: Path):
    (git_repo / "rtl" / "naïve.v").write_text("module naive;\nendmodule\n")
    latin1_name(git_repo / "rtl")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "two names that aren't ASCII")
    result = scope.resolve(git_repo, scope.Scope.CHANGED)
    destination = tmp_path / "out.tar.gz"

    upload = runner.build_tarball(git_repo, result.upload_files, destination, result.focus_files)

    assert Path("rtl/naïve.v") in upload.uploaded
    with tarfile.open(destination, "r:gz") as tar:
        names = tar.getnames()
        focus = tar.extractfile(runner.FOCUS_MANIFEST)
        assert focus is not None and focus.read() == "rtl/naïve.v\n".encode()
    assert "rtl/naïve.v" in names and not any("caf" in n for n in names)
    assert "- rtl/naïve.v\n" in runner.build_prompt(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("summary", "expected"),
    [("\nFails at cycle 12.\n\n", "Fails at cycle 12."), ("  \n", None), (None, None)],
    ids=["summary", "blank", "none"],
)
async def test_the_summary_is_the_tasks_output_summary_stripped(summary: str | None, expected: str | None):
    task = types.SimpleNamespace(status=TaskStatus.COMPLETE, output_summary=summary)

    assert await runner.fetch_summary(_FakeProject(task)) == expected


HARDWARE = "/api/v3/user/has-hardware-verification-access"
SOFTWARE = "/api/v3/user/has-software-verification-access"


async def test_language_access_asks_aristotle_about_verilog_and_c_and_not_about_rust(api_server):
    access = {HARDWARE: True, SOFTWARE: False}

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"has_access": access[request.url.path]})

    requests = api_server(answer, answer)

    assert await runner.language_access(["C", "Rust", "Verilog"]) == {"C": False, "Rust": None, "Verilog": True}
    assert sorted(r.url.path for r in requests) == [HARDWARE, SOFTWARE]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, json={"detail": "Forbidden"}),
        httpx.Response(200, json={"has_access": "yes"}),
        httpx.Response(200, json=[True]),
        httpx.Response(200, content=b"<html>", headers={"Content-Type": "application/json"}),
    ],
    ids=["refused", "not a boolean", "not an object", "not json"],
)
async def test_a_language_aristotle_gives_no_clear_answer_about_is_unknown(api_server, response: httpx.Response):
    requests = api_server(response)

    assert await runner.language_access(["Verilog"]) == {"Verilog": None}
    assert len(requests) == 1


async def test_every_language_is_unknown_when_aristotle_answers_too_slowly(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runner, "LANGUAGE_CHECK_SECONDS", 0.05)

    async def stall(*_args, **_kwargs):
        await asyncio.sleep(1)

    with patch.object(runner.AristotleRequestClient, "get", side_effect=stall):
        assert await runner.language_access(["C", "Verilog"]) == {"C": None, "Verilog": None}


async def test_a_runs_prompt_is_its_first_message():
    task = Mock(spec=AgentTask)
    events = [
        Mock(event_type=EventType.RUNNING_COMMAND, content="ls"),
        Mock(event_type=EventType.MESSAGE, content="Check each of the following properties."),
        Mock(event_type=EventType.MESSAGE, content="A follow-up message."),
    ]
    task.get_events = AsyncMock(return_value=(events, None))

    assert await runner.fetch_prompt(task) == "Check each of the following properties."
    task.get_events.assert_awaited_once_with(limit=5, newest_first=False)


async def test_a_run_with_no_message_has_no_prompt():
    task = Mock(spec=AgentTask)
    task.get_events = AsyncMock(return_value=([Mock(event_type=EventType.RUNNING_COMMAND, content="ls")], None))

    assert await runner.fetch_prompt(task) is None


def _block_repo(tmp_path: Path) -> Path:
    """A repository whose aristotle.f names a tracked file an ignore rule matches, and whose top includes a tracked
    header by absolute path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_bytes(b"gen/\n")
    (repo / "inc").mkdir()
    (repo / "inc" / "defs.vh").write_bytes(b"`define W 8\n")
    (repo / "top.v").write_bytes(
        f'`include "{(repo / "inc" / "defs.vh").as_posix()}"\nmodule top; endmodule\n'.encode()
    )
    (repo / "gen").mkdir()
    (repo / "gen" / "regs.v").write_bytes(b"module regs; endmodule\n")
    (repo / "aristotle.f").write_bytes(b"-sv\ntop.v\ngen/regs.v\n")
    git(repo, "add", "-A")
    git(repo, "add", "-f", "gen/regs.v")
    git(repo, "commit", "-qm", "initial")
    return repo


def test_build_tarball_adds_the_blocks_files_and_its_generated_filelist(tmp_path: Path, caplog):
    repo = _block_repo(tmp_path)
    block = bundle.read(repo / "aristotle.f", repo, environ={})
    result = scope.resolve_checkout(repo)
    destination = tmp_path / "upload.tar.gz"

    with caplog.at_level(logging.INFO, logger="aristotle"):
        upload = runner.build_tarball(repo, result.upload_files, destination, None, block)

    with tarfile.open(destination) as tar:
        names = sorted(tar.getnames())
        top = tar.extractfile("top.v")
        generated = tar.extractfile("aristotle.f")
        assert top is not None and generated is not None
        top_text, generated_text = top.read().decode(), generated.read().decode()
    assert names == [".gitignore", "aristotle.f", "gen/regs.v", "inc/defs.vh", "top.v"]
    assert top_text.startswith('`include "inc/defs.vh"\n')
    assert generated_text.splitlines()[-2:] == ["top.v", "gen/regs.v"]
    assert len(upload.uploaded) == 5
    assert (
        "Uploading 1 tracked file the checkout's upload leaves out, held back by an ignore rule or reached through a "
        "linked folder, since the Verilog block needs it: gen/regs.v"
    ) in caplog.messages


def test_build_tarball_replaces_a_root_filelist_spelled_in_any_case(tmp_path: Path):
    """A root Aristotle.f is the block's filelist, as ci finds it, and the generated one replaces it, once."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "top.v").write_bytes(b"module top; endmodule\n")
    (repo / "Aristotle.f").write_bytes(b"-sv\ntop.v\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "initial")
    block = bundle.read(repo / "Aristotle.f", repo, environ={})
    destination = tmp_path / "upload.tar.gz"

    runner.build_tarball(repo, scope.resolve_checkout(repo).upload_files, destination, None, block)

    with tarfile.open(destination) as tar:
        names = sorted(tar.getnames())
        generated = tar.extractfile("Aristotle.f")
        assert generated is not None
        assert generated.read().decode().startswith("# Generated by the aristotle CLI from Aristotle.f")
    assert names == ["Aristotle.f", "top.v"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_build_tarball_uploads_a_committed_filelist_that_links_to_a_held_back_file(tmp_path: Path):
    """A root aristotle.f that goes up as committed is a block file, so its link's target needn't be in the upload."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_bytes(b"block.f\n")
    (repo / "top.v").write_bytes(b"module top; endmodule\n")
    (repo / "block.f").write_bytes(b"top.v\n")
    (repo / "aristotle.f").symlink_to("block.f")
    git(repo, "add", "-A")
    git(repo, "add", "-f", "block.f")
    git(repo, "commit", "-qm", "initial")
    block = bundle.read(repo / "aristotle.f", repo, environ={})
    assert block.filelist_text is None
    destination = tmp_path / "upload.tar.gz"

    upload = runner.build_tarball(repo, scope.resolve_checkout(repo).upload_files, destination, None, block)

    with tarfile.open(destination) as tar:
        committed = tar.extractfile("aristotle.f")
        assert committed is not None
        assert committed.read() == b"top.v\n"
    assert Path("aristotle.f") in upload.uploaded
