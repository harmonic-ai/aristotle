"""Tests for aristotlelib.ci.console."""

import dataclasses
import logging

import pytest

from aristotlelib.ci import console
from aristotlelib.ci.manifest import Kind, RequestedProperty
from aristotlelib.ci.results import RunResults
from aristotlelib.declaration import DeclarationEvidence, DeclarationFailureReason, DeclarationStatus
from aristotlelib.printable import escape_logged_text
from tests.ci.conftest import expecting_refuted, make_declaration

NO_OVERFLOW = RequestedProperty("no_overflow", Kind.NATURAL_LANGUAGE, "The FIFO never overflows.")


def test_a_run_that_stopped_is_skipped_with_its_reason():
    results = RunResults.from_declarations(
        [
            make_declaration("Spec.Done", DeclarationStatus.PROVED),
            make_declaration("Spec.Slow", DeclarationStatus.IN_PROGRESS),
            make_declaration(
                "Spec.Unlucky", DeclarationStatus.FAILED, failure_reason=DeclarationFailureReason.OUT_OF_BUDGET
            ),
        ],
        duration_seconds=1.0,
    )

    rendered = console.render(results)

    # A stopped run's property counts as in progress, and its own line names the reason.
    assert "1 proved, 0 refuted, 1 in progress, 1 unresolved" in rendered
    assert "  SKIP  Spec.Slow  (in progress)" in rendered
    assert "  SKIP  Spec.Unlucky  (unresolved (out of budget))" in rendered
    assert "FAIL" not in rendered


@pytest.mark.parametrize(
    ("run", "said"),
    [({"task_failed": True}, "failed due to an internal error."), ({"canceled": True}, "cancelled outside this job.")],
    ids=["failed", "cancelled"],
)
def test_a_failed_or_cancelled_run_that_reached_nothing_says_only_that(run: dict, said: str):
    results = RunResults.from_declarations([], duration_seconds=1.0, **run)

    rendered = " ".join(console.render(results).split())

    assert said in rendered
    assert "Properties below" not in rendered


def test_a_cancelled_run_says_so_above_the_properties_it_reached_and_leaves_their_proofs_unchecked():
    results = RunResults.from_declarations([make_declaration("Spec.Done")], duration_seconds=1.0, canceled=True)

    rendered = console.render(results)

    assert "PASS" not in rendered
    assert rendered.index("cancelled outside this job") < rendered.index("Spec.Done")
    assert "a proof among them is unchecked, but a refutation counts" in " ".join(rendered.split())


def test_a_manifest_property_the_run_did_not_register_is_skipped_as_not_registered():
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=[NO_OVERFLOW])

    rendered = console.render(results)

    assert "0 proved, 0 refuted, 0 in progress, 1 not registered" in rendered
    assert "  SKIP  no_overflow  (not registered)" in rendered
    assert "No properties were registered" not in rendered


def test_a_manifest_property_is_reported_under_its_id_not_the_declarations_name():
    declaration = make_declaration(
        "Spec.NoOverflow",
        DeclarationStatus.REFUTED,
        description="Overflows at cycle 12.",
        evidence=DeclarationEvidence.COUNTEREXAMPLE,
    ).model_copy(update={"declaration_id": "no_overflow"})
    results = RunResults.from_declarations([declaration], duration_seconds=1.0, requested=[NO_OVERFLOW])

    rendered = console.render(results)

    assert "  FAIL  [refuted] no_overflow" in rendered
    assert "      Overflows at cycle 12." in rendered
    assert "Spec.NoOverflow" not in rendered


def test_suggestions_are_listed_after_the_verdict_and_do_not_count_in_it():
    results = RunResults.from_declarations(
        [
            make_declaration("Spec.NoOverflow").model_copy(update={"declaration_id": "no_overflow"}),
            make_declaration(
                "Spec.Extra",
                DeclarationStatus.REFUTED,
                description="Wraps at cycle 12.",
                evidence=DeclarationEvidence.COUNTEREXAMPLE,
            ),
            make_declaration("Spec.Idea", DeclarationStatus.IN_PROGRESS, description=None),
        ],
        duration_seconds=1.0,
        requested=[NO_OVERFLOW],
    )

    rendered = console.render(results)

    assert "1 proved, 0 refuted, 0 in progress" in rendered
    assert "FAIL" not in rendered
    assert "SKIP" not in rendered
    section = rendered.index("  Suggested by Aristotle, not part of the verdict:")
    assert rendered.index("  PASS  no_overflow") < section
    assert rendered.index("  -     Spec.Extra  (refuted)\n        Wraps at cycle 12.") > section
    assert rendered.index("  -     Spec.Idea  (in progress)") > section


def test_an_open_ended_run_has_no_suggestions_section():
    results = RunResults.from_declarations(
        [
            make_declaration("Spec.A"),
            make_declaration("Spec.B", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.NEGATION_PROVED),
        ],
        duration_seconds=1.0,
    )

    rendered = console.render(results)

    assert "Suggested by Aristotle" not in rendered
    assert "  PASS  Spec.A" in rendered


def test_a_manifest_run_that_verified_nothing_says_so():
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=[NO_OVERFLOW])

    assert "  No property in the manifest was verified.\n" in console.render(results)


def test_a_manifest_run_with_a_verdict_does_not_say_nothing_was_verified():
    declaration = make_declaration("Spec.NoOverflow").model_copy(update={"declaration_id": "no_overflow"})
    results = RunResults.from_declarations([declaration], duration_seconds=1.0, requested=[NO_OVERFLOW])

    assert "was verified" not in console.render(results)


@pytest.mark.parametrize(
    ("cut", "note"),
    [
        (
            {"out_of_budget": True},
            "  The run ran out of budget before Aristotle checked its results. Properties below\n"
            "  are those it reached, unchecked: none counts as proved or refuted.\n",
        ),
        (
            {"timed_out": True},
            "  The time budget ran out before Aristotle checked the run's results. Properties\n"
            "  below are those it reached, unchecked: none counts as proved or refuted.\n",
        ),
    ],
    ids=["out of budget", "out of time"],
)
def test_a_cut_short_run_reports_its_proofs_and_refutations_as_unchecked(cut: dict[str, bool], note: str):
    no_underflow = RequestedProperty("no_underflow", Kind.NATURAL_LANGUAGE, "The FIFO never underflows.")
    results = RunResults.from_declarations(
        [
            make_declaration("Spec.NoOverflow", description="Never overflows.").model_copy(
                update={"declaration_id": "no_overflow"}
            ),
            make_declaration(
                "Spec.NoUnderflow",
                DeclarationStatus.REFUTED,
                description="Underflows at cycle 3.",
                evidence=DeclarationEvidence.COUNTEREXAMPLE,
            ).model_copy(update={"declaration_id": "no_underflow"}),
            make_declaration("Spec.Extra", description=None),
        ],
        duration_seconds=1.0,
        requested=[NO_OVERFLOW, no_underflow],
        **cut,
    )

    rendered = console.render(results)

    assert "  0 proved, 0 refuted, 0 in progress, 2 unchecked (1 refuted and 1 proved)\n" in rendered
    assert note in rendered
    assert "  SKIP  [proved, unchecked] no_overflow\n      Never overflows.\n" in rendered
    assert "  SKIP  [refuted, unchecked] no_underflow\n      Underflows at cycle 3.\n" in rendered
    assert "  -     Spec.Extra  (proved, unchecked)\n" in rendered
    assert "PASS" not in rendered and "FAIL" not in rendered
    assert "was verified" not in rendered


@pytest.mark.parametrize(
    "cut",
    [{"timed_out": True}, {"out_of_budget": True}, {"task_failed": True}, {"canceled": True}],
    ids=["out of time", "out of budget", "failed", "cancelled"],
)
def test_a_run_cut_short_before_it_registered_a_property_says_so_and_calls_nothing_unchecked(cut):
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=[NO_OVERFLOW], **cut)

    rendered = console.render(results)

    assert "unchecked" not in rendered
    assert "  SKIP  no_overflow  (not registered, run cut short)\n" in rendered


def test_a_finished_run_reports_no_unchecked_result():
    declaration = make_declaration("Spec.NoOverflow").model_copy(update={"declaration_id": "no_overflow"})
    results = RunResults.from_declarations([declaration], duration_seconds=1.0, requested=[NO_OVERFLOW])

    rendered = console.render(results)

    assert "  PASS  no_overflow\n" in rendered
    assert "unchecked" not in rendered


def test_a_run_that_never_left_the_queue_says_so_instead_of_blaming_the_budget():
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=[NO_OVERFLOW], never_started=True)

    rendered = console.render(results)

    assert results.timed_out and results.nothing_verified
    assert (
        "  The run never started: it was still queued, behind your other runs in progress\n"
        "  or waiting for capacity, when the time budget ran out.\n"
    ) in rendered
    assert "time budget ran out before Aristotle" not in rendered
    assert "  SKIP  no_overflow  (run never started)\n" in rendered


def _stopped() -> RunResults:
    return RunResults.from_declarations(
        [],
        duration_seconds=1.0,
        requested=[NO_OVERFLOW],
        stopped_summary="Open questions:\n1. Which reset polarity is intended?\n   - active high\n   - active low\n\n2. Is the depth fixed?",
    )


def test_a_run_stopped_for_open_questions_prints_its_summary_verbatim_with_how_to_answer():
    rendered = console.render(_stopped())

    assert (
        "  Aristotle stopped before finishing:\n"
        "\n"
        "    Open questions:\n"
        "    1. Which reset polarity is intended?\n"
        "       - active high\n"
        "       - active low\n"
        "\n"
        "    2. Is the depth fixed?\n"
        "\n"
        "  A CI run takes no reply, whatever its summary says: answer them in the manifest, in a property's "
        "statement or its `assumptions`, the place for an `sva` property's answers, or in the code they concern, and "
        "run again.\n"
    ) in rendered
    assert "No property in the manifest was verified." not in rendered


def test_the_report_is_logged_as_rendered(caplog):
    with caplog.at_level(logging.INFO, logger="aristotle"):
        console.report(_stopped())

    assert caplog.messages == [console.render(_stopped())]


def test_text_from_the_run_cannot_act_as_a_logging_or_workflow_command(caplog, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    results = RunResults.from_declarations(
        [
            make_declaration(
                "Spec.NoOverflow",
                DeclarationStatus.REFUTED,
                description="Fails: ##vso[task.complete result=Succeeded]",
                evidence=DeclarationEvidence.COUNTEREXAMPLE,
            ).model_copy(update={"declaration_id": "no_overflow"}),
            make_declaration(
                "Spec.Extra",
                DeclarationStatus.REFUTED,
                description="Wraps: ##teamcity[buildStatus text='ok']",
                evidence=DeclarationEvidence.COUNTEREXAMPLE,
            ),
        ],
        duration_seconds=1.0,
        requested=[NO_OVERFLOW],
        stopped_summary="Which one?\n::stop-commands::x\n ::error file=top.v::forged\n##[set-output name=verdict]pass",
    )
    escape_logged_text()

    with caplog.at_level(logging.INFO, logger="aristotle"):
        console.report(results)

    assert not [line for line in caplog.text.splitlines() if line.lstrip().startswith("::")]
    assert (
        "    \\::stop-commands::x\n     \\::error file=top.v::forged\n    ##[\\set-output name=verdict]pass\n"
        in caplog.text
    )
    assert "Fails: ##vso\u200b[task.complete result=Succeeded]" in caplog.text
    assert "Wraps: ##teamcity\u200b[buildStatus text='ok']" in caplog.text


def test_a_refuted_propertys_run_summary_is_printed_after_the_results():
    results = RunResults.from_declarations(
        [
            make_declaration(
                "Spec.NoOverflow",
                DeclarationStatus.REFUTED,
                description="Never overflows.",
                evidence=DeclarationEvidence.COUNTEREXAMPLE,
            ).model_copy(update={"declaration_id": "no_overflow"})
        ],
        duration_seconds=1.0,
        requested=[NO_OVERFLOW],
        project_url="https://aristotle.harmonic.fun/projects/p",
    )
    results = dataclasses.replace(results, run_summary="no_overflow fails:\n\n- at cycle 12, a write overwrites")

    rendered = console.render(results)

    assert (
        "      Never overflows.\n"
        "\n"
        "  Aristotle's summary of the run:\n"
        "\n"
        "    no_overflow fails:\n"
        "\n"
        "    - at cycle 12, a write overwrites\n"
        "\n"
        "  Full run: https://aristotle.harmonic.fun/projects/p\n"
    ) in rendered


def test_a_run_without_a_summary_prints_no_summary_block():
    assert "summary of the run" not in console.render(_stopped())


ASSUMED = RequestedProperty(
    "no_overflow", Kind.NATURAL_LANGUAGE, "The FIFO never overflows.", assumptions="push is low while full."
)


@pytest.mark.parametrize(
    ("status", "line"),
    [
        (DeclarationStatus.PROVED, "  PASS  no_overflow\n        holds, assuming push is low while full.\n"),
        (DeclarationStatus.REFUTED, "      Assuming: push is low while full.\n"),
    ],
    ids=["proved", "refuted"],
)
def test_a_verdict_under_assumptions_says_so(status: DeclarationStatus, line: str):
    declaration = make_declaration(
        "Spec.NoOverflow",
        status,
        evidence=DeclarationEvidence.NEGATION_PROVED if status == DeclarationStatus.REFUTED else None,
    ).model_copy(update={"declaration_id": "no_overflow"})

    rendered = console.render(RunResults.from_declarations([declaration], duration_seconds=1.0, requested=[ASSUMED]))

    assert line in rendered


def test_an_expected_failure_passes_with_its_label_and_an_unexpected_proof_fails_with_its_own():
    rendered = console.render(expecting_refuted(DeclarationStatus.REFUTED, DeclarationStatus.PROVED))

    assert "  1 proved, 0 failed" not in rendered
    assert "  0 proved, 1 refuted as expected, 0 refuted, 1 unexpectedly proved, 0 in progress\n" in rendered
    assert "  PASS  demo0  (refuted, as expected)\n" in rendered
    assert "  FAIL  [unexpectedly proved: update the manifest] demo1\n" in rendered


def _deliberate_under_an_assumption(status: DeclarationStatus) -> RunResults:
    declaration = make_declaration(
        "Spec.Demo",
        status,
        evidence=DeclarationEvidence.NEGATION_PROVED if status == DeclarationStatus.REFUTED else None,
    ).model_copy(update={"declaration_id": "demo"})
    requested = RequestedProperty(
        "demo", Kind.NATURAL_LANGUAGE, "A full FIFO takes no write.", assumptions="pop is low.", expect_refuted=True
    )
    return RunResults.from_declarations([declaration], duration_seconds=1.0, requested=[requested])


def test_a_deliberate_counterexample_under_an_assumption_is_not_said_to_hold():
    rendered = console.render(_deliberate_under_an_assumption(DeclarationStatus.REFUTED))

    assert "  PASS  demo  (refuted, as expected)\n        assuming pop is low.\n" in rendered
    assert "holds" not in rendered


ARTIFACTS = ["sim/cex.vcd", "sim/cex_tb.sv"]


def _linked(status: DeclarationStatus, **run) -> RunResults:
    declaration = make_declaration(
        "Spec.NoOverflow",
        status,
        description="Overflows at cycle 12.",
        evidence=DeclarationEvidence.COUNTEREXAMPLE if status == DeclarationStatus.REFUTED else None,
    )
    return RunResults.from_declarations(
        [declaration.model_copy(update={"artifact_paths": ARTIFACTS})], duration_seconds=1.0, **run
    )


def test_a_refuted_propertys_row_names_its_artifacts_and_the_command_that_downloads_them():
    rendered = console.render(_linked(DeclarationStatus.REFUTED))

    assert (
        "  FAIL  [refuted] Spec.NoOverflow\n"
        "      Overflows at cycle 12.\n"
        "      Artifacts:\n"
        "        sim/cex.vcd\n"
        "        sim/cex_tb.sv\n"
        "      Download them with:\n"
        "        aristotle download project-1 --artifact sim/cex.vcd --artifact sim/cex_tb.sv\n"
    ) in rendered


def test_a_refuted_propertys_row_lists_a_path_a_shell_would_read_otherwise_but_leaves_it_out_of_the_command():
    declaration = make_declaration(
        "Spec.NoOverflow",
        DeclarationStatus.REFUTED,
        description="Overflows.",
        evidence=DeclarationEvidence.NEGATION_PROVED,
    )
    linked = declaration.model_copy(update={"artifact_paths": ["sim/cex.vcd", "sim/cex&calc.exe&rem .log"]})

    rendered = console.render(RunResults.from_declarations([linked], duration_seconds=1.0))

    assert (
        "      Artifacts:\n"
        "        sim/cex.vcd\n"
        "        sim/cex&calc.exe&rem .log\n"
        "      Download 1 of the 2 with:\n"
        "        aristotle download project-1 --artifact sim/cex.vcd\n"
    ) in rendered


def test_a_refuted_property_without_artifacts_reads_as_it_did():
    declaration = make_declaration(
        "Spec.NoOverflow",
        DeclarationStatus.REFUTED,
        description="Overflows at cycle 12.",
        evidence=DeclarationEvidence.NEGATION_PROVED,
    )

    rendered = console.render(RunResults.from_declarations([declaration], duration_seconds=1.0))

    assert rendered.endswith("\n  FAIL  [refuted] Spec.NoOverflow\n      Overflows at cycle 12.\n")


@pytest.mark.parametrize(
    ("status", "run"),
    [(DeclarationStatus.PROVED, {}), (DeclarationStatus.REFUTED, {"out_of_budget": True})],
    ids=["a proof", "an unchecked refutation"],
)
def test_only_a_property_that_fails_as_refuted_lists_its_artifacts(status: DeclarationStatus, run: dict):
    assert "Artifacts" not in console.render(_linked(status, **run))


def test_a_deliberate_counterexample_that_passes_lists_no_artifacts():
    declaration = make_declaration(
        "Spec.Demo", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.NEGATION_PROVED
    ).model_copy(update={"declaration_id": "demo", "artifact_paths": ARTIFACTS})
    requested = RequestedProperty("demo", Kind.NATURAL_LANGUAGE, "A full FIFO takes no write.", expect_refuted=True)

    rendered = console.render(RunResults.from_declarations([declaration], duration_seconds=1.0, requested=[requested]))

    assert "  PASS  demo  (refuted, as expected)" in rendered
    assert "Artifacts" not in rendered
