"""Tests for what importing aristotlelib does to the importing program, and what the package ships."""

import re
import subprocess
import sys
from importlib import resources
from pathlib import Path

from packaging.version import Version

from aristotlelib._vendor import tomli

REPO = Path(__file__).resolve().parent.parent


def _python(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)


def test_importing_the_library_leaves_the_programs_logging_configuration_to_it():
    run = _python(
        "import logging, aristotlelib\n"
        "logging.basicConfig(format='app: %(levelname)s %(message)s')\n"
        "logging.getLogger('aristotle').warning('from the library')\n"
        "logging.getLogger('app').info('below the default level')\n"
        "print(len(logging.root.handlers), logging.getLevelName(logging.root.level))"
    )

    assert (run.returncode, run.stdout, run.stderr) == (0, "1 WARNING\n", "app: WARNING from the library\n")


def test_the_library_prints_nothing_until_the_program_configures_logging():
    run = _python("import logging, aristotlelib\nlogging.getLogger('aristotle').error('from the library')")

    assert (run.returncode, run.stdout, run.stderr) == (0, "", "")


def test_the_package_is_marked_as_typed():
    assert resources.files("aristotlelib").joinpath("py.typed").is_file()


def _project() -> dict:
    return tomli.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_the_package_page_installs_the_version_it_ships_with():
    project = _project()
    page = (REPO / project["readme"]).read_text(encoding="utf-8")
    pins = re.findall(r"aristotlelib==([^\s\"'`]+)", page)
    wheels = re.findall(r"aristotlelib-([^-\s]+)-py3-none-any\.whl", page)

    assert set(pins + wheels) <= {project["version"]}


def test_a_releases_package_page_installs_its_major_version_over_an_older_one():
    """A bare `aristotlelib` leaves an older install in place, and an unquoted `>` is a shell redirect."""
    project = _project()
    version = Version(project["version"])
    page = (REPO / project["readme"]).read_text(encoding="utf-8")
    installs = re.findall(r"(?:uv tool|pip) install ([^\n`]*)", page)

    assert version.is_prerelease or (installs and set(installs) == {f'"aristotlelib>={version.major}"'})


def test_only_a_pre_release_ships_the_beta_package_page():
    project = _project()

    assert project["readme"] != "README-beta.md" or Version(project["version"]).is_prerelease


def test_the_action_runs_the_image_its_release_pushes():
    """A release pushes its exact version's image before it moves `v3`, so an Action pinned to a release keeps its CLI."""
    action = (REPO / "action.yml").read_text(encoding="utf-8")
    images = re.findall(r"^\s*image:\s*\"?([^\"\s]+)", action, re.MULTILINE)

    assert images == [f"docker://ghcr.io/harmonic-ai/aristotle:{_project()['version']}"]


def test_the_image_build_copies_the_package_page():
    copies = [
        line.split()[1:-1]
        for line in (REPO / "Dockerfile").read_text(encoding="utf-8").splitlines()
        if line.startswith("COPY ")
    ]

    assert any(_project()["readme"] in sources for sources in copies)
