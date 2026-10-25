"""Tests for aristotlelib.ci.manifest."""

import math
from pathlib import Path
from typing import Any

import pytest

from aristotlelib.ci import manifest
from aristotlelib.ci.manifest import Kind, ManifestError, RequestedProperty
from tests.conftest import NEEDS_INT_DIGIT_LIMIT

FULL = """\
repo_id = "example-org/fifo"

[check]
enable_proof_repair = true
max_wall_time_seconds = 3600
max_cost_dollars = 25

[requested_properties.no_overflow]
kind = "sva"
identifier = "fifo_no_overflow"
assumptions = '''
  wr_en is held low while rst is high.
'''
property_result_id = "pr-123"

[requested_properties.parse_header_safe]
kind = "c_function_no_ub"
identifier = "parse_header"

[requested_properties.acks-in-order]
kind = "natural_language"
property = "Every request is acknowledged in the order it was issued."
"""

MINIMAL = """\
[requested_properties.acks]
kind = "natural_language"
property = "Every request is acknowledged."
"""

NL = {"kind": "natural_language", "property": "It holds."}


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / manifest.MANIFEST_NAME
    path.write_text(text, encoding="utf-8")
    return path


def with_property(entry: object, **document: Any) -> dict[str, Any]:
    return {"requested_properties": {"p": entry}, **document}


def parse_error(document: dict[str, Any]) -> str:
    with pytest.raises(ManifestError) as e:
        manifest.parse(document, Path("aristotle.toml"))
    return str(e.value)


class UnshownTable(dict):
    """A table whose repr raises RecursionError, as one nested past the stack's depth does."""

    def __repr__(self) -> str:
        raise RecursionError("maximum recursion depth exceeded while getting the repr of an object")


def test_load_reads_every_key_and_keeps_the_file_order(tmp_path: Path):
    path = write(tmp_path, FULL)

    loaded = manifest.load(path)

    assert loaded == manifest.Manifest(
        path=path,
        properties=[
            RequestedProperty(
                "no_overflow",
                Kind.SVA,
                "fifo_no_overflow",
                property_result_id="pr-123",
                assumptions="wr_en is held low while rst is high.",
            ),
            RequestedProperty("parse_header_safe", Kind.C_FUNCTION_NO_UB, "parse_header"),
            RequestedProperty(
                "acks-in-order", Kind.NATURAL_LANGUAGE, "Every request is acknowledged in the order it was issued."
            ),
        ],
        max_wall_time_seconds=3600,
        max_cost_dollars=25.0,
        enable_proof_repair=True,
        repo_id="example-org/fifo",
    )
    assert isinstance(loaded.max_cost_dollars, float)


def test_load_minimal_manifest_leaves_every_optional_key_unset(tmp_path: Path):
    loaded = manifest.load(write(tmp_path, MINIMAL))

    assert loaded.properties == [RequestedProperty("acks", Kind.NATURAL_LANGUAGE, "Every request is acknowledged.")]
    assert loaded.max_wall_time_seconds is None
    assert loaded.max_cost_dollars is None
    assert loaded.enable_proof_repair is None
    assert loaded.repo_id is None


def test_multi_line_natural_language_property_is_stripped_at_both_ends(tmp_path: Path):
    path = write(
        tmp_path,
        '[requested_properties.acks]\nkind = "natural_language"\nproperty = """\n'
        "  Every request is acknowledged,\n  in order.\n\n"
        '"""\n',
    )

    assert manifest.load(path).properties[0].statement == "Every request is acknowledged,\n  in order."


def test_quoted_id_made_of_bare_key_characters_is_accepted(tmp_path: Path):
    path = write(tmp_path, MINIMAL.replace("[requested_properties.acks]", '[requested_properties."fifo-1_a"]'))

    assert manifest.load(path).properties[0].id == "fifo-1_a"


@pytest.mark.parametrize("seconds", [1, manifest.MAX_WALL_TIME_SECONDS])
def test_max_wall_time_seconds_accepts_the_bounds(seconds: int):
    loaded = manifest.parse(with_property(NL, check={"max_wall_time_seconds": seconds}), Path("aristotle.toml"))

    assert loaded.max_wall_time_seconds == seconds


WALL_TIME = "check.max_wall_time_seconds: must be a whole number of seconds from 1 to 604800"
COST = "check.max_cost_dollars: must be a positive number"
KINDS = "natural_language, sva, c_function_no_ub"


@pytest.mark.parametrize(
    "document, problem",
    [
        pytest.param(
            with_property(NL, extra=1),
            "extra: unknown key; expected one of repo_id, check, requested_properties",
            id="unknown-top-level-key",
        ),
        pytest.param(
            with_property(NL, check={"max_tokens": 10}),
            "check.max_tokens: unknown key; expected one of enable_proof_repair, max_wall_time_seconds, max_cost_dollars",
            id="unknown-check-key",
        ),
        pytest.param(
            with_property({**NL, "description": "x"}),
            "requested_properties.p.description: unknown key; expected one of kind, property, identifier, "
            "assumptions, expect, property_result_id",
            id="unknown-property-key",
        ),
        pytest.param(
            with_property({**NL, "assumptions": " "}),
            "requested_properties.p.assumptions: must be a non-empty string",
            id="blank-assumptions",
        ),
        pytest.param(
            with_property({**NL, "assumptions": ["rst is active high"]}),
            "requested_properties.p.assumptions: must be a non-empty string",
            id="assumptions-not-a-string",
        ),
        pytest.param(
            with_property({**NL, "expect": "proved"}),
            'requested_properties.p.expect: must be "refuted", the only expectation a property can state',
            id="expect-proved",
        ),
        pytest.param(
            with_property({**NL, "expect": "disproved"}),
            'requested_properties.p.expect: "disproved" was renamed to "refuted"; '
            'replace expect = "disproved" with expect = "refuted"',
            id="expect-old-spelling",
        ),
        pytest.param(
            with_property({**NL, "expect": True}),
            'requested_properties.p.expect: must be "refuted", the only expectation a property can state',
            id="expect-not-a-string",
        ),
        pytest.param(
            {"repo_id": "r"},
            "requested_properties: missing; add at least one [requested_properties.<id>] table",
            id="requested-properties-missing",
        ),
        pytest.param(
            {"requested_properties": {}},
            "requested_properties: empty; add at least one [requested_properties.<id>] table",
            id="requested-properties-empty",
        ),
        pytest.param(
            {"requested_properties": [NL]},
            "requested_properties: must be a table of [requested_properties.<id>] tables",
            id="requested-properties-not-a-table",
        ),
        pytest.param(
            with_property("It holds."),
            "requested_properties.p: must be a table ([requested_properties.p])",
            id="property-not-a-table",
        ),
        pytest.param(
            with_property({"property": "It holds."}),
            f"requested_properties.p.kind: missing; expected one of {KINDS}",
            id="kind-missing",
        ),
        pytest.param(
            with_property({"kind": "ltl", "property": "It holds."}),
            f"requested_properties.p.kind: 'ltl' is not one of {KINDS}",
            id="kind-unknown",
        ),
        pytest.param(
            with_property({"kind": 1, "identifier": "x"}),
            f"requested_properties.p.kind: 1 is not one of {KINDS}",
            id="kind-integer",
        ),
        pytest.param(
            with_property({"kind": ["sva"], "identifier": "x"}),
            f"requested_properties.p.kind: ['sva'] is not one of {KINDS}",
            id="kind-unhashable",
        ),
        # A hexadecimal integer reaches parse() at any length, past what int() writes in decimal.
        pytest.param(
            with_property({"kind": 16**4000, "identifier": "x"}),
            f"requested_properties.p.kind: an integer is not one of {KINDS}",
            id="kind-integer-past-the-digit-limit",
            marks=NEEDS_INT_DIGIT_LIMIT,
        ),
        pytest.param(
            with_property({"kind": [16**4000], "identifier": "x"}),
            f"requested_properties.p.kind: an array is not one of {KINDS}",
            id="kind-array-holding-an-integer-past-the-digit-limit",
            marks=NEEDS_INT_DIGIT_LIMIT,
        ),
        pytest.param(
            with_property({"kind": UnshownTable(a=1), "identifier": "x"}),
            f"requested_properties.p.kind: a table is not one of {KINDS}",
            id="kind-nested-past-the-stack",
        ),
        pytest.param(
            with_property({**NL, "identifier": "acks"}),
            "requested_properties.p.identifier: not used by kind natural_language; state it under property",
            id="natural-language-with-identifier",
        ),
        pytest.param(
            with_property({"kind": "sva", "identifier": "x", "property": "It holds."}),
            "requested_properties.p.property: not used by kind sva; state it under identifier",
            id="sva-with-property",
        ),
        pytest.param(
            with_property({"kind": "sva"}),
            "requested_properties.p.identifier: missing; kind sva needs it",
            id="sva-without-identifier",
        ),
        pytest.param(
            with_property({"kind": "natural_language"}),
            "requested_properties.p.property: missing; kind natural_language needs it",
            id="natural-language-without-property",
        ),
        pytest.param(
            with_property({"kind": "c_function_no_ub", "identifier": "parse header"}),
            "requested_properties.p.identifier: must be a single name, with no spaces",
            id="identifier-with-space",
        ),
        pytest.param(
            with_property({"kind": "sva", "identifier": "fifo\tok"}),
            "requested_properties.p.identifier: must be a single name, with no spaces",
            id="identifier-with-tab",
        ),
        pytest.param(
            with_property({"kind": "sva", "identifier": ""}),
            "requested_properties.p.identifier: must be a non-empty string",
            id="identifier-empty",
        ),
        pytest.param(
            with_property({"kind": "natural_language", "property": " \n "}),
            "requested_properties.p.property: must be a non-empty string",
            id="property-blank",
        ),
        pytest.param(
            with_property({"kind": "natural_language", "property": 5}),
            "requested_properties.p.property: must be a non-empty string",
            id="property-not-a-string",
        ),
        pytest.param(
            with_property({**NL, "property_result_id": ""}),
            "requested_properties.p.property_result_id: must be a non-empty string",
            id="property-result-id-empty",
        ),
        pytest.param(with_property(NL, repo_id=42), "repo_id: must be a non-empty string", id="repo-id-not-a-string"),
        pytest.param(with_property(NL, repo_id="  "), "repo_id: must be a non-empty string", id="repo-id-blank"),
        pytest.param(with_property(NL, check=5), "check: must be a table ([check])", id="check-not-a-table"),
        pytest.param(
            with_property(NL, check={"enable_proof_repair": "yes"}),
            "check.enable_proof_repair: must be true or false",
            id="enable-proof-repair-not-a-bool",
        ),
        *(
            pytest.param(with_property(NL, check={"max_wall_time_seconds": value}), WALL_TIME, id=f"wall-time-{name}")
            for name, value in [
                ("zero", 0),
                ("negative", -60),
                ("over-a-week", 604801),
                ("float", 60.0),
                ("true", True),
            ]
        ),
        *(
            pytest.param(with_property(NL, check={"max_cost_dollars": value}), COST, id=f"cost-{name}")
            for name, value in [
                ("zero", 0),
                ("negative", -1.5),
                ("inf", math.inf),
                ("nan", math.nan),
                ("past-a-float", 10**400),
                ("true", True),
                ("string", "10"),
            ]
        ),
    ],
)
def test_invalid_manifest_names_the_problem_under_its_dotted_key(document: dict[str, Any], problem: str):
    message = parse_error(document)

    assert message.startswith("aristotle.toml is not valid:\n")
    assert f"\n  {problem}" in message


@pytest.mark.parametrize("property_id", ["fifo full", "a.b", ""])
def test_quoted_id_outside_bare_key_characters_is_rejected(tmp_path: Path, property_id: str):
    path = write(tmp_path, MINIMAL.replace("[requested_properties.acks]", f'[requested_properties."{property_id}"]'))

    with pytest.raises(ManifestError) as e:
        manifest.load(path)

    assert f"\n  requested_properties.{property_id!r}: an id may use only letters, digits, _ and -" in str(e.value)


def test_every_problem_is_reported_in_one_error(tmp_path: Path):
    path = write(
        tmp_path,
        "extra = 1\n\n[check]\nmax_wall_time_seconds = 0\n\n"
        '[requested_properties.ok]\nkind = "natural_language"\nproperty = "It holds."\n\n'
        '[requested_properties.a]\nidentifier = "x"\nnote = "y"\n\n'
        '[requested_properties.b]\nkind = "sva"\nidentifier = "fifo ok"\n',
    )

    with pytest.raises(ManifestError) as e:
        manifest.load(path)

    assert str(e.value) == (
        f"{path} is not valid:\n"
        "  extra: unknown key; expected one of repo_id, check, requested_properties\n"
        f"  {WALL_TIME}\n"
        "  requested_properties.a.note: unknown key; expected one of kind, property, identifier, assumptions, expect, "
        "property_result_id\n"
        f"  requested_properties.a.kind: missing; expected one of {KINDS}\n"
        "  requested_properties.b.identifier: must be a single name, with no spaces"
    )


@pytest.mark.parametrize(
    ("entry", "problems"),
    [
        (
            {"kind": "ltl", "property": " "},
            [f"kind: 'ltl' is not one of {KINDS}", "property: must be a non-empty string"],
        ),
        (
            {"identifier": "fifo ok"},
            [f"kind: missing; expected one of {KINDS}", "identifier: must be a single name, with no spaces"],
        ),
    ],
    ids=["unknown kind", "missing kind"],
)
def test_a_kind_that_is_missing_or_unknown_hides_no_problem_with_the_statement(
    entry: dict[str, Any], problems: list[str]
):
    message = parse_error(with_property(entry))

    for problem in problems:
        assert f"\n  requested_properties.p.{problem}" in message


def test_an_id_that_is_not_valid_hides_no_problem_with_its_entry(tmp_path: Path):
    path = write(tmp_path, '[requested_properties."fifo full"]\nkind = "ltl"\nproperty = "It holds."\nnote = 1\n')

    with pytest.raises(ManifestError) as e:
        manifest.load(path)

    assert str(e.value) == (
        f"{path} is not valid:\n"
        "  requested_properties.'fifo full': an id may use only letters, digits, _ and -\n"
        "  requested_properties.'fifo full'.note: unknown key; expected one of kind, property, identifier, "
        "assumptions, expect, property_result_id\n"
        f"  requested_properties.'fifo full'.kind: 'ltl' is not one of {KINDS}"
    )


def test_invalid_toml_names_the_file(tmp_path: Path):
    path = write(tmp_path, "[requested_properties.acks\n")

    with pytest.raises(ManifestError) as e:
        manifest.load(path)

    assert str(e.value).startswith(f"{path} is not valid TOML: ")


@NEEDS_INT_DIGIT_LIMIT
def test_a_decimal_integer_past_the_digit_limit_is_invalid_toml(tmp_path: Path):
    path = write(tmp_path, f"repo_id = {'9' * 5000}\n\n{MINIMAL}")

    with pytest.raises(ManifestError) as e:
        manifest.load(path)

    assert str(e.value).startswith(f"{path} is not valid TOML: ")
    assert "integer string conversion" in str(e.value)


@pytest.mark.parametrize(
    "line",
    [
        "repo_id = " + "[" * 5000 + "]" * 5000,
        "repo_id = " + "{ a = " * 5000 + "1" + " }" * 5000,
        "repo_id" + ".a" * 5000 + " = 1",
    ],
    ids=["arrays", "inline tables", "dotted key"],
)
def test_nesting_past_the_stack_is_invalid_toml(tmp_path: Path, line: str):
    path = write(tmp_path, f"{line}\n\n{MINIMAL}")

    with pytest.raises(ManifestError) as e:
        manifest.load(path)

    assert str(e.value).startswith(f"{path} is not valid TOML: ")


def test_a_kind_nested_as_deep_as_toml_reads_is_named(tmp_path: Path):
    """A dotted key of up to 1000 parts nests tables that deep, past repr's depth on Python 3.10 and 3.11."""
    path = write(tmp_path, '[requested_properties.p]\nidentifier = "x"\nkind' + ".a" * 990 + " = 1\n")

    with pytest.raises(ManifestError) as e:
        manifest.load(path)

    assert "\n  requested_properties.p.kind: " in str(e.value)


def test_manifests_are_read_with_tomli_on_every_python():
    # Python 3.15's tomllib reads TOML 1.1, which the image's Python rejects; tomli 2.3 doesn't, on any Python.
    assert manifest.tomllib.__name__ == "aristotlelib._vendor.tomli"


def test_toml_1_1_is_rejected_on_every_python_as_the_image_rejects_it(tmp_path: Path):
    # A trailing comma in an inline table is TOML 1.1, which tomli 2.4 and Python 3.15's tomllib read.
    path = write(tmp_path, f"check = {{ max_wall_time_seconds = 60, }}\n\n{MINIMAL}")

    with pytest.raises(ManifestError) as e:
        manifest.load(path)

    assert str(e.value).startswith(f"{path} is not valid TOML: ")


def test_bytes_that_are_not_utf8_are_rejected(tmp_path: Path):
    path = tmp_path / manifest.MANIFEST_NAME
    path.write_bytes(b'repo_id = "caf\xe9"\n' + MINIMAL.encode())

    with pytest.raises(ManifestError) as e:
        manifest.load(path)

    assert str(e.value) == f"{path} is not UTF-8 text."


def test_utf8_byte_order_mark_is_accepted(tmp_path: Path):
    path = tmp_path / manifest.MANIFEST_NAME
    path.write_bytes(b"\xef\xbb\xbf" + MINIMAL.encode())

    assert [p.id for p in manifest.load(path).properties] == ["acks"]


def test_directory_raises_manifest_error(tmp_path: Path):
    with pytest.raises(ManifestError) as e:
        manifest.load(tmp_path)

    assert str(e.value).startswith(f"Cannot read {tmp_path}: ")


def test_missing_file_raises_file_not_found(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        manifest.load(tmp_path / manifest.MANIFEST_NAME)


def test_not_enforced_lists_the_accepted_keys_aristotle_ignores(tmp_path: Path):
    loaded = manifest.load(write(tmp_path, FULL))

    assert loaded.not_enforced() == [
        "repo_id",
        "check.max_cost_dollars",
        "check.enable_proof_repair",
        "property_result_id (1 property)",
    ]


def test_not_enforced_counts_properties_with_a_result_id_and_lists_false_proof_repair(tmp_path: Path):
    text = FULL.replace('repo_id = "example-org/fifo"\n', "").replace("max_cost_dollars = 25\n", "")
    text = text.replace("enable_proof_repair = true", "enable_proof_repair = false")
    text = text.replace('identifier = "parse_header"', 'identifier = "parse_header"\nproperty_result_id = "pr-456"')

    assert manifest.load(write(tmp_path, text)).not_enforced() == [
        "check.enable_proof_repair",
        "property_result_id (2 properties)",
    ]


def test_not_enforced_is_empty_when_only_enforced_keys_are_set(tmp_path: Path):
    loaded = manifest.load(write(tmp_path, "[check]\nmax_wall_time_seconds = 600\n\n" + MINIMAL))

    assert loaded.not_enforced() == []


def _round_trip(text: str) -> manifest.Manifest:
    return manifest.parse(manifest.tomllib.loads(text), Path("aristotle.toml"))


@pytest.mark.parametrize(
    "statement",
    [
        'Say "hi" and mean it.',
        "A back\\slash, C:\\path\\x",
        "Two\nlines\twith a tab",
        "Control \x00\x01\x08\x0b\x0c\x0e\x1f\x7f characters",
        "A carriage\r\nreturn",
        "Unicode: ∀ x ≤ y, 💡",
        "'''triple''' and \"\"\"quotes\"\"\"",
    ],
)
def test_any_statement_round_trips(statement: str):
    text = manifest.render([RequestedProperty("p", Kind.NATURAL_LANGUAGE, statement)], header=[])

    assert _round_trip(text).properties[0].statement == statement.strip()


def test_a_lone_surrogate_becomes_the_replacement_character():
    text = manifest.render([RequestedProperty("p", Kind.NATURAL_LANGUAGE, "bad \ud800 char")], header=[])

    assert _round_trip(text).properties[0].statement == "bad \ufffd char"
    text.encode("utf-8")


def test_every_kind_is_written_under_its_own_key():
    properties = [
        RequestedProperty("nl", Kind.NATURAL_LANGUAGE, "It holds."),
        RequestedProperty("assertion", Kind.SVA, "top.u_fifo.assert_no_overflow"),
        RequestedProperty("push", Kind.C_FUNCTION_NO_UB, "fifo_push", property_result_id="r-1"),
        RequestedProperty("assumed", Kind.SVA, "top.assert_ack", assumptions='rst is "active high"\nfor one cycle'),
        RequestedProperty("demo", Kind.NATURAL_LANGUAGE, "The FIFO drops a write when full.", expect_refuted=True),
    ]

    parsed = _round_trip(manifest.render(properties, header=[]))

    assert parsed.properties == properties


def test_header_notes_and_check_are_comments_and_a_table():
    text = manifest.render(
        [RequestedProperty("p", Kind.NATURAL_LANGUAGE, "It holds.")],
        header=["First line.", "Second\nline."],
        notes={"p": "Fifo.p (rtl/fifo.v)\nwith a newline"},
        max_wall_time_seconds=1800,
    )

    assert text.startswith("# First line.\n# Second line.\n")
    assert "# Fifo.p (rtl/fifo.v) with a newline\n[requested_properties.p]\n" in text
    assert text.endswith("\n")
    assert _round_trip(text).max_wall_time_seconds == 1800


def test_no_check_table_without_a_budget():
    text = manifest.render([RequestedProperty("p", Kind.NATURAL_LANGUAGE, "It holds.")], header=[])

    assert "[check]" not in text
    assert _round_trip(text).max_wall_time_seconds is None


@pytest.mark.parametrize(("property_id", "valid"), [("a_b-1", True), ("a.b", False), ("a b", False), ("", False)])
def test_is_valid_id(property_id: str, valid: bool):
    assert manifest.is_valid_id(property_id) is valid
