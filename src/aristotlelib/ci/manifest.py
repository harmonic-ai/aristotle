"""Reading and validating ``aristotle.toml``, the properties ``aristotle ci`` checks."""

import dataclasses
import math
import re
from enum import Enum
from pathlib import Path
from typing import Any

# tomli 2.3 on every Python: tomli 2.4 and Python 3.15's tomllib read TOML 1.1, which the image's Python rejects.
from aristotlelib._vendor import tomli as tomllib

MANIFEST_NAME = "aristotle.toml"

MAX_WALL_TIME_SECONDS = 7 * 24 * 60 * 60
"""A week: far beyond any real run, and small enough that the deadline arithmetic stays finite."""

_ID = re.compile(r"[A-Za-z0-9_-]+")
"""A property id: the characters of a TOML bare key."""


class Kind(str, Enum):
    NATURAL_LANGUAGE = "natural_language"
    """A property stated in prose, under ``property``."""

    SVA = "sva"
    """A SystemVerilog assertion in the design, named by ``identifier``."""

    C_FUNCTION_NO_UB = "c_function_no_ub"
    """A C function, named by ``identifier``, that must have no undefined behavior."""

    def __str__(self) -> str:
        return self.value


#: The key each kind is stated under; the other one is rejected.
STATEMENT_KEYS = {
    Kind.NATURAL_LANGUAGE: "property",
    Kind.SVA: "identifier",
    Kind.C_FUNCTION_NO_UB: "identifier",
}

_TOP_LEVEL_KEYS = ("repo_id", "check", "requested_properties")
_CHECK_KEYS = ("enable_proof_repair", "max_wall_time_seconds", "max_cost_dollars")
_PROPERTY_KEYS = ("kind", "property", "identifier", "assumptions", "expect", "property_result_id")

EXPECT_REFUTED = "refuted"
"""The one value of a property's ``expect``: a deliberate counterexample, which passes while refuted."""


@dataclasses.dataclass(frozen=True)
class RequestedProperty:
    """One ``[requested_properties.<id>]`` table."""

    id: str
    kind: Kind
    statement: str
    """``property`` for a natural-language property, else ``identifier``."""
    property_result_id: str | None = None
    assumptions: str | None = None
    """What the property is checked under, such as answers agreed with Aristotle; sent to the run with it."""
    expect_refuted: bool = False
    """``expect = "refuted"``: the property passes while it is refuted, and fails once it is proved."""


@dataclasses.dataclass(frozen=True)
class Manifest:
    path: Path
    properties: list[RequestedProperty]
    """In the order the file lists them."""
    max_wall_time_seconds: int | None = None
    max_cost_dollars: float | None = None
    enable_proof_repair: bool | None = None
    repo_id: str | None = None

    def not_enforced(self) -> list[str]:
        """The keys this manifest sets that Aristotle accepts but does not act on yet."""
        keys = [
            name
            for name, value in (
                ("repo_id", self.repo_id),
                ("check.max_cost_dollars", self.max_cost_dollars),
                ("check.enable_proof_repair", self.enable_proof_repair),
            )
            if value is not None
        ]
        with_result = sum(p.property_result_id is not None for p in self.properties)
        if with_result:
            keys.append(f"property_result_id ({with_result} {'property' if with_result == 1 else 'properties'})")
        return keys


class ManifestError(Exception):
    """Raised when a manifest cannot be read or is invalid; the message names every problem."""

    def __init__(self, message: str, foreign: bool = False) -> None:
        super().__init__(message)
        self.foreign = foreign
        """The file is TOML with no requested_properties, as another tool's file of that name would be."""


def load(path: Path) -> Manifest:
    """Read and validate the manifest at ``path``; raises FileNotFoundError when there is none, else ManifestError."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise
    except OSError as e:
        raise ManifestError(f"Cannot read {path}: {e.strerror or e}") from None
    try:
        # utf-8-sig: a byte-order mark from a Windows editor is not a TOML error worth failing a build on.
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ManifestError(f"{path} is not UTF-8 text.") from None
    try:
        document = tomllib.loads(text)
    # ValueError: a TOMLDecodeError, or an integer past int()'s digit limit; RecursionError: nesting past the stack.
    except (ValueError, RecursionError) as e:
        raise ManifestError(f"{path} is not valid TOML: {e}") from None
    return parse(document, path)


def parse(document: dict[str, Any], path: Path) -> Manifest:
    """Validate a decoded manifest; raises ManifestError listing every problem, each under its dotted key."""
    problems: list[str] = []
    _unknown_keys(document, _TOP_LEVEL_KEYS, "", problems)

    repo_id = _optional_string(document, "repo_id", "repo_id", problems)

    check = document.get("check", {})
    max_wall_time_seconds = max_cost_dollars = enable_proof_repair = None
    if not isinstance(check, dict):
        problems.append("check: must be a table ([check])")
    else:
        _unknown_keys(check, _CHECK_KEYS, "check.", problems)
        max_wall_time_seconds = _wall_time(check, problems)
        max_cost_dollars = _cost(check, problems)
        enable_proof_repair = check.get("enable_proof_repair")
        if enable_proof_repair is not None and not isinstance(enable_proof_repair, bool):
            problems.append("check.enable_proof_repair: must be true or false")
            enable_proof_repair = None

    properties = _properties(document.get("requested_properties"), problems)

    if problems:
        message = f"{path} is not valid:\n" + "\n".join(f"  {problem}" for problem in problems)
        raise ManifestError(message, foreign="requested_properties" not in document)
    return Manifest(
        path=path,
        properties=properties,
        max_wall_time_seconds=max_wall_time_seconds,
        max_cost_dollars=max_cost_dollars,
        enable_proof_repair=enable_proof_repair,
        repo_id=repo_id,
    )


def _unknown_keys(table: dict[str, Any], allowed: tuple[str, ...], prefix: str, problems: list[str]) -> None:
    for key in table:
        if key not in allowed:
            problems.append(f"{prefix}{key}: unknown key; expected one of {', '.join(allowed)}")


def _optional_string(table: dict[str, Any], key: str, name: str, problems: list[str]) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{name}: must be a non-empty string")
        return None
    return value


def _wall_time(check: dict[str, Any], problems: list[str]) -> int | None:
    value = check.get("max_wall_time_seconds")
    if value is None:
        return None
    # bool is an int in Python, and `true` is not a number of seconds.
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_WALL_TIME_SECONDS:
        problems.append(
            f"check.max_wall_time_seconds: must be a whole number of seconds from 1 to {MAX_WALL_TIME_SECONDS}"
        )
        return None
    return value


def _cost(check: dict[str, Any], problems: list[str]) -> float | None:
    value = check.get("max_cost_dollars")
    if value is None:
        return None
    try:
        cost = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    except OverflowError:  # An integer past a float's range.
        cost = None
    if cost is None or not (math.isfinite(cost) and cost > 0):
        problems.append("check.max_cost_dollars: must be a positive number")
        return None
    return cost


def _properties(table: object, problems: list[str]) -> list[RequestedProperty]:
    if table is None:
        problems.append("requested_properties: missing; add at least one [requested_properties.<id>] table")
        return []
    if not isinstance(table, dict):
        problems.append("requested_properties: must be a table of [requested_properties.<id>] tables")
        return []
    if not table:
        problems.append("requested_properties: empty; add at least one [requested_properties.<id>] table")
        return []

    properties: list[RequestedProperty] = []
    for property_id, entry in table.items():
        valid = is_valid_id(property_id)
        # Repr for an id that isn't valid, so that a quoted key with spaces or dots is visible as such.
        name = f"requested_properties.{property_id if valid else repr(property_id)}"
        if not valid:
            problems.append(f"{name}: an id may use only letters, digits, _ and -")
        if not isinstance(entry, dict):
            problems.append(f"{name}: must be a table ([{name}])")
            continue
        # Checked even under an id that isn't valid, so the error names every problem.
        requested = _property(property_id, entry, name, problems)
        if requested is not None and valid:
            properties.append(requested)
    return properties


def _property(property_id: str, entry: dict[str, Any], name: str, problems: list[str]) -> RequestedProperty | None:
    before = len(problems)
    _unknown_keys(entry, _PROPERTY_KEYS, f"{name}.", problems)
    property_result_id = _optional_string(entry, "property_result_id", f"{name}.property_result_id", problems)
    assumptions = _optional_string(entry, "assumptions", f"{name}.assumptions", problems)
    expect = entry.get("expect")
    if expect == "disproved":
        problems.append(
            f'{name}.expect: "disproved" was renamed to "refuted"; replace expect = "disproved" with expect = "refuted"'
        )
    elif expect is not None and expect != EXPECT_REFUTED:
        problems.append(f'{name}.expect: must be "{EXPECT_REFUTED}", the only expectation a property can state')

    raw_kind = entry.get("kind")
    kinds = ", ".join(str(kind) for kind in Kind)
    kind = None
    if raw_kind is None:
        problems.append(f"{name}.kind: missing; expected one of {kinds}")
    else:
        # Compared, as Kind(raw_kind) reprs a value it doesn't know, which can raise as _shown() says.
        kind = next((member for member in Kind if member.value == raw_kind), None)
        if kind is None:
            problems.append(f"{name}.kind: {_shown(raw_kind)} is not one of {kinds}")
    if kind is None:
        # The statement the kind needs is unknown, so check whichever is given.
        for key in ("property", "identifier"):
            if key in entry:
                _statement(entry[key], f"{name}.{key}", key, problems)
        return None

    key = STATEMENT_KEYS[kind]
    other = "identifier" if key == "property" else "property"
    if other in entry:
        problems.append(f"{name}.{other}: not used by kind {kind}; state it under {key}")
    statement = entry.get(key)
    if statement is None:
        problems.append(f"{name}.{key}: missing; kind {kind} needs it")
    else:
        _statement(statement, f"{name}.{key}", key, problems)

    if len(problems) > before or not isinstance(statement, str):
        return None
    return RequestedProperty(
        id=property_id,
        kind=kind,
        statement=statement.strip(),
        property_result_id=property_result_id,
        assumptions=None if assumptions is None else assumptions.strip(),
        expect_refuted=expect == EXPECT_REFUTED,
    )


def _statement(statement: object, label: str, key: str, problems: list[str]) -> None:
    """Note a ``property`` or ``identifier`` that is empty, not a string, or, as an identifier, holds a space."""
    if not isinstance(statement, str) or not statement.strip():
        problems.append(f"{label}: must be a non-empty string")
    elif key == "identifier" and any(c.isspace() for c in statement):
        problems.append(f"{label}: must be a single name, with no spaces")


def _shown(value: object) -> str:
    """``value``'s repr, or its TOML type where repr raises: for an integer past int()'s digit limit, or nesting past
    the stack."""
    try:
        return repr(value)
    except (ValueError, RecursionError):
        return "an integer" if isinstance(value, int) else "an array" if isinstance(value, list) else "a table"


def is_valid_id(property_id: str) -> bool:
    return _ID.fullmatch(property_id) is not None


def _toml_string(value: str) -> str:
    """``value`` as a TOML basic string; a lone surrogate, which no TOML file can hold, becomes U+FFFD."""
    escaped = []
    for char in value:
        code = ord(char)
        if char in '"\\':
            escaped.append("\\" + char)
        elif char == "\n":
            escaped.append("\\n")
        elif char == "\t":
            escaped.append("\\t")
        elif code < 0x20 or code == 0x7F:
            escaped.append(f"\\u{code:04X}")
        elif 0xD800 <= code <= 0xDFFF:
            escaped.append("\ufffd")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def _comment(text: str) -> str:
    """``text`` as one TOML comment line."""
    flat = "".join(" " if ord(c) < 0x20 or ord(c) == 0x7F or 0xD800 <= ord(c) <= 0xDFFF else c for c in text)
    return f"# {flat}".rstrip()


def render(
    properties: list[RequestedProperty],
    header: list[str],
    notes: dict[str, str] | None = None,
    max_wall_time_seconds: int | None = None,
    budget_note: str | None = None,
) -> str:
    """A manifest checking ``properties``, under ``header`` comment lines, each property after its note if any, and
    the time budget after ``budget_note``."""
    lines = [_comment(line) for line in header]
    if max_wall_time_seconds is not None:
        lines.extend(["", "[check]"])
        if budget_note:
            lines.append(_comment(budget_note))
        lines.append(f"max_wall_time_seconds = {max_wall_time_seconds}")
    for requested in properties:
        lines.append("")
        if notes and requested.id in notes:
            lines.append(_comment(notes[requested.id]))
        lines.append(f"[requested_properties.{requested.id}]")
        lines.append(f"kind = {_toml_string(requested.kind.value)}")
        lines.append(f"{STATEMENT_KEYS[requested.kind]} = {_toml_string(requested.statement)}")
        if requested.assumptions is not None:
            lines.append(f"assumptions = {_toml_string(requested.assumptions)}")
        if requested.expect_refuted:
            lines.append(f"expect = {_toml_string(EXPECT_REFUTED)}")
        if requested.property_result_id is not None:
            lines.append(f"property_result_id = {_toml_string(requested.property_result_id)}")
    return "\n".join(lines) + "\n"
