"""The agent skill in skills/aristotle: its frontmatter, its links, the plugin marketplace, and every command it names.

Also the setup guide for coding agents, docs/agent-setup.md, which installs the skill and the CLI.
"""

import argparse
import json
import re
import shlex
from pathlib import Path

import httpx
import pytest
from packaging.version import Version

from aristotlelib import api_request
from aristotlelib._vendor import tomli
from aristotlelib.cli.controller import action_classes

REPO = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO / "skills" / "aristotle"
SKILL_FILES = sorted(SKILL_DIR.rglob("*.md"))
GUIDE = REPO / "docs" / "agent-setup.md"
# The public copy of this repository, which the plugin and the clone commands install the skill from.
MIRROR = "https://github.com/harmonic-ai/aristotle"
PLACEHOLDER = re.compile(r"<[A-Za-z][^<>]*>")
SHELL_OPERATOR = re.compile(r"[;&|<>]+")
# The CLI's name as a word, not the end of a path such as the skill's folder in a PowerShell copy, which shlex can't split.
COMMAND_WORD = re.compile(r"(?<![\w/\\.-])aristotle(?![\w.-])")
# What a plugin's root loads besides its skills. The plugin's root is this repository, so any of these would ship to
# every user of the plugin, and a plugin.json would conflict with the marketplace entry's skills.
PLUGIN_COMPONENTS = [
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".cursor-plugin/plugin.json",
    "plugin.json",
    ".lsp.json",
    ".mcp.json",
    "agents",
    "bin",
    "commands",
    "hooks",
    "monitors",
    "output-styles",
    "settings.json",
    "themes",
    "workflows",
]


def _frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    return dict(line.split(": ", 1) for line in lines[1:end])


class _Rejected(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """Raises argparse's error message instead of exiting; subparsers inherit the class."""

    def error(self, message):
        raise _Rejected(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="aristotle")
    parser.add_argument("--version", action="version", version="aristotlelib")
    parser.add_argument("--api-key")
    subparsers = parser.add_subparsers(dest="command")
    for cls in action_classes():
        cls(subparsers).add_arguments()
    return parser


def _snippets(text: str) -> list[str]:
    """Shell snippets: fenced code blocks with continuations joined, and inline code spans outside them."""
    fence = r"^ *```[a-z]*\n(.*?)^ *```"
    blocks = re.findall(fence, text, flags=re.MULTILINE | re.DOTALL)
    prose = re.sub(fence, "", text, flags=re.MULTILINE | re.DOTALL)
    lines = [line for block in blocks for line in block.replace("\\\n", " ").splitlines()]
    return lines + re.findall(r"`([^`\n]+)`", prose)


def _commands(text: str) -> list[list[str]]:
    """The arguments after ``aristotle`` in each snippet that runs a named command."""
    commands = []
    for snippet in _snippets(text):
        if not COMMAND_WORD.search(snippet):
            continue
        lexer = shlex.shlex(PLACEHOLDER.sub("ID", snippet), posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        segment: list[str] = []
        for token in [*lexer, ";"]:
            if not SHELL_OPERATOR.fullmatch(token):
                segment.append(token)
                continue
            if "aristotle" in segment:
                args = segment[segment.index("aristotle") + 1 :]
                if args and re.fullmatch(r"-.*|[a-z]+", args[0]):
                    commands.append(args)
            segment = []
    return commands


def _rejection(parser: argparse.ArgumentParser, args: list[str]) -> str | None:
    """argparse's error for ``args``, or None when they parse (``--help`` and ``--version`` included)."""
    try:
        parser.parse_args(args)
    except _Rejected as e:
        return str(e)
    except SystemExit:
        pass
    return None


def test_the_frontmatter_names_the_skill_and_says_when_to_use_it():
    meta = _frontmatter((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))

    assert meta["name"] == SKILL_DIR.name
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", meta["name"])
    assert 0 < len(meta["description"]) <= 1024


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda path: path.name)
def test_every_relative_link_resolves(path: Path):
    links = re.findall(r"\]\(([^)#:]+)(?:#[^)]*)?\)", path.read_text(encoding="utf-8"))

    assert [link for link in links if not (path.parent / link).is_file()] == []


def _plugin() -> dict:
    (plugin,) = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))["plugins"]
    return plugin


def test_the_marketplace_plugin_installs_the_skill_from_this_repository():
    plugin = _plugin()

    # The whole repository at a tag: Claude Code's git-subdir checkout fails on git before 2.36.
    assert {key: value for key, value in plugin["source"].items() if key != "ref"} == {
        "source": "url",
        "url": MIRROR + ".git",
    }
    assert [(REPO / skill).resolve() for skill in plugin["skills"]] == [SKILL_DIR]


def test_the_plugin_and_the_copy_install_the_skill_from_this_versions_release():
    """The version PR moves all of them to its tag, so the skill a user installs describes the CLI that release installs."""
    version = tomli.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    clones = [re.findall(r"git clone .*", path.read_text(encoding="utf-8")) for path in (REPO / "README.md", GUIDE)]

    # The version is how Claude Code tells a release's plugin from one it installed from main at the tag's commit.
    assert (_plugin()["version"], _plugin()["source"]["ref"]) == (version, f"v{version}")
    assert [len(found) for found in clones] == [0, 2]
    assert all(f" --branch v{version} {MIRROR}.git " in clone for found in clones for clone in found)


def test_the_skill_says_3_0_0_is_not_on_pypi_only_while_this_is_a_pre_release():
    """Its install advice, and the guide's, asks for the release candidate Harmonic gave the user; 3.0.0's version PR
    has to rewrite both."""
    version = tomli.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if not Version(version).is_prerelease:
        for path in (SKILL_DIR / "SKILL.md", GUIDE):
            # Whitespace folded, so a rewrap that splits the phrase across lines can't hide it.
            assert "isn't on PyPI yet" not in " ".join(path.read_text(encoding="utf-8").split())


@pytest.mark.parametrize("path", [SKILL_DIR / "SKILL.md", GUIDE], ids=lambda path: path.name)
def test_an_agent_asks_how_to_log_in_before_it_meets_the_login_command(path: Path):
    """An agent carries out the steps as it reads them, and a headless one must end on the question instead."""
    text = " ".join(path.read_text(encoding="utf-8").split()).lower()
    ask = text.find("start a login only when they answer")

    assert 0 <= ask < text.index("aristotle login --json")


def test_the_skill_and_the_guide_route_a_refusal_on_the_error_line_the_cli_prints(monkeypatch):
    """A refusal without Aristotle's format exits 3, 4 or 5 by where it was refused, so they go by its Error: line."""
    monkeypatch.setattr(api_request, "getproxies", dict)
    url = httpx.URL("https://aristotle.harmonic.fun/api/v3/project")
    lines = [api_request._refusal_message("403 Forbidden", url, known_proxy=known) for known in (False, True)]
    prefixes = [line.split(" (", 1)[0] for line in lines]

    assert prefixes == ["Error: request refused", "Error: request blocked by a proxy"]
    for path in (SKILL_DIR / "SKILL.md", GUIDE):
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert [prefix for prefix in prefixes if f"`{prefix}`" not in text] == []


def test_the_repository_holds_no_plugin_component_but_the_skill():
    assert [path for path in PLUGIN_COMPONENTS if (REPO / path).exists()] == []


@pytest.mark.parametrize("path", [*SKILL_FILES, GUIDE], ids=lambda path: path.name)
def test_every_command_the_skill_names_parses(path: Path):
    commands = _commands(path.read_text(encoding="utf-8"))
    parser = _parser()
    rejected = []
    for args in commands:
        error = _rejection(parser, args)
        # A bare command named in prose, such as `aristotle properties`, may leave out required arguments.
        bare = all(re.fullmatch(r"[a-z]+", arg) for arg in args)
        if error and not (bare and error.startswith("the following arguments are required")):
            rejected.append(f"{' '.join(args)}: {error}")

    assert commands
    assert rejected == []
