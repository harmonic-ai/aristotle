"""Shared fixtures for the CI tests."""

import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from aristotlelib.declaration import (
    Declaration,
    DeclarationEvidence,
    DeclarationFailureReason,
    DeclarationSource,
    DeclarationStatus,
    DeclarationType,
)


def make_declaration(
    name: str = "Spec.Example",
    status: DeclarationStatus = DeclarationStatus.PROVED,
    declaration_type: DeclarationType = DeclarationType.PROPERTY,
    filename: str = "Spec/Property.lean",
    description: str | None = "The example property holds.",
    lean_code: str | None = None,
    source_file: str | None = None,
    source_line: int | None = None,
    source_anchor: str | None = None,
    failure_reason: DeclarationFailureReason | None = None,
    evidence: DeclarationEvidence | None = None,
) -> Declaration:
    """Build a Declaration without needing a server response."""
    return Declaration(
        project_id="project-1",
        declaration_id=name.lower().replace(".", "-"),
        declaration_type=declaration_type,
        source=DeclarationSource.AGENT,
        name=name,
        status=status,
        failure_reason=failure_reason,
        evidence=evidence,
        filename=filename,
        source_file=source_file,
        source_line=source_line,
        source_anchor=source_anchor,
        lean_code=lean_code,
        description=description,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
    )


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A repository with one commit containing Verilog, C, docs and a secret."""
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")

    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "fifo.v").write_text("module fifo;\nendmodule\n")
    (tmp_path / "rtl" / "driver.c").write_text("int main(void) { return 0; }\n")
    (tmp_path / "README.md").write_text("docs\n")
    (tmp_path / ".env").write_text("SECRET=hunter2\n")

    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def commit_bare_shaped(repo: Path, name: str = "hw") -> Path:
    """Commit the folder ``name``, which git run inside it reads as a bare repository whose config only says it is one,
    holding top.v; its HEAD is spelled as git takes one and local_file_utils.is_git_dir() doesn't."""
    folder = repo / name
    for marker in ("objects", "refs"):
        (folder / marker).mkdir(parents=True)
        (folder / marker / ".keep").write_text("")
    (folder / "HEAD").write_text("ref:refs/heads/main\n")
    (folder / "config").write_text("[core]\n\tbare = true\n")
    (folder / "top.v").write_text("module top;\nendmodule\n")
    git(repo, "add", name)
    git(repo, "commit", "-qm", f"{name}, shaped like a bare repository")
    return folder


def recording_git(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The command line of every git command run from now on, as subprocess.run() gets it."""
    calls: list[list[str]] = []
    real_run = subprocess.run

    def run(argv, *args, **kwargs):
        if argv[0] == "git":
            calls.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def without_git(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    def run(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "run", run)


def refusing_git(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    # git's own test switch reads the checkout as another user's, as a container that mounts it as root does; no
    # config of this machine's may mark it safe.
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(repo / "no-such-gitconfig"))


def latin1_name(directory: Path, content: str = "module cafe;\nendmodule\n") -> str:
    """Write ``café.v`` into ``directory`` with its name in Latin-1, so not UTF-8, and return the name as Python spells
    it; skip where the file system takes only UTF-8 names."""
    if os.name == "nt":
        pytest.skip("Windows file names are Unicode")
    name = os.fsdecode(b"caf\xe9.v")
    try:
        (directory / name).write_text(content)
    except OSError:
        pytest.skip("this file system takes only UTF-8 names")
    return name


def expecting_refuted(*statuses: DeclarationStatus):
    """RunResults of a manifest run whose properties the manifest all expects refuted, left with ``statuses``."""
    from aristotlelib.ci.manifest import Kind, RequestedProperty
    from aristotlelib.ci.results import RunResults

    declarations = [
        make_declaration(
            f"Spec.Demo{i}",
            status,
            evidence=DeclarationEvidence.NEGATION_PROVED if status == DeclarationStatus.REFUTED else None,
        ).model_copy(update={"declaration_id": f"demo{i}"})
        for i, status in enumerate(statuses)
    ]
    requested = [
        RequestedProperty(f"demo{i}", Kind.NATURAL_LANGUAGE, "A full FIFO takes no write.", expect_refuted=True)
        for i in range(len(statuses))
    ]
    return RunResults.from_declarations(declarations, duration_seconds=1.0, requested=requested)
