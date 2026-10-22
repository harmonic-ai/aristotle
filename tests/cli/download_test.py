"""Tests for aristotlelib.cli.download module."""

import argparse
import errno
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.command.download import DownloadAction
from aristotlelib.cli.command.submit import SubmitAction
from aristotlelib.project import Project, ProjectHasNoFilesError, ProjectStatus
from tests.ci.conftest import git


async def test_download_action_basic():
    """Test downloading project files."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = DownloadAction(subparsers)
    action.add_action_arguments()

    args = action.parser.parse_args(["project-123"])

    mock_project = Mock(spec=Project)
    mock_project.project_id = "project-123"
    mock_project.get_files = AsyncMock(return_value=Path("/path/to/result.tar.gz"))

    with patch("aristotlelib.cli.command.download.Project.from_id", return_value=mock_project) as mock_from_id:
        await action.run_action(args)

    # Verify Project.from_id was called with correct ID
    mock_from_id.assert_called_once_with("project-123")

    # Verify get_files was called with no destination
    mock_project.get_files.assert_called_once_with(destination=None)


async def test_download_action_with_destination(tmp_path):
    """Test downloading project files with --destination flag."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = DownloadAction(subparsers)
    action.add_action_arguments()

    destination = str(tmp_path / "custom" / "path" / "output.tar.gz")
    args = action.parser.parse_args(["project-789", "--destination", destination])

    mock_project = Mock(spec=Project)
    mock_project.project_id = "project-789"
    mock_project.get_files = AsyncMock(return_value=Path(destination))

    with patch("aristotlelib.cli.command.download.Project.from_id", return_value=mock_project) as mock_from_id:
        await action.run_action(args)

    # Verify destination was passed
    mock_from_id.assert_called_once_with("project-789")
    mock_project.get_files.assert_called_once_with(destination=destination)


async def test_download_action_loads_project_from_id():
    """Test that download action loads project from the provided ID."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = DownloadAction(subparsers)
    action.add_action_arguments()

    project_id = "specific-project-id-123"
    args = action.parser.parse_args([project_id])

    mock_project = Mock(spec=Project)
    mock_project.project_id = project_id
    mock_project.get_files = AsyncMock(return_value=Path("result.tar.gz"))

    with patch("aristotlelib.cli.command.download.Project.from_id", return_value=mock_project) as mock_from_id:
        await action.run_action(args)

    # Verify Project.from_id was called with correct ID
    mock_from_id.assert_called_once_with(project_id)


async def test_download_json_prints_where_the_archive_went(capsys):
    parser = argparse.ArgumentParser()
    action = DownloadAction(parser.add_subparsers())
    action.add_action_arguments()
    mock_project = Mock(spec=Project)
    mock_project.project_id = "project-123"
    mock_project.get_files = AsyncMock(return_value=Path("FIFO overflow_aristotle.tar.gz"))

    with patch("aristotlelib.cli.command.download.Project.from_id", return_value=mock_project):
        await action.run_action(action.parser.parse_args(["project-123", "--json"]))

    assert json.loads(capsys.readouterr().out) == {
        "project_id": "project-123",
        "result_path": "FIFO overflow_aristotle.tar.gz",
    }


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
async def test_download_says_where_it_saved_the_archive(json_flag, capsys, caplog):
    caplog.set_level(logging.INFO)
    parser = argparse.ArgumentParser()
    action = DownloadAction(parser.add_subparsers())
    action.add_action_arguments()
    mock_project = Mock(spec=Project)
    mock_project.project_id = "project-123"
    mock_project.get_files = AsyncMock(return_value=Path("FIFO overflow_aristotle.tar.gz"))

    with patch("aristotlelib.cli.command.download.Project.from_id", return_value=mock_project):
        assert await action.run_action(action.parser.parse_args(["project-123", *json_flag])) is None

    assert caplog.messages == ["Saved the project's files to FIFO overflow_aristotle.tar.gz"]
    out = capsys.readouterr().out
    if json_flag:
        assert json.loads(out)["result_path"] == "FIFO overflow_aristotle.tar.gz"
    else:
        assert out == ""


def _project_whose_download_fails(error: Exception) -> Mock:
    mock_project = Mock(spec=Project)
    mock_project.project_id = "project-123"
    mock_project.get_files = AsyncMock(side_effect=error)
    return mock_project


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
async def test_download_of_a_project_without_files_says_so_and_exits_4(json_flag, capsys, caplog):
    parser = argparse.ArgumentParser()
    action = DownloadAction(parser.add_subparsers())
    action.add_action_arguments()
    error = ProjectHasNoFilesError("Project project-123 has no files to download yet.")

    with patch("aristotlelib.cli.command.download.Project.from_id", return_value=_project_whose_download_fails(error)):
        code = await action.run_action(action.parser.parse_args(["project-123", *json_flag]))

    assert code == 4
    assert capsys.readouterr().out == ""
    assert "Project project-123 has no files to download yet." in caplog.text


async def test_download_leaves_other_api_errors_to_the_generic_handler():
    parser = argparse.ArgumentParser()
    action = DownloadAction(parser.add_subparsers())
    action.add_action_arguments()
    error = AristotleAPIError("Request failed: timed out")

    with (
        patch("aristotlelib.cli.command.download.Project.from_id", return_value=_project_whose_download_fails(error)),
        pytest.raises(AristotleAPIError, match="timed out"),
    ):
        await action.run_action(action.parser.parse_args(["project-123"]))


PROJECT_ID = "2fa30605-173d-4e2b-8cad-849b55641da3"


def _project_with_files() -> Project:
    return Project(
        project_id=PROJECT_ID,
        status=ProjectStatus.IDLE,
        created_at=datetime(2026, 9, 1, 12, 0),
        last_updated=datetime(2026, 9, 1, 13, 0),
        description=None,
        has_input=True,
        has_files=True,
    )


def _archive_client() -> AsyncMock:
    archive = Mock()
    archive.headers = {"content-disposition": f'attachment; filename="{PROJECT_ID}-aristotle.tar.gz"'}
    archive.content = b"archive"
    client = AsyncMock()
    client.get = AsyncMock(return_value=archive)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.mark.parametrize(
    ("destination", "saved"),
    [
        (".", f"{PROJECT_ID}-aristotle.tar.gz"),
        ("new/dir/result.tar.gz", "new/dir/result.tar.gz"),
        ("out/", f"out/{PROJECT_ID}-aristotle.tar.gz"),
        ("existing.tar.gz", "existing.tar.gz"),
    ],
)
async def test_download_saves_into_directories_and_makes_missing_ones(
    destination: str, saved: str, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "existing.tar.gz").write_bytes(b"old")
    parser = argparse.ArgumentParser()
    action = DownloadAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.download.Project.from_id", return_value=_project_with_files()),
        patch.object(Project, "refresh", AsyncMock()),
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=_archive_client()),
    ):
        argv = [PROJECT_ID, "--json", "--destination", destination]
        assert await action.run_action(action.parser.parse_args(argv)) is None

    assert (tmp_path / saved).read_bytes() == b"archive"
    assert Path(json.loads(capsys.readouterr().out)["result_path"]) == Path(saved)


@pytest.mark.parametrize("destination", ["notes.txt/result.tar.gz", "notes.txt/"])
async def test_download_to_a_path_through_a_file_exits_2_before_any_request(destination, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_text("notes")
    parser = argparse.ArgumentParser()
    action = DownloadAction(parser.add_subparsers())
    action.add_action_arguments()

    with (
        patch("aristotlelib.cli.command.download.Project.resolve_id") as resolve_id,
        pytest.raises(SystemExit) as exited,
    ):
        action.preflight(action.parser.parse_args([PROJECT_ID, "--destination", destination]))

    assert exited.value.code == 2
    resolve_id.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"Cannot save the archive to {destination}: notes.txt is not a directory" in captured.err
    assert (tmp_path / "notes.txt").read_text() == "notes"


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
async def test_download_that_cannot_save_says_why_and_exits_3(json_flag, capsys, caplog):
    parser = argparse.ArgumentParser()
    action = DownloadAction(parser.add_subparsers())
    action.add_action_arguments()
    error = OSError(errno.ENOSPC, "No space left on device", "result.tar.gz")

    with patch("aristotlelib.cli.command.download.Project.from_id", return_value=_project_whose_download_fails(error)):
        code = await action.run_action(action.parser.parse_args(["project-123", *json_flag]))

    assert code == 3
    assert capsys.readouterr().out == ""
    assert caplog.messages == [
        "The project's files were not saved: [Errno 28] No space left on device: 'result.tar.gz'"
    ]


@pytest.mark.parametrize("action_class", [DownloadAction, SubmitAction])
def test_a_destination_starting_with_a_tilde_is_the_home_directory(action_class, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    action = action_class(argparse.ArgumentParser().add_subparsers())
    action.add_arguments()

    args = action.parser.parse_args(["x", "--destination=~/results/"])

    assert args.destination == f"{tmp_path}/results/"


def _project_record() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "project_id": PROJECT_ID,
            "status": 2,
            "created_at": "2026-10-12T19:00:00",
            "last_updated": "2026-10-12T20:00:00",
            "name": "counter6 properties",
            "has_input": True,
            "has_files": True,
        },
    )


def _file(content: bytes) -> httpx.Response:
    return httpx.Response(200, content=content, headers={"content-type": "application/octet-stream"})


def _no_such_file(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"detail": f"File {request.url.params['file_path']} not found"})


def _run(argv: list[str]) -> int:
    """The command as the CLI runs it: preflight, then the action; SystemExit's code for a usage error."""
    action = DownloadAction(argparse.ArgumentParser().add_subparsers())
    action.add_arguments()
    try:
        # The top-level parser gives every command its --api-key.
        return action.run(action.parser.parse_args(argv, argparse.Namespace(api_key=None)))
    except SystemExit as e:
        assert isinstance(e.code, int)
        return e.code


def test_an_artifact_is_saved_at_its_path_under_the_destination(api_server, tmp_path, capsys, caplog):
    caplog.set_level(logging.INFO)
    requests = api_server(_project_record(), _file(b"$timescale 1ns $end"))
    destination = tmp_path / "artifacts"

    code = _run([PROJECT_ID, "--artifact", "sim/never_at_max_cex.vcd", "--destination", str(destination)])

    assert code == 0
    saved = destination / "sim" / "never_at_max_cex.vcd"
    assert saved.read_bytes() == b"$timescale 1ns $end"
    assert [(r.url.path, dict(r.url.params)) for r in requests] == [
        (f"/api/v3/project/{PROJECT_ID}", {}),
        (f"/api/v3/project/{PROJECT_ID}/file/download", {"file_path": "sim/never_at_max_cex.vcd"}),
    ]
    assert [r.getMessage() for r in caplog.records if r.name != "httpx"] == [
        f"Saved sim/never_at_max_cex.vcd to {saved}"
    ]
    assert capsys.readouterr().out == ""


def test_several_artifacts_are_saved_at_their_paths_under_aristotle_artifacts_by_default(
    api_server, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_text("the user's own")
    api_server(_project_record(), _file(b"vcd"), _file(b"tb"), _file(b"readme"))

    argv = [
        PROJECT_ID,
        "--json",
        "--artifact",
        "sim/cex.vcd",
        "--artifact",
        "sim/tb/cex_tb.sv",
        "--artifact",
        "README.md",
    ]
    assert _run(argv) == 0

    folder = tmp_path / "aristotle-artifacts"
    assert (folder / "sim" / "cex.vcd").read_bytes() == b"vcd"
    assert (folder / "sim" / "tb" / "cex_tb.sv").read_bytes() == b"tb"
    assert (folder / "README.md").read_bytes() == b"readme"
    assert (tmp_path / "README.md").read_text() == "the user's own"
    assert json.loads(capsys.readouterr().out) == {
        "project_id": PROJECT_ID,
        "files": [
            {"artifact_path": "sim/cex.vcd", "saved_path": str(Path("aristotle-artifacts", "sim", "cex.vcd"))},
            {
                "artifact_path": "sim/tb/cex_tb.sv",
                "saved_path": str(Path("aristotle-artifacts", "sim", "tb", "cex_tb.sv")),
            },
            {"artifact_path": "README.md", "saved_path": str(Path("aristotle-artifacts", "README.md"))},
        ],
        "missing": [],
    }


def test_an_artifact_named_twice_is_downloaded_once(api_server, tmp_path, capsys):
    requests = api_server(_project_record(), _file(b"vcd"))

    argv = [
        PROJECT_ID,
        "--json",
        "--artifact",
        "sim/cex.vcd",
        "--artifact",
        "sim/cex.vcd",
        "--destination",
        str(tmp_path),
    ]
    assert _run(argv) == 0

    assert len(requests) == 2
    assert [f["artifact_path"] for f in json.loads(capsys.readouterr().out)["files"]] == ["sim/cex.vcd"]


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_a_path_the_project_has_no_file_at_is_named_the_rest_are_saved_and_it_exits_4(
    json_flag, api_server, tmp_path, capsys, caplog
):
    api_server(_project_record(), _file(b"vcd"), _no_such_file, _file(b"log"))

    argv = ["--artifact", "sim/cex.vcd", "--artifact", "sim/gone.vcd", "--artifact", "sim/cex.log"]
    code = _run([PROJECT_ID, *argv, "--destination", str(tmp_path), *json_flag])

    assert code == 4
    assert sorted(p.name for p in (tmp_path / "sim").iterdir()) == ["cex.log", "cex.vcd"]
    assert f"Project {PROJECT_ID} has no file sim/gone.vcd." in caplog.messages
    out = capsys.readouterr().out
    if json_flag:
        printed = json.loads(out)
        assert [f["artifact_path"] for f in printed["files"]] == ["sim/cex.vcd", "sim/cex.log"]
        assert printed["missing"] == ["sim/gone.vcd"]
    else:
        assert out == ""


@pytest.mark.parametrize(
    ("artifact", "error"),
    [
        ("", "an empty path names no file"),
        ("sim/\0cex.vcd", "a path in the project can't hold a NUL"),
        ("/etc/passwd", "a path in the project is relative, so it can't start with /"),
        ("C:cex.vcd", "a path in the project can't start with a drive"),
        ("C:\\cex.vcd", "a path in the project uses /, not a backslash"),
        ("\\\\host\\share\\cex.vcd", "a path in the project uses /, not a backslash"),
        ("sim\\cex.vcd", "a path in the project uses /, not a backslash"),
        ("../../etc/passwd", "a path in the project can't have a .. segment"),
        ("sim/../../cex.vcd", "a path in the project can't have a .. segment"),
        ("./cex.vcd", "a path in the project can't have a . segment"),
        (".git/hooks/pre-commit", "a path in the project can't name a .git folder"),
        ("sim/.GIT/config", "a path in the project can't name a .git folder"),
        (
            "sim/HEAD",
            "a path in the project can't name a file HEAD: git takes a folder that holds one for a repository",
        ),
        ("sim//cex.vcd", "a path in the project can't have an empty segment, as // or a / at its end leaves"),
        ("sim/", "a path in the project can't have an empty segment, as // or a / at its end leaves"),
    ],
)
def test_a_path_that_could_leave_the_destination_exits_2_before_any_request(
    artifact, error, api_server, tmp_path, capsys
):
    requests = api_server()

    code = _run([PROJECT_ID, "--artifact", "sim/fine.vcd", "--artifact", artifact, "--destination", str(tmp_path)])

    assert code == 2
    assert requests == []
    assert f"error: --artifact '{artifact}': {error}" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("link", ["sim", "sim/cex.vcd"], ids=["a linked folder", "a linked file"])
def test_a_link_in_the_destination_that_leads_out_of_it_exits_2_before_any_request(link, api_server, tmp_path, capsys):
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "cex.vcd").write_bytes(b"theirs")
    (tmp_path / "artifacts").mkdir()
    if link == "sim":
        (tmp_path / "artifacts" / "sim").symlink_to(tmp_path / "outside")
    else:
        (tmp_path / "artifacts" / "sim").mkdir()
        (tmp_path / "artifacts" / "sim" / "cex.vcd").symlink_to(tmp_path / "outside" / "cex.vcd")
    requests = api_server()

    code = _run([PROJECT_ID, "--artifact", "sim/cex.vcd", "--destination", str(tmp_path / "artifacts")])

    assert code == 2
    assert requests == []
    target = tmp_path / "artifacts" / "sim" / "cex.vcd"
    assert (
        f"error: --artifact 'sim/cex.vcd': {target} is outside {tmp_path / 'artifacts'} once links are followed"
        in capsys.readouterr().err
    )
    assert (tmp_path / "outside" / "cex.vcd").read_bytes() == b"theirs"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_link_that_appears_while_a_file_downloads_stops_the_command_before_its_write(api_server, tmp_path, caplog):
    (tmp_path / "outside").mkdir()
    destination = tmp_path / "artifacts"
    destination.mkdir()

    def link_then_file(request: httpx.Request) -> httpx.Response:
        (destination / "sim").symlink_to(tmp_path / "outside")
        return _file(b"vcd")

    requests = api_server(_project_record(), _file(b"first"), link_then_file)

    argv = [PROJECT_ID, "--artifact", "first.vcd", "--artifact", "sim/cex.vcd", "--destination", str(destination)]
    assert _run(argv) == 2

    assert len(requests) == 3
    assert (destination / "first.vcd").read_bytes() == b"first"
    assert list((tmp_path / "outside").iterdir()) == []
    target = destination / "sim" / "cex.vcd"
    assert f"--artifact 'sim/cex.vcd': {target} is outside {destination} once links are followed" in caplog.messages


_FSMONITOR = b"[core]\n\tfsmonitor = touch fsmonitor-ran\n"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_link_in_a_checkout_into_its_git_folder_exits_2_before_any_request(api_server, tmp_path, monkeypatch, capsys):
    git(tmp_path, "init", "-q")
    config = (tmp_path / ".git" / "config").read_bytes()
    (tmp_path / "sim").symlink_to(".git")
    monkeypatch.chdir(tmp_path)
    requests = api_server(_project_record(), _file(_FSMONITOR))

    assert _run([PROJECT_ID, "--artifact", "sim/config", "--destination", "."]) == 2

    assert requests == []
    assert (tmp_path / ".git" / "config").read_bytes() == config
    assert (
        "error: --artifact 'sim/config': sim/config is inside a .git folder or a git directory once links are followed"
        in capsys.readouterr().err
    )


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_folder_swapped_for_a_link_into_the_git_folder_while_a_file_downloads_stops_the_command_before_its_write(
    api_server, tmp_path, monkeypatch, caplog
):
    git(tmp_path, "init", "-q")
    config = (tmp_path / ".git" / "config").read_bytes()
    (tmp_path / "sim").mkdir()
    monkeypatch.chdir(tmp_path)

    def swap_then_file(request: httpx.Request) -> httpx.Response:
        (tmp_path / "sim").rmdir()
        (tmp_path / "sim").symlink_to(".git")
        return _file(_FSMONITOR)

    requests = api_server(_project_record(), swap_then_file)

    assert _run([PROJECT_ID, "--artifact", "sim/config", "--destination", "."]) == 2

    assert len(requests) == 2
    assert (tmp_path / ".git" / "config").read_bytes() == config
    assert (
        "--artifact 'sim/config': sim/config is inside a .git folder or a git directory once links are followed"
        in caplog.messages
    )


@pytest.mark.parametrize("destination", [".git", ".git/hooks", ".GIT./hooks", "GIT~1", "lib.git", "lib.git/hooks/x"])
def test_a_destination_in_a_git_folder_or_a_git_directory_exits_2_before_any_request(
    destination, api_server, tmp_path, monkeypatch, capsys
):
    git(tmp_path, "init", "-q")
    git(tmp_path, "init", "-q", "--bare", "lib.git")
    monkeypatch.chdir(tmp_path)
    before = sorted(tmp_path.rglob("*"))
    requests = api_server()

    assert _run([PROJECT_ID, "--artifact", "sim/cex.vcd", "--destination", destination]) == 2

    assert requests == []
    assert (
        f"error: Cannot save the files to {destination}: it is inside a .git folder or a git directory"
        in capsys.readouterr().err
    )
    assert sorted(tmp_path.rglob("*")) == before


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("points_to", [".git", ".git/hooks", "lib.git"])
def test_a_destination_that_links_into_a_git_folder_or_a_git_directory_exits_2_before_any_request(
    points_to, api_server, tmp_path, monkeypatch, capsys
):
    git(tmp_path, "init", "-q")
    git(tmp_path, "init", "-q", "--bare", "lib.git")
    (tmp_path / "out").symlink_to(points_to)
    monkeypatch.chdir(tmp_path)
    before = sorted(tmp_path.rglob("*"))
    requests = api_server()

    assert _run([PROJECT_ID, "--artifact", "sim/cex.vcd", "--destination", "out"]) == 2

    assert requests == []
    assert (
        "error: Cannot save the files to out: it is inside a .git folder or a git directory" in capsys.readouterr().err
    )
    assert sorted(tmp_path.rglob("*")) == before


def test_without_a_destination_a_default_folder_inside_a_git_folder_exits_2_before_any_request(
    api_server, tmp_path, monkeypatch, capsys
):
    git(tmp_path, "init", "-q")
    monkeypatch.chdir(tmp_path / ".git")
    requests = api_server()

    assert _run([PROJECT_ID, "--artifact", "sim/cex.vcd"]) == 2

    assert requests == []
    assert (
        "error: Cannot save the files to aristotle-artifacts: it is inside a .git folder or a git directory"
        in capsys.readouterr().err
    )
    assert not (tmp_path / ".git" / "aristotle-artifacts").exists()


def test_artifacts_are_saved_in_a_checkout_beside_its_git_folder(api_server, tmp_path, monkeypatch):
    git(tmp_path, "init", "-q")
    config = (tmp_path / ".git" / "config").read_bytes()
    monkeypatch.chdir(tmp_path)
    api_server(_project_record(), _file(b"vcd"), _file(b"[sim]\n"))

    assert _run([PROJECT_ID, "--artifact", "sim/cex.vcd", "--artifact", "sim/config", "--destination", "."]) == 0

    assert (tmp_path / "sim" / "cex.vcd").read_bytes() == b"vcd"
    assert (tmp_path / "sim" / "config").read_bytes() == b"[sim]\n"
    assert (tmp_path / ".git" / "config").read_bytes() == config


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("points_to", ["..", "gone"], ids=["a folder", "nothing"])
def test_a_link_where_the_default_folder_goes_exits_2_before_any_request(
    points_to, api_server, tmp_path, monkeypatch, capsys
):
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path / "repo")
    (tmp_path / "repo" / "aristotle-artifacts").symlink_to(points_to)
    requests = api_server()

    assert _run([PROJECT_ID, "--artifact", "sim/cex.vcd"]) == 2

    assert requests == []
    assert (
        f"error: Cannot save the files to aristotle-artifacts: it is a link to '{points_to}'. Name another folder with "
        "--destination." in capsys.readouterr().err
    )
    assert [p.name for p in tmp_path.iterdir()] == ["repo"]
    assert [p.name for p in (tmp_path / "repo").iterdir()] == ["aristotle-artifacts"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_linked_default_folder_whose_target_cannot_be_read_still_exits_2_and_says_so(
    api_server, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "aristotle-artifacts").symlink_to("..")
    readlink = os.readlink

    def unreadable(path, *args, **kwargs):
        if os.fspath(path) == "aristotle-artifacts":
            raise PermissionError(errno.EACCES, "Permission denied")
        return readlink(path, *args, **kwargs)

    monkeypatch.setattr("aristotlelib.cli.command.download.os.readlink", unreadable)
    requests = api_server()

    assert _run([PROJECT_ID, "--artifact", "sim/cex.vcd"]) == 2

    assert requests == []
    assert (
        "error: Cannot save the files to aristotle-artifacts: it is a link whose target can't be read (Permission "
        "denied). Name another folder with --destination." in capsys.readouterr().err
    )


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_link_given_as_the_destination_is_saved_through(api_server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "real").mkdir()
    (tmp_path / "aristotle-artifacts").symlink_to("real")
    api_server(_project_record(), _file(b"vcd"))

    assert _run([PROJECT_ID, "--artifact", "sim/cex.vcd", "--destination", "aristotle-artifacts"]) == 0

    assert (tmp_path / "real" / "sim" / "cex.vcd").read_bytes() == b"vcd"


@pytest.mark.parametrize("destination", ["notes.txt", "notes.txt/artifacts"])
def test_artifacts_to_a_destination_through_a_file_exit_2_before_any_request(
    destination, api_server, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes.txt").write_text("notes")
    requests = api_server()

    assert _run([PROJECT_ID, "--artifact", "sim/cex.vcd", "--destination", destination]) == 2

    assert requests == []
    assert f"Cannot save the files to {destination}: notes.txt is not a directory" in capsys.readouterr().err
    assert (tmp_path / "notes.txt").read_text() == "notes"


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permission bits, which root bypasses")
def test_artifacts_to_a_folder_that_cannot_be_written_exit_2_before_any_request(api_server, tmp_path, capsys):
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked").chmod(0o555)
    requests = api_server()

    try:
        code = _run([PROJECT_ID, "--artifact", "sim/cex.vcd", "--destination", str(tmp_path / "locked" / "out")])
    finally:
        (tmp_path / "locked").chmod(0o755)

    assert code == 2
    assert requests == []
    locked = tmp_path / "locked"
    assert f"Cannot save the files to {locked / 'out'}: {locked} is not writable" in capsys.readouterr().err


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_an_artifact_that_cannot_be_written_is_named_and_the_command_exits_3(
    json_flag, api_server, tmp_path, capsys, caplog
):
    # A file where the artifact's folder must go.
    (tmp_path / "sim").write_text("notes")
    api_server(_project_record(), _file(b"readme"), _file(b"vcd"))

    argv = [
        PROJECT_ID,
        "--artifact",
        "README.md",
        "--artifact",
        "sim/cex.vcd",
        "--destination",
        str(tmp_path),
        *json_flag,
    ]
    assert _run(argv) == 3

    assert (tmp_path / "README.md").read_bytes() == b"readme"
    assert (tmp_path / "sim").read_text() == "notes"
    assert any(message.startswith("sim/cex.vcd was not saved: ") for message in caplog.messages)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("status", [403, 404])
def test_artifacts_of_a_project_the_key_cannot_see_exit_4_as_download_does(status, api_server, tmp_path, caplog):
    requests = api_server(httpx.Response(status, json={"detail": "Forbidden"}))

    assert _run([PROJECT_ID, "--artifact", "sim/cex.vcd", "--destination", str(tmp_path)]) == 4

    assert len(requests) == 1
    assert f"Project {PROJECT_ID} not found or not accessible." in caplog.messages
    assert list(tmp_path.iterdir()) == []
