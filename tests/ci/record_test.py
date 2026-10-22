"""Tests for aristotlelib.ci.record."""

import dataclasses
import os
import subprocess
from pathlib import Path

import pytest

from aristotlelib.ci import runner, scope
from aristotlelib.ci.manifest import Kind, Manifest, RequestedProperty
from aristotlelib.ci.record import NOTE, RunRecord, archive_digest, files_digest, manifest_digest
from tests.ci.conftest import git, latin1_name

FULL = RequestedProperty("full", Kind.NATURAL_LANGUAGE, "The FIFO never overflows.", assumptions="Reset is held.")
EMPTY = RequestedProperty("empty", Kind.SVA, "no_underflow")
COMMIT = "0123456789abcdef0123456789abcdef01234567"
FILES = "cd" * 32
DIGEST = "ab" * 32


def _manifest(*properties: RequestedProperty) -> Manifest:
    return Manifest(path=Path("aristotle.toml"), properties=list(properties))


def _head(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def _uploaded(root: Path) -> str:
    """files_digest of what a run from ``root`` uploads."""
    return files_digest(root, scope.resolve_checkout(root, warn=False).upload_files)


def test_the_digest_is_of_what_the_run_checks_not_of_how_the_manifest_lists_it():
    reordered = Manifest(
        path=Path("other.toml"),
        properties=[EMPTY, dataclasses.replace(FULL, expect_disproved=True)],
        max_wall_time_seconds=600,
    )

    assert manifest_digest(reordered) == manifest_digest(_manifest(FULL, EMPTY))


@pytest.mark.parametrize(
    "changed",
    [
        RequestedProperty("full2", Kind.NATURAL_LANGUAGE, FULL.statement, assumptions=FULL.assumptions),
        RequestedProperty("full", Kind.SVA, FULL.statement, assumptions=FULL.assumptions),
        RequestedProperty("full", Kind.NATURAL_LANGUAGE, "The FIFO never underflows.", assumptions=FULL.assumptions),
        RequestedProperty("full", Kind.NATURAL_LANGUAGE, FULL.statement),
    ],
    ids=["id", "kind", "statement", "assumptions"],
)
def test_the_digest_changes_with_any_property_the_run_is_asked_to_check(changed: RequestedProperty):
    assert manifest_digest(_manifest(changed, EMPTY)) != manifest_digest(_manifest(FULL, EMPTY))


@pytest.mark.parametrize(
    "record",
    [RunRecord(COMMIT, FILES, DIGEST), RunRecord("c" * 64, FILES, DIGEST), RunRecord(None, FILES, DIGEST)],
    ids=["sha-1 repository", "sha-256 repository", "no commit"],
)
def test_a_record_reads_back_from_the_end_of_the_prompt_it_ends(record: RunRecord):
    prompt = f"Check each of the following properties.\n\n{record.prompt_ending}\n"

    assert prompt.splitlines()[-2:] == [NOTE, record.line]
    assert RunRecord.ending(prompt) == record


@pytest.mark.parametrize(
    "prompt",
    [
        "Check each of the following properties.",
        f"{RunRecord(COMMIT, FILES, DIGEST).line}\nThen check them again.",
        f"Note: {RunRecord(COMMIT, FILES, DIGEST).line}",
        RunRecord(COMMIT, FILES, DIGEST).line.replace(COMMIT, COMMIT[:12]),
        "",
    ],
    ids=["none", "not last", "not a whole line", "short commit", "empty"],
)
def test_a_prompt_that_does_not_end_with_a_whole_record_has_none(prompt: str):
    assert RunRecord.ending(prompt) is None


def test_a_checkout_records_its_commit_and_what_it_uploads(git_repo: Path):
    record = RunRecord.of_checkout(git_repo, scope.resolve_checkout(git_repo).upload_files, _manifest(FULL))

    assert record == RunRecord(_head(git_repo), _uploaded(git_repo), manifest_digest(_manifest(FULL)))


def test_a_repository_without_a_commit_records_none(tmp_path: Path):
    git(tmp_path, "init", "-q")
    (tmp_path / "fifo.v").write_text("module fifo;\nendmodule\n")
    git(tmp_path, "add", "-A")

    assert RunRecord.of_checkout(tmp_path, [Path("fifo.v")], _manifest(FULL)).commit is None


def test_the_files_digest_follows_each_uploaded_files_content_committed_or_not(git_repo: Path):
    before = _uploaded(git_repo)
    (git_repo / "rtl" / "fifo.v").write_text("module fifo2;\nendmodule\n")
    changed = _uploaded(git_repo)
    git(git_repo, "commit", "-qam", "fifo2")

    assert changed != before
    assert _uploaded(git_repo) == changed


def test_the_files_digest_follows_each_uploaded_files_path(git_repo: Path):
    before = _uploaded(git_repo)
    git(git_repo, "mv", "rtl/fifo.v", "rtl/queue.v")

    assert _uploaded(git_repo) != before


def test_a_new_commit_with_the_same_files_has_the_same_digest(git_repo: Path):
    before = _uploaded(git_repo)
    git(git_repo, "commit", "-q", "--allow-empty", "-m", "a merge commit with the same tree, as Jenkins makes")

    assert _uploaded(git_repo) == before


def test_a_change_to_a_file_the_run_does_not_upload_leaves_the_digest(git_repo: Path):
    (git_repo / ".gitignore").write_text("*.log\n")
    (git_repo / "big.log").write_text("v1\n")
    git(git_repo, "add", "-f", ".gitignore", "big.log")
    git(git_repo, "commit", "-qm", "a tracked file a .gitignore rule holds back")
    before = _uploaded(git_repo)

    (git_repo / "notes.txt").write_text("untracked\n")
    (git_repo / ".env").write_text("SECRET=never uploaded\n")
    (git_repo / "big.log").write_text("held back\n")

    assert _uploaded(git_repo) == before


def test_only_files_under_the_root_count(git_repo: Path):
    before = _uploaded(git_repo / "rtl")
    (git_repo / "README.md").write_text("changed outside rtl\n")

    assert _uploaded(git_repo / "rtl") == before


def test_a_path_that_is_not_a_readable_file_counts_as_absent(git_repo: Path):
    files = [Path("rtl/fifo.v"), Path("rtl"), Path("missing.v")]

    assert files_digest(git_repo, files) == files_digest(git_repo, [Path("rtl/fifo.v")])


def test_a_manifest_among_the_uploaded_files_counts_as_one_of_them(git_repo: Path):
    (git_repo / "aristotle.toml").write_text("[check]\nmax_wall_time_seconds = 600\n")
    git(git_repo, "add", "aristotle.toml")
    files = scope.resolve_checkout(git_repo).upload_files
    checked = Manifest(path=git_repo / "aristotle.toml", properties=[FULL])
    before = RunRecord.of_checkout(git_repo, files, checked)

    (git_repo / "aristotle.toml").write_text("# A comment.\n[check]\nmax_wall_time_seconds = 600\n")
    after = RunRecord.of_checkout(git_repo, files, checked)

    assert (after.files, after.manifest) == (_uploaded(git_repo), before.manifest)
    assert after.files != before.files


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_link_counts_as_its_targets_content_only_when_the_upload_holds_its_target(git_repo: Path):
    (git_repo / "link.v").symlink_to("rtl/fifo.v")
    (git_repo / "out.v").symlink_to(git_repo / "README.md")
    copy = git_repo / "copy"
    (copy / "rtl").mkdir(parents=True)
    for name in ("rtl/fifo.v", "link.v"):
        (copy / name).write_bytes((git_repo / "rtl" / "fifo.v").read_bytes())

    linked = files_digest(git_repo, [Path("rtl/fifo.v"), Path("link.v"), Path("out.v")])

    assert linked == files_digest(copy, [Path("rtl/fifo.v"), Path("link.v")])


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
def test_a_link_to_a_fifo_or_a_device_is_left_out_without_reading_it(git_repo: Path):
    os.mkfifo(git_repo / "pipe")
    (git_repo / "pipe.v").symlink_to(git_repo / "pipe")
    (git_repo / "zero.v").symlink_to("/dev/zero")

    assert files_digest(git_repo, [Path("rtl/fifo.v"), Path("pipe.v"), Path("zero.v"), Path("pipe")]) == (
        files_digest(git_repo, [Path("rtl/fifo.v")])
    )


def test_an_archive_records_the_files_it_holds_as_the_checkout_they_came_from(git_repo: Path, tmp_path: Path):
    files = scope.resolve_checkout(git_repo, warn=False).upload_files
    upload = runner.build_tarball(git_repo, files, tmp_path / "out.tar.gz", None)

    assert archive_digest(tmp_path / "out.tar.gz", upload.uploaded) == files_digest(git_repo, files)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_an_archive_records_a_link_and_a_name_that_is_not_utf8_as_the_checkout_does(git_repo: Path, tmp_path: Path):
    (git_repo / "link.v").symlink_to("rtl/fifo.v")
    files = [Path("rtl/fifo.v"), Path("link.v"), Path(latin1_name(git_repo))]
    upload = runner.build_tarball(git_repo, files, tmp_path / "out.tar.gz", None)

    assert sorted(upload.uploaded) == sorted(files)
    assert archive_digest(tmp_path / "out.tar.gz", upload.uploaded) == files_digest(git_repo, files)
