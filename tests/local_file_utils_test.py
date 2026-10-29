"""Tests for aristotlelib.local_file_utils module."""

import errno
import logging
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from aristotlelib import local_file_utils
from aristotlelib.local_file_utils import (
    MATHLIB_INCLUDED_STD_PACKAGES,
    MAX_FILE_SIZE_BYTES,
    SECRET_FILE_PATTERNS,
    FileChangedError,
    LeanProjectError,
    SkippedSymlinkError,
    add_file,
    add_symlinked_file,
    file_identity,
    get_expected_toolchain,
    get_lean_toolchain_version,
    get_pruned_dirnames,
    gitignored_files,
    has_any_lean_files,
    is_in_secret_folder,
    is_secret_file,
    is_secret_name,
    is_secret_path,
    load_gitignore_spec,
    secret_files,
    should_skip_input_file,
    upload_too_large,
)
from tests.ci.conftest import commit_bare_shaped, git
from tests.conftest import fstat_on_another_device, hard_link, swapped_for_a_link

SECRET_FILE_EXAMPLES = {
    ".env": ".env",
    ".env.*": ".env.production",
    ".envrc": ".envrc",
    "*.pem": "server.pem",
    "*.key": "deploy.key",
    "*.p12": "cert.p12",
    "*.pfx": "cert.pfx",
    "*.jks": "release.jks",
    "*.keystore": "release.keystore",
    "*.ppk": "signing.ppk",
    "id_rsa": "id_rsa",
    "id_dsa": "id_dsa",
    "id_ecdsa": "id_ecdsa",
    "id_ecdsa_sk": "id_ecdsa_sk",
    "id_ed25519": "id_ed25519",
    "id_ed25519_sk": "id_ed25519_sk",
    ".netrc": ".netrc",
    "_netrc": "_netrc",
    ".git-credentials": ".git-credentials",
    ".npmrc": ".npmrc",
    ".pypirc": ".pypirc",
    ".pgpass": ".pgpass",
    ".my.cnf": ".my.cnf",
    ".htpasswd": ".htpasswd",
    ".vault-token": ".vault-token",
    ".s3cfg": ".s3cfg",
    ".boto": ".boto",
    "kubeconfig": "kubeconfig",
    "*.tfstate": "prod.tfstate",
    "*.tfstate.*": "terraform.tfstate.1760000000.backup",
    "*.tfvars": "prod.auto.tfvars",
    "*.tfvars.json": "terraform.tfvars.json",
    "credentials": "credentials",
    "credentials.json": "credentials.json",
    "credentials.toml": "credentials.toml",
    "application_default_credentials.json": "application_default_credentials.json",
    "*.setup.json": "app.setup.json",
}


@pytest.mark.parametrize(
    ("empty", "found", "ignored", "skipped", "unreadable", "expected"),
    [
        (True, 0, 0, [], [], "Nothing to upload: out is empty."),
        (False, 0, 0, [], [], "Nothing to upload from out: it holds no file Aristotle reads."),
        (False, 1, 1, [], [], "Nothing to upload from out: its only file matches an ignore rule."),
        (False, 2, 2, [], [], "Nothing to upload from out: both its files match an ignore rule."),
        (False, 3, 3, [], [], "Nothing to upload from out: all 3 of its files match an ignore rule."),
        (
            False,
            1,
            0,
            [".DS_Store"],
            [],
            "Nothing to upload from out: its only file, .DS_Store, is one Aristotle skips.",
        ),
        (
            False,
            4,
            0,
            ["a.olean", "b.olean", "c.olean", "d.olean"],
            [],
            "Nothing to upload from out: all 4 of its files are ones Aristotle skips (a.olean, b.olean, c.olean, …).",
        ),
        (False, 1, 0, [], [], "Nothing to upload from out: its only file is left out, as noted above."),
        (
            False,
            2,
            1,
            [".DS_Store"],
            [],
            "Nothing to upload from out: neither of its files goes up; 1 matches an ignore rule and 1 is one Aristotle "
            "skips (.DS_Store).",
        ),
        (
            False,
            4,
            1,
            [".DS_Store"],
            [],
            "Nothing to upload from out: none of its 4 files goes up; 1 matches an ignore rule, 1 is one Aristotle "
            "skips (.DS_Store) and 2 are left out, as noted above.",
        ),
        (
            False,
            1,
            0,
            [".DS_Store"],
            ["src"],
            "Nothing to upload from out: its folder src can't be read, and nothing else in it goes up.",
        ),
        (
            False,
            0,
            0,
            [],
            ["a", "b", "c", "d/e"],
            "Nothing to upload from out: 4 of its folders (a, b, c, …) can't be read, and nothing else in it goes up.",
        ),
    ],
    ids=[
        "empty",
        "nothing reached",
        "one ignored",
        "two ignored",
        "all ignored",
        "one skipped",
        "all skipped",
        "one noted",
        "two mixed",
        "mixed",
        "one unreadable",
        "several unreadable",
    ],
)
def test_nothing_to_upload_says_whether_the_folder_is_empty_or_why_its_files_stay(
    empty, found, ignored, skipped, unreadable, expected
):
    message = local_file_utils.nothing_to_upload(
        Path("out"), empty, found, ignored, [Path(name) for name in skipped], [Path(name) for name in unreadable]
    )

    assert message == expected


def test_get_pruned_dirnames_skips_git_in_any_case_or_spelling():
    """A case-insensitive disk takes .Git for git's own folder, and HFS+ ignores some code points in a name."""
    dirs = [".Git", ".GIT", ".g\u200cit", ".github", "src"]
    assert get_pruned_dirnames(dirs, Path(".")) == [".github", "src"]


@pytest.mark.parametrize(
    ("name", "secret"),
    [("id_rsa", True), ("ID_RSA", True), ("id_r\u017fa", True), (".e\u200cnv", True), ("readme.md", False)],
    ids=["exact", "upper case", "long s", "HFS-ignorable", "not one"],
)
def test_a_secret_name_matches_in_any_case_or_spelling_a_disk_takes(name: str, secret: bool):
    assert is_secret_name(name) is secret


@pytest.mark.parametrize(
    ("path", "inside"),
    [
        (".git/config", True),
        ("sub/.GIT/config", True),
        (".g\u200cit/config", True),
        (".github/workflows/ci.yml", False),
        ("x.git/config", False),
        (".gitignore", False),
    ],
)
def test_a_git_folder_counts_in_any_case_or_spelling_a_disk_takes(path: str, inside: bool):
    assert local_file_utils.in_git_folder(Path(path)) is inside


def _git_dir(folder: Path, head: str = "ref: refs/heads/main\n") -> Path:
    folder.mkdir(parents=True)
    (folder / "HEAD").write_text(head)
    (folder / "objects").mkdir()
    (folder / "refs").mkdir()
    return folder


def test_a_config_naming_the_repository_format_marks_a_git_directory_without_a_head(tmp_path: Path):
    """A checkout of a bare repository committed with its HEAD ignored has the config git writes, and no HEAD."""
    folder = tmp_path / "lib.git"
    folder.mkdir()
    (folder / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = true\n")
    assert local_file_utils.is_git_dir(folder)
    assert local_file_utils.in_git_dir(folder / "hooks" / "pre-commit.sample")


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_root_typed_as_a_folder_that_holds_credentials_counts_by_that_name(tmp_path: Path):
    """As typed, through a link to a folder of another name, the root's own name still counts."""
    (tmp_path / "plain").mkdir()
    (tmp_path / ".docker").symlink_to(tmp_path / "plain")
    assert is_secret_path(Path("config.json"), tmp_path / ".docker")


@pytest.mark.parametrize(
    ("text", "git_dir"),
    [
        ("# repositoryformatversion is git's\nwidth = 8\n", False),
        ("# repositoryformatversion = 0\n", False),
        ("repositoryformatversion: 0\n", False),
        ("[core]\n  RepositoryFormatVersion=1\n", True),
    ],
    ids=["a mention", "a commented-out setting", "no equals sign", "a setting"],
)
def test_a_config_marks_a_git_directory_only_by_setting_the_format(tmp_path: Path, text: str, git_dir: bool):
    (tmp_path / "config").write_text(text)
    assert local_file_utils.is_git_dir(tmp_path) is git_dir


@pytest.mark.parametrize("marker", ["git directory", "gitfile"])
def test_a_folder_whose_git_entry_git_reads_is_a_work_tree_whatever_else_it_holds(tmp_path: Path, marker: str):
    if marker == "git directory":
        _git_dir(tmp_path / ".git")
    else:
        (tmp_path / ".git").write_text("gitdir: ../elsewhere/.git/worktrees/x\n")
    (tmp_path / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    assert not local_file_utils.is_git_dir(tmp_path)
    assert not local_file_utils.in_git_dir(tmp_path / "rtl" / "top.v")


def test_a_bare_repository_holding_a_stray_empty_git_folder_is_still_one(tmp_path: Path):
    """git reads it as bare, as that .git is neither a git directory nor a gitfile."""
    bare = _git_dir(tmp_path / "lib.git")
    (bare / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = true\n")
    (bare / ".git").mkdir()
    assert local_file_utils.is_git_dir(bare)
    assert local_file_utils.in_git_dir(bare / "hooks" / "pre-commit.sample")


def test_a_work_tree_is_found_before_a_git_directory_its_own_files_would_look_like(tmp_path: Path):
    """A checkout whose top folder tracks a HEAD and a config is a work tree: its .git says so first."""
    checkout = tmp_path / "checkout"
    _git_dir(checkout / ".git")
    (checkout / "HEAD").write_text("ref: refs/heads/main\n")
    (checkout / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    assert not local_file_utils.in_git_dir(checkout / "rtl" / "top.v")


def test_a_worktree_inside_a_bare_repository_is_a_work_tree_not_part_of_it(tmp_path: Path):
    bare = _git_dir(tmp_path / "proj.git")
    (bare / "main").mkdir()
    (bare / "main" / ".git").write_text("gitdir: ../worktrees/main\n")
    (bare / "hooks").mkdir()
    assert not local_file_utils.in_git_dir(bare / "main" / "top.v")
    assert local_file_utils.in_git_dir(bare / "hooks" / "pre-commit")
    assert local_file_utils.in_git_dir(bare)


@pytest.mark.parametrize(
    ("head", "beside", "git_dir"),
    [
        ("ref: refs/heads/main\n", "objects and refs", True),
        ("0123456789abcdef0123456789abcdef01234567\n", "objects and refs", True),
        ("not a ref\n", "objects and refs", False),
        ("ref: refs/heads/main\n", "commondir", True),
        ("ref: refs/heads/main\n", "config", True),
        ("ref: refs/heads/main\n", "a design's config", False),
        ("ref: refs/heads/main\n", "refs", False),
        ("ref: refs/heads/main\n", "nothing", False),
    ],
    ids=[
        "ref",
        "commit",
        "a design's HEAD",
        "linked worktree",
        "a clone of one with no history",
        "a design's HEAD and config",
        "refs alone",
        "HEAD alone",
    ],
)
def test_a_git_directory_is_known_by_a_head_git_would_read(tmp_path: Path, head: str, beside: str, git_dir: bool):
    folder = tmp_path / "x"
    if beside == "objects and refs":
        _git_dir(folder, head)
    else:
        folder.mkdir()
        (folder / "HEAD").write_text(head)
        if beside == "commondir":
            (folder / "commondir").write_text("../..\n")
        elif beside == "config":
            (folder / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = true\n")
        elif beside == "a design's config":
            (folder / "config").write_text("width = 8\n")
        elif beside == "refs":
            (folder / "refs").mkdir()
    assert local_file_utils.is_git_dir(folder) is git_dir


def test_get_pruned_dirnames_root_skips_git():
    """At the project root, .git should be pruned."""
    dirs = [".git", ".lake", "src", "test"]
    result = get_pruned_dirnames(dirs, Path("."))
    assert ".git" not in result
    assert ".lake" in result
    assert "src" in result


def test_get_pruned_dirnames_lake_packages_skips_std():
    """Inside .lake/packages, all standard mathlib packages should be pruned."""
    dirs = list(MATHLIB_INCLUDED_STD_PACKAGES) + ["my_custom_dep"]
    result = get_pruned_dirnames(dirs, Path(".lake/packages"))
    assert result == ["my_custom_dep"]


def test_get_pruned_dirnames_lake_build_skips_ir_bin():
    """Inside .lake/build, ir and bin should be pruned."""
    dirs = ["ir", "bin", "lib"]
    result = get_pruned_dirnames(dirs, Path(".lake/build"))
    assert result == ["lib"]


def test_get_pruned_dirnames_nested_lake():
    """Handles .lake appearing at a non-root level (matches **/.lake pattern)."""
    dirs = list(MATHLIB_INCLUDED_STD_PACKAGES) + ["custom"]
    result = get_pruned_dirnames(dirs, Path("subproject/.lake/packages"))
    assert result == ["custom"]


def test_get_pruned_dirnames_unrelated_dir_unchanged():
    """Directories outside .lake should not be affected (except .git)."""
    dirs = ["foo", "bar", "packages"]
    result = get_pruned_dirnames(dirs, Path("src/lib"))
    assert result == dirs


def test_get_pruned_dirnames_git_pruned_at_any_depth():
    """.git is pruned regardless of directory depth."""
    dirs = [".git", "sub"]
    result = get_pruned_dirnames(dirs, Path("a/b/c"))
    assert result == ["sub"]


def test_should_skip_input_file_olean():
    """Test that .olean files are skipped."""
    file_path = Path("test.olean")
    result = should_skip_input_file(file_path)
    assert result is True


def test_should_skip_input_file_ds_store():
    """Test that .DS_Store files are skipped."""
    assert should_skip_input_file(Path(".DS_Store")) is True
    assert should_skip_input_file(Path("subdir/.DS_Store")) is True


def test_should_skip_input_file_macos_resource_fork():
    """Test that macOS resource fork (._*) files are skipped."""
    assert should_skip_input_file(Path("._Test.lean")) is True
    assert should_skip_input_file(Path("._lakefile.toml")) is True
    assert should_skip_input_file(Path("subdir/._Deep.lean")) is True


def test_should_skip_input_file_vim_swap():
    """Test that vim swap files are skipped."""
    assert should_skip_input_file(Path(".file.txt.swp")) is True
    assert should_skip_input_file(Path(".file.txt.swo")) is True
    assert should_skip_input_file(Path(".file.txt.swn")) is True
    assert should_skip_input_file(Path("subdir/.main.lean.swp")) is True


def test_should_skip_input_file_emacs_backup():
    """Test that emacs backup and autosave files are skipped."""
    assert should_skip_input_file(Path("file.txt~")) is True
    assert should_skip_input_file(Path("#file.txt#")) is True
    assert should_skip_input_file(Path(".#file.txt")) is True
    assert should_skip_input_file(Path("subdir/main.lean~")) is True


@pytest.mark.parametrize("name", [".env", "subdir/.env.local", "test.setup.json"])
def test_should_skip_input_file_leaves_credential_files_to_the_reported_check(name: str):
    assert should_skip_input_file(Path(name)) is False


def test_should_skip_input_file_git_directory():
    """Test that files in .git directories are skipped."""
    file_path = Path("project/.git/config")
    result = should_skip_input_file(file_path)
    assert result is True


def test_should_skip_input_file_lake_build_ir():
    """Test that files in .lake/build/ir are skipped."""
    file_path = Path("project/.lake/build/ir/MyFile.ir")
    result = should_skip_input_file(file_path)
    assert result is True


def test_should_skip_input_file_lake_packages_mathlib():
    """Test that mathlib package files are skipped."""
    file_path = Path("project/.lake/packages/mathlib/SomeFile.lean")
    result = should_skip_input_file(file_path)
    assert result is True


def test_should_skip_input_file_lean_source():
    """Test that normal .lean files are NOT skipped."""
    file_path = Path("MyTheorem.lean")
    result = should_skip_input_file(file_path)
    assert result is False


def test_should_skip_input_file_regular_file():
    """Test that regular files are NOT skipped."""
    file_path = Path("README.md")
    result = should_skip_input_file(file_path)
    assert result is False


def test_should_skip_input_file_with_base_path():
    """Test skipping with a base path for relative matching."""
    base_path = Path("/home/user/project")
    file_path = Path("/home/user/project/.lake/build/ir/File.ir")
    result = should_skip_input_file(file_path, base_path=base_path)
    assert result is True


def test_should_skip_input_file_ilean():
    """Test that .ilean files are skipped."""
    file_path = Path("test.ilean")
    result = should_skip_input_file(file_path)
    assert result is True


def test_should_skip_input_file_trace():
    """Test that .trace files are skipped."""
    file_path = Path("test.trace")
    result = should_skip_input_file(file_path)
    assert result is True


@pytest.mark.parametrize(
    ("size", "message"),
    [
        (MAX_FILE_SIZE_BYTES, None),
        (MAX_FILE_SIZE_BYTES + 1, "The upload is 100.0 MB compressed, over the 100 MB limit."),
        (106032949, "The upload is 101.1 MB compressed, over the 100 MB limit."),
    ],
    ids=["at the limit", "one byte over", "101.1 MB"],
)
def test_upload_too_large_says_how_large_the_compressed_archive_is(size, message):
    assert upload_too_large(size) == message


def test_get_lean_toolchain_version_exists(tmp_path):
    """Test getting lean-toolchain version when file exists at top level."""
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.24.0\n")

    result = get_lean_toolchain_version(tmp_path)
    assert result == "leanprover/lean4:v4.24.0"


def test_get_lean_toolchain_version_missing(tmp_path):
    """Test getting lean-toolchain version when file doesn't exist."""
    result = get_lean_toolchain_version(tmp_path)
    assert result is None


def test_get_lean_toolchain_version_strips_whitespace(tmp_path):
    """Test that lean-toolchain version is stripped of whitespace."""
    (tmp_path / "lean-toolchain").write_text("  leanprover/lean4:v4.24.0  \n\n")

    result = get_lean_toolchain_version(tmp_path)
    assert result == "leanprover/lean4:v4.24.0"


def test_has_any_lean_files_exists():
    """Test has_any_lean_files when .lean files exist."""
    mock_path = Mock(spec=Path)
    mock_lean_file = Mock(spec=Path)
    mock_path.rglob = Mock(return_value=[mock_lean_file])

    result = has_any_lean_files(mock_path)
    assert result is True


def test_has_any_lean_files_missing():
    """Test has_any_lean_files when no .lean files exist."""
    mock_path = Mock(spec=Path)
    mock_path.rglob = Mock(return_value=[])

    result = has_any_lean_files(mock_path)
    assert result is False


async def test_get_expected_toolchain_success():
    """Test get_expected_toolchain when API returns successfully."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "lean_toolchain_version": "leanprover/lean4:v4.24.0",
        "mathlib_hash": "f897ebcf72cd16f89ab4577d0c826cd14afaafc7",
    }

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch(
        "aristotlelib.local_file_utils.api_request.AristotleRequestClient",
        return_value=mock_client,
    ):
        result = await get_expected_toolchain()

    assert result == "leanprover/lean4:v4.24.0"
    mock_client.get.assert_called_once_with("/version")


async def test_get_expected_toolchain_api_error():
    """Test get_expected_toolchain when API call fails."""
    mock_client = AsyncMock()
    mock_client.get.side_effect = Exception("API error")
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch(
        "aristotlelib.local_file_utils.api_request.AristotleRequestClient",
        return_value=mock_client,
    ):
        result = await get_expected_toolchain()

    # Should return None on error
    assert result is None


async def test_get_expected_toolchain_missing_field():
    """Test get_expected_toolchain when API response is missing the field."""
    mock_response = Mock()
    mock_response.json.return_value = {"mathlib_hash": "f897ebcf72cd16f89ab4577d0c826cd14afaafc7"}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch(
        "aristotlelib.local_file_utils.api_request.AristotleRequestClient",
        return_value=mock_client,
    ):
        result = await get_expected_toolchain()

    # Should return None when field is missing
    assert result is None


def test_load_gitignore_spec_no_file():
    """Test load_gitignore_spec returns None when no .gitignore exists."""
    with tempfile.TemporaryDirectory() as temp_dir:
        result = load_gitignore_spec(Path(temp_dir))
        assert result is None


def test_load_gitignore_spec_with_file():
    """Test load_gitignore_spec loads patterns from .gitignore."""
    with tempfile.TemporaryDirectory() as temp_dir:
        gitignore_path = Path(temp_dir) / ".gitignore"
        gitignore_path.write_text("*.log\nbuild/\n")

        spec = load_gitignore_spec(Path(temp_dir))
        assert spec is not None
        assert spec.match_file("test.log") is True
        assert spec.match_file("build/output.txt") is True
        assert spec.match_file("src/main.py") is False


def test_load_gitignore_spec_exempts_lake():
    """Test that .lake directories are exempt from gitignore even if matched."""
    with tempfile.TemporaryDirectory() as temp_dir:
        gitignore_path = Path(temp_dir) / ".gitignore"
        # Gitignore that would normally exclude .lake
        gitignore_path.write_text(".lake\n.lake/\n")

        spec = load_gitignore_spec(Path(temp_dir))
        assert spec is not None
        # .lake should NOT be matched due to exemption patterns
        assert spec.match_file(".lake") is False
        assert spec.match_file(".lake/build/lib/foo.olean") is False


def test_load_gitignore_spec_exempts_nested_lake():
    """Test that nested .lake directories are also exempt from gitignore."""
    with tempfile.TemporaryDirectory() as temp_dir:
        gitignore_path = Path(temp_dir) / ".gitignore"
        gitignore_path.write_text("**/.lake\n")

        spec = load_gitignore_spec(Path(temp_dir))
        assert spec is not None
        # Nested .lake should NOT be matched due to exemption patterns
        assert spec.match_file("subproject/.lake") is False
        assert spec.match_file("subproject/.lake/build/lib/foo.olean") is False


def test_should_skip_input_file_with_gitignore():
    """Test should_skip_input_file respects gitignore patterns."""
    with tempfile.TemporaryDirectory() as temp_dir:
        gitignore_path = Path(temp_dir) / ".gitignore"
        gitignore_path.write_text("*.log\nnode_modules/\n")

        spec = load_gitignore_spec(Path(temp_dir))

        # Files matching gitignore should be skipped
        assert should_skip_input_file(Path("debug.log"), gitignore_spec=spec) is True
        assert should_skip_input_file(Path("node_modules/pkg/index.js"), gitignore_spec=spec) is True

        # Files not matching should not be skipped
        assert should_skip_input_file(Path("src/main.py"), gitignore_spec=spec) is False


def test_should_skip_input_file_gitignore_does_not_skip_lake():
    """Test that .lake files are not skipped even if gitignored."""
    with tempfile.TemporaryDirectory() as temp_dir:
        gitignore_path = Path(temp_dir) / ".gitignore"
        gitignore_path.write_text(".lake\n.lake/\n")

        spec = load_gitignore_spec(Path(temp_dir))

        # .lake files should NOT be skipped even with gitignore
        # (using .lean files since .olean is in the built-in skip list)
        assert should_skip_input_file(Path(".lake/build/lib/Foo.lean"), gitignore_spec=spec) is False
        assert should_skip_input_file(Path(".lake/lakefile.lean"), gitignore_spec=spec) is False


def test_should_skip_input_file_mathlib_still_skipped_with_gitignore():
    """Test that mathlib packages are still skipped via built-in patterns even with .lake exemptions."""
    with tempfile.TemporaryDirectory() as temp_dir:
        gitignore_path = Path(temp_dir) / ".gitignore"
        gitignore_path.write_text(".lake\n")

        spec = load_gitignore_spec(Path(temp_dir))

        # Mathlib packages should STILL be skipped (via built-in patterns, not gitignore)
        assert should_skip_input_file(Path(".lake/packages/mathlib/Mathlib/Algebra.lean"), gitignore_spec=spec) is True
        assert should_skip_input_file(Path(".lake/packages/batteries/Batteries.lean"), gitignore_spec=spec) is True

        # But custom packages should NOT be skipped
        assert should_skip_input_file(Path(".lake/packages/my_custom_dep/Custom.lean"), gitignore_spec=spec) is False


def _symlink(link: Path, target: str) -> None:
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this platform cannot create symlinks")


def test_a_symlink_is_judged_by_the_identity_of_the_file_it_reaches_not_by_its_spelling(tmp_path):
    """A hard link stands in for `.ENV` on a case-insensitive volume: another name for the same file."""
    (tmp_path / ".env").write_bytes(b"TOKEN=1\n")
    os.link(tmp_path / ".env", tmp_path / "ALIAS")
    _symlink(tmp_path / "notes", "ALIAS")

    with tarfile.open(tmp_path / "upload.tar", "w") as tar:
        with pytest.raises(SkippedSymlinkError, match="target not uploaded"):
            add_symlinked_file(tar, tmp_path / "notes", "notes", archived=set())
        add_symlinked_file(tar, tmp_path / "notes", "notes", archived={file_identity(os.stat(tmp_path / ".env"))})

    with tarfile.open(tmp_path / "upload.tar") as tar:
        assert tar.getnames() == ["notes"]
        assert tar.extractfile("notes").read() == b"TOKEN=1\n"


def _retargeted_before_opening(link: Path, target: str):
    """os.open, but the link is pointed at ``target`` first: a swap between the check and the read."""
    real_open = os.open

    def open_(path, flags, *args):
        link.unlink()
        link.symlink_to(target)
        return real_open(path, flags, *args)

    return patch.object(os, "open", side_effect=open_)


def test_a_target_swapped_between_the_check_and_the_read_is_left_out(tmp_path):
    (tmp_path / "a.sv").write_bytes(b"a\n")
    (tmp_path / ".env").write_bytes(b"TOKEN=1\n")
    _symlink(tmp_path / "notes", "a.sv")
    archived = {file_identity(os.stat(tmp_path / "a.sv"))}

    with tarfile.open(tmp_path / "upload.tar", "w") as tar:
        with (
            _retargeted_before_opening(tmp_path / "notes", ".env"),
            pytest.raises(SkippedSymlinkError, match="target not uploaded"),
        ):
            add_symlinked_file(tar, tmp_path / "notes", "notes", archived)
        assert tar.getnames() == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes on this platform")
def test_a_target_swapped_for_a_named_pipe_is_left_out_without_blocking(tmp_path):
    (tmp_path / "a.sv").write_bytes(b"a\n")
    os.mkfifo(tmp_path / "pipe")
    _symlink(tmp_path / "notes", "a.sv")
    archived = {file_identity(os.stat(tmp_path / "a.sv"))}

    def blocked(signum, frame):
        raise TimeoutError("opening the named pipe blocked")

    previous = signal.signal(signal.SIGALRM, blocked)
    signal.alarm(5)
    try:
        with tarfile.open(tmp_path / "upload.tar", "w") as tar:
            with (
                _retargeted_before_opening(tmp_path / "notes", "pipe"),
                pytest.raises(SkippedSymlinkError, match="target not uploaded"),
            ):
                add_symlinked_file(tar, tmp_path / "notes", "notes", archived)
            assert tar.getnames() == []
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.skipif(not os.path.isdir("/dev/fd"), reason="no /dev/fd to count open descriptors")
def test_a_target_swapped_for_a_directory_is_left_out_and_its_descriptor_closed(tmp_path):
    (tmp_path / "a.sv").write_bytes(b"a\n")
    (tmp_path / "src").mkdir()
    _symlink(tmp_path / "notes", "a.sv")
    archived = {file_identity(os.stat(tmp_path / "a.sv"))}

    with tarfile.open(tmp_path / "upload.tar", "w") as tar:
        open_before = len(os.listdir("/dev/fd"))
        with (
            _retargeted_before_opening(tmp_path / "notes", "src"),
            pytest.raises(SkippedSymlinkError, match="target not uploaded"),
        ):
            add_symlinked_file(tar, tmp_path / "notes", "notes", archived)
        assert len(os.listdir("/dev/fd")) == open_before
        assert tar.getnames() == []


def test_a_target_whose_filesystem_reports_inode_0_is_left_out(tmp_path):
    (tmp_path / "a.sv").write_bytes(b"a\n")
    _symlink(tmp_path / "notes", "a.sv")
    real = os.stat(tmp_path / "a.sv")
    no_inode = os.stat_result((real.st_mode, 0, real.st_dev, *real[3:10]))

    with tarfile.open(tmp_path / "upload.tar", "w") as tar:
        with (
            patch.object(os, "stat", return_value=no_inode),
            patch.object(os, "fstat", return_value=no_inode),
            patch.object(os, "open", wraps=os.open) as opened,
            pytest.raises(SkippedSymlinkError, match="target not uploaded"),
        ):
            add_symlinked_file(tar, tmp_path / "notes", "notes", archived={(real.st_dev, 0)})
        opened.assert_not_called()
        assert tar.getnames() == []
    assert file_identity(no_inode) is None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes on this platform")
def test_a_symlink_to_something_other_than_a_regular_file_is_left_out_without_opening_it(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    _symlink(tmp_path / "notes", "pipe")

    with tarfile.open(tmp_path / "upload.tar", "w") as tar:
        with (
            patch.object(os, "open", wraps=os.open) as opened,
            pytest.raises(SkippedSymlinkError, match="target not uploaded"),
        ):
            add_symlinked_file(tar, tmp_path / "notes", "notes", archived={file_identity(os.stat(tmp_path / "pipe"))})
        opened.assert_not_called()
        assert tar.getnames() == []


def test_each_name_of_a_hard_linked_file_goes_in_as_a_file_holding_its_content(tmp_path):
    (tmp_path / "a.sv").write_bytes(b"module a; endmodule\n")
    hard_link(tmp_path / "a.sv", tmp_path / "b.sv")

    with tarfile.open(tmp_path / "upload.tar", "w", dereference=True) as tar:
        for name in ("a.sv", "b.sv"):
            add_file(tar, tmp_path / name, name, os.lstat(tmp_path / name))

    with tarfile.open(tmp_path / "upload.tar") as tar:
        assert [(m.name, m.type, tar.extractfile(m).read()) for m in tar.getmembers()] == [
            ("a.sv", tarfile.REGTYPE, b"module a; endmodule\n"),
            ("b.sv", tarfile.REGTYPE, b"module a; endmodule\n"),
        ]


NO_FOLLOW = pytest.mark.parametrize("no_follow", [True, False], ids=["O_NOFOLLOW", "without O_NOFOLLOW, as on Windows"])
"""Runs a test where the open refuses a link, and with O_NOFOLLOW removed, as Windows has none."""


def _no_follow(no_follow: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip where ``no_follow`` needs an O_NOFOLLOW the platform lacks; remove O_NOFOLLOW unless ``no_follow``."""
    if no_follow and not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("no O_NOFOLLOW on this platform")
    if not no_follow:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)


def _archived(tar_path: Path) -> list[tuple[str, bytes]]:
    with tarfile.open(tar_path) as tar:
        return [(m.name, tar.extractfile(m).read()) for m in tar.getmembers()]


@NO_FOLLOW
def test_a_file_swapped_for_a_link_before_it_is_opened_is_refused_not_followed(no_follow, tmp_path, monkeypatch):
    _no_follow(no_follow, monkeypatch)
    (tmp_path / "a.sv").write_bytes(b"a\n")
    (tmp_path / "id_ed25519").write_bytes(b"private key\n")
    checked = os.lstat(tmp_path / "a.sv")

    with tarfile.open(tmp_path / "upload.tar", "w", dereference=True) as tar:
        with (
            swapped_for_a_link(tmp_path / "a.sv", tmp_path / "id_ed25519"),
            pytest.raises(FileChangedError) as raised,
        ):
            add_file(tar, tmp_path / "a.sv", "a.sv", checked)
        assert tar.getnames() == []
    # Refused by the open itself, or else by the identity of the file it opened.
    assert (getattr(raised.value.__cause__, "errno", None) == errno.ELOOP) is no_follow
    assert local_file_utils.archive_error(raised.value) == (
        f"Could not archive {tmp_path / 'a.sv'}: it changed while it was being uploaded. Try again."
    )


@NO_FOLLOW
def test_a_file_replaced_by_another_before_it_is_opened_goes_in_only_where_the_open_refuses_a_link(
    no_follow, tmp_path, monkeypatch
):
    """As an editor saves one; without O_NOFOLLOW the open's device and inode must be the checked file's."""
    _no_follow(no_follow, monkeypatch)
    (tmp_path / "a.sv").write_bytes(b"a\n")
    (tmp_path / "a.sv.new").write_bytes(b"edited\n")
    checked = os.lstat(tmp_path / "a.sv")
    real_open = os.open

    def open_(path, flags, *args):
        os.replace(tmp_path / "a.sv.new", tmp_path / "a.sv")
        return real_open(path, flags, *args)

    with tarfile.open(tmp_path / "upload.tar", "w", dereference=True) as tar:
        with (
            patch.object(os, "open", side_effect=open_),
            nullcontext() if no_follow else pytest.raises(FileChangedError),
        ):
            add_file(tar, tmp_path / "a.sv", "a.sv", checked)

    assert _archived(tmp_path / "upload.tar") == ([("a.sv", b"edited\n")] if no_follow else [])


@NO_FOLLOW
def test_a_file_whose_descriptor_gives_another_device_and_inode_goes_in_only_where_the_open_refuses_a_link(
    no_follow, tmp_path, monkeypatch
):
    """As overlayfs gave one before Linux 4.19."""
    _no_follow(no_follow, monkeypatch)
    (tmp_path / "a.sv").write_bytes(b"a\n")
    checked = os.lstat(tmp_path / "a.sv")
    monkeypatch.setattr(os, "fstat", fstat_on_another_device(os.fstat))

    with tarfile.open(tmp_path / "upload.tar", "w", dereference=True) as tar:
        with nullcontext() if no_follow else pytest.raises(FileChangedError):
            add_file(tar, tmp_path / "a.sv", "a.sv", checked)

    assert _archived(tmp_path / "upload.tar") == ([("a.sv", b"a\n")] if no_follow else [])


@pytest.mark.skipif(not hasattr(os, "O_NONBLOCK"), reason="no open that doesn't block on this platform")
def test_a_file_a_lease_holds_is_opened_again_waiting_for_the_holder(tmp_path):
    """The open that doesn't block gets EAGAIN where a lease holder is, and open() waits for it."""
    (tmp_path / "a.sv").write_bytes(b"a\n")
    real_open = os.open
    blocking = []

    def open_(path, flags, *args):
        blocking.append(not flags & os.O_NONBLOCK)
        if flags & os.O_NONBLOCK:
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable", str(path))
        return real_open(path, flags, *args)

    with tarfile.open(tmp_path / "upload.tar", "w", dereference=True) as tar:
        with patch.object(os, "open", side_effect=open_):
            add_file(tar, tmp_path / "a.sv", "a.sv", os.lstat(tmp_path / "a.sv"))

    assert blocking == [False, True]
    assert _archived(tmp_path / "upload.tar") == [("a.sv", b"a\n")]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes on this platform")
def test_a_file_a_lease_holds_that_turns_into_a_named_pipe_is_refused_without_a_second_open(tmp_path):
    (tmp_path / "a.sv").write_bytes(b"a\n")
    checked = os.lstat(tmp_path / "a.sv")
    opens = []

    def open_(path, flags, *args):
        opens.append(flags)
        os.unlink(path)
        os.mkfifo(path)
        raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable", str(path))

    with tarfile.open(tmp_path / "upload.tar", "w", dereference=True) as tar:
        with patch.object(os, "open", side_effect=open_), pytest.raises(FileChangedError):
            add_file(tar, tmp_path / "a.sv", "a.sv", checked)
        assert tar.getnames() == []
    assert len(opens) == 1


def test_every_secret_pattern_has_an_example():
    assert set(SECRET_FILE_EXAMPLES) == set(SECRET_FILE_PATTERNS)


@pytest.mark.parametrize("name", [*SECRET_FILE_EXAMPLES.values(), *(n.upper() for n in SECRET_FILE_EXAMPLES.values())])
def test_a_file_named_like_a_credential_matches_in_either_case(name: str):
    assert is_secret_file(Path("config") / name)
    assert is_secret_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "fifo.v",
        "keyboard.c",
        "environment.rs",
        "README.md",
        "id_stage.sv",
        ".environment",
        "id_rsa.pub",
        "Credentials.lean",
        "credentials_test.lean",
        "aws_credentials",
        "main.tf",
        ".terraform.lock.hcl",
        "terraform.tfvars.example",
        "kubeconfig.go",
    ],
)
def test_ordinary_files_are_not_treated_as_secrets(name: str):
    assert not is_secret_file(Path(name))


def test_secret_files_picks_out_aws_credentials_and_not_files_merely_named_after_credentials():
    files = [Path(".aws/credentials"), Path("Proofs/credentials_test.lean"), Path("Credentials/Basic.lean")]

    assert secret_files(files) == [Path(".aws/credentials")]


@pytest.mark.parametrize(
    ("path", "secret"),
    [
        (".env/production.yaml", True),
        ("sub/.netrc/inner", True),
        (".docker/config.json", True),
        (".envrc", True),
        ("sub/.env.production", True),
        ("rtl/m.v", False),
        ("docker/config.json", False),
    ],
)
def test_a_path_is_secret_by_its_name_or_by_a_folder_that_holds_credentials(path: str, secret: bool):
    assert is_secret_path(Path(path)) is secret
    assert secret_files([Path(path)]) == ([Path(path)] if secret else [])


@pytest.mark.parametrize(
    ("path", "inside"),
    [
        (".env/production", True),
        ("config/.ENV.local/web", True),
        (".docker/config.json", True),
        ("tools/.Docker/config.json", True),
        ("docker/config.json", False),
        (".aws/credentials", True),
        ("home/u/.ssh/config", True),
        (".kube/config", True),
        (".gnupg/private-keys-v1.d/x.key", True),
        (".azure/msal_token_cache.json", True),
        (".terraform.d/credentials.tfrc.json", True),
        ("home/u/.config/gh/hosts.yml", True),
        (".config/gcloud/configurations/config_default", True),
        (".config/glab-cli/config.yml", True),
        (".\u017fsh/config", True),
        ("inventory/hosts.yml", False),
        ("gh/hosts.yml", False),
        (".config/ghostty/config", False),
        ("Credentials/Basic.lean", False),
        ("credentials/notes.md", False),
        ("src/.env", False),
    ],
)
def test_only_a_hidden_folder_named_like_a_credential_file_holds_its_files_back(path: str, inside: bool):
    assert is_in_secret_folder(Path(path)) is inside


@pytest.mark.parametrize(
    ("root", "path", "inside"),
    [
        ("home/u/.config", "gh/hosts.yml", True),
        ("home/u/.docker", "config.json", True),
        ("home/u/.ssh", "config", True),
        ("ci/.env.d/repo", "rtl/a.v", False),
        ("home/u/.config", "nvim/init.lua", False),
        ("C/Users/u", "AppData/Roaming/GitHub CLI/hosts.yml", True),
        ("C/Users/u/AppData/Roaming", "GitHub CLI/hosts.yml", True),
        ("C/Users/u/AppData", "Roaming/gcloud/access_tokens.db", True),
        ("C/Users/u/AppData/Roaming/gcloud", "access_tokens.db", True),
        ("home/u/.config/gh/repo", "rtl/a.v", False),
        ("home/u/.config/nvim", "lua/init.lua", False),
    ],
)
def test_the_roots_own_name_counts_for_a_folder_that_holds_credentials(
    tmp_path: Path, root: str, path: str, inside: bool
):
    """A root that is itself such a folder holds its files back, but the folders above the root don't count."""
    assert is_in_secret_folder(Path(path), tmp_path / root) is inside
    assert is_secret_path(Path(path), tmp_path / root) is inside


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_root_linked_to_a_folder_that_holds_credentials_counts_by_where_it_resolves(tmp_path: Path):
    (tmp_path / ".config" / "gh").mkdir(parents=True)
    (tmp_path / "cfg").symlink_to(tmp_path / ".config")
    assert is_secret_path(Path("gh/hosts.yml"), tmp_path / "cfg")


def _repository(root: Path) -> Path:
    git(root, "init", "-q")
    return root


def _write(root: Path, *paths: str) -> list[Path]:
    """Create each file under ``root``; return the paths as given."""
    for path in paths:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("x\n")
    return [Path(p) for p in paths]


def test_in_a_git_work_tree_every_gitignore_applies(tmp_path):
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text("*.vcd\n")
    (repo / "rtl" / "gen").mkdir(parents=True)
    (repo / "rtl" / ".gitignore").write_text("gen/\n")
    files = _write(repo, "top.sv", "wave.vcd", "rtl/fifo.sv", "rtl/gen/out.sv", "rtl/café notes.vcd")

    assert gitignored_files(repo, files) == {Path("wave.vcd"), Path("rtl/gen/out.sv"), Path("rtl/café notes.vcd")}


def test_a_subfolder_of_a_git_work_tree_gets_the_repositorys_gitignore(tmp_path):
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text("*.vcd\n/rtl/build/\n")
    _write(repo / "rtl", "fifo.sv", "wave.vcd", "build/out.sv")

    ignored = gitignored_files(repo / "rtl", [Path("fifo.sv"), Path("wave.vcd"), Path("build/out.sv")])

    assert ignored == {Path("wave.vcd"), Path("build/out.sv")}


def test_a_folder_git_ignores_whose_git_folder_git_passes_over_gets_its_own_gitignore_alone(tmp_path):
    """An empty .git, which git looks past to the repository above it."""
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text("sub/\n")
    files = _write(repo / "sub", "Main.lean", "run.log")
    (repo / "sub" / ".gitignore").write_text("*.log\n")
    (repo / "sub" / ".git").mkdir()

    assert gitignored_files(repo / "sub", files) == {Path("run.log")}
    assert local_file_utils.left_out_rules(repo / "sub", [Path("run.log")]) == {Path("run.log"): "sub/.gitignore"}


def test_a_folder_shaped_like_a_bare_repository_gets_the_checkouts_gitignore(tmp_path, caplog):
    repo = _repository(tmp_path)
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text("*.log\n")
    hw = commit_bare_shaped(repo)
    (hw / "kept.log").write_text("tracked, then ignored\n")
    git(repo, "add", "-f", "hw/kept.log")

    with caplog.at_level(logging.WARNING, logger="aristotle"):
        ignored = gitignored_files(hw, [*_write(hw, "run.log"), Path("kept.log"), Path("top.v")])

    assert ignored == {Path("run.log"), Path("kept.log")}
    assert "  kept.log  (.gitignore:1: *.log)" in caplog.messages[0]


def test_a_tracked_file_that_a_gitignore_rule_matches_is_ignored_and_named(tmp_path, caplog):
    repo = _repository(tmp_path)
    files = _write(repo, "notes.log", "top.sv")
    git(repo, "add", "-A")
    (repo / ".gitignore").write_text("*.log\n")
    files += _write(repo, "debug.log")

    with caplog.at_level(logging.WARNING, logger="aristotle"):
        assert gitignored_files(repo, files) == {Path("notes.log"), Path("debug.log")}
    assert caplog.messages == [
        local_file_utils.held_back_warning({Path("notes.log"): ".gitignore:1: *.log"}, include_ignored=True)
    ]


def test_the_held_back_warning_names_every_file_with_its_rule_and_how_to_upload_it():
    rules = {Path("docs/spec.json"): "hw/docs/.gitignore:1: *.json", Path("build/keep.h"): ""}

    assert local_file_utils.held_back_warning(rules) == (
        "Not uploading 2 tracked files that an ignore rule matches:\n"
        "  build/keep.h\n"
        "  docs/spec.json  (hw/docs/.gitignore:1: *.json)\n"
        "To upload one, add a line after its rule: `!/` and its path from the folder of the rule's .gitignore, with a "
        "backslash before any *, ?, [ or \\ in it. If the rule matches a folder above the file, first let each folder through and ignore its contents "
        "again, a level at a time: for build/gen/keep.h and a `build/` rule, the lines are `!/build/`, `/build/*`, "
        "`!/build/gen/`, `/build/gen/*`, then `!/build/gen/keep.h`."
    )


def test_the_held_back_warning_of_submits_folder_names_include_ignored_too():
    rules = {Path("build/keep.h"): ".gitignore:1: build/"}

    assert local_file_utils.held_back_warning(rules, include_ignored=True) == (
        local_file_utils.held_back_warning(rules) + " Or pass --include-ignored (from Python, the include_ignored "
        "argument), which uploads the files ignore rules keep out."
    )


@pytest.mark.parametrize(
    ("uploaded", "ignored", "included", "summary"),
    [
        (1, 0, 0, "Uploading 1 file."),
        (3, 1, 0, "Uploading 3 files; 1 left out because it matches an ignore rule."),
        (3, 2, 0, "Uploading 3 files; 2 left out because they match an ignore rule."),
        (3, 0, 1, "Uploading 3 files; 1 of them matches an ignore rule."),
        (3, 0, 2, "Uploading 3 files; 2 of them match an ignore rule."),
    ],
)
def test_the_upload_summary_counts_what_an_ignore_rule_left_out_or_matches_of_what_goes_up(
    uploaded: int, ignored: int, included: int, summary: str
):
    assert local_file_utils.upload_summary(uploaded, ignored, included) == summary


def test_the_rule_of_each_file_is_named_by_its_file_from_the_repositorys_top(tmp_path):
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text("build/\n")
    (repo / "hw" / "docs").mkdir(parents=True)
    (repo / "hw" / "docs" / ".gitignore").write_text("*.json\n")
    with open(repo / ".git" / "info" / "exclude", "a") as exclude:
        exclude.write("secret.txt\n")
    _write(repo / "hw", "docs/spec.json", "build/gen/keep.h", "secret.txt", "top.sv")

    rules = local_file_utils.ignore_rules(
        repo / "hw", [Path("docs/spec.json"), Path("build/gen/keep.h"), Path("secret.txt"), Path("top.sv")]
    )

    assert rules[Path("docs/spec.json")] == "hw/docs/.gitignore:1: *.json"
    assert rules[Path("build/gen/keep.h")] == ".gitignore:1: build/"
    # A rule of .git/info/exclude holds no tracked file back, so none is named.
    assert rules[Path("secret.txt")] == ""
    assert rules[Path("top.sv")] == ""


def test_without_git_no_rule_is_named(tmp_path):
    with patch.object(local_file_utils.subprocess, "run", side_effect=FileNotFoundError("git")):
        assert local_file_utils.ignore_rules(tmp_path, [Path("a.log")]) == {Path("a.log"): ""}


@pytest.mark.parametrize("in_git", [True, False], ids=["git", "no git"])
def test_leans_build_files_are_never_ignored_at_any_depth(tmp_path, caplog, in_git: bool):
    root = _repository(tmp_path) if in_git else tmp_path
    (root / ".gitignore").write_text("*.json\nlakefile.*\nlean-toolchain\npkg/\n")
    files = _write(root, "lake-manifest.json", "lakefile.lean", "lean-toolchain", "pkg/lakefile.toml", "spec.json")
    if in_git:
        git(root, "add", "-f", "lake-manifest.json")

    with caplog.at_level(logging.WARNING, logger="aristotle"):
        assert gitignored_files(root, files) == {Path("spec.json")}
    assert not caplog.messages


def test_lake_contents_are_never_ignored_in_a_git_work_tree(tmp_path):
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text(".lake\n*.c\n")
    files = _write(repo, ".lake/packages/custom/Custom.lean", ".lake/packages/custom/ffi.c", "Main.c")

    assert gitignored_files(repo, files) == {Path("Main.c")}


def test_outside_a_git_work_tree_only_the_top_level_gitignore_applies(tmp_path):
    (tmp_path / ".gitignore").write_text("*.vcd\n")
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / ".gitignore").write_text("*.sv\n")
    files = _write(tmp_path, "wave.vcd", "rtl/fifo.sv")

    assert gitignored_files(tmp_path, files) == {Path("wave.vcd")}


def test_a_folder_that_git_ignores_gets_only_its_own_gitignore(tmp_path):
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text("scratch/\n*.vcd\n")
    project = repo / "scratch" / "demo"
    project.mkdir(parents=True)
    (project / ".gitignore").write_text("*.log\n")
    files = _write(project, "top.sv", "run.log", "wave.vcd")

    assert gitignored_files(project, files) == {Path("run.log")}


def test_in_a_folder_that_git_ignores_a_tracked_file_its_own_gitignore_matches_is_named(tmp_path, caplog):
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text("scratch/\n")
    project = repo / "scratch" / "demo"
    project.mkdir(parents=True)
    (project / ".gitignore").write_text("*.log\n")
    files = _write(project, "top.sv", "kept.log", "run.log")
    git(repo, "add", "-f", "scratch/demo/kept.log")

    with caplog.at_level(logging.WARNING, logger="aristotle"):
        assert gitignored_files(project, files) == {Path("kept.log"), Path("run.log")}
    assert caplog.messages == [
        local_file_utils.held_back_warning({Path("kept.log"): "scratch/demo/.gitignore"}, include_ignored=True)
    ]


def test_a_folder_whose_contents_a_rule_ignores_is_not_itself_ignored(tmp_path):
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text("config/*\n!config/*.example\n")
    files = _write(repo / "config", "database.yml", "database.yml.example")

    assert gitignored_files(repo / "config", files) == {Path("database.yml")}


def test_without_git_only_the_top_level_gitignore_applies(tmp_path, caplog):
    (tmp_path / ".gitignore").write_text("*.vcd\n")
    files = _write(tmp_path, "wave.vcd", "top.sv")

    with patch.object(local_file_utils.subprocess, "run", side_effect=FileNotFoundError("git")):
        assert gitignored_files(tmp_path, files) == {Path("wave.vcd")}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_without_git_in_a_repository_the_fallback_is_a_warning(tmp_path, caplog):
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text("*.vcd\n")
    files = _write(repo, "wave.vcd", "top.sv")

    with patch.object(local_file_utils.subprocess, "run", side_effect=FileNotFoundError("git")):
        assert gitignored_files(repo, files) == {Path("wave.vcd")}
    assert "git is not installed, so only" in caplog.text


def _refusing_rev_parse(real_run, stderr: bytes):
    """subprocess.run, but ``git rev-parse --is-inside-work-tree`` exits 128 with ``stderr``."""

    def run(command, *args, **kwargs):
        if "--is-inside-work-tree" in command:
            return subprocess.CompletedProcess(command, 128, b"", stderr)
        return real_run(command, *args, **kwargs)

    return run


def test_a_repository_git_refuses_falls_back_with_a_warning(tmp_path, caplog):
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text("*.vcd\n")
    (repo / "notes").mkdir()
    (repo / "notes" / ".gitignore").write_text("*.log\n")
    files = _write(repo, "wave.vcd", "notes/debug.log", "top.sv")
    refusal = _refusing_rev_parse(subprocess.run, b"fatal: detected dubious ownership in repository")

    with patch.object(local_file_utils.subprocess, "run", side_effect=refusal):
        assert gitignored_files(repo, files) == {Path("wave.vcd")}
    assert f"git refused {repo}, so only its top-level .gitignore was applied" in caplog.text


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permissions, as a non-root user")
def test_a_project_under_a_folder_that_cannot_be_searched_falls_back_without_crashing(tmp_path, monkeypatch):
    locked = tmp_path / "locked"
    project = locked / "project"
    project.mkdir(parents=True)
    (project / ".gitignore").write_text("*.vcd\n")
    files = _write(project, "wave.vcd", "top.sv")
    monkeypatch.chdir(project)
    locked.chmod(0o000)
    try:
        assert gitignored_files(Path("."), files) == {Path("wave.vcd")}
    finally:
        locked.chmod(0o700)


def test_a_working_directory_that_cannot_be_resolved_falls_back_without_crashing(tmp_path, monkeypatch):
    (tmp_path / ".gitignore").write_text("*.vcd\n")
    files = _write(tmp_path, "wave.vcd", "top.sv")
    monkeypatch.chdir(tmp_path)
    refusal = _refusing_rev_parse(subprocess.run, b"fatal: Unable to read current working directory")

    with (
        patch.object(local_file_utils.subprocess, "run", side_effect=refusal),
        patch.object(os, "getcwd", side_effect=PermissionError(13, "Permission denied")),
    ):
        assert gitignored_files(Path("."), files) == {Path("wave.vcd")}


def test_a_folder_outside_any_repository_falls_back_quietly_in_any_language(tmp_path, caplog):
    (tmp_path / ".gitignore").write_text("*.vcd\n")
    files = _write(tmp_path, "wave.vcd", "top.sv")
    refusal = _refusing_rev_parse(subprocess.run, "fatal: Kein Git-Repository".encode())

    with patch.object(local_file_utils.subprocess, "run", side_effect=refusal):
        assert gitignored_files(tmp_path, files) == {Path("wave.vcd")}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def _failing_check_ignore(real_run):
    """subprocess.run, but ``git check-ignore`` exits 128."""

    def run(command, *args, **kwargs):
        if "check-ignore" in command:
            return subprocess.CompletedProcess(command, 128, b"", b"fatal: index file corrupt")
        return real_run(command, *args, **kwargs)

    return run


def test_git_failing_to_say_what_it_ignores_is_an_error(tmp_path):
    repo = _repository(tmp_path)
    files = _write(repo, "top.sv")

    with (
        patch.object(local_file_utils.subprocess, "run", _failing_check_ignore(subprocess.run)),
        pytest.raises(
            LeanProjectError, match="Could not ask git which files in .* it ignores: fatal: index file corrupt"
        ),
    ):
        gitignored_files(repo, files)


def test_git_failing_to_say_what_it_tracks_is_a_warning_and_the_ignored_files_stay_out(tmp_path, caplog):
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text("*.log\n")
    files = _write(repo, "run.log", "top.sv")
    real_run = subprocess.run

    def run(command, *args, **kwargs):
        if "ls-files" in command:
            return subprocess.CompletedProcess(command, 128, b"", b"fatal: index file corrupt")
        return real_run(command, *args, **kwargs)

    with patch.object(local_file_utils.subprocess, "run", run), caplog.at_level(logging.WARNING, logger="aristotle"):
        assert gitignored_files(repo, files) == {Path("run.log")}
    assert caplog.messages == [
        f"git could not say which files in {repo} it tracks, so none held back is named: fatal: index file corrupt"
    ]


def test_git_stalling_is_an_error(tmp_path):
    repo = _repository(tmp_path)
    files = _write(repo, "top.sv")
    real_run = subprocess.run

    def run(command, *args, **kwargs):
        if "check-ignore" in command:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return real_run(command, *args, **kwargs)

    with (
        patch.object(local_file_utils.subprocess, "run", run),
        pytest.raises(LeanProjectError, match="git check-ignore timed out after 60s"),
    ):
        gitignored_files(repo, files)


@pytest.mark.skipif(os.name == "nt", reason="Windows file names cannot hold ':'")
def test_a_file_name_that_reads_as_pathspec_magic_is_judged_as_a_file_name(tmp_path):
    repo = _repository(tmp_path)
    (repo / ".gitignore").write_text("*.log\n")
    files = _write(repo, ":(exclude)notes.log", ":!run.log", ":/top.sv", "a*b.log")

    assert gitignored_files(repo, files) == {Path(":(exclude)notes.log"), Path(":!run.log"), Path("a*b.log")}


def test_the_archive_name_of_a_path_is_its_bytes_as_utf8():
    assert local_file_utils.archive_name(Path("rtl/naïve.v")) == "rtl/naïve.v"
    latin1 = Path(b"rtl/caf\xe9.v".decode(sys.getfilesystemencoding(), "surrogateescape"))
    assert local_file_utils.archive_name(latin1) == "rtl/caf\\xe9.v"
    assert (local_file_utils.is_utf8_name(Path("rtl/naïve.v")), local_file_utils.is_utf8_name(latin1)) == (True, False)


def test_a_name_the_file_system_encoding_cannot_hold_is_not_utf8_and_is_named_by_its_escape():
    assert local_file_utils.archive_name(Path("rtl/caf\ud800.v")) == "rtl/caf\\ud800.v"
    assert local_file_utils.is_utf8_name(Path("rtl/caf\ud800.v")) is False


def test_an_upload_name_is_the_name_itself_unless_it_is_not_utf8():
    assert local_file_utils.upload_name("café") == "café"
    assert local_file_utils.upload_name(b"r\xe9sum\xe9".decode("utf-8", "surrogateescape")) == "r\\xe9sum\\xe9"
    assert local_file_utils.upload_name("a//b\\c\udce9") == "a//b\\c\\xe9"


def test_text_with_an_undecodable_byte_is_not_utf8_but_any_other_text_is():
    undecodable = b"caf\xe9.v".decode("utf-8", "surrogateescape")
    assert (local_file_utils.is_utf8_text("café.v"), local_file_utils.is_utf8_text(undecodable)) == (True, False)


def test_a_listing_names_every_path_one_to_a_line_past_five():
    assert local_file_utils.listing([Path(f"f{i}.v") for i in range(5)]) == " f0.v, f1.v, f2.v, f3.v, f4.v"
    assert local_file_utils.listing([Path(f"f{i}.v") for i in range(6)]) == "".join(f"\n  f{i}.v" for i in range(6))


def test_naming_rules_leaves_a_repository_an_inherited_git_dir_names_alone(tmp_path, monkeypatch):
    # As in a hook or an alias run in a linked worktree, which sets GIT_DIR: git init there would make it bare.
    (tmp_path / "victim").mkdir()
    (tmp_path / "repo").mkdir()
    victim = _repository(tmp_path / "victim")
    repo = _repository(tmp_path / "repo")
    (repo / ".gitignore").write_text("*.log\n")
    monkeypatch.setenv("GIT_DIR", str(victim / ".git"))

    rules = local_file_utils.ignore_rules(repo, [Path("x.log")])

    monkeypatch.delenv("GIT_DIR")
    bare = subprocess.run(["git", "-C", str(victim), "config", "--bool", "core.bare"], capture_output=True, text=True)
    assert bare.stdout.strip() == "false"
    assert rules == {Path("x.log"): ".gitignore:1: *.log"}


@pytest.mark.parametrize(
    ("ceilings", "found"),
    [
        (None, "outer"),
        ([], "outer"),
        (["{outer}"], None),
        (["{outer}/a"], None),
        (["{outer}/a/"], None),
        (["{outer}/a/b/c"], "outer"),
        (["{tmp}"], "outer"),
        (["{tmp}", "{outer}/a"], None),
        (["outer/a"], "outer"),
        (["{link}"], None),
        (["", "{link}"], "outer"),
        # As written, a Windows path keeps its backslashes, and git compares it with a path in forward slashes.
        (["", "{outer}/a"], "outer" if os.sep == "\\" else None),
    ],
    ids=[
        "no ceiling",
        "an empty list",
        "at the repository",
        "between",
        "between, with a trailing slash",
        "at the folder searched from",
        "above the repository",
        "the nearest of two",
        "a relative entry",
        "a link to the repository",
        "the link as written, after an empty entry",
        "between, as written",
    ],
)
def test_the_repository_found_is_the_one_git_finds_under_git_ceiling_directories(
    tmp_path, monkeypatch, ceilings, found
):
    tmp_path = tmp_path.resolve()
    outer = tmp_path / "outer"
    start = outer / "a" / "b" / "c"
    start.mkdir(parents=True)
    _repository(outer)
    link = tmp_path / "link"
    if any("{link}" in ceiling for ceiling in ceilings or []):
        try:
            link.symlink_to(outer, target_is_directory=True)
        except OSError:
            pytest.skip("this system can't make a symlink")
    if ceilings is None:
        monkeypatch.delenv("GIT_CEILING_DIRECTORIES", raising=False)
    else:
        named = [ceiling.format(tmp=tmp_path, outer=outer, link=link) for ceiling in ceilings]
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", os.pathsep.join(named))

    by_git = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True, text=True)
    expected = None if found is None else tmp_path / found

    assert (Path(by_git.stdout.strip()).resolve() if by_git.returncode == 0 else None) == expected
    assert local_file_utils.enclosing_repository(start) == expected


@pytest.mark.parametrize(
    ("entry", "on_windows", "elsewhere"),
    [
        ("/work", True, True),
        ("\\work", True, False),
        ("C:\\work", True, False),
        ("C:work", True, False),
        ("1:\\work", True, False),
        ("ä:work", True, False),
        ("work", False, False),
    ],
)
def test_a_ceiling_entry_is_absolute_where_git_takes_it_so(entry, on_windows, elsewhere):
    assert local_file_utils._is_absolute(entry) == (on_windows if os.name == "nt" else elsewhere)


@pytest.mark.parametrize("across", ["", "1"], ids=["", "GIT_DISCOVERY_ACROSS_FILESYSTEM"])
def test_the_repository_search_stays_on_the_folders_file_system_as_git_does(tmp_path, monkeypatch, across):
    outer = tmp_path.resolve() / "outer"
    mount = outer / "mnt"
    (mount / "proj").mkdir(parents=True)
    _repository(outer)
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if os.fspath(path) == str(mount) or os.fspath(path).startswith(str(mount) + os.sep):
            return os.stat_result((*result[:2], result.st_dev + 1, *result[3:]))
        return result

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setenv("GIT_DISCOVERY_ACROSS_FILESYSTEM", across)

    found = local_file_utils.enclosing_repository(mount / "proj")

    # git for Windows has no device numbers to compare.
    assert found == (outer if across or os.name == "nt" else None)


def test_a_repository_search_across_file_systems_passes_a_mount_point(tmp_path, monkeypatch):
    outer = tmp_path.resolve() / "outer"
    mount = outer / "mnt"
    (mount / "proj").mkdir(parents=True)
    _repository(outer)
    monkeypatch.setattr(local_file_utils, "_device", lambda folder: int(mount in (folder, *folder.parents)))
    monkeypatch.delenv("GIT_DISCOVERY_ACROSS_FILESYSTEM", raising=False)

    assert local_file_utils.enclosing_repository(mount / "proj", across_filesystems=True) == outer
