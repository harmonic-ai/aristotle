"""Tests for aristotlelib.ci.results."""

import dataclasses

import pytest

from aristotlelib.ci.manifest import Kind, RequestedProperty
from aristotlelib.ci.results import (
    CANCELED_REASON,
    EXPECTED_FAILURE,
    MAX_SUMMARY_CHARACTERS,
    NOT_REGISTERED,
    OUT_OF_BUDGET_REASON,
    TASK_FAILED_REASON,
    TIME_BUDGET_REASON,
    UNCHECKED_NOTE,
    UNEXPECTEDLY_PROVED,
    FailOn,
    PropertyResult,
    RunResults,
    failure_reason_label,
    proved,
    refuted,
    result_label,
)
from aristotlelib.declaration import Declaration, DeclarationFailureReason, DeclarationStatus, DeclarationType
from aristotlelib.exit_codes import EXIT_INFRASTRUCTURE_ERROR, EXIT_RATE_LIMITED, EXIT_SUCCESS, EXIT_VERIFICATION_FAILED
from tests.ci.conftest import make_declaration


def _results(*statuses: DeclarationStatus) -> RunResults:
    return RunResults.from_declarations(
        [make_declaration(f"Spec.P{i}", status) for i, status in enumerate(statuses)],
        duration_seconds=1.0,
    )


def test_properties_are_sorted_by_file_then_name():
    results = RunResults.from_declarations(
        [
            make_declaration("Spec.B", filename="b.lean"),
            make_declaration("Spec.C", filename="a.lean"),
            make_declaration("Spec.A", filename="a.lean"),
        ],
        duration_seconds=1.0,
    )

    assert [p.name for p in results.properties] == ["Spec.A", "Spec.C", "Spec.B"]
    assert results.suggestions == []


@pytest.mark.parametrize(
    "status,bucket",
    [
        (DeclarationStatus.PROVED, "passed"),
        (DeclarationStatus.NEGATED, "failed"),
        (DeclarationStatus.COUNTEREXAMPLE, "failed"),
        (DeclarationStatus.IN_PROGRESS, "without_verdict"),
        (DeclarationStatus.FAILED, "without_verdict"),
        (DeclarationStatus.UNKNOWN, "without_verdict"),
    ],
)
def test_each_status_lands_in_one_bucket(status: DeclarationStatus, bucket: str):
    results = _results(status)
    row = results.properties[0]

    assert row.declaration is not None
    assert (row.proved, row.refuted) == (proved(row.declaration), refuted(row.declaration))
    assert (row.proved, row.refuted) == (bucket == "passed", bucket == "failed")
    assert {name: len(getattr(results, name)) for name in ("passed", "failed", "without_verdict")} == {
        name: int(name == bucket) for name in ("passed", "failed", "without_verdict")
    }


def test_definitions_are_excluded_from_the_verdict():
    results = RunResults.from_declarations(
        [make_declaration("Spec.Helper", DeclarationStatus.IN_PROGRESS, DeclarationType.DEFINITION)],
        duration_seconds=1.0,
    )

    assert results.total == 0
    assert results.exit_code(FailOn.FAILURE) == EXIT_SUCCESS


@pytest.mark.parametrize("status", [DeclarationStatus.NEGATED, DeclarationStatus.COUNTEREXAMPLE])
def test_a_definitive_negative_fails_the_build(status: DeclarationStatus):
    assert _results(DeclarationStatus.PROVED, status).exit_code(FailOn.FAILURE) == EXIT_VERIFICATION_FAILED


def test_a_property_in_progress_does_not_fail_the_build_by_default():
    results = _results(DeclarationStatus.PROVED, DeclarationStatus.IN_PROGRESS)

    assert results.exit_code(FailOn.FAILURE) == EXIT_SUCCESS


def test_unresolved_is_the_old_name_of_no_verdict():
    assert FailOn("unresolved") is FailOn.NO_VERDICT
    assert str(FailOn("unresolved")) == "no-verdict"


def test_strict_mode_fails_on_a_property_without_a_verdict():
    results = _results(DeclarationStatus.PROVED, DeclarationStatus.IN_PROGRESS)

    assert results.exit_code(FailOn.NO_VERDICT) == EXIT_VERIFICATION_FAILED


def test_advisory_mode_never_fails_the_build():
    results = _results(DeclarationStatus.NEGATED, DeclarationStatus.COUNTEREXAMPLE)

    assert results.exit_code(FailOn.NEVER) == EXIT_SUCCESS


def test_an_empty_run_succeeds_by_default():
    assert _results().exit_code(FailOn.FAILURE) == EXIT_SUCCESS


def test_strict_mode_fails_an_empty_run():
    assert _results().exit_code(FailOn.NO_VERDICT) == EXIT_VERIFICATION_FAILED


def _failed_task(*statuses: DeclarationStatus) -> RunResults:
    return RunResults.from_declarations(
        [make_declaration(f"Spec.P{i}", status) for i, status in enumerate(statuses)],
        duration_seconds=1.0,
        task_failed=True,
    )


def test_a_failed_task_with_a_refutation_is_a_verification_failure():
    assert _failed_task(DeclarationStatus.NEGATED).exit_code(FailOn.FAILURE) == EXIT_VERIFICATION_FAILED


@pytest.mark.parametrize("fail_on", [FailOn.FAILURE, FailOn.NO_VERDICT])
def test_a_failed_task_without_a_refutation_is_an_infrastructure_error(fail_on: FailOn):
    assert _failed_task(DeclarationStatus.PROVED).exit_code(fail_on) == EXIT_INFRASTRUCTURE_ERROR


@pytest.mark.parametrize("statuses", [(), (DeclarationStatus.PROVED,), (DeclarationStatus.NEGATED,)])
def test_advisory_mode_does_not_fail_the_build_for_a_failed_task(statuses: tuple[DeclarationStatus, ...]):
    assert _failed_task(*statuses).exit_code(FailOn.NEVER) == EXIT_SUCCESS


@pytest.mark.parametrize("fail_on", [FailOn.FAILURE, FailOn.NO_VERDICT])
def test_a_cancelled_task_without_a_refutation_is_an_infrastructure_error(fail_on: FailOn):
    results = RunResults.from_declarations([make_declaration()], duration_seconds=1.0, canceled=True)

    assert results.exit_code(fail_on) == EXIT_INFRASTRUCTURE_ERROR
    assert results.exit_code(FailOn.NEVER) == EXIT_SUCCESS


def test_a_cancelled_task_with_a_refutation_is_a_verification_failure():
    results = RunResults.from_declarations(
        [make_declaration(status=DeclarationStatus.NEGATED)], duration_seconds=1.0, canceled=True
    )

    assert results.exit_code(FailOn.FAILURE) == EXIT_VERIFICATION_FAILED


def test_an_unknown_status_counts_as_unresolved_rather_than_passing():
    # A status this client does not know must not count as a pass.
    results = _results(DeclarationStatus.UNKNOWN)

    assert len(results.without_verdict) == 1
    assert results.exit_code(FailOn.NO_VERDICT) == EXIT_VERIFICATION_FAILED
    # Nor as work still going.
    assert results.summary_line() == "0 proved, 0 disproved, 0 in progress, 1 unknown"


def test_a_run_that_stopped_is_unresolved_rather_than_disproved():
    # FAILED is the run's failure, not the property's: it has no verdict.
    results = _results(DeclarationStatus.PROVED, DeclarationStatus.FAILED)

    assert results.failed == []
    assert len(results.without_verdict) == 1
    assert results.exit_code(FailOn.FAILURE) == EXIT_SUCCESS
    assert results.exit_code(FailOn.NO_VERDICT) == EXIT_VERIFICATION_FAILED
    assert results.summary_line() == "1 proved, 0 disproved, 0 in progress, 1 unresolved"


@pytest.mark.parametrize(
    "reason,expected",
    [
        (DeclarationFailureReason.OUT_OF_BUDGET, "unresolved (out of budget)"),
        (DeclarationFailureReason.SYSTEM_ERROR, "unresolved (system error)"),
        (
            DeclarationFailureReason.PROOF_CHECK_FAILED,
            "unresolved (the proof no longer checks; re-run to re-prove)",
        ),
        (DeclarationFailureReason.INCOMPLETE, "unresolved (incomplete)"),
        (DeclarationFailureReason.CANCELED, "unresolved (cancelled)"),
        (DeclarationFailureReason.UNKNOWN, "unresolved (reason not reported)"),
        (None, "unresolved (reason not reported)"),
    ],
)
def test_the_label_of_an_unverified_property_carries_its_reason(reason: DeclarationFailureReason | None, expected: str):
    declaration = make_declaration("Spec.P", DeclarationStatus.FAILED, failure_reason=reason)

    assert result_label(declaration) == expected


@pytest.mark.parametrize("fail_on", list(FailOn))
def test_a_proof_that_no_longer_checks_counts_like_any_other_unverified_property(fail_on: FailOn):
    def run(reason: DeclarationFailureReason) -> RunResults:
        declaration = make_declaration("Spec.P", DeclarationStatus.FAILED, failure_reason=reason)
        return RunResults.from_declarations([declaration], duration_seconds=1.0)

    broken, out_of_budget = (
        run(DeclarationFailureReason.PROOF_CHECK_FAILED),
        run(DeclarationFailureReason.OUT_OF_BUDGET),
    )

    assert broken.exit_code(fail_on) == out_of_budget.exit_code(fail_on)
    assert broken.summary_line() == out_of_budget.summary_line() == "0 proved, 0 disproved, 0 in progress, 1 unresolved"


def test_a_verdict_carries_no_failure_reason():
    assert failure_reason_label(make_declaration("Spec.P", DeclarationStatus.PROVED)) is None
    assert result_label(make_declaration("Spec.P", DeclarationStatus.NEGATED)) == "disproved"


def test_summary_line_counts_each_bucket():
    results = _results(
        DeclarationStatus.PROVED,
        DeclarationStatus.PROVED,
        DeclarationStatus.COUNTEREXAMPLE,
        DeclarationStatus.IN_PROGRESS,
    )

    assert results.summary_line() == "2 proved, 1 disproved, 1 in progress"


def _declared(declaration_id: str, name: str = "Spec.P", **kwargs) -> Declaration:
    """A declaration registered under ``declaration_id``, whatever its name."""
    return make_declaration(name, **kwargs).model_copy(update={"declaration_id": declaration_id})


def _requested(*ids: str) -> list[RequestedProperty]:
    return [RequestedProperty(id=i, kind=Kind.NATURAL_LANGUAGE, statement=f"{i} holds.") for i in ids]


def test_requested_properties_are_matched_by_id_in_manifest_order():
    zeta = _declared("zeta", name="alpha", status=DeclarationStatus.PROVED)
    alpha = _declared("alpha", name="zeta", status=DeclarationStatus.NEGATED)

    results = RunResults.from_declarations([alpha, zeta], duration_seconds=1.0, requested=_requested("zeta", "alpha"))

    assert results.properties == [
        PropertyResult("zeta", zeta, "zeta holds."),
        PropertyResult("alpha", alpha, "alpha holds."),
    ]
    assert results.suggestions == []
    assert results.not_registered == []


def test_a_requested_id_with_nothing_registered_is_an_unresolved_row():
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=_requested("fifo"))
    row = results.properties[0]

    assert (row.name, row.declaration) == ("fifo", None)
    assert (row.label, row.result_label, row.failure_reason) == (NOT_REGISTERED, NOT_REGISTERED, None)
    assert row.description == "fifo holds."
    assert results.not_registered == results.without_verdict == [row]
    assert results.summary_line() == "0 proved, 0 disproved, 0 in progress, 1 not registered"


def test_a_property_registered_under_another_id_is_a_suggestion():
    renamed = _declared("fifo-overflow", name="fifo", status=DeclarationStatus.PROVED)

    results = RunResults.from_declarations([renamed], duration_seconds=1.0, requested=_requested("fifo"))

    assert results.properties == [PropertyResult("fifo", None, "fifo holds.")]
    assert results.suggestions == [PropertyResult("fifo", renamed)]
    assert results.passed == []


def test_a_withdrawn_property_under_a_requested_id_counts_as_not_registered():
    withdrawn = _declared("fifo", status=DeclarationStatus.REMOVED)

    results = RunResults.from_declarations([withdrawn], duration_seconds=1.0, requested=_requested("fifo"))

    assert results.properties == [PropertyResult("fifo", None, "fifo holds.")]
    assert results.not_registered == results.properties
    assert results.suggestions == []


def test_a_definition_under_a_requested_id_does_not_match():
    definition = _declared("fifo", declaration_type=DeclarationType.DEFINITION)

    results = RunResults.from_declarations([definition], duration_seconds=1.0, requested=_requested("fifo"))

    assert results.properties == [PropertyResult("fifo", None, "fifo holds.")]
    assert results.suggestions == []


def test_suggestions_are_the_other_live_properties_sorted_by_file_then_name():
    results = RunResults.from_declarations(
        [
            _declared("fifo"),
            _declared("s1", name="Spec.B", filename="b.lean"),
            _declared("s2", name="Spec.C", filename="a.lean"),
            _declared("s3", name="Spec.A", filename="a.lean"),
            _declared("s4", name="Spec.Withdrawn", status=DeclarationStatus.REMOVED),
            _declared("s5", name="Spec.Helper", declaration_type=DeclarationType.DEFINITION),
        ],
        duration_seconds=1.0,
        requested=_requested("fifo"),
    )

    assert [s.name for s in results.suggestions] == ["Spec.A", "Spec.C", "Spec.B"]
    assert all(s.requested is None for s in results.suggestions)


@pytest.mark.parametrize("fail_on", [FailOn.FAILURE, FailOn.NO_VERDICT])
def test_a_refuted_suggestion_does_not_fail_the_build(fail_on: FailOn):
    results = RunResults.from_declarations(
        [_declared("fifo"), _declared("extra", name="Spec.Extra", status=DeclarationStatus.COUNTEREXAMPLE)],
        duration_seconds=1.0,
        requested=_requested("fifo"),
    )

    assert results.failed == []
    assert results.exit_code(fail_on) == EXIT_SUCCESS


def test_a_disproved_requested_property_fails_the_build():
    results = RunResults.from_declarations(
        [_declared("a"), _declared("b", name="Spec.B", status=DeclarationStatus.NEGATED)],
        duration_seconds=1.0,
        requested=_requested("a", "b"),
    )

    assert results.exit_code(FailOn.FAILURE) == EXIT_VERIFICATION_FAILED


@pytest.mark.parametrize(
    ("declarations", "kwargs"),
    [
        ([], {}),
        ([_declared("a", status=DeclarationStatus.IN_PROGRESS), _declared("b", status=DeclarationStatus.FAILED)], {}),
        ([_declared("elsewhere", status=DeclarationStatus.PROVED)], {}),
    ],
    ids=["nothing registered", "no verdicts", "only a suggestion proved"],
)
@pytest.mark.parametrize(
    ("fail_on", "expected"),
    [
        (FailOn.FAILURE, EXIT_VERIFICATION_FAILED),
        (FailOn.NO_VERDICT, EXIT_VERIFICATION_FAILED),
        (FailOn.NEVER, EXIT_SUCCESS),
    ],
)
def test_a_manifest_run_that_verified_no_property_fails_unless_advisory(declarations, kwargs, fail_on, expected):
    results = RunResults.from_declarations(declarations, duration_seconds=1.0, requested=_requested("a", "b"), **kwargs)

    assert results.nothing_verified
    assert results.exit_code(fail_on) == expected


@pytest.mark.parametrize("status", [DeclarationStatus.PROVED, DeclarationStatus.COUNTEREXAMPLE])
def test_one_verdict_is_enough_for_a_manifest_run_to_have_verified_something(status: DeclarationStatus):
    results = RunResults.from_declarations(
        [_declared("a", status=status)], duration_seconds=1.0, requested=_requested("a", "b")
    )

    assert not results.nothing_verified


@pytest.mark.parametrize("stopped", ["task_failed", "canceled"])
def test_a_failed_or_cancelled_run_is_an_infrastructure_error_not_a_run_that_verified_nothing(stopped: str):
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=_requested("a"), **{stopped: True})

    assert not results.nothing_verified
    assert results.exit_code(FailOn.FAILURE) == EXIT_INFRASTRUCTURE_ERROR


def test_an_open_ended_run_with_no_verdicts_passes_by_default():
    results = RunResults.from_declarations(
        [make_declaration(status=DeclarationStatus.IN_PROGRESS)], duration_seconds=1.0
    )

    assert not results.nothing_verified
    assert results.exit_code(FailOn.FAILURE) == EXIT_SUCCESS


@pytest.mark.parametrize(
    "declaration,expected",
    [
        (make_declaration(description="What the run checked."), "What the run checked."),
        (make_declaration(description=None), "What the manifest asked for."),
        (make_declaration(description=""), "What the manifest asked for."),
        (None, "What the manifest asked for."),
    ],
    ids=["described", "undescribed", "empty", "not registered"],
)
def test_a_rows_description_prefers_the_runs_over_the_manifests(declaration: Declaration | None, expected: str):
    assert PropertyResult("p", declaration, "What the manifest asked for.").description == expected


CUT_SHORT = [{"out_of_budget": True}, {"timed_out": True}]
CUT_SHORT_IDS = ["out of budget", "out of time"]


@pytest.mark.parametrize(
    ("cut", "reason"), zip(CUT_SHORT, [OUT_OF_BUDGET_REASON, TIME_BUDGET_REASON]), ids=CUT_SHORT_IDS
)
def test_a_cut_short_run_reports_every_proof_and_disproof_as_unchecked(cut: dict[str, bool], reason: str):
    results = RunResults.from_declarations(
        [
            _declared("a", status=DeclarationStatus.PROVED),
            _declared("b", status=DeclarationStatus.NEGATED),
            _declared("c", status=DeclarationStatus.IN_PROGRESS),
            _declared("extra", name="Spec.Extra", status=DeclarationStatus.PROVED),
        ],
        duration_seconds=1.0,
        requested=_requested("a", "b", "c"),
        **cut,
    )

    assert results.cut_short and results.unchecked_reason == reason
    assert results.passed == results.failed == []
    assert [(p.unchecked_verdict, p.result_label) for p in results.without_verdict] == [
        (True, "disproved, unchecked"),
        (True, "proved, unchecked"),
        (False, "in progress"),
    ]
    assert [s.result_label for s in results.suggestions] == ["proved, unchecked"]
    assert results.summary_line() == "0 proved, 0 disproved, 1 in progress, 2 unchecked (1 disproved and 1 proved)"


@pytest.mark.parametrize("cut", CUT_SHORT, ids=CUT_SHORT_IDS)
@pytest.mark.parametrize("requested", [_requested("a"), None], ids=["manifest", "open-ended"])
@pytest.mark.parametrize("status", [DeclarationStatus.PROVED, DeclarationStatus.NEGATED], ids=["proved", "negated"])
@pytest.mark.parametrize(
    ("fail_on", "expected"),
    [
        (FailOn.FAILURE, EXIT_INFRASTRUCTURE_ERROR),
        (FailOn.NO_VERDICT, EXIT_INFRASTRUCTURE_ERROR),
        (FailOn.NEVER, EXIT_SUCCESS),
    ],
)
def test_a_cut_short_run_is_an_infrastructure_error_unless_advisory_and_ended(
    cut: dict[str, bool], requested, status: DeclarationStatus, fail_on: FailOn, expected: int
):
    """A run the time budget cut hasn't ended, so --fail-on never doesn't pass it either."""
    results = RunResults.from_declarations(
        [_declared("a", status=status)], duration_seconds=1.0, requested=requested, **cut
    )

    assert not results.nothing_verified
    assert results.exit_code(fail_on) == (EXIT_INFRASTRUCTURE_ERROR if cut.get("timed_out") else expected)


@pytest.mark.parametrize("cut", CUT_SHORT, ids=CUT_SHORT_IDS)
def test_a_cut_short_manifest_run_with_nothing_registered_is_not_one_that_verified_nothing(cut: dict[str, bool]):
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=_requested("a"), **cut)

    assert not results.nothing_verified
    assert results.exit_code(FailOn.FAILURE) == EXIT_INFRASTRUCTURE_ERROR


@pytest.mark.parametrize(
    ("cut", "requested", "reason"),
    [
        (
            {"out_of_budget": True},
            _requested("a"),
            "The run ran out of budget with no property under any of the manifest's ids.",
        ),
        (
            {"timed_out": True},
            _requested("a"),
            "The time budget ran out with no property under any of the manifest's ids.",
        ),
        ({"timed_out": True}, None, "The time budget ran out with no property in the run."),
    ],
    ids=["out of budget", "out of time", "open-ended"],
)
def test_a_cut_short_run_with_nothing_registered_says_so_rather_than_that_its_results_are_unchecked(
    cut: dict[str, bool], requested, reason: str
):
    # A suggestion is not one of the manifest's properties.
    suggestion = [_declared("extra", name="Spec.Extra", status=DeclarationStatus.PROVED)] if requested else []

    results = RunResults.from_declarations(suggestion, duration_seconds=1.0, requested=requested, **cut)

    assert results.nothing_registered
    assert results.unchecked_reason == results.cut_short_note == reason


@pytest.mark.parametrize("cut", CUT_SHORT, ids=CUT_SHORT_IDS)
def test_the_note_on_a_cut_short_run_that_reached_a_property_says_what_it_reached_is_unchecked(cut: dict[str, bool]):
    results = RunResults.from_declarations(
        [_declared("a", status=DeclarationStatus.IN_PROGRESS)],
        duration_seconds=1.0,
        requested=_requested("a", "b"),
        **cut,
    )

    assert not results.nothing_registered
    assert results.cut_short_note == f"{results.unchecked_reason} {UNCHECKED_NOTE}"


def test_a_run_that_was_not_cut_short_has_no_note():
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=_requested("a"))

    assert results.nothing_registered and results.cut_short_note is None


def test_a_run_that_never_started_is_not_cut_short():
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=_requested("a"), never_started=True)

    assert results.timed_out and not results.cut_short
    assert results.unchecked_reason is None


@pytest.mark.parametrize("requested", [_requested("a"), None], ids=["manifest", "open-ended"])
@pytest.mark.parametrize("fail_on", list(FailOn))
def test_a_run_still_queued_when_the_budget_ran_out_is_rate_limited_whatever_fail_on_is(requested, fail_on: FailOn):
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=requested, never_started=True)

    assert results.exit_code(fail_on) == EXIT_RATE_LIMITED


def test_a_run_that_finished_keeps_its_verdicts():
    results = RunResults.from_declarations(
        [_declared("a", status=DeclarationStatus.PROVED)], duration_seconds=1.0, requested=_requested("a")
    )

    assert not results.cut_short and results.unchecked_reason is None
    assert [p.result_label for p in results.passed] == ["proved"]
    assert results.summary_line() == "1 proved, 0 disproved, 0 in progress"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [({"task_failed": True}, TASK_FAILED_REASON), ({"canceled": True}, CANCELED_REASON)],
    ids=["failed", "cancelled"],
)
def test_a_run_that_failed_or_was_cancelled_leaves_its_proofs_unchecked(kwargs: dict[str, bool], reason: str):
    results = RunResults.from_declarations(
        [_declared("a", status=DeclarationStatus.PROVED)], duration_seconds=1.0, requested=_requested("a"), **kwargs
    )

    assert not results.cut_short and results.cut_short_note is None
    assert results.unchecked_reason == reason
    assert results.passed == []
    assert [p.result_label for p in results.without_verdict] == ["proved, unchecked"]
    assert results.summary_line() == "0 proved, 0 disproved, 0 in progress, 1 unchecked (1 proved)"


@pytest.mark.parametrize(("fail_on", "expected"), [(FailOn.FAILURE, 1), (FailOn.NO_VERDICT, 1), (FailOn.NEVER, 0)])
def test_a_run_that_stopped_with_work_left_fails_the_build_unless_fail_on_is_never(fail_on: FailOn, expected: int):
    results = RunResults.from_declarations(
        [make_declaration("Spec.A", DeclarationStatus.PROVED)],
        duration_seconds=1.0,
        requested=[RequestedProperty(id="spec-a", kind=Kind.NATURAL_LANGUAGE, statement="A holds.")],
        stopped_summary="Which reset polarity is intended?",
    )

    assert results.exit_code(fail_on) == expected
    # It names its own cause, so it isn't reported as a run that verified nothing.
    assert not results.nothing_verified


def _with_summary(summary: str) -> RunResults:
    return dataclasses.replace(_results(DeclarationStatus.NEGATED), run_summary=summary)


def test_a_short_run_summary_is_carried_whole():
    assert _with_summary("Fails at cycle 12.").run_summary_excerpt == "Fails at cycle 12."


def test_a_long_run_summary_is_cut_back_to_a_line_end_and_says_so():
    line = "x" * 99
    summary = "\n".join([line] * 100)

    excerpt = _with_summary(summary).run_summary_excerpt

    assert excerpt is not None
    kept, note = excerpt.rsplit("\n", 1)
    assert len(kept) <= MAX_SUMMARY_CHARACTERS and kept.endswith(line) and summary.startswith(kept)
    assert note == f"… (cut at {MAX_SUMMARY_CHARACTERS} characters; the job log has the whole summary)"


def test_a_long_run_summary_without_line_ends_is_cut_at_the_limit():
    excerpt = _with_summary("y" * (MAX_SUMMARY_CHARACTERS * 2)).run_summary_excerpt

    assert excerpt is not None and excerpt.startswith("y" * MAX_SUMMARY_CHARACTERS + "\n… (cut at")


def test_a_run_without_a_summary_has_no_excerpt():
    assert _results(DeclarationStatus.NEGATED).run_summary_excerpt is None


def _expecting(status: DeclarationStatus, **run: bool) -> RunResults:
    """A manifest run with one property the manifest expects disproved, which the run left with ``status``."""
    declaration = make_declaration("Spec.Demo", status).model_copy(update={"declaration_id": "demo"})
    requested = [RequestedProperty("demo", Kind.NATURAL_LANGUAGE, "A full FIFO takes no write.", expect_disproved=True)]
    return RunResults.from_declarations([declaration], duration_seconds=1.0, requested=requested, **run)


@pytest.mark.parametrize("status", [DeclarationStatus.NEGATED, DeclarationStatus.COUNTEREXAMPLE])
def test_a_disproof_the_manifest_expects_passes_as_an_expected_failure(status: DeclarationStatus):
    results = _expecting(status)

    assert [p.name for p in results.passed] == ["demo"]
    assert results.failed == [] and results.without_verdict == []
    assert (results.passed[0].label, results.passed[0].result_label) == (EXPECTED_FAILURE, EXPECTED_FAILURE)
    assert results.summary_line() == "0 proved, 1 disproved as expected, 0 disproved, 0 in progress"
    assert results.exit_code(FailOn.FAILURE) == EXIT_SUCCESS
    assert results.exit_code(FailOn.NO_VERDICT) == EXIT_SUCCESS


def test_a_proof_of_a_property_the_manifest_expects_disproved_fails():
    results = _expecting(DeclarationStatus.PROVED)

    assert [p.name for p in results.failed] == ["demo"]
    assert results.passed == []
    assert results.failed[0].label == UNEXPECTEDLY_PROVED
    assert results.summary_line() == "0 proved, 0 disproved, 1 unexpectedly proved, 0 in progress"
    assert results.exit_code(FailOn.FAILURE) == EXIT_VERIFICATION_FAILED


@pytest.mark.parametrize(
    ("status", "run"),
    [
        (DeclarationStatus.IN_PROGRESS, {}),
        (DeclarationStatus.NEGATED, {"out_of_budget": True}),
        (DeclarationStatus.PROVED, {"canceled": True}),
    ],
    ids=["no verdict", "unchecked disproof", "unchecked proof"],
)
def test_a_property_the_manifest_expects_disproved_without_a_checked_verdict_is_unresolved(status, run):
    results = _expecting(status, **run)

    assert [p.name for p in results.without_verdict] == ["demo"]
    assert results.passed == [] and results.failed == []
    assert results.without_verdict[0].result_label not in (EXPECTED_FAILURE, UNEXPECTEDLY_PROVED)
