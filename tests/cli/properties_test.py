"""Tests for aristotlelib.cli.command.properties."""

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest

from aristotlelib.agent_task import TaskStatus
from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.api_action import fetch_tasks
from aristotlelib.cli.command.properties import (
    NAME_WIDTH,
    STATUS_WIDTH,
    PropertiesAction,
    shared_namespaces,
    shorten_name,
    shown_names,
    truncate_middle,
)
from aristotlelib.cli.controller import main
from aristotlelib.declaration import (
    Declaration,
    DeclarationEvidence,
    DeclarationFailureReason,
    DeclarationSource,
    DeclarationStatus,
    DeclarationType,
)
from aristotlelib.project import Project
from aristotlelib.results import DeclarationPage, DeclarationRecord
from tests.cli.conftest import UNKNOWN_EVIDENCE_NAME, unknown_evidence_page

LIST_ALL = "aristotlelib.cli.command.properties.Declaration.list_all_for_project"
TERMINAL_WIDTH = "aristotlelib.cli.command.properties.terminal_width"
SHORTENED = "Some names are shortened to fit the terminal"
NOW = datetime.now(timezone.utc)
"""One clock reading for every declaration, so those a test leaves at the default times keep their order."""


@pytest.fixture(autouse=True)
def tasks():
    """Patch the fetch of the project's tasks; none, as for a project without one, unless a test sets them."""
    with patch("aristotlelib.cli.command.properties.fetch_tasks", AsyncMock(return_value=[])) as mock:
        yield mock


def _action() -> PropertiesAction:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = PropertiesAction(subparsers)
    action.add_action_arguments()
    return action


def _declaration(name: str = "Spec.Foo", **overrides) -> Declaration:
    fields = dict(
        project_id="project-123",
        declaration_id=name.lower(),
        declaration_type=DeclarationType.PROPERTY,
        source=DeclarationSource.AGENT,
        name=name,
        status=DeclarationStatus.PROVED,
        filename="Spec/Property.lean",
        created_at=NOW - timedelta(days=2),
        last_updated=NOW - timedelta(hours=1),
    )
    fields.update(overrides)
    if fields["status"] == DeclarationStatus.REFUTED:
        fields.setdefault("evidence", DeclarationEvidence.NEGATION_PROVED)
    return Declaration(**fields)


async def _run(argv: list[str], everything=()):
    """Run the command with the read of every page patched to return ``everything``."""
    action = _action()
    args = action.parser.parse_args(argv)
    with patch(LIST_ALL, return_value=list(everything)) as mock_all:
        # Preflight, then the action, as run() does.
        assert action.preflight(args) is None
        await action.run_action(args)
    return mock_all


def _rows(out: str) -> list[str]:
    """The table body: everything after the header and the rule."""
    lines = out.splitlines()
    return lines[2:] if len(lines) > 2 else []


# --- every page is read, then filtered, sorted and cut to --limit here ---


async def test_the_default_reads_every_page_and_shows_the_30_most_recently_updated(capsys, caplog):
    caplog.set_level(logging.INFO)
    # Oldest first, so only the sort here puts the newest at the top.
    everything = [_declaration(f"Spec.P{i:02}", last_updated=NOW - timedelta(minutes=i)) for i in reversed(range(45))]

    mock_all = await _run(["project-123"], everything=everything)

    mock_all.assert_called_once_with("project-123", declaration_type=DeclarationType.PROPERTY)
    out = capsys.readouterr().out
    assert out.startswith("NAME")
    assert "STATUS" in out and "EVIDENCE" in out and "CREATED BY" in out and "UPDATED" in out and "LOCATION" in out
    assert [row.split()[0] for row in _rows(out)] == [f"Spec.P{i:02}" for i in range(30)]
    assert "15 more not shown. Raise --limit to see more." in caplog.text
    assert "Pagination key" not in caplog.text


@pytest.mark.parametrize("flags", [[], ["--status", "PROVED"]], ids=["unfiltered", "filtered"])
async def test_limit_takes_any_number_since_the_rows_are_picked_here(flags, capsys, caplog):
    caplog.set_level(logging.INFO)
    everything = [_declaration(f"Spec.P{i}") for i in range(150)]

    await _run(["project-123", *flags, "--limit", "5"], everything=everything)
    assert len(_rows(capsys.readouterr().out)) == 5
    assert "145 more not shown. Raise --limit to see more." in caplog.text
    caplog.clear()

    # 100 is the server's page size, not a ceiling.
    await _run(["project-123", *flags, "--limit", "150"], everything=everything)
    assert len(_rows(capsys.readouterr().out)) == 150
    assert "not shown" not in caplog.text


async def test_kind_definition_is_a_server_side_filter(caplog):
    caplog.set_level(logging.INFO)

    mock_all = await _run(["project-123", "--kind", "definition"])

    mock_all.assert_called_once_with("project-123", declaration_type=DeclarationType.DEFINITION)
    assert "No definitions found." in caplog.text


async def test_empty_table_says_so_and_prints_nothing(capsys, caplog):
    caplog.set_level(logging.INFO)

    await _run(["project-123"])

    assert capsys.readouterr().out == ""
    assert "No properties found." in caplog.text


# --- filters and sorts ---


async def test_refuted_filter_drains_every_page_and_keeps_all_evidence(capsys):
    everything = [
        _declaration("Spec.Proved", status=DeclarationStatus.PROVED),
        _declaration("Spec.Refuted", status=DeclarationStatus.REFUTED),
        _declaration("Spec.Cex", status=DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE),
        _declaration("Spec.Both", status=DeclarationStatus.REFUTED, evidence=DeclarationEvidence.BOTH),
        _declaration("Spec.Open", status=DeclarationStatus.IN_PROGRESS),
    ]

    mock_all = await _run(["project-123", "--status", "REFUTED"], everything=everything)

    mock_all.assert_called_once_with("project-123", declaration_type=DeclarationType.PROPERTY)
    assert {row.split()[0] for row in _rows(capsys.readouterr().out)} == {"Spec.Refuted", "Spec.Cex", "Spec.Both"}


@pytest.mark.parametrize("status", ["PROVED", "REFUTED", "IN_PROGRESS", "FAILED"])
async def test_each_status_filter_keeps_only_that_status(status: str, capsys):
    everything = [_declaration(f"Spec.{s.name}", status=s) for s in DeclarationStatus if s != DeclarationStatus.UNKNOWN]

    await _run(["project-123", "--status", status], everything=everything)

    assert [row.split()[0] for row in _rows(capsys.readouterr().out)] == [f"Spec.{DeclarationStatus[status].name}"]


@pytest.mark.parametrize("json_flag", [[], ["--json"]], ids=["table", "--json"])
async def test_status_repeated_keeps_properties_with_any_of_them(json_flag, capsys):
    everything = [_declaration(f"Spec.{s.name}", status=s) for s in DeclarationStatus if s != DeclarationStatus.UNKNOWN]

    await _run(
        ["project-123", "--status", "REFUTED", "--status", "PROVED", "--sort", "name", *json_flag],
        everything=everything,
    )

    out = capsys.readouterr().out
    names = (
        [d["name"] for d in json.loads(out)["declarations"]] if json_flag else [row.split()[0] for row in _rows(out)]
    )
    assert names == ["Spec.PROVED", "Spec.REFUTED"]


async def test_refutation_rows_show_evidence_separately_without_printing_the_message(capsys):
    message = "Refuted by a counterexample. See the VCD trace in Artifacts."
    declarations = [
        _declaration(f"Spec.{name}", status=DeclarationStatus.REFUTED, evidence=evidence, evidence_message=message)
        for name, evidence in [
            ("Cex", DeclarationEvidence.COUNTEREXAMPLE),
            ("Formal", DeclarationEvidence.NEGATION_PROVED),
            ("Both", DeclarationEvidence.BOTH),
        ]
    ]
    declarations.append(_declaration("Spec.Proved", evidence=DeclarationEvidence.COUNTEREXAMPLE))

    await _run(["project-123"], everything=declarations)

    out = capsys.readouterr().out
    assert message not in out
    header = out.splitlines()[0]
    labels = [row[header.index("STATUS") : header.index("EVIDENCE")].strip() for row in _rows(out)]
    assert labels == ["REFUTED", "REFUTED", "REFUTED", "PROVED"]
    evidence = [row[header.index("EVIDENCE") : header.index("CREATED BY")].strip() for row in _rows(out)]
    assert evidence == ["COUNTEREXAMPLE", "NEGATION_PROVED", "BOTH", "-"]


@pytest.mark.parametrize("json_flag", [[], ["--json"]], ids=["table", "--json"])
def test_an_evidence_kind_this_version_does_not_know_reads_as_unknown(json_flag, api_server, capsys, caplog):
    project_id = "5f2b8c1e-3d4a-4b6c-8e9f-0a1b2c3d4e5f"
    api_server(unknown_evidence_page(project_id))

    with patch("sys.argv", ["aristotle", "properties", project_id, *json_flag]):
        assert main() == 0

    out, err = capsys.readouterr()
    assert "Traceback" not in err
    if json_flag:
        [record] = json.loads(out)["declarations"]
        assert (record["name"], record["status_name"], record["evidence"]) == (UNKNOWN_EVIDENCE_NAME, "REFUTED", 0)
        assert DeclarationPage.model_validate_json(out).declarations[0].evidence is DeclarationEvidence.UNKNOWN
    else:
        header, _, row = out.splitlines()
        assert row[header.index("STATUS") : header.index("CREATED BY")].split() == ["REFUTED", "UNKNOWN"]
    assert caplog.messages == ["Unknown DeclarationEvidence value '4'; treating as UNKNOWN"]


def test_status_before_the_project_id_still_parses():
    args = _action().parser.parse_args(["--status", "REFUTED", "project-123"])

    assert (args.project_id, args.status) == ("project-123", ["REFUTED"])


@pytest.mark.parametrize(
    "argv", [["--status", "ALL", "--status", "REFUTED"], ["--status", "PROVED", "--status", "ALL"]]
)
async def test_status_all_with_another_status_is_refused(argv, capsys):
    action = _action()
    args = action.parser.parse_args(["project-123", *argv])

    with patch(LIST_ALL) as mock_all:
        with pytest.raises(SystemExit) as exit_info:
            action.preflight(args)

    assert exit_info.value.code == 2
    assert "--status ALL can't be combined with another status." in capsys.readouterr().err
    mock_all.assert_not_called()


@pytest.mark.parametrize("who,expected", [("you", "Spec.Mine"), ("aristotle", "Spec.Theirs")])
async def test_created_by_filters_on_source(who: str, expected: str, capsys):
    everything = [
        _declaration("Spec.Mine", source=DeclarationSource.USER),
        _declaration("Spec.Theirs", source=DeclarationSource.AGENT),
    ]

    mock_all = await _run(["project-123", "--created-by", who], everything=everything)

    mock_all.assert_called_once()
    assert [row.split()[0] for row in _rows(capsys.readouterr().out)] == [expected]


async def test_sort_by_name_is_alphabetical_and_ignores_case(capsys):
    everything = [_declaration("Spec.beta"), _declaration("Spec.Gamma"), _declaration("Spec.alpha")]

    await _run(["project-123", "--sort", "name"], everything=everything)

    assert [row.split()[0] for row in _rows(capsys.readouterr().out)] == ["Spec.alpha", "Spec.beta", "Spec.Gamma"]


async def test_sort_by_created_is_newest_first(capsys):
    now = datetime.now(timezone.utc)
    # last_updated order is the reverse of created_at order.
    everything = [
        _declaration("Spec.Oldest", created_at=now - timedelta(days=3), last_updated=now - timedelta(hours=1)),
        _declaration("Spec.Middle", created_at=now - timedelta(days=2), last_updated=now - timedelta(hours=2)),
        _declaration("Spec.Newest", created_at=now - timedelta(days=1), last_updated=now - timedelta(hours=3)),
    ]

    await _run(["project-123", "--sort", "created"], everything=everything)

    assert [row.split()[0] for row in _rows(capsys.readouterr().out)] == ["Spec.Newest", "Spec.Middle", "Spec.Oldest"]


async def test_filtered_rows_keep_newest_updated_first(capsys):
    now = datetime.now(timezone.utc)
    everything = [
        _declaration("Spec.Stale", last_updated=now - timedelta(days=5)),
        _declaration("Spec.Fresh", last_updated=now - timedelta(minutes=5)),
    ]

    await _run(["project-123", "--status", "PROVED"], everything=everything)

    assert [row.split()[0] for row in _rows(capsys.readouterr().out)] == ["Spec.Fresh", "Spec.Stale"]


async def test_a_filter_shows_the_first_limit_rows_and_counts_the_rest(capsys, caplog):
    caplog.set_level(logging.INFO)
    everything = [_declaration(f"Spec.P{i}") for i in range(5)]

    await _run(["project-123", "--status", "PROVED", "--limit", "2"], everything=everything)

    assert len(_rows(capsys.readouterr().out)) == 2
    assert "3 more not shown. Raise --limit to see more." in caplog.text


async def test_limit_is_applied_after_the_sort(capsys, caplog):
    caplog.set_level(logging.INFO)
    everything = [_declaration("Spec.zulu"), _declaration("Spec.alpha")]

    await _run(["project-123", "--sort", "name", "--limit", "1"], everything=everything)

    assert [row.split()[0] for row in _rows(capsys.readouterr().out)] == ["Spec.alpha"]
    assert "1 more not shown" in caplog.text


@pytest.mark.parametrize("limit", ["0", "-1"])
async def test_a_limit_below_one_is_refused_before_any_request(limit: str, capsys, resolve_project):
    action = _action()
    args = action.parser.parse_args(["project-123", "--limit", limit])

    with patch(LIST_ALL) as mock_all:
        with pytest.raises(SystemExit) as exit_info:
            action.preflight(args)

    assert exit_info.value.code == 2
    assert "--limit must be at least 1" in capsys.readouterr().err
    mock_all.assert_not_called()
    resolve_project.assert_not_awaited()


async def test_a_definition_shows_no_status(capsys):
    definition = _declaration("Spec.Width", status=DeclarationStatus.IN_PROGRESS)
    definition.declaration_type = DeclarationType.DEFINITION

    await _run(["project-123", "--kind", "definition"], everything=[definition])

    assert _rows(capsys.readouterr().out)[0].split()[:2] == ["Spec.Width", "-"]


async def test_status_is_refused_for_definitions(capsys, resolve_project):
    action = _action()
    args = action.parser.parse_args(["project-123", "--kind", "definition", "--status", "IN_PROGRESS"])

    with patch(LIST_ALL) as mock_all:
        with pytest.raises(SystemExit) as exit_info:
            action.preflight(args)

    assert exit_info.value.code == 2
    assert "definitions are never proved or refuted" in capsys.readouterr().err
    mock_all.assert_not_called()
    resolve_project.assert_not_awaited()


@pytest.mark.parametrize("argv", [["--pagination-key", "k1"], ["--pagination-key=k1", "--sort", "name"]])
def test_pagination_key_is_an_unknown_option(argv, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["aristotle", "properties", "project-123", *argv])

    with patch(LIST_ALL) as listed_all, pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 2
    listed_all.assert_not_called()
    assert capsys.readouterr().err.endswith("aristotle properties: error: unknown option --pagination-key\n")


# --- output formats ---


async def test_json_is_the_whole_listing_and_nothing_else(capsys, caplog):
    caplog.set_level(logging.INFO)
    everything = [_declaration("Spec.A"), _declaration("Spec.B", source_file="rtl/top.sv", source_line=7)]

    await _run(["project-123", "--json"], everything=everything)

    out = capsys.readouterr().out
    printed = json.loads(out)  # would fail if anything else had been printed
    assert list(printed) == ["project_id", "declarations", "pagination_key"]
    assert printed["pagination_key"] is None
    records = printed["declarations"]
    assert [r["name"] for r in records] == ["Spec.A", "Spec.B"]
    assert set(records[0]) == set(DeclarationRecord.model_json_schema(mode="serialization")["properties"])
    assert records[0]["status"] == DeclarationStatus.PROVED.value
    assert records[0]["status_name"] == "PROVED"
    assert records[1]["source_file"] == "rtl/top.sv" and records[1]["source_line"] == 7
    assert caplog.text == ""


async def test_json_carries_each_declarations_artifact_paths(capsys):
    everything = [_declaration("Spec.A"), _declaration("Spec.B", artifact_paths=["sim/cex.vcd", "sim/cex_tb.sv"])]

    await _run(["project-123", "--json"], everything=everything)

    records = json.loads(capsys.readouterr().out)["declarations"]
    assert [r["artifact_paths"] for r in records] == [[], ["sim/cex.vcd", "sim/cex_tb.sv"]]


async def test_json_with_a_filter_is_the_filtered_set(capsys):
    everything = [
        _declaration("Spec.Proved", status=DeclarationStatus.PROVED),
        _declaration("Spec.Refuted", status=DeclarationStatus.REFUTED),
    ]

    await _run(["project-123", "--status", "REFUTED", "--json"], everything=everything)

    page = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in page["declarations"]] == ["Spec.Refuted"]
    assert page["pagination_key"] is None


@pytest.mark.parametrize("flags", [[], ["--status", "PROVED"]], ids=["unfiltered", "filtered"])
async def test_json_is_every_match_unless_a_limit_is_given(flags, capsys, caplog):
    caplog.set_level(logging.INFO)
    everything = [_declaration(f"Spec.P{i}") for i in range(150)]

    # No --limit: every match, and no key, though the server's pages hold 100.
    await _run(["project-123", *flags, "--json"], everything=everything)
    printed = json.loads(capsys.readouterr().out)
    assert (len(printed["declarations"]), printed["pagination_key"]) == (150, None)
    assert "not shown" not in caplog.text

    # Explicit --limit: the slice and the "more not shown" note apply.
    await _run(["project-123", *flags, "--json", "--limit", "5"], everything=everything)
    printed = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in printed["declarations"]] == [f"Spec.P{i}" for i in range(5)]
    assert printed["pagination_key"] is None
    assert "145 more not shown" in caplog.text


async def test_json_with_nothing_found_is_an_empty_page(capsys, caplog):
    caplog.set_level(logging.INFO)

    await _run(["project-123", "--json"])

    assert json.loads(capsys.readouterr().out) == {
        "project_id": "project-123",
        "declarations": [],
        "pagination_key": None,
    }
    assert "No properties found." not in caplog.text


async def test_failed_rows_carry_the_reason(capsys):
    everything = [
        _declaration(
            "Spec.Budget", status=DeclarationStatus.FAILED, failure_reason=DeclarationFailureReason.OUT_OF_BUDGET
        ),
        _declaration(
            "Spec.Crash", status=DeclarationStatus.FAILED, failure_reason=DeclarationFailureReason.SYSTEM_ERROR
        ),
        _declaration(
            "Spec.Unfinished", status=DeclarationStatus.FAILED, failure_reason=DeclarationFailureReason.INCOMPLETE
        ),
        _declaration(
            "Spec.Cancelled", status=DeclarationStatus.FAILED, failure_reason=DeclarationFailureReason.CANCELED
        ),
        _declaration("Spec.NoReason", status=DeclarationStatus.FAILED),
    ]

    await _run(["project-123"], everything=everything)

    rows = _rows(capsys.readouterr().out)
    assert "UNRESOLVED (out of budget)" in rows[0]
    assert "UNRESOLVED (system error)" in rows[1]
    assert "UNRESOLVED (incomplete)" in rows[2]
    assert "UNRESOLVED (cancelled)" in rows[3]
    assert "UNRESOLVED " in rows[4] and "(" not in rows[4]


async def test_a_row_whose_proof_no_longer_checks_says_so_within_the_status_column(capsys):
    broken = _declaration(
        "Spec.Broken", status=DeclarationStatus.FAILED, failure_reason=DeclarationFailureReason.PROOF_CHECK_FAILED
    )

    await _run(["project-123"], everything=[broken, _declaration("Spec.Holds")])

    out = capsys.readouterr().out
    rows = _rows(out)
    # The column widens to the longest status, so the next one stays aligned.
    evidence = out.splitlines()[0].index("EVIDENCE")
    created_by = rows[0].index("Aristotle")
    assert rows[0][NAME_WIDTH + 1 : evidence].rstrip() == "UNRESOLVED (proof broken)"
    assert rows[1].index("Aristotle") == created_by


async def test_a_verdict_aristotle_never_checked_says_so_within_the_status_column(capsys, tasks):
    # A cancelled task started two hours ago, after one that finished: Spec.Own was updated an hour ago, Spec.Earlier
    # three.
    now = datetime.now(timezone.utc)
    tasks.return_value = [
        Mock(status=TaskStatus.CANCELED, created_at=now - timedelta(hours=2)),
        Mock(status=TaskStatus.COMPLETE, created_at=now - timedelta(hours=4)),
    ]
    page = [_declaration("Spec.Own"), _declaration("Spec.Earlier", last_updated=now - timedelta(hours=3))]

    await _run(["project-123"], everything=page)

    out = capsys.readouterr().out
    rows = _rows(out)
    status = rows[0].index("PROVED")
    evidence = out.splitlines()[0].index("EVIDENCE")
    created_by = rows[0].index("Aristotle")
    assert rows[0][status:evidence].rstrip() == "PROVED (unchecked)"
    assert rows[1][status:evidence].rstrip() == "PROVED"
    assert rows[1].index("Aristotle") == created_by
    tasks.assert_awaited_once_with("project-123", page, strict=False)


async def test_definitions_fetch_no_tasks_and_are_never_unchecked(capsys, tasks):
    definition = _declaration(declaration_type=DeclarationType.DEFINITION)

    await _run(["project-123", "--kind", "definition"], everything=[definition])
    capsys.readouterr()
    await _run(["project-123", "--kind", "definition", "--json"], everything=[definition])

    tasks.assert_not_awaited()
    assert [r["unchecked"] for r in json.loads(capsys.readouterr().out)["declarations"]] == [False]


async def test_json_says_which_verdicts_aristotle_never_checked_and_keeps_their_status(capsys, tasks):
    # As in the table test: a cancelled task started two hours ago, after one that finished.
    now = datetime.now(timezone.utc)
    tasks.return_value = [
        Mock(status=TaskStatus.CANCELED, created_at=now - timedelta(hours=2)),
        Mock(status=TaskStatus.COMPLETE, created_at=now - timedelta(hours=4)),
    ]
    page = [_declaration("Spec.Own"), _declaration("Spec.Earlier", last_updated=now - timedelta(hours=3))]

    await _run(["project-123", "--json"], everything=page)

    records = json.loads(capsys.readouterr().out)["declarations"]
    assert [(r["name"], r["status"], r["unchecked"]) for r in records] == [
        ("Spec.Own", DeclarationStatus.PROVED.value, True),
        ("Spec.Earlier", DeclarationStatus.PROVED.value, False),
    ]
    tasks.assert_awaited_once_with("project-123", page, strict=True)


async def test_json_fails_rather_than_call_a_verdict_checked_when_the_tasks_cannot_be_listed(capsys, tasks):
    # The table still lists the properties, as before; the JSON would state `unchecked` without knowing.
    tasks.side_effect = fetch_tasks
    page = [_declaration("Spec.Own")]
    down = AristotleAPIError("down", status_code=503)

    with patch.object(Project, "from_id", AsyncMock(side_effect=down)):
        await _run(["project-123"], everything=page)
        assert _rows(capsys.readouterr().out)[0].startswith("Spec.Own")
        with pytest.raises(AristotleAPIError):
            await _run(["project-123", "--json"], everything=page)

    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(("code", "name"), [(3, "PROOF_CHECK_FAILED"), (4, "INCOMPLETE"), (5, "CANCELED")])
async def test_json_keeps_the_code_and_name_of_the_failure_reason(code: int, name: str, capsys):
    failed = _declaration("Spec.Failed", status=DeclarationStatus.FAILED, failure_reason=code)

    await _run(["project-123", "--json"], everything=[failed])

    record = json.loads(capsys.readouterr().out)["declarations"][0]
    assert (record["failure_reason"], record["failure_reason_name"]) == (code, name)


async def test_created_by_and_location_columns(capsys):
    everything = [
        _declaration("Spec.A", source=DeclarationSource.USER, source_file="rtl/top.sv", source_line=12),
        _declaration("Spec.B", source=DeclarationSource.AGENT, source_file="rtl/top.sv"),
        _declaration("Spec.C", source=DeclarationSource.UNKNOWN),
    ]

    await _run(["project-123"], everything=everything)

    rows = _rows(capsys.readouterr().out)
    assert rows[0].split()[3:] == ["you", "1", "hour", "ago", "rtl/top.sv:12"]
    assert rows[1].split()[3:] == ["Aristotle", "1", "hour", "ago", "rtl/top.sv"]
    assert rows[2].split()[3:] == ["-", "1", "hour", "ago", "-"]


CREDITS = [f"CreditCounterSpec.Credits{n}.credit_counter_never_exceeds_max_zero_unconstrained" for n in (2, 3)]


async def test_piped_output_prints_every_name_in_full_in_one_column(capsys, caplog):
    caplog.set_level(logging.INFO)
    names = [*CREDITS, "Spec.Short"]

    with patch(TERMINAL_WIDTH, return_value=None):
        await _run(["project-123"], everything=[_declaration(name) for name in names])

    rows = _rows(capsys.readouterr().out)
    assert [row.split()[0] for row in rows] == names
    status = max(map(len, names)) + 1
    assert all(row[status:].startswith("PROVED") for row in rows)
    assert SHORTENED not in caplog.text


async def test_a_wide_terminal_prints_every_name_in_full(capsys, caplog):
    caplog.set_level(logging.INFO)

    with patch(TERMINAL_WIDTH, return_value=200):
        await _run(["project-123"], everything=[_declaration(name) for name in CREDITS])

    assert [row.split()[0] for row in _rows(capsys.readouterr().out)] == CREDITS
    assert SHORTENED not in caplog.text


async def test_a_narrow_terminal_cuts_the_shared_namespace_before_the_middle(capsys, caplog):
    caplog.set_level(logging.INFO)

    with patch(TERMINAL_WIDTH, return_value=120):
        await _run(["project-123"], everything=[_declaration(name) for name in CREDITS])

    out = capsys.readouterr().out
    shown = [row.split()[0] for row in _rows(out)]
    width = 120 - (STATUS_WIDTH + len("EVIDENCE") + 10 + 14 + len("LOCATION") + 5)
    assert all(len(name) == width for name in shown)
    assert shown[0].startswith("...Credits2.") and shown[1].startswith("...Credits3.")
    assert all(len(line) <= 120 for line in out.splitlines())
    assert SHORTENED in caplog.text


async def test_a_narrow_terminal_keeps_room_for_readable_names(capsys):
    name = "Spec.Arbiter.Fairness." + "VeryLongIdentifier" * 3

    with patch(TERMINAL_WIDTH, return_value=80):
        await _run(["project-123"], everything=[_declaration(name)])

    shown = _rows(capsys.readouterr().out)[0].split()[0]
    assert len(shown) == NAME_WIDTH - len("EVIDENCE") - 1
    assert shown.startswith("Spec.Arbiter") and shown.endswith("LongIdentifier")


@pytest.mark.parametrize(
    ("names", "namespaces"),
    [
        (["A.B.x", "A.B.y"], ["A.B.", "A.B."]),
        (["A.B.c", "A.Bc.d"], ["A.", "A."]),
        (["Ab.x", "Ac.y"], ["", ""]),
        (["A.x", "B.y"], ["", ""]),
        (["A.B.x"], [""]),
        (["C.Credits2.x", "C.Credits3.x", "Arbiter.y"], ["C.", "C.", ""]),
        (["S.A.x", "S.B.z", "S.A.y"], ["S.A.", "S.", "S.A."]),
    ],
)
def test_shared_namespaces_are_whole_dotted_components_another_name_shares(names: list[str], namespaces: list[str]):
    shared = shared_namespaces(names)
    assert [shared[name] for name in names] == namespaces


@pytest.mark.parametrize("columns", [80, 120])
async def test_a_narrow_terminal_keeps_names_from_several_namespaces_apart(columns: int, capsys):
    names = [*CREDITS, "ArbiterSpec.no_double_grant"]

    with patch(TERMINAL_WIDTH, return_value=columns):
        await _run(["project-123"], everything=[_declaration(name) for name in names])

    shown = [row.split()[0] for row in _rows(capsys.readouterr().out)]
    assert shown[0].startswith("...Credits2.") and shown[1].startswith("...Credits3.")
    assert shown[2] == "ArbiterSpec.no_double_grant"


def test_nothing_is_matched_when_every_name_fits():
    names = [*CREDITS, "Spec.Short"]

    with patch("aristotlelib.cli.command.properties.shortened_name_pattern") as pattern:
        assert shown_names(names, 200) == names

    pattern.assert_not_called()


def test_names_that_would_still_look_alike_are_shown_whole():
    alike = [f"Spec.{'x' * 30}{n}{'y' * 30}" for n in (1, 2)]

    assert shown_names([*alike, "Spec.short"], 40) == [*alike, "Spec.short"]


def test_shorten_name_cuts_the_namespace_then_the_middle():
    assert shorten_name("Spec.Short", 20, "Spec.") == "Spec.Short"
    assert shorten_name("LongNamespace.property_name", 20, "LongNamespace.") == "...property_name"
    assert shorten_name("LongNamespace.a_much_longer_property_name", 20, "LongNamespace.") == "...a_muc...erty_name"
    assert shorten_name("Ns.a_much_longer_property_name", 20, "Ns.") == "Ns.a_muc...erty_name"


def test_truncate_middle_keeps_both_ends():
    assert truncate_middle("abcdefghij", 7) == "ab...ij"
    assert truncate_middle("short", 7) == "short"
    assert truncate_middle("exactly", 7) == "exactly"


# --- a project the server will not show ---


@pytest.mark.parametrize("json_flag", [[], ["--json"]], ids=["table", "--json"])
async def test_a_missing_project_is_one_line_not_a_traceback(json_flag, capsys, caplog):
    caplog.set_level(logging.ERROR)
    action = _action()
    args = action.parser.parse_args(["project-typo", *json_flag])
    # An unknown project answers 403.
    forbidden = AristotleAPIError("forbidden", status_code=403)

    with patch(LIST_ALL, side_effect=forbidden):
        with pytest.raises(SystemExit) as exited:
            await action.run_action(args)

    assert exited.value.code == 4

    assert capsys.readouterr().out == ""
    assert "Project project-typo not found or not accessible." in caplog.text


async def test_other_api_errors_still_reach_the_generic_handler():
    action = _action()
    args = action.parser.parse_args(["project-123"])

    with patch(LIST_ALL, side_effect=AristotleAPIError("down", status_code=500)):
        with pytest.raises(AristotleAPIError):
            await action.run_action(args)


@pytest.mark.parametrize(
    "status",
    [
        {"status": DeclarationStatus.REFUTED, "evidence": DeclarationEvidence.COUNTEREXAMPLE},
        {"status": DeclarationStatus.REFUTED, "evidence": DeclarationEvidence.BOTH},
        {"status": DeclarationStatus.FAILED, "failure_reason": DeclarationFailureReason.OUT_OF_BUDGET},
    ],
    ids=["counterexample", "both", "out of budget"],
)
async def test_wider_columns_take_space_from_names_so_the_table_still_fits(status, capsys):
    long_name = "Spec.Arbiter.Fairness." + "VeryLongIdentifier" * 3
    # A 30-character location is the column's most; names shorten to accommodate status and evidence.
    located = {"source_file": "rtl/arbiter/round_robin.sv", "source_line": 117}
    everything = [_declaration(long_name, **status, **located), _declaration("Spec.Holds", **located)]

    with patch(TERMINAL_WIDTH, return_value=120):
        await _run(["project-123"], everything=everything)

    assert all(len(line) <= 120 for line in capsys.readouterr().out.splitlines())


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--status", "REFUTED"], ["Spec.REFUTED"]),
        (["--status", "IN_PROGRESS"], ["Spec.IN_PROGRESS"]),
        (["--status", "UNRESOLVED"], ["Spec.FAILED"]),
        # NOT_VERIFIED, UNRESOLVED's old name, still works for scripts written against it.
        (["--status", "NOT_VERIFIED"], ["Spec.FAILED"]),
        (["--status", "UNRESOLVED", "--status", "PROVED"], ["Spec.FAILED", "Spec.PROVED"]),
    ],
    ids=["refuted", "in progress", "unresolved", "not verified, the old name", "unresolved or proved"],
)
async def test_status_takes_the_outcome_words_the_table_shows(argv, expected, capsys):
    everything = [_declaration(f"Spec.{s.name}", status=s) for s in DeclarationStatus if s != DeclarationStatus.UNKNOWN]

    await _run(["project-123", *argv, "--sort", "name", "--json"], everything=everything)

    assert [d["name"] for d in json.loads(capsys.readouterr().out)["declarations"]] == expected


async def test_a_status_word_lists_a_verdict_aristotle_never_checked_marked_so(capsys, tasks):
    """The word matches the status reported, as the table words it, though show counts the verdict as unchecked."""
    now = datetime.now(timezone.utc)
    tasks.return_value = [Mock(status=TaskStatus.OUT_OF_BUDGET, created_at=now - timedelta(hours=2))]
    refuted = _declaration("Spec.Refuted", status=DeclarationStatus.REFUTED, last_updated=now - timedelta(hours=1))

    await _run(["project-123", "--status", "REFUTED"], everything=[refuted, _declaration("Spec.Holds")])

    [row] = _rows(capsys.readouterr().out)
    assert row.split()[:3] == ["Spec.Refuted", "REFUTED", "(unchecked)"]


POINTER = "properties lists a project's properties; show one property with:\n  aristotle property <project-id> <name>"


@pytest.mark.parametrize(
    ("argv", "left_over"),
    [
        (["project-123", "fifo_never_overflows"], "fifo_never_overflows"),
        (["FIFO run", "Spec.never overflows", "--json"], "Spec.never overflows"),
        (["FIFO", "overflow"], "overflow"),
        (["project-123", "--status", "PROVED", "REFUTED"], "REFUTED"),
        (["--status", "PROVED", "REFUTED", "project-123"], "project-123"),
        (["project-123", "--kind", "property", "definition"], "definition"),
        (["project-123", "--limit", "5", "10"], "10"),
        (["FIFO", "run", "--status", "PROVED", "REFUTED"], "run REFUTED"),
    ],
    ids=[
        "a name",
        "after an option",
        "an unquoted name",
        "a second status",
        "before the project",
        "a default",
        "a number",
        "on both sides of an option",
    ],
)
def test_a_word_left_over_is_unrecognized_and_points_at_the_property_command(argv, left_over, monkeypatch, capsys):
    # The CLI's own parser, which adds the command's pointer to argparse's error for words left over.
    monkeypatch.setattr("sys.argv", ["aristotle", "properties", *argv])

    with patch(LIST_ALL) as listed_all, pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 2
    listed_all.assert_not_called()
    assert capsys.readouterr().err.endswith(
        f"aristotle properties: error: unrecognized arguments: {left_over}. {POINTER}\n"
    )
