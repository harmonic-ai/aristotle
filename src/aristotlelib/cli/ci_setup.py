"""``aristotle ci setup`` — write the CI manifest from the properties Aristotle proposes for a repository."""

import argparse
import asyncio
import functools
import logging
import math
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path, PurePath, PurePosixPath

from aristotlelib import local_file_utils
from aristotlelib.agent_task import INCOMPLETE_ENDING, TERMINAL_STATUSES, AgentTask, TaskStatus
from aristotlelib.api_request import AristotleAPIError
from aristotlelib.ci import context, location, manifest, naming, runner, scope
from aristotlelib.ci.results import proved, refuted, status_label
from aristotlelib.cli.api_action import (
    PROJECT_REFERENCE_HELP,
    APIAction,
    exit_code_for,
    fetch_tasks,
    project_from_reference,
)
from aristotlelib.cli.command.rename import project_name_argument
from aristotlelib.cli.question_flow import can_prompt
from aristotlelib.declaration import Declaration, DeclarationStatus, DeclarationType
from aristotlelib.event import EventType
from aristotlelib.exit_codes import (
    EXIT_INFRASTRUCTURE_ERROR,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    EXIT_VERIFICATION_FAILED,
)
from aristotlelib.project import AgentQuestionsSetting, Project
from aristotlelib.results import setter
from aristotlelib.results import unchecked as never_checked
from aristotlelib.shell import shell_hint

logger = logging.getLogger("aristotle")

SETUP_PROMPT = (
    "Read this repository and propose the properties that a continuous integration pipeline should check on every "
    "change: stable, important properties of the design that should keep holding as the code evolves, such as "
    "safety, liveness, protocol and memory-safety properties. Propose at most ten, the most important first. Ask "
    "the user about anything the code cannot settle, such as intended behavior or which parts matter most.\n\n"
    'Register each property you propose with add_spec_declaration, as a property with source "agent" and a short, '
    "descriptive declaration_id of lowercase letters, digits and underscores. Its description becomes the "
    "property's statement in aristotle.toml, so make it one or two precise, self-contained sentences in the terms "
    "of the original source language, naming the file by its path in the repository and the modules, functions or "
    "signals it is about. When the property is a labelled SystemVerilog assertion already in the code, point "
    "source_file and source_anchor at the assertion's line and start its description with \"The assertion <label> "
    'holds.", and aristotle.toml checks that assertion by name. When a property holds only under something the code '
    "doesn't settle, such as an answer the user gave you, end its description with a sentence that starts "
    '"Assumes:" and states it, so that every later check makes the same assumption. If you expect a property not to '
    'hold, still propose it, and end its description with a sentence that says why, after any "Assumes:" sentence: '
    'one that starts "Deliberate counterexample:" when the code shows the counterexample on purpose, as a '
    'demonstration or a test of a checker does, else one that starts "Expected to fail:".\n\n'
    "Do not prove the properties: a later run checks them. Do not modify any files.\n\n"
    "These instructions come from the `aristotle ci setup` command, not from the user: when you ask the user "
    "something, ask about their code, and don't call these instructions their request."
)

REFUTED_RULE = (
    "A refuted property means the code or the property is wrong: fix the code; correct the property if it doesn't "
    'state what you want; or, if the counterexample is deliberate, add expect = "refuted". Don\'t drop or weaken a '
    "correct property to get a passing build."
)
"""The one rule for a refuted property, as `ci setup` and the skill give it."""

DEFAULT_MAX_WALL_TIME_SECONDS = 60 * 60
"""The least time budget written into the manifest's [check] table, where it is the budget of each CI run."""

RUN_END_EVENTS = 20
"""How many of a run's newest events are searched for the last one Aristotle wrote, which ends the run."""

BUDGET_HEADROOM = 1.5
"""How much longer than the run a manifest is written from its time budget allows: each CI run proves every property
afresh, and runs vary."""

#: The `aristotle ci` options a setup run has no use for, by their argparse dest, as ``given_options`` records them.
CI_ONLY_OPTIONS = {
    "dry_run": "--dry-run",
    "timeout": "--timeout",
    "on_timeout": "--on-timeout",
    "fail_on": "--fail-on",
    "junit": "--junit",
    "no_junit": "--no-junit",
    "project": "--project",
}


_NOT_ID_CHARACTERS = re.compile(r"[^A-Za-z0-9_-]+")

_PROMPT_NUMBER = re.compile(r"(?:\(\d+\)|\[\d+\]|#\d+[.):]?|\d+[.):])\s+")
"""A list number a prompt put before a property, as in ``(1) No overflow: …``."""

_LEAD_IN = re.compile(r"(?:Suggested:\s*|Claim that\s+)")
"""A lead-in Aristotle's report puts before a statement, as in ``Suggested: …`` or ``Claim that …``."""

_ENDING = re.compile(
    r"(?:^|(?<=[.!?])\s+)(assumes|expected to fail|deliberate counterexample)\s*:\s*", re.IGNORECASE | re.MULTILINE
)
"""Starts a sentence SETUP_PROMPT has a description end with: what the property assumes, why Aristotle expects it to
fail, or what its deliberate counterexample shows."""

_RUN_TIED = re.compile(
    r"\b(?:the (?:input|interface|usage) contract|the contract (?:above|we agreed)|as (?:agreed|discussed)"
    r"|as you (?:said|asked|confirmed)|(?:your|the user's) answers?|this (?:run|conversation))\b",
    re.IGNORECASE,
)
"""Wording that may refer to the conversation of the run a statement was copied from."""

_FIXED_VALUE = re.compile(
    r"(?:`|\b)([A-Z][A-Z0-9_]+)`?\s*(?:==?|is|of|fixed (?:at|to)|set to)\s*`?(\d{1,18})\b(?!`?(?:\.\d|['-]))`?"
)
"""A statement giving an upper-case name, such as a Verilog parameter or a C macro, a whole number, either in
backticks or not; not one that goes on as a decimal, a Verilog literal or a compound such as 8-bit."""

_SETTING = re.compile(
    r"[#`][ \t]*define[ \t]+(?P<macro>[A-Z][A-Z0-9_]+)[ \t]+(?P<macro_value>[^\n]*)"
    r"|\.(?P<override>[A-Z][A-Z0-9_]+)[ \t]*\((?P<override_value>[^)\n]{0,100})\)"
    r"|\b(?:const|static|let|def)[ \t]+(?:mut[ \t]+)?(?P<typed>[A-Z][A-Z0-9_]+)[ \t]*:[^=;\n]{0,100}=(?!=)"
    r"(?P<typed_value>[^;,)\n]*)"
    r"|\b(?P<assigned>[A-Z][A-Z0-9_]+)[ \t]*=(?!=)(?P<assigned_value>[^;,)\n]*)"
)
"""Code setting an upper-case name on one line: a C or Verilog macro, a Verilog parameter override, a declaration
with a type, as in Rust or Lean, or an assignment."""

_INTEGER = re.compile(
    r"\(*\s*(?:(?:\d+\s*)?'[sS]?(?P<base>[dDhHoObB])\s*|0(?P<prefix>[xXoObB]))?(?P<digits>[0-9a-fA-F][0-9a-fA-F_]*)"
    r"(?:[uUlL]+|_?[iu](?:8|16|32|64|128|size))?\s*\)*"
)
"""A whole number as code writes one, in parentheses or not: decimal, hexadecimal, octal or binary, as a Verilog
literal, sized or not, or with a C or Rust type suffix."""

_BASES = {"d": 10, "h": 16, "x": 16, "o": 8, "b": 2}

_VERILOG_SUFFIXES = (".v", ".sv", ".vh", ".svh")

_COMMENT_OR_STRING = re.compile(r'"(?:\\.|[^"\\\n])*(?:"|(?=\n)|\\?\Z)|//[^\n]*|/\*.*?(?:\*/|\Z)', re.DOTALL)
"""A Verilog string, which stays, or a comment, which is blanked."""

_MODULE_HEADER = re.compile(r"\s*(?:macro)?module\s+(?:(?:automatic|static)\s+)?([A-Za-z_][A-Za-z0-9_$]*)")

_ENDMODULE = re.compile(r"(?:^|;)\s*endmodule\b")

_LABELLED_ASSERTION = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_$]*)\s*:\s*assert\s+property\b")


def _blanked(text: str) -> str:
    """``text`` with its ``//`` and ``/* */`` comments blanked in place; strings stay."""
    return _COMMENT_OR_STRING.sub(lambda m: m[0] if m[0].startswith('"') else re.sub(r"[^\n]", " ", m[0]), text)


def _without_attributes(line: str) -> str:
    """``line`` less each ``(*`` up to the nearest ``*)`` after it, in linear time; a ``(*`` with none after it stays."""
    kept = []
    start = 0
    while (opening := line.find("(*", start)) != -1 and (closing := line.find("*)", opening + 2)) != -1:
        kept.append(line[start:opening])
        start = closing + 2
    kept.append(line[start:])
    return "".join(kept)


def _code(lines: tuple[str, ...]) -> list[str]:
    """``lines`` with comments blanked in place, and attributes and a byte-order mark removed."""
    return [_without_attributes(line) for line in _blanked("\n".join(lines).lstrip("\ufeff")).split("\n")]


def _module(declaration: Declaration, root: Path) -> tuple[str | None, str | None]:
    """The Verilog module around the declaration's line in its file under ``root``, or the file's only module when
    the line is unknown, and that line with comments blanked; Nones for any other file."""
    path = declaration.source_file
    if not path or not path.lower().endswith(_VERILOG_SUFFIXES):
        return None, None
    lines = location.lines_in_checkout(path, root)
    if lines is None:
        return None, None
    code = _code(lines)
    line = location.line_in_checkout(declaration, root)
    if line is None:
        modules = {header.group(1) for text in code if (header := _MODULE_HEADER.match(text))}
        return (modules.pop() if len(modules) == 1 else None), None
    module = None
    for number, text in enumerate(code[:line], start=1):
        if header := _MODULE_HEADER.match(text):
            module = header.group(1)
        # The property's own line can't close its module, even a one-line one.
        if number < line and _ENDMODULE.search(text):
            module = None
    return module, code[line - 1] if line <= len(code) else None


def _assertion(
    declaration: Declaration, module: str | None, description: str, code_line: str | None = None
) -> str | None:
    """The manifest identifier of the labelled SystemVerilog assertion the declaration is anchored on, read from
    ``code_line`` when known, when its name ends with the label or its description opens by saying the assertion,
    perhaps "in module m", holds; else None."""
    if not (declaration.source_file or "").lower().endswith(_VERILOG_SUFFIXES):
        return None
    labelled = _LABELLED_ASSERTION.match(code_line if code_line is not None else declaration.source_anchor or "")
    if labelled is None:
        return None
    label = labelled.group(1)
    holds = (
        rf"(?i:(?:the\s+)?(?:systemverilog\s+)?(?:assertion\s+)?)`?(?:[A-Za-z_][A-Za-z0-9_$]*\.)*{re.escape(label)}`?"
        r"(?:\s+(?i:in|of)\s+(?i:module\s+)?`?[A-Za-z0-9_$./]+`?){0,2}\s+(?i:holds)\s*(?:[.:]|$)"
    )
    if declaration.name.rsplit(".", 1)[-1] != label and not re.match(holds, description, re.MULTILINE):
        return None
    return f"{module}.{label}" if module else label


def _mentions(statement: str, name: str, inside_paths: bool = False) -> bool:
    """Whether ``statement`` has ``name`` as a whole identifier or path, not as part of a longer one, and with
    ``inside_paths`` also as a path's last part."""
    before = r"A-Za-z0-9_$.-" if inside_paths else r"A-Za-z0-9_$./\\-"
    after = r"A-Za-z0-9_$" if inside_paths else r"A-Za-z0-9_$/\\"
    return re.search(rf"(?<![{before}]){re.escape(name)}(?![{after}]|\.[A-Za-z0-9_])", statement) is not None


def _located(statement: str, path: str | None, module: str | None) -> str:
    """``statement``, ending with the module and file it is about where it doesn't name them."""
    if not path:
        return statement
    names_file = _mentions(statement, path, inside_paths=True) or _mentions(
        statement, PurePosixPath(path).name, inside_paths=True
    )
    names_module = module is None or _mentions(statement, module)
    if names_file and names_module:
        return statement
    if names_module:
        return f"{statement} (In `{path}`.)"
    if names_file:
        return f"{statement} (Module `{module}`.)"
    return f"{statement} (Module `{module}` in `{path}`.)"


def _endings(description: str) -> tuple[str, str | None, str | None, str | None]:
    """``description`` without the sentences SETUP_PROMPT has it end with, then what they say the property assumes,
    why Aristotle expects it to fail and what its deliberate counterexample shows, in any order: each runs from the
    first start of its kind to the start of the next, and is None where there is none."""
    starts: dict[str, re.Match[str]] = {}
    for match in _ENDING.finditer(description):
        starts.setdefault(match[1].lower(), match)
    ordered = sorted(starts.values(), key=lambda match: match.start())
    ends = {
        match[1].lower(): description[match.end() : following.start() if following else None].strip()
        for match, following in zip(ordered, [*ordered[1:], None])
    }
    head = description[: ordered[0].start()] if ordered else description
    return head.strip(), ends.get("assumes"), ends.get("expected to fail"), ends.get("deliberate counterexample")


def _integer(text: str) -> int | None:
    """The whole number ``text`` is, as _INTEGER reads one; None for anything else, such as an expression."""
    match = _INTEGER.fullmatch(text.strip())
    if match is None:
        return None
    try:
        return int(match["digits"].replace("_", ""), _BASES[(match["base"] or match["prefix"] or "d").lower()])
    except ValueError:
        return None


def _source_text(path: Path) -> str:
    """The text of the regular file at ``path``, undecodable bytes replaced; empty for a symlink, any other kind of
    file, one over location.MAX_SEARCHED_FILE_BYTES, or one that can't be read."""
    try:
        if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > location.MAX_SEARCHED_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _code_values(root: Path) -> dict[str, dict[int | None, str]]:
    """The values the tracked source files under ``root`` set upper-case names to, outside comments, each with the
    first file that sets it; None for one that isn't a whole number. Each file is read alone, as _source_text reads
    it."""
    values: dict[str, dict[int | None, str]] = {}
    try:
        files = scope.tracked_source_files(root)
    except scope.ScopeError:
        return values
    for path in files:
        for match in _SETTING.finditer(_blanked(_source_text(root / path))):
            name = next(group for group in ("macro", "override", "typed", "assigned") if match[group])
            values.setdefault(match[name], {}).setdefault(_integer(match[f"{name}_value"]), path.as_posix())
    return values


def _run_tied(
    requested: manifest.RequestedProperty, code_values: Callable[[], dict[str, dict[int | None, str]]]
) -> str | None:
    """Why ``requested`` may depend on the run it was copied from: wording in its statement or assumptions that may
    come from that run's conversation, or a whole number they give a name, quoted, where the code, as ``code_values``
    reads it, sets that name only to others, which it names with where; None when neither shows."""
    fields = ((requested.statement, "it says"), (requested.assumptions or "", "its assumptions say"))
    for text, says in fields:
        if wording := _RUN_TIED.search(text):
            return f'{says} "{wording.group()}", which may refer to the conversation of the run it was copied from'
    for text, says in fields:
        for match in _FIXED_VALUE.finditer(text):
            name, value = match.groups()
            given = code_values().get(name, {})
            numbers = sorted(number for number in given if number is not None)
            # A value that isn't a whole number could be any.
            if numbers and len(numbers) == len(given) and int(value) not in numbers:
                where = " or ".join(f"{number} in {given[number]}" for number in numbers)
                return f'{says} "{match.group()}", and the code sets {name} only to {where}'
    return None


def _proposals(
    declarations: list[Declaration], root: Path, tasks: Sequence[AgentTask], copied: bool = False
) -> tuple[list[manifest.RequestedProperty], dict[str, str], list[str], list[str], list[str], list[str]]:
    """The registered properties as manifest entries in the order proposed, a note on each, and the ids of those
    already refuted and not deliberately, of those Aristotle expects to fail, of those written with
    ``expect = "refuted"``: deliberate counterexamples, by Aristotle's account, that no checked proof settles, and,
    when ``copied`` from another run, of those whose statements may depend on that run. A verdict the project's
    ``tasks``, newest first, show Aristotle never checked, as unchecked() judges it, is noted unchecked."""
    proposed = sorted(
        (
            d
            for d in declarations
            if d.declaration_type == DeclarationType.PROPERTY and d.status != DeclarationStatus.REMOVED
        ),
        key=lambda d: (d.created_at, d.declaration_id),
    )
    properties: list[manifest.RequestedProperty] = []
    notes: dict[str, str] = {}
    already_refuted: list[str] = []
    expected_to_fail: list[str] = []
    deliberate: list[str] = []
    run_tied: list[str] = []
    # Read once, and only if a copied statement fixes a value.
    code_values = functools.cache(lambda: _code_values(root))
    assertions: set[str] = set()
    for declaration in proposed:
        base = declaration.declaration_id
        if not manifest.is_valid_id(base):
            base = _NOT_ID_CHARACTERS.sub("_", base).strip("_") or "property"
        property_id, suffix = base, 2
        while property_id in notes:
            property_id, suffix = f"{base}_{suffix}", suffix + 1
        description, assumptions, expectation, shows = _endings((declaration.description or "").strip())
        assumptions = assumptions or None
        if number := _PROMPT_NUMBER.match(description):
            description = description[number.end() :]
        if lead_in := _LEAD_IN.match(description):
            description = description[lead_in.end() :]
            if number := _PROMPT_NUMBER.match(description):
                description = description[number.end() :]
        module, code_line = _module(declaration, root)
        identifier = _assertion(declaration, module, description, code_line)
        unchecked = never_checked(declaration, tasks)
        # A deliberate counterexample stands unless a checked proof settles the property, or Aristotle also
        # expects the property to fail, which may be a bug.
        expect_refuted = shows is not None and expectation is None and not (proved(declaration) and not unchecked)
        if identifier is not None and identifier not in assertions:
            assertions.add(identifier)
            properties.append(
                manifest.RequestedProperty(
                    property_id,
                    manifest.Kind.SVA,
                    identifier,
                    assumptions=assumptions,
                    expect_refuted=expect_refuted,
                )
            )
        else:
            identifier = None
            statement = _located(description or declaration.name, declaration.source_file, module)
            properties.append(
                manifest.RequestedProperty(
                    property_id,
                    manifest.Kind.NATURAL_LANGUAGE,
                    statement,
                    assumptions=assumptions,
                    expect_refuted=expect_refuted,
                )
            )
        note = f"{declaration.name} ({declaration.source_file})" if declaration.source_file else declaration.name
        if unchecked:
            note += f"; {status_label(declaration.status)} when proposed, unchecked"
        elif proved(declaration):
            note += "; proved when proposed"
        elif refuted(declaration) and expect_refuted:
            note += f"; {status_label(declaration.status)} when proposed, a deliberate counterexample"
            note += f": {shows}" if shows else ""
        elif refuted(declaration):
            note += f"; {status_label(declaration.status)} when proposed, so CI fails on it as it stands"
            already_refuted.append(property_id)
        settled = not unchecked and (proved(declaration) or refuted(declaration))
        if expect_refuted:
            deliberate.append(property_id)
            if not settled:
                note += "; a deliberate counterexample, Aristotle says" + (f": {shows}" if shows else "")
        # An expectation stands unless a checked verdict settles the property.
        elif expectation is not None and not settled:
            note += (
                f"; Aristotle expects it to be refuted: {expectation}"
                if expectation
                else "; Aristotle expects it to be refuted"
            )
            if shows:
                note = f"{note.removesuffix('.')}; Aristotle also calls it a deliberate counterexample: {shows}"
            expected_to_fail.append(property_id)
        if identifier is not None and description:
            note = f"{note.removesuffix('.')}; Aristotle describes it as: {description}"
        if copied and (why := _run_tied(properties[-1], code_values)):
            note = f"{note.removesuffix('.')}; check it before you commit: {why}"
            run_tied.append(property_id)
        notes[property_id] = note
    return properties, notes, already_refuted, expected_to_fail, deliberate, run_tied


def _create(path: Path, text: str) -> None:
    """Create ``path`` holding ``text``, removing it again if the write fails; FileExistsError if it exists."""
    f = open(path, "x", encoding="utf-8", newline="\n")
    try:
        with f:
            f.write(text)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _name_fits(directory: Path, name: str) -> bool:
    """Whether ``directory``'s file system takes a file named ``name``: within the limit it reports, or 255 UTF-16 code
    units on Windows; True where no limit is known."""
    if os.name == "nt":
        return len(name.encode("utf-16-le", "surrogatepass")) // 2 <= 255
    try:
        longest = os.pathconf(directory, "PC_NAME_MAX")
    except (AttributeError, OSError, ValueError):
        return True
    # Some FUSE file systems report 0 for a limit they don't know.
    return longest <= 0 or len(os.fsencode(name)) <= longest


def _write_file(path: Path, text: str, replace: bool) -> None:
    """Write ``text`` to ``path`` whole or not at all; without ``replace``, FileExistsError if it exists."""
    if not replace:
        _create(path, text)
        return
    temporary = path.with_name(f".aristotle-{uuid.uuid4().hex[:8]}.tmp")
    _create(temporary, text)
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _path_arg(path: Path | str) -> str:
    """``path`` as an argument that is never an option, with forward slashes, which every shell and git take."""
    word = PurePath(path).as_posix()
    return f"./{word}" if word.startswith("-") else word


def _missing(path: Path) -> bool:
    """Whether nothing is at ``path``; False for a path that can't be looked up, such as one too long to name."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _in_repository(directory: Path) -> bool:
    """Whether ``directory`` is in a git work tree, or, when git is missing or refuses the checkout, under a .git."""
    try:
        return scope._run_git(directory, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except scope.ScopeError:
        return local_file_utils.enclosing_repository(directory) is not None


async def _run_seconds(task: AgentTask) -> float | None:
    """How long ``task`` ran, queued time included, from its creation to the newest of its last RUN_END_EVENTS events
    that Aristotle wrote; None if there is none or they can't be fetched.

    Not to its last update, which rating, renaming or opening a pull request moves later, nor to a question the user
    asked about the finished run, which the web app once recorded as an event on it."""
    try:
        events, _ = await task.get_events(limit=RUN_END_EVENTS)
    except AristotleAPIError as e:
        logger.debug(f"Could not fetch the run's events, so the time budget is the default: {e}")
        return None
    end = next((event.created_at for event in events if event.event_type != EventType.QUESTION), None)
    if end is None:
        return None
    return max((end - task.created_at).total_seconds(), 0.0)


async def _setting_run(
    declarations: list[Declaration], tasks: Sequence[AgentTask]
) -> tuple[AgentTask | None, float | None]:
    """Of the runs that last updated the properties in ``declarations``, by the project's ``tasks``, newest first, as
    setter() attributes them, the one that ran longest, and how long, as _run_seconds measures it. When it can attribute
    none, no run, and the newest task's length; None for a length unknown."""
    setters: dict[str, AgentTask] = {}
    for declaration in _copied(declarations):
        if (task := setter(declaration, tasks)) is not None:
            setters.setdefault(task.agent_task_id, task)
    if not setters:
        return None, await _run_seconds(tasks[0]) if tasks else None
    longest, took = None, None
    for task in setters.values():
        seconds = await _run_seconds(task)
        if seconds is not None and (took is None or seconds > took):
            longest, took = task, seconds
    return longest, took


def _copied(declarations: list[Declaration]) -> list[Declaration]:
    """The properties a manifest takes from ``declarations``."""
    return [
        d
        for d in declarations
        if d.declaration_type == DeclarationType.PROPERTY and d.status != DeclarationStatus.REMOVED
    ]


def _minutes(seconds: float) -> int:
    """``seconds`` to the nearest minute, and at least one."""
    return max(round(seconds / 60), 1)


def _budget_seconds(took: float | None) -> int:
    """The time budget to write: BUDGET_HEADROOM times ``took``, the source run's length, in whole minutes, between
    DEFAULT_MAX_WALL_TIME_SECONDS and the manifest's maximum; the default when the length is unknown."""
    if took is None:
        return DEFAULT_MAX_WALL_TIME_SECONDS
    budget = math.ceil(took * BUDGET_HEADROOM / 60) * 60
    return min(max(budget, DEFAULT_MAX_WALL_TIME_SECONDS), manifest.MAX_WALL_TIME_SECONDS)


def _shown(path: Path) -> str:
    """``path`` relative to the working directory when it is under it."""
    try:
        return str(path.absolute().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


class SetupAction(APIAction):
    """Have Aristotle propose properties for the repository, asking its questions, and write them to the manifest."""

    _root: Path
    _path: Path
    _target: list[str]
    """The --directory and --manifest a printed command repeats."""

    @property
    def command_name(self) -> str:
        return "setup"

    @property
    def description(self) -> str:
        return (
            f"Have Aristotle propose properties for this repository, answering its questions as it goes, and write "
            f"them to {manifest.MANIFEST_NAME} for you to review and commit."
        )

    @classmethod
    def priority(cls) -> int:
        return 0

    def add_action_arguments(self) -> None:
        # SUPPRESS: `aristotle ci --manifest x setup` keeps the value the ci parser read.
        self.parser.add_argument(
            "--manifest",
            type=Path,
            default=argparse.SUPPRESS,
            help=f"Where to write the manifest (default: {manifest.MANIFEST_NAME} in the directory).",
        )
        self.parser.add_argument(
            "--directory",
            type=Path,
            default=argparse.SUPPRESS,
            help=(
                "The repository, or the subproject in it, to propose properties for (default: the nearest folder up "
                f"to the repository's top level that holds {manifest.MANIFEST_NAME}, as `aristotle ci` reads, else "
                "this directory; with --manifest, this directory)."
            ),
        )
        self.parser.add_argument(
            "--name",
            type=project_name_argument,
            default=argparse.SUPPRESS,
            help=(
                "Name for the setup run's project (default: the repository and the directory in it, then "
                "'ci setup', and the branch and commit)."
            ),
        )
        self.parser.add_argument(
            "--force",
            action="store_true",
            help="Replace an existing manifest.",
        )
        self.parser.add_argument(
            "--from",
            dest="from_project",
            metavar="PROJECT",
            help=(
                f"Write the manifest from an existing project's properties instead of starting a run: "
                f"{PROJECT_REFERENCE_HELP}. Needs no terminal."
            ),
        )

    def preflight(self, args: argparse.Namespace) -> int | None:
        if args.from_project is None and (named := os.environ.get("ARISTOTLE_FROM")):
            # Said, as a variable left set would otherwise write, or with --force replace, the manifest unannounced.
            logger.info(f"Writing the manifest from the project ARISTOTLE_FROM names: {named}")
            args.from_project = named
        supplied: set[str] = getattr(args, "given_options", set())
        if ignored := [flag for dest, flag in CI_ONLY_OPTIONS.items() if dest in supplied]:
            self.parser.error(
                f"{', '.join(ignored)} {'is an option' if len(ignored) == 1 else 'are options'} of `aristotle ci`, "
                "not of `aristotle ci setup`."
            )
        directory = getattr(args, "directory", None)
        given = getattr(args, "manifest", None)
        # os.path, not pathlib: before 3.14, Path.is_dir() and resolve() raise for a path too long or out of reach.
        if directory is not None and not os.path.isdir(directory):
            logger.error(f"--directory: {directory} is not a directory.")
            return EXIT_USAGE_ERROR
        if directory is not None and not os.access(directory, os.R_OK | os.X_OK):
            logger.error(f"--directory: {directory} can't be read.")
            return EXIT_USAGE_ERROR
        here = Path.cwd().resolve()
        if directory is not None:
            self._root = directory.resolve()
        elif given is not None:
            self._root = here
        else:
            # The manifest `aristotle ci` reads from here, which one written here would hide.
            nearest = scope.local_root(here, manifest.MANIFEST_NAME)
            self._root = nearest if os.path.isfile(nearest / manifest.MANIFEST_NAME) else here
        self._path = given or self._root / manifest.MANIFEST_NAME
        # A folder found above this one is named, so that a printed command reaches it from here alone.
        found = (
            os.path.relpath(self._root, here) if self._root != here and directory is None and given is None else None
        )
        self._target = (
            (["--directory", _path_arg(directory)] if directory else [])
            + (["--directory", _path_arg(found)] if found else [])
            + (["--manifest", _path_arg(given)] if given else [])
        )
        if os.path.isdir(self._path):
            logger.error(
                f"{_shown(self._path)} is a directory. Name the manifest file, as {_shown(self._path / manifest.MANIFEST_NAME)}."
            )
            return EXIT_USAGE_ERROR
        if os.path.lexists(self._path) and not args.force:
            if found:
                logger.error(
                    f"{_shown(self._path)} already exists, and `aristotle ci` run here reads it. Pass --force to "
                    "replace it, or --directory . to set up this folder on its own."
                )
            else:
                logger.error(f"{_shown(self._path)} already exists. Pass --force to replace it.")
            return EXIT_USAGE_ERROR
        if not os.path.isdir(self._path.parent):
            why = "does not exist; make it first" if _missing(self._path.parent) else "is not a directory"
            logger.error(f"Cannot write {self._path}: {self._path.parent} {why}.")
            return EXIT_USAGE_ERROR
        # Checked before the run, which would otherwise end with a manifest it can't write.
        if not os.access(self._path.parent, os.W_OK | os.X_OK):
            logger.error(f"Cannot write {self._path}: {self._path.parent} is not writable.")
            return EXIT_USAGE_ERROR
        if not _name_fits(self._path.parent, self._path.name):
            logger.error(f"Cannot write {self._path}: its name is longer than the file system takes.")
            return EXIT_USAGE_ERROR
        if args.from_project is None and not can_prompt():
            logger.error(
                "`aristotle ci setup` asks questions as it goes, so it runs only at a terminal. "
                "To write the manifest from an existing project instead, pass --from <project>."
            )
            return EXIT_USAGE_ERROR
        # After every refusal, so that it is said only when the manifest will be replaced.
        if found:
            logger.warning(
                f"Setting up {_shown(self._root)}, whose {manifest.MANIFEST_NAME} `aristotle ci` run here reads, and "
                "replacing it. Pass --directory . to set up this folder on its own."
            )
        return None

    async def run_action(self, args: argparse.Namespace) -> int:
        if args.from_project is not None:
            source = await project_from_reference(args.from_project)
            tasks, _ = await source.get_tasks(limit=1)
            if tasks and tasks[0].status not in TERMINAL_STATUSES:
                logger.error(
                    f"Project {source.project_id}'s run is not known to have finished ({tasks[0].status.name}), "
                    "so its properties may still change. Run this again once it has."
                )
                return EXIT_USAGE_ERROR
            declarations = await source.get_declarations()
            try:
                # Back to every copied property's last update, verdict or not, so that each is attributed to its run.
                history = await fetch_tasks(
                    source.project_id,
                    _copied(declarations),
                    tasks[0] if tasks else None,
                    strict=True,
                    every_update=True,
                )
            except Exception as e:
                # Its verdicts would be noted checked or not on a guess, in a file meant to be committed.
                logger.error(
                    f"Project {source.project_id}'s tasks couldn't be listed, so its proofs can't be told apart from "
                    f"ones Aristotle never checked, and nothing was written: {e}\n"
                    + shell_hint("Once Aristotle answers, run it again with", self._from(source, args.force))
                )
                return exit_code_for(e) if isinstance(e, AristotleAPIError) else EXIT_INFRASTRUCTURE_ERROR
            # Not the newest task's length: a short follow-up after the run that set the verdicts would cut the budget.
            run, took = await _setting_run(declarations, history)
            return self._write(declarations, source, args.force, history, took, copied=True, run=run)

        try:
            scope_result = scope.resolve_checkout(self._root)
        except scope.ScopeError as e:
            logger.error(str(e))
            return EXIT_USAGE_ERROR
        scope.warn_held_back(self._root, scope_result)

        name = getattr(args, "name", None) or naming.default_name(
            self._root, context.detect(), scope_result.prefix, "ci setup"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            tarball_path = Path(temp_dir) / "aristotle-setup.tar.gz"
            upload = runner.build_tarball(self._root, scope_result.upload_files, tarball_path, None)
            local_file_utils.log_excluded_secrets(upload.excluded)
            if not upload.uploaded:
                logger.error("No file could be uploaded; the warnings above say why.")
                return EXIT_USAGE_ERROR
            size = tarball_path.stat().st_size
            try:
                runner.check_size(self._root, len(upload.uploaded), size)
            except runner.UnusableUploadError as e:
                logger.error(str(e))
                return EXIT_USAGE_ERROR
            logger.info(f"Uploading {runner.describe_upload(len(upload.uploaded), size)}.")
            upload_name = runner.new_upload_name(self._root)
            try:
                project = await Project.create(
                    prompt=SETUP_PROMPT,
                    tar_file_path=tarball_path,
                    public_file_path=f"{upload_name}.tar.gz",
                    agent_questions_setting=AgentQuestionsSetting.TIMEOUT_15_MIN,
                )
            except (KeyboardInterrupt, asyncio.CancelledError):
                await runner.interrupted_create(upload_name, cancel=True)
                raise
        project.log_created()
        await runner.name_project(project, name)

        later = shell_hint(
            f"Once it finishes, write {_shown(self._path)} from it with", self._from(project, args.force)
        )
        try:
            tasks, _ = await project.get_tasks(limit=1)
            if not tasks:
                logger.error(f"Project {project.project_id} was created but has no task to wait on. {later}")
                return EXIT_INFRASTRUCTURE_ERROR
            task = tasks[0]
            try:
                # A follow-up wouldn't write the manifest: --from does, below.
                await task.wait_for_completion(
                    interactive=True, title=project.description, resumable=False, _incomplete_ending=False
                )
            except (KeyboardInterrupt, asyncio.CancelledError):
                # The wait's own message already says how to follow or cancel the run.
                logger.info(later)
                raise
            if task.status not in TERMINAL_STATUSES:
                logger.error(
                    f"Stopped following the setup run before it was known to have finished ({task.status.name}), "
                    f"so nothing was written. {later}"
                )
                return EXIT_INFRASTRUCTURE_ERROR
            if task.status in (TaskStatus.FAILED, TaskStatus.CANCELED):
                again = f"Run `aristotle ci setup` again, or write {_shown(self._path)} from what it did with"
                logger.error(
                    "The setup run did not finish, so nothing was written. "
                    + shell_hint(again, self._from(project, args.force))
                )
                return EXIT_INFRASTRUCTURE_ERROR
            declarations = await project.get_declarations()
            took = await _run_seconds(task)
        except AristotleAPIError as e:
            logger.error(f"{e}\nNothing was written. {later}")
            return exit_code_for(e)

        if task.status == TaskStatus.COMPLETE_WITH_ERRORS:
            # A run that leaves its properties unproved, as SETUP_PROMPT asks, ends COMPLETE_WITH_ERRORS.
            logger.info(
                "Aristotle proposed the properties; setup leaves proving them to `aristotle ci`."
                if _copied(declarations)
                else INCOMPLETE_ENDING
            )
        return self._write(declarations, project, args.force, [task], took)

    def _ci_flags(self) -> list[str]:
        """The --directory and --manifest that `aristotle ci` needs at the top level of the checkout to read the
        manifest as setup wrote it."""
        top = scope.local_root(self._path.parent.resolve())
        try:
            flags = [] if self._root == top else ["--directory", _path_arg(self._root.relative_to(top))]
            if "--manifest" in self._target:
                flags += ["--manifest", _path_arg(os.path.relpath(self._path.parent.resolve() / self._path.name, top))]
        except ValueError:
            # The folder set up is outside the manifest's checkout, or on another drive: no one command reads both.
            return []
        return flags

    def _from(self, project: Project, force: bool) -> list[str]:
        """The command that writes this manifest from ``project``."""
        return [
            "aristotle",
            "ci",
            "setup",
            "--from",
            project.project_id,
            *self._target,
            *(["--force"] if force else []),
        ]

    def _write(
        self,
        declarations: list[Declaration],
        project: Project,
        force: bool,
        tasks: Sequence[AgentTask],
        took: float | None,
        copied: bool = False,
        run: AgentTask | None = None,
    ) -> int:
        """Write the proposed properties to the manifest, with a time budget from ``took``, the length of the run they
        come from, ``run`` when named, and say what to do next; ``tasks`` as in _proposals, and ``copied`` from another
        run's properties, flag the statements that may depend on that run."""
        if run is None:
            ran, noted = "the run it was written from", "The run this was written from"
        else:
            ran = f"the run that set its properties, task {run.agent_task_id},"
            noted = f"The run that set these properties, task {run.agent_task_id},"
        properties, notes, already_refuted, expected_to_fail, deliberate, run_tied = _proposals(
            declarations, self._root, tasks, copied=copied
        )
        if not properties:
            logger.error(f"Aristotle proposed no properties, so nothing was written. See {project.web_url()}")
            return EXIT_VERIFICATION_FAILED

        text = manifest.render(
            properties,
            header=[
                "Properties `aristotle ci` checks on every run.",
                f"Written by `aristotle ci setup` from {project.web_url()}",
                "Review each one before you commit this file: CI fails on a refuted property unless it is marked "
                'expect = "refuted", and on a proved one that is.',
            ],
            notes=notes,
            max_wall_time_seconds=(budget := _budget_seconds(took)),
            budget_note=(
                f"{noted} took {_minutes(took)} min. The budget allows half as long again, and at least an hour."
                if took is not None
                else None
            ),
        )
        shown = _shown(self._path)
        try:
            _write_file(self._path, text, replace=force)
        except FileExistsError:
            logger.error(
                f"{shown} was created while setup ran, so it was not replaced. "
                + shell_hint("To replace it", self._from(project, force=True))
            )
            return EXIT_USAGE_ERROR
        except OSError as e:
            logger.error(
                f"Could not write {shown}: {e.strerror or e}\n"
                + shell_hint("Once that is fixed", self._from(project, force))
            )
            return EXIT_INFRASTRUCTURE_ERROR

        # git runs in the manifest's directory unless that is the working directory.
        here = self._path.parent.resolve() == Path.cwd().resolve()
        git = ["git"] if here else ["git", "-C", _path_arg(_shown(self._path.parent.resolve()))]
        count = len(properties)
        lines = [f"Wrote {shown} with {count} {'property' if count == 1 else 'properties'}:"]
        for p in properties:
            kind = "" if p.kind is manifest.Kind.NATURAL_LANGUAGE else f" ({p.kind})"
            assumes = f" Assumes: {p.assumptions}" if p.assumptions else ""
            lines.append(f"  {p.id}{kind}: {p.statement}{assumes}")
        if took is not None:
            longer = " Give the CI job longer than that." if budget > DEFAULT_MAX_WALL_TIME_SECONDS else ""
            lines.append(f"Its time budget is {budget // 60} minutes; {ran} took {_minutes(took)}.{longer}")
        lines.append(
            shell_hint(
                "Review them and edit the file as you see fit, then check it without starting a run",
                ["aristotle", "ci", "--dry-run", *self._target],
            )
        )
        if _in_repository(self._path.parent):
            lines.append(
                shell_hint(
                    "and commit it",
                    [*git, "add", "--", _path_arg(self._path.name)],
                    [*git, "commit", "-m", f"Add {self._path.name}"],
                )
            )
            if (flags := self._ci_flags()) and flags != self._target:
                lines.append(
                    shell_hint(
                        "In CI, `aristotle ci` starts from the checkout's top level, so the job reads this manifest with",
                        ["aristotle", "ci", *flags],
                    )
                )
        else:
            lines.append(f"{shown} is outside any git repository, so `aristotle ci` reads it only with --manifest.")
        logger.info("\n".join(lines))
        if already_refuted:
            logger.warning(
                f"Aristotle already refuted {', '.join(already_refuted)}, so `aristotle ci` fails on "
                f"{'it' if len(already_refuted) == 1 else 'them'} as it stands. {REFUTED_RULE}"
            )
        if expected_to_fail:
            logger.warning(
                f"Aristotle expects {', '.join(expected_to_fail)} to be refuted, so `aristotle ci` would fail on "
                f"{'it' if len(expected_to_fail) == 1 else 'them'}. {REFUTED_RULE}"
            )
        if deliberate:
            one = len(deliberate) == 1
            logger.warning(
                f"Aristotle says {', '.join(deliberate)} {'shows a counterexample' if one else 'show counterexamples'} "
                f'on purpose, so {"it is" if one else "they are"} written with expect = "refuted": `aristotle ci` '
                "passes a deliberate counterexample while it stays refuted, and fails it once it is proved. Make "
                "sure each one is deliberate: if not, remove that line."
            )
        if run_tied:
            logger.warning(
                f"Check {', '.join(run_tied)} before you commit: --from copies a run's properties as they are, and "
                "these may depend on that run. Each one's comment says why."
            )
        return EXIT_SUCCESS
