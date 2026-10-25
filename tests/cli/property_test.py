"""Tests for aristotlelib.cli.command.property."""

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest

from aristotlelib.agent_task import TaskStatus
from aristotlelib.api_request import AristotleAPIError
from aristotlelib.cli.api_action import fetch_tasks
from aristotlelib.cli.command.properties import shown_names
from aristotlelib.cli.command.property import PropertyAction, find_by_name
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
from aristotlelib.results import DeclarationRecord
from tests.cli.conftest import UNKNOWN_EVIDENCE_NAME, unknown_evidence_page

LIST_ALL = "aristotlelib.cli.command.property.Declaration.list_all_for_project"


@pytest.fixture(autouse=True)
def tasks():
    """Patch the fetch of the project's tasks; none, as for a project without one, unless a test sets them."""
    with patch("aristotlelib.cli.command.property.fetch_tasks", AsyncMock(return_value=[])) as mock:
        yield mock


def _action() -> PropertyAction:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    action = PropertyAction(subparsers)
    action.add_action_arguments()
    return action


def _declaration(name: str = "Spec.Arbiter.NoDoubleGrant", **overrides) -> Declaration:
    now = datetime.now(timezone.utc)
    fields = dict(
        project_id="project-123",
        declaration_id=name.lower(),
        declaration_type=DeclarationType.PROPERTY,
        source=DeclarationSource.AGENT,
        name=name,
        status=DeclarationStatus.PROVED,
        filename="Spec/Property.lean",
        source_file="rtl/arbiter.sv",
        source_line=42,
        source_anchor="assign grant = req & ~busy;",
        lean_code="theorem NoDoubleGrant : ∀ t, ¬ (grant0 t ∧ grant1 t) := by\n  simp",
        description="The arbiter never grants two requesters at once.",
        created_at=now - timedelta(days=2),
        last_updated=now - timedelta(hours=1),
    )
    fields.update(overrides)
    return Declaration(**fields)


async def _run(argv: list[str], everything):
    action = _action()
    args = action.parser.parse_args(argv)
    with patch(LIST_ALL, return_value=list(everything)) as mock_all:
        try:
            await action.run_action(args)
            mock_all.exit_code = 0
        except SystemExit as e:
            mock_all.exit_code = e.code
    return mock_all


async def test_exact_name_prints_the_full_record(capsys, monkeypatch):
    monkeypatch.delenv("ARISTOTLE_WEB_URL", raising=False)
    mock_all = await _run(["project-123", "Spec.Arbiter.NoDoubleGrant"], [_declaration()])

    # Without --kind, both kinds are fetched.
    mock_all.assert_called_once_with("project-123", declaration_type=None)
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == "Spec.Arbiter.NoDoubleGrant"
    assert "Kind:          property" in lines
    assert "Status:        PROVED" in lines
    assert "Evidence:      -" in lines
    assert "Created by:    Aristotle" in lines
    assert "Source:        rtl/arbiter.sv:42" in lines
    assert "Anchor:        assign grant = req & ~busy;" in lines
    assert "Generated:     Spec/Property.lean" in lines
    assert "Web:           https://aristotle.harmonic.fun/projects/project-123" in lines
    created = next(line for line in lines if line.startswith("Created:"))
    updated = next(line for line in lines if line.startswith("Last updated:"))
    assert "UTC (2 days ago)" in created
    assert "UTC (1 hour ago)" in updated
    assert "Description:\n  The arbiter never grants two requesters at once.\n" in out
    # Lean code is printed verbatim.
    assert "Lean statement:\ntheorem NoDoubleGrant : ∀ t, ¬ (grant0 t ∧ grant1 t) := by\n  simp\n" in out
    assert out.endswith("Artifacts:     -\n")


async def test_the_web_link_follows_the_selected_deployment(capsys, monkeypatch):
    monkeypatch.setenv("ARISTOTLE_WEB_URL", "https://staging.example/")
    await _run(["project-123", "Spec.Arbiter.NoDoubleGrant"], [_declaration()])

    assert "Web:           https://staging.example/projects/project-123" in capsys.readouterr().out.splitlines()


async def test_case_does_not_matter_when_it_is_unambiguous(capsys):
    await _run(["project-123", "spec.arbiter.nodoublegrant"], [_declaration()])

    assert capsys.readouterr().out.splitlines()[0] == "Spec.Arbiter.NoDoubleGrant"


async def test_trailing_segments_of_a_dotted_name_are_enough(capsys):
    everything = [_declaration("Spec.Arbiter.NoDoubleGrant"), _declaration("Spec.Arbiter.Fairness")]

    await _run(["project-123", "Arbiter.NoDoubleGrant"], everything)
    assert capsys.readouterr().out.splitlines()[0] == "Spec.Arbiter.NoDoubleGrant"

    await _run(["project-123", "nodoublegrant"], everything)
    assert capsys.readouterr().out.splitlines()[0] == "Spec.Arbiter.NoDoubleGrant"


async def test_a_substring_that_is_not_a_segment_does_not_match(capsys, caplog):
    caplog.set_level(logging.ERROR)

    assert (await _run(["project-123", "DoubleGrant"], [_declaration("Spec.Arbiter.NoDoubleGrant")])).exit_code == 4

    assert capsys.readouterr().out == ""
    assert "No property named 'DoubleGrant'" in caplog.text


async def test_an_exact_name_beats_a_suffix_match(capsys, caplog):
    caplog.set_level(logging.ERROR)
    everything = [_declaration("Foo", declaration_id="short"), _declaration("Bar.Foo", declaration_id="long")]

    assert (await _run(["project-123", "Foo"], everything)).exit_code == 0

    out = capsys.readouterr().out
    assert out.splitlines()[0] == "Foo"
    assert caplog.text == ""


GOLDBACH = [
    "Goldbach.goldbachConjecture_iff_count_pos",
    "Goldbach.goldbachConjecture_iff_forall_two_mul",
    "Goldbach.goldbachConjecture_iff_goldbachOriginal",
    "Goldbach.weakGoldbach_of_goldbachConjecture",
    "Goldbach.Credits2.credit_counter_never_exceeds_max_zero_unconstrained",
    "Goldbach.Credits3.credit_counter_never_exceeds_max_zero_unconstrained",
]
SEVERAL_NAMESPACES = [
    "CreditCounterSpec.Credits2.credit_counter_never_exceeds_max_zero_unconstrained",
    "CreditCounterSpec.Credits3.credit_counter_never_exceeds_max_zero_unconstrained",
    "ArbiterSpec.Credits2.credit_counter_never_exceeds_max_zero_unconstrained",
    "ArbiterSpec.no_double_grant_after_any_reset_sequence_of_any_length",
    "fifo_never_overflows_when_the_writer_respects_full_and_the_reader_is_stalled",
]
NEAR_TWINS = [
    "Spec.Arbiter.no_starvation_under_any_request_pattern_of_any_length",
    "Impl.Arbiter.no_starvation_under_any_request_pattern_of_any_length",
    "Alpha.X.some_long_property_name_that_needs_cutting_somewhere_in_it",
    "Beta.Y.X.some_long_property_name_that_needs_cutting_somewhere_in_it",
    "Spec.Foo_with_a_name_long_enough_to_be_cut_somewhere_in_the_middle",
    "Spec.foo_with_a_name_long_enough_to_be_cut_somewhere_in_the_middle",
]


@pytest.mark.parametrize(
    "names",
    [GOLDBACH, SEVERAL_NAMESPACES, NEAR_TWINS],
    ids=["one namespace", "several namespaces", "near twins"],
)
def test_every_name_as_properties_shows_it_finds_that_property(names: list[str]):
    everything = [_declaration(name, declaration_id=f"id-{i}") for i, name in enumerate(names)]

    for width in range(40, 90):
        shown = shown_names(names, width)
        assert len(set(shown)) == len(names), width
        for declaration, text in zip(everything, shown):
            assert find_by_name(everything, text) == [declaration], text


def test_a_leading_ellipsis_stands_for_a_whole_namespace():
    top, nested = _declaration("Main.thm", declaration_id="top"), _declaration("Spec.Main.thm", declaration_id="nested")

    assert find_by_name([top, nested], "...Main.thm") == [nested]
    assert find_by_name([top, nested], "...ain.thm") == []


async def test_a_shortened_name_that_fits_several_is_listed_instead_of_guessed(capsys, caplog):
    caplog.set_level(logging.ERROR)
    everything = [_declaration("Spec.A.Foo"), _declaration("Spec.B.Foo")]

    assert (await _run(["project-123", "...Foo"], everything)).exit_code == 2

    assert "'...Foo' matches 2 declarations" in caplog.text


async def test_several_matches_are_listed_instead_of_guessed(capsys, caplog):
    caplog.set_level(logging.ERROR)
    everything = [_declaration("Spec.A.Foo"), _declaration("Spec.B.Foo")]

    assert (await _run(["project-123", "foo"], everything)).exit_code == 2

    assert capsys.readouterr().out == ""
    assert "'foo' matches 2 declarations" in caplog.text
    assert "  Spec.A.Foo" in caplog.text and "  Spec.B.Foo" in caplog.text


async def test_no_match_points_at_the_list_command(capsys, caplog):
    caplog.set_level(logging.ERROR)

    assert (await _run(["project-123", "Spec.Missing"], [_declaration()])).exit_code == 4

    assert capsys.readouterr().out == ""
    assert "No property named 'Spec.Missing' in project project-123" in caplog.text
    assert "aristotle properties project-123" in caplog.text


async def test_json_prints_the_one_record_as_an_object(capsys):
    await _run(["project-123", "Spec.Arbiter.NoDoubleGrant", "--json"], [_declaration(), _declaration("Spec.Other")])

    record = json.loads(capsys.readouterr().out)
    assert isinstance(record, dict)
    assert record["name"] == "Spec.Arbiter.NoDoubleGrant"
    assert set(record) == set(DeclarationRecord.model_json_schema(mode="serialization")["properties"])
    assert record["source_anchor"] == "assign grant = req & ~busy;"


async def test_missing_optional_fields_show_as_dashes(capsys):
    bare = _declaration(source_file=None, source_line=None, source_anchor=None, lean_code=None, description=None)

    await _run(["project-123", "Spec.Arbiter.NoDoubleGrant"], [bare])

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert "Source:        -" in lines
    assert "Anchor:        -" in lines
    assert "Description:   -" in lines
    assert "Lean statement:\n-\n" in out
    assert out.endswith("Artifacts:     -\n")


async def test_definitions_and_failures_are_described_too(capsys):
    definition = _declaration(
        "Spec.Width",
        declaration_type=DeclarationType.DEFINITION,
        status=DeclarationStatus.IN_PROGRESS,
        source=DeclarationSource.USER,
    )
    await _run(["project-123", "Spec.Width"], [definition])
    lines = capsys.readouterr().out.splitlines()
    assert "Kind:          definition" in lines
    assert "Created by:    you" in lines
    # The API reports every definition as IN_PROGRESS forever; the web shows no status for one.
    assert "Status:        -" in lines

    failed = _declaration(status=DeclarationStatus.FAILED, failure_reason=DeclarationFailureReason.SYSTEM_ERROR)
    await _run(["project-123", "Spec.Arbiter.NoDoubleGrant"], [failed])
    assert "Status:        UNRESOLVED (system error)" in capsys.readouterr().out.splitlines()

    unfinished = _declaration(status=DeclarationStatus.FAILED, failure_reason=DeclarationFailureReason.INCOMPLETE)
    await _run(["project-123", "Spec.Arbiter.NoDoubleGrant"], [unfinished])
    assert "Status:        UNRESOLVED (incomplete)" in capsys.readouterr().out.splitlines()

    cancelled = _declaration(status=DeclarationStatus.FAILED, failure_reason=DeclarationFailureReason.CANCELED)
    await _run(["project-123", "Spec.Arbiter.NoDoubleGrant"], [cancelled])
    assert "Status:        UNRESOLVED (cancelled)" in capsys.readouterr().out.splitlines()

    broken = _declaration(status=DeclarationStatus.FAILED, failure_reason=DeclarationFailureReason.PROOF_CHECK_FAILED)
    await _run(["project-123", "Spec.Arbiter.NoDoubleGrant"], [broken])
    assert (
        "Status:        UNRESOLVED (the proof no longer checks; re-run to re-prove)"
        in capsys.readouterr().out.splitlines()
    )


@pytest.mark.parametrize(
    ("setter", "status"),
    [
        (TaskStatus.CANCELED, "PROVED (unchecked: the task that last updated it ended before Aristotle checked it)"),
        (TaskStatus.COMPLETE, "PROVED"),
    ],
    ids=["set by a cancelled task", "set by a task that finished"],
)
async def test_a_proof_is_unchecked_only_if_the_task_that_set_it_ended_before_checking_it(
    capsys, tasks, setter: TaskStatus, status: str
):
    # The proof was updated an hour ago, by the task before a cancelled follow-up.
    now = datetime.now(timezone.utc)
    tasks.return_value = [
        Mock(status=TaskStatus.CANCELED, created_at=now - timedelta(minutes=30)),
        Mock(status=setter, created_at=now - timedelta(hours=2)),
    ]
    declaration = _declaration()

    await _run(["project-123", "NoDoubleGrant"], [declaration])

    assert f"Status:        {status}" in capsys.readouterr().out.splitlines()
    tasks.assert_awaited_once_with("project-123", [declaration], strict=False)


@pytest.mark.parametrize(("setter", "unchecked"), [(TaskStatus.CANCELED, True), (TaskStatus.COMPLETE, False)])
async def test_json_says_whether_aristotle_checked_the_proof(capsys, tasks, setter: TaskStatus, unchecked: bool):
    now = datetime.now(timezone.utc)
    tasks.return_value = [
        Mock(status=TaskStatus.CANCELED, created_at=now - timedelta(minutes=30)),
        Mock(status=setter, created_at=now - timedelta(hours=2)),
    ]

    await _run(["project-123", "NoDoubleGrant", "--json"], [_declaration()])

    record = json.loads(capsys.readouterr().out)
    assert (record["status"], record["unchecked"]) == (3, unchecked)


async def test_json_fails_rather_than_call_a_proof_checked_when_the_tasks_cannot_be_listed(capsys, tasks):
    # The record still prints without --json, as before; the JSON would state `unchecked` without knowing.
    tasks.side_effect = fetch_tasks
    down = AristotleAPIError("down", status_code=503)

    with patch.object(Project, "from_id", AsyncMock(side_effect=down)):
        assert (await _run(["project-123", "NoDoubleGrant"], [_declaration()])).exit_code == 0
        assert "Status:        PROVED" in capsys.readouterr().out.splitlines()
        with pytest.raises(AristotleAPIError):
            await _run(["project-123", "NoDoubleGrant", "--json"], [_declaration()])

    assert capsys.readouterr().out == ""


async def test_a_definition_does_not_fetch_the_tasks(capsys, tasks):
    await _run(["project-123", "Spec.Def"], [_declaration("Spec.Def", declaration_type=DeclarationType.DEFINITION)])

    assert "Status:        -" in capsys.readouterr().out.splitlines()
    tasks.assert_not_awaited()


async def test_description_entities_read_as_on_the_web(capsys):
    escaped = _declaration(description="accepts mmu2carb_&lt;c&gt;_rf_pvld &amp; arb_h0_&#60;c&#62;_gnt")

    await _run(["project-123", "Spec.Arbiter.NoDoubleGrant"], [escaped])

    assert "Description:\n  accepts mmu2carb_<c>_rf_pvld & arb_h0_<c>_gnt\n" in capsys.readouterr().out


async def test_a_property_and_a_definition_with_one_name_are_told_apart(capsys, caplog):
    caplog.set_level(logging.ERROR)
    both = [
        _declaration("Spec.Width", declaration_id="the-property"),
        _declaration("Spec.Width", declaration_id="the-definition", declaration_type=DeclarationType.DEFINITION),
    ]

    assert (await _run(["project-123", "Spec.Width"], both)).exit_code == 2

    assert capsys.readouterr().out == ""
    assert "'Spec.Width' matches 2 declarations" in caplog.text
    assert "  Spec.Width (property, id the-property)" in caplog.text
    assert "  Spec.Width (definition, id the-definition)" in caplog.text
    assert "--kind" in caplog.text


async def test_two_properties_with_one_name_are_told_apart_by_id(capsys, caplog):
    """Same-name properties are listed with their ids, and an id is accepted as the argument."""
    caplog.set_level(logging.ERROR)
    twins = [
        _declaration("ring_peek_many", declaration_id="prop-ring-peek-many"),
        _declaration(
            "ring_peek_many",
            declaration_id="prop-ring-peek-many-2",
            status=DeclarationStatus.REFUTED,
            evidence=DeclarationEvidence.COUNTEREXAMPLE,
        ),
    ]

    assert (await _run(["project-123", "ring_peek_many"], twins)).exit_code == 2

    assert capsys.readouterr().out == ""
    assert "'ring_peek_many' matches 2 declarations" in caplog.text
    assert "  ring_peek_many (property, id prop-ring-peek-many)" in caplog.text
    assert "  ring_peek_many (property, id prop-ring-peek-many-2)" in caplog.text
    assert "the id of one" in caplog.text
    caplog.clear()

    await _run(["project-123", "prop-ring-peek-many-2"], twins)

    out = capsys.readouterr().out
    assert out.splitlines()[0] == "ring_peek_many"
    assert "Status:        REFUTED\n" in out
    assert "Evidence:      COUNTEREXAMPLE" in out
    assert caplog.text == ""


async def test_kind_breaks_the_tie_and_is_a_server_side_filter(capsys):
    definition = _declaration("Spec.Width", declaration_type=DeclarationType.DEFINITION)

    mock_all = await _run(["project-123", "Spec.Width", "--kind", "definition"], [definition])

    mock_all.assert_called_once_with("project-123", declaration_type=DeclarationType.DEFINITION)
    assert "Kind:          definition" in capsys.readouterr().out.splitlines()


async def test_no_match_under_a_kind_names_that_kind(capsys, caplog):
    caplog.set_level(logging.ERROR)

    assert (await _run(["project-123", "Spec.Missing", "--kind", "definition"], [])).exit_code == 4

    assert capsys.readouterr().out == ""
    assert "No definition named 'Spec.Missing'" in caplog.text
    assert "aristotle properties project-123 --kind definition" in caplog.text


async def test_a_missing_project_is_one_line_not_a_traceback(capsys, caplog):
    caplog.set_level(logging.ERROR)
    action = _action()
    args = action.parser.parse_args(["project-typo", "Spec.Foo"])

    # An unknown project answers 403.
    with patch(LIST_ALL, side_effect=AristotleAPIError("forbidden", status_code=403)):
        with pytest.raises(SystemExit) as exited:
            await action.run_action(args)

    assert exited.value.code == 4

    assert capsys.readouterr().out == ""
    assert "Project project-typo not found or not accessible." in caplog.text


async def test_other_api_errors_still_reach_the_generic_handler():
    action = _action()
    args = action.parser.parse_args(["project-123", "Spec.Foo"])

    with patch(LIST_ALL, side_effect=AristotleAPIError("down", status_code=500)):
        with pytest.raises(AristotleAPIError):
            await action.run_action(args)


async def test_the_record_lists_the_artifacts_and_the_command_that_downloads_them(capsys, caplog):
    caplog.set_level(logging.INFO)
    message = "Refuted by a counterexample and a formal Lean proof of the negation (internal). See the VCD trace in Artifacts."
    linked = _declaration(
        status=DeclarationStatus.REFUTED,
        evidence=DeclarationEvidence.BOTH,
        evidence_message=message,
        artifact_paths=["sim/cex.vcd", "sim/cex_tb.sv", "sim/run_cex.sh"],
    )

    await _run(["project-123", "Spec.Arbiter.NoDoubleGrant"], [linked])

    lines = capsys.readouterr().out.splitlines()
    assert "Status:        REFUTED" in lines
    assert "Evidence:      BOTH" in lines
    assert "Generated:     Spec/Property.lean" in lines
    evidence = lines.index("Refutation evidence:")
    assert lines.index("Description:") < lines.index("Lean statement:") < evidence
    assert lines[evidence + 1 :] == [
        f"  {message}",
        "",
        "Artifacts:     sim/cex.vcd",
        "               sim/cex_tb.sv",
        "               sim/run_cex.sh",
    ]
    assert caplog.messages == [
        "\nDownload them with:\n"
        "  aristotle download project-123 --artifact sim/cex.vcd --artifact sim/cex_tb.sv --artifact sim/run_cex.sh"
    ]


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        (DeclarationType.PROPERTY, DeclarationStatus.PROVED),
        (DeclarationType.PROPERTY, DeclarationStatus.FAILED),
        (DeclarationType.PROPERTY, DeclarationStatus.IN_PROGRESS),
        (DeclarationType.DEFINITION, DeclarationStatus.REFUTED),
    ],
)
async def test_non_refuted_properties_and_definitions_do_not_show_evidence(kind, status, capsys):
    message = "A stale refutation message."
    declaration = _declaration(
        declaration_type=kind, status=status, evidence=DeclarationEvidence.BOTH, evidence_message=message
    )

    await _run(["project-123", declaration.name], [declaration])

    out = capsys.readouterr().out
    assert "Evidence:      -" in out
    assert "Refutation evidence:" not in out
    assert message not in out


@pytest.mark.parametrize("json_flag", [[], ["--json"]], ids=["text", "--json"])
def test_an_evidence_kind_this_version_does_not_know_reads_as_unknown(json_flag, api_server, capsys):
    project_id = "5f2b8c1e-3d4a-4b6c-8e9f-0a1b2c3d4e5f"
    api_server(unknown_evidence_page(project_id))

    with patch("sys.argv", ["aristotle", "property", project_id, UNKNOWN_EVIDENCE_NAME, *json_flag]):
        assert main() == 0

    out, err = capsys.readouterr()
    assert "Traceback" not in err
    if json_flag:
        record = json.loads(out)
        assert (record["status_name"], record["evidence"]) == ("REFUTED", 0)
        assert DeclarationRecord.model_validate_json(out).evidence is DeclarationEvidence.UNKNOWN
    else:
        assert "Evidence:      UNKNOWN" in out.splitlines()
        assert "Refuted by a kind of evidence added after this version." in out


async def test_a_record_without_artifacts_shows_a_dash_and_no_command(capsys, caplog):
    caplog.set_level(logging.INFO)

    await _run(["project-123", "Spec.Arbiter.NoDoubleGrant"], [_declaration()])

    assert "Artifacts:     -" in capsys.readouterr().out.splitlines()
    assert caplog.messages == []


async def test_the_command_leaves_out_a_path_a_shell_would_read_otherwise_and_keeps_a_dash_from_reading_as_an_option(
    capsys, caplog
):
    caplog.set_level(logging.INFO)
    linked = _declaration(artifact_paths=["sim/cex&calc.exe&rem .vcd", "-cex.vcd"])

    await _run(["project-123", "Spec.Arbiter.NoDoubleGrant"], [linked])

    assert "Artifacts:     sim/cex&calc.exe&rem .vcd" in capsys.readouterr().out.splitlines()
    assert caplog.messages == ["\nDownload 1 of the 2 with:\n  aristotle download project-123 --artifact=-cex.vcd"]


async def test_the_command_leaves_out_a_path_no_system_can_save_and_counts_the_rest(capsys, caplog):
    caplog.set_level(logging.INFO)
    linked = _declaration(artifact_paths=["sim/cex.vcd", "sim\\cex.log"])

    await _run(["project-123", "Spec.Arbiter.NoDoubleGrant"], [linked])

    lines = capsys.readouterr().out.splitlines()
    assert lines[lines.index("Artifacts:     sim/cex.vcd") + 1] == "               sim\\cex.log"
    assert caplog.messages == ["\nDownload 1 of the 2 with:\n  aristotle download project-123 --artifact sim/cex.vcd"]


async def test_no_command_follows_when_this_system_can_save_none_of_them(capsys, caplog):
    caplog.set_level(logging.INFO)

    await _run(["project-123", "Spec.Arbiter.NoDoubleGrant"], [_declaration(artifact_paths=["../cex.vcd"])])

    assert "Artifacts:     ../cex.vcd" in capsys.readouterr().out.splitlines()
    assert caplog.messages == []


async def test_json_carries_the_artifact_paths(capsys):
    message = "Refuted by a counterexample. See the VCD trace in Artifacts."
    linked = _declaration(
        status=DeclarationStatus.REFUTED,
        evidence=DeclarationEvidence.COUNTEREXAMPLE,
        evidence_message=message,
        artifact_paths=["sim/cex.vcd"],
    )

    await _run(["project-123", "Spec.Arbiter.NoDoubleGrant", "--json"], [linked])

    record = json.loads(capsys.readouterr().out)
    assert record["artifact_paths"] == ["sim/cex.vcd"]
    assert record["evidence"] == DeclarationEvidence.COUNTEREXAMPLE
    assert record["evidence_message"] == message
