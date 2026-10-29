"""Classification of a run's declarations into a build verdict."""

import dataclasses
from enum import Enum

from aristotlelib.agent_task import BudgetFailureReason
from aristotlelib.ci.manifest import RequestedProperty
from aristotlelib.declaration import Declaration, DeclarationFailureReason, DeclarationStatus, DeclarationType
from aristotlelib.exit_codes import (
    EXIT_INFRASTRUCTURE_ERROR,
    EXIT_RATE_LIMITED,
    EXIT_SUCCESS,
    EXIT_VERIFICATION_FAILED,
)
from aristotlelib.project import download_artifacts_hint


class FailOn(str, Enum):
    FAILURE = "failure"
    """Fail the build on a definitive negative result, or when Aristotle stopped the run for decisions only the user
    can make. The default."""

    NO_VERDICT = "no-verdict"
    """Also fail when any property has no verdict, whether in progress, unresolved or unchecked, or, outside a manifest
    run, the run registered none. ``unresolved``, its old name, still reads as this."""

    NEVER = "never"
    """Exit 0 once a run has finished, whatever its verdicts or task status. Advisory mode."""

    def __str__(self) -> str:
        return self.value

    @classmethod
    def _missing_(cls, value: object) -> "FailOn | None":
        return FAIL_ON_ALIASES.get(value) if isinstance(value, str) else None


FAIL_ON_ALIASES = {"unresolved": FailOn.NO_VERDICT}
"""Old names --fail-on, ARISTOTLE_FAIL_ON and the Action's fail-on input still take, though none lists them."""


#: Human-readable labels for each status, for report output, in the words every command uses for its outcome.
STATUS_LABELS = {
    DeclarationStatus.PROVED: "proved",
    DeclarationStatus.REFUTED: "refuted",
    DeclarationStatus.IN_PROGRESS: "in progress",
    DeclarationStatus.UNKNOWN: "unknown",
    DeclarationStatus.REMOVED: "withdrawn",
    DeclarationStatus.FAILED: "unresolved",
}

#: Why a FAILED property has no verdict, for report output.
FAILURE_REASON_LABELS = {
    DeclarationFailureReason.OUT_OF_BUDGET: "out of budget",
    DeclarationFailureReason.SYSTEM_ERROR: "system error",
    DeclarationFailureReason.PROOF_CHECK_FAILED: "the proof no longer checks; re-run to re-prove",
    DeclarationFailureReason.INCOMPLETE: "incomplete",
    DeclarationFailureReason.CANCELED: "cancelled",
}


def proved(declaration: Declaration) -> bool:
    return declaration.status == DeclarationStatus.PROVED


def refuted(declaration: Declaration) -> bool:
    # FAILED is unresolved: the run stopped first, or its proof no longer checks.
    return declaration.status == DeclarationStatus.REFUTED


def status_label(status: DeclarationStatus) -> str:
    return STATUS_LABELS.get(status, status.name.lower())


def failure_reason_label(declaration: Declaration) -> str | None:
    """Why a ``FAILED`` property has no verdict, or None for every other status."""
    if declaration.status != DeclarationStatus.FAILED:
        return None
    reason = declaration.failure_reason
    if reason is None or reason == DeclarationFailureReason.UNKNOWN:
        return "reason not reported"
    return FAILURE_REASON_LABELS.get(reason, reason.name.lower().replace("_", " "))


def result_label(declaration: Declaration) -> str:
    """The status label, with the reason a property is unresolved after it."""
    reason = failure_reason_label(declaration)
    label = status_label(declaration.status)
    return f"{label} ({reason})" if reason else label


NOT_REGISTERED = "not registered"
"""The result label of a manifest property the run registered nothing under."""

NOT_REGISTERED_CUT = "not registered, run cut short"
"""The result label of a manifest property a run had registered nothing under when it was cut short: by its budget or
the time budget, or by failing or being cancelled."""

EXPECTED_FAILURE = "refuted, as expected"
"""The result label of a property the manifest expects to be refuted, and that was: it passes."""

UNEXPECTEDLY_PROVED = "unexpectedly proved: update the manifest"
"""The result label of a property the manifest expects to be refuted, but that was proved: it fails."""

NEVER_STARTED = "run never started"
"""The result label of a manifest property whose run was still queued when the time budget ran out."""

STOPPED_REASON = "Aristotle stopped the run for decisions only you can make."
"""Why the properties of a run Aristotle stopped for questions have no verdict."""

FINISHED_UNCHECKED_REASON = "The run finished without checking it."
"""Why a property still in progress when its run finished normally has no verdict."""

NOT_REGISTERED_REASON = "Aristotle registered no property under this id, or withdrew it."
"""Why a manifest property of a run that finished normally has nothing registered under its id."""

NEVER_STARTED_REASON = (
    "The run never started: it was still queued, behind your other runs in progress or waiting for capacity, when "
    "the time budget ran out."
)
"""Why a run that never left the queue verified nothing."""

OUT_OF_BUDGET_REASON = "The run ran out of budget before Aristotle checked its results."
"""Why the property results of a run that ended OUT_OF_BUDGET are unchecked."""

BILLING_STOP_REASON = "Billing stopped the run before Aristotle checked its results."
"""Why the property results of a run billing stopped, which ends OUT_OF_BUDGET, are unchecked."""

TASK_FAILED_REASON = "The run failed due to an internal error before Aristotle checked its results."
"""Why the property results of a run that ended FAILED are unchecked."""

CANCELED_REASON = "The run was cancelled outside this job before Aristotle checked its results."
"""Why the property results of a run cancelled outside this job are unchecked."""

PROOF_CHECK_FAILED_REASON = (
    "Aristotle marked it proved, but at the end of the run its proof was missing, didn't build or used `sorry`. "
    "Re-run to re-prove it."
)
"""Why a property whose proof no longer checks has no verdict."""

TIME_BUDGET_REASON = "The time budget ran out before Aristotle checked the run's results."
"""Why the property results of a run that hit this job's time budget after it started are unchecked."""

UNCHECKED_NOTE = "Properties below are those it reached, unchecked: none counts as proved or refuted."
"""Follows the unchecked reason above a cut-short run's properties."""

UNCHECKED_PROOFS_NOTE = (
    "Properties below are those it reached: a proof among them is unchecked, but a refutation counts."
)
"""Follows the notice of a run that failed or was cancelled, which never checked the proofs it reported."""

MAX_SUMMARY_CHARACTERS = 4000
"""Most of a run's summary that the JUnit report and the GitHub job summary carry; the log prints it whole."""


def excerpt(summary: str) -> str:
    """``summary``, or its first MAX_SUMMARY_CHARACTERS cut back to a line end, with a note saying so."""
    if len(summary) <= MAX_SUMMARY_CHARACTERS:
        return summary
    cut = summary[:MAX_SUMMARY_CHARACTERS]
    if (line_end := cut.rfind("\n")) > MAX_SUMMARY_CHARACTERS // 2:
        cut = cut[:line_end]
    return f"{cut.rstrip()}\n… (cut at {MAX_SUMMARY_CHARACTERS} characters; the job log has the whole summary)"


@dataclasses.dataclass(frozen=True)
class PropertyResult:
    """One property in the verdict, and what the run registered for it."""

    name: str
    """The manifest id, or the declaration's name in an open-ended run."""
    declaration: Declaration | None
    """None when the run registered no property under the manifest id."""
    requested: str | None = None
    """The manifest's statement of the property; None in an open-ended run."""
    never_started: bool = False
    """The run was still queued when the time budget ran out."""
    cut: bool = False
    """The run ended before finishing, as it does when it runs out of budget, fails or is cancelled elsewhere, or the
    time budget ran out after it started."""
    unchecked: bool = False
    """Aristotle had not checked the run's results, so what it reported for the property is the agent's claim."""
    assumptions: str | None = None
    """The manifest's ``assumptions`` for the property, under which its verdict holds; None when it states none."""
    expect_refuted: bool = False
    """The manifest expects a counterexample: a refutation passes and a proof fails."""

    @property
    def passed(self) -> bool:
        """A checked verdict the manifest wants: a proof, or a refutation it expects."""
        return self.refuted if self.expect_refuted else self.proved

    @property
    def failed(self) -> bool:
        """A checked verdict the manifest doesn't want: a refutation, or a proof of a property it expects refuted."""
        return self.proved if self.expect_refuted else self.refuted

    @property
    def proved(self) -> bool:
        return not self.unchecked and self.declaration is not None and proved(self.declaration)

    @property
    def refuted(self) -> bool:
        return not self.unchecked and self.declaration is not None and refuted(self.declaration)

    @property
    def unchecked_verdict(self) -> bool:
        """A proof or refutation the run reported but nothing checked, which therefore has no verdict."""
        return (
            self.unchecked and self.declaration is not None and (proved(self.declaration) or refuted(self.declaration))
        )

    @property
    def label(self) -> str:
        if self.declaration is None:
            return self._unregistered_label
        return self._expectation_label or status_label(self.declaration.status)

    @property
    def result_label(self) -> str:
        if self.declaration is None:
            return self._unregistered_label
        if expectation := self._expectation_label:
            return expectation
        label = result_label(self.declaration)
        return f"{label}, unchecked" if self.unchecked_verdict else label

    @property
    def _expectation_label(self) -> str | None:
        """The label of a checked verdict on a property the manifest expects refuted; None otherwise."""
        if not self.expect_refuted:
            return None
        if self.refuted:
            return EXPECTED_FAILURE
        if self.proved:
            return UNEXPECTEDLY_PROVED
        return None

    @property
    def _unregistered_label(self) -> str:
        if self.never_started:
            return NEVER_STARTED
        return NOT_REGISTERED_CUT if self.cut else NOT_REGISTERED

    @property
    def failure_reason(self) -> str | None:
        return None if self.declaration is None else failure_reason_label(self.declaration)

    @property
    def description(self) -> str | None:
        """What the run said of the property, else what the manifest asked for."""
        if self.declaration is not None and self.declaration.description:
            return self.declaration.description
        return self.requested

    @property
    def artifact_paths(self) -> list[str]:
        """The files Aristotle linked to a property that fails as refuted, such as its counterexample's trace; none
        for any other."""
        if self.declaration is None or not (self.failed and self.refuted):
            return []
        return self.declaration.artifact_paths


def artifact_lines(result: PropertyResult) -> list[str]:
    """``result``'s artifact_paths under "Artifacts:", one a line, then the command that downloads them, as
    download_artifacts_hint() gives it; none without."""
    if result.declaration is None or not result.artifact_paths:
        return []
    hint = download_artifacts_hint(result.declaration.project_id, result.artifact_paths) or ""
    return ["Artifacts:", *(f"  {path}" for path in result.artifact_paths), *hint.splitlines()]


def _reported_properties(declarations: list[Declaration]) -> list[Declaration]:
    """The registered, unwithdrawn properties, sorted by reported path, then name."""
    properties = [
        d
        for d in declarations
        if d.declaration_type == DeclarationType.PROPERTY and d.status != DeclarationStatus.REMOVED
    ]
    properties.sort(key=lambda d: (d.source_file or d.filename, d.name))
    return properties


@dataclasses.dataclass(frozen=True)
class RunResults:
    """The outcome of a verification run, sorted into build-relevant buckets."""

    properties: list[PropertyResult]
    duration_seconds: float
    project_url: str | None = None
    timed_out: bool = False
    never_started: bool = False
    """The time budget ran out while the run was still queued; implies ``timed_out``."""
    task_failed: bool = False
    """The task itself ended FAILED; the properties are whatever it reached first, its proofs unchecked."""
    canceled: bool = False
    """The task was cancelled outside this job before it finished; the properties are whatever it reached first, its
    proofs unchecked."""
    out_of_budget: bool = False
    """The task ended OUT_OF_BUDGET; the properties are whatever it reached first, unchecked."""
    budget_failure_reason: BudgetFailureReason | None = None
    """Why billing stopped the task, which then ended OUT_OF_BUDGET; None when it didn't."""
    path_prefix: str = ""
    """Prepended to reported source paths, which are relative to the root, to make them repository-relative."""
    suggestions: list[PropertyResult] = dataclasses.field(default_factory=list)
    """Properties the run registered beyond the manifest's; never part of the verdict."""
    from_manifest: bool = False
    """The verdict is a manifest's properties, so a run that verifies none of them fails the build."""
    stopped_summary: str | None = None
    """The summary of a run Aristotle stopped for decisions only the user can make; it lists the open questions."""
    run_summary: str | None = None
    """The summary of a run that ended with a property failed or without a verdict, out of budget or with none
    registered, where Aristotle usually names a counterexample or says why a property has no verdict."""
    left_note: str | None = None
    """That the run goes on after the time budget ran out, as the job left it, and how to report on it later."""

    @classmethod
    def from_declarations(
        cls,
        declarations: list[Declaration],
        duration_seconds: float,
        project_url: str | None = None,
        timed_out: bool = False,
        task_failed: bool = False,
        canceled: bool = False,
        path_prefix: str = "",
        requested: list[RequestedProperty] | None = None,
        never_started: bool = False,
        out_of_budget: bool = False,
        stopped_summary: str | None = None,
    ) -> "RunResults":
        """Build results from a run's declarations.

        With ``requested``, the verdict is each requested property, matched by declaration id among the registered,
        unwithdrawn properties, in that order, and every other such property is a suggestion. Without it, the verdict
        is every registered, unwithdrawn property. Every property of a run that ran out of budget or time is unchecked,
        and every proof of one that failed or was cancelled.
        """
        reported = _reported_properties(declarations)

        def unchecked(declaration: Declaration | None) -> bool:
            # A failed or cancelled run never checked its proofs; a refutation it reported still counts.
            stopped = task_failed or canceled
            return out_of_budget or timed_out or (stopped and declaration is not None and proved(declaration))

        if requested is None:
            properties = [PropertyResult(d.name, d, unchecked=unchecked(d)) for d in reported]
            suggestions = []
        else:
            registered = {d.declaration_id: d for d in reported}
            wanted = {p.id for p in requested}
            properties = [
                PropertyResult(
                    p.id,
                    registered.get(p.id),
                    p.statement,
                    never_started=never_started,
                    cut=out_of_budget or task_failed or canceled or (timed_out and not never_started),
                    unchecked=unchecked(registered.get(p.id)),
                    assumptions=p.assumptions,
                    expect_refuted=p.expect_refuted,
                )
                for p in requested
            ]
            suggestions = [
                PropertyResult(d.name, d, unchecked=unchecked(d)) for d in reported if d.declaration_id not in wanted
            ]
        return cls(
            properties=properties,
            duration_seconds=duration_seconds,
            project_url=project_url,
            timed_out=timed_out or never_started,
            never_started=never_started,
            task_failed=task_failed,
            canceled=canceled,
            out_of_budget=out_of_budget,
            path_prefix=path_prefix,
            suggestions=suggestions,
            from_manifest=requested is not None,
            stopped_summary=stopped_summary,
        )

    @property
    def passed(self) -> list[PropertyResult]:
        return [p for p in self.properties if p.passed]

    @property
    def failed(self) -> list[PropertyResult]:
        return [p for p in self.properties if p.failed]

    @property
    def without_verdict(self) -> list[PropertyResult]:
        """The properties with no verdict, whether in progress, unresolved, unchecked or not registered, which --fail-on
        no-verdict fails the build on; a refutation the run reported but nothing checked first."""
        without = [p for p in self.properties if not p.passed and not p.failed]
        return sorted(without, key=lambda p: not (p.unchecked_verdict and p.declaration and refuted(p.declaration)))

    @property
    def in_progress_reason(self) -> str:
        """Why a property still in progress has no verdict: the run stopped for questions, was cut short, failed or
        was cancelled first, or finished without checking it."""
        if self.stopped_summary is not None:
            return STOPPED_REASON
        return self.unchecked_reason or FINISHED_UNCHECKED_REASON

    @property
    def stop_clause(self) -> str | None:
        """How the run stopped before it finished, to follow "when", as in "it ran out of budget"; None for a run that
        finished."""
        if self.stopped_summary is not None:
            return "Aristotle stopped it for decisions only you can make"
        if self.task_failed:
            return "it failed due to an internal error"
        if self.canceled:
            return "it was cancelled outside this job"
        if self.out_of_budget:
            return "it ran out of budget" if self.budget_failure_reason is None else "billing stopped it"
        if self.cut_short:
            return "the time budget ran out"
        return None

    @property
    def not_registered_reason(self) -> str:
        """Why a manifest property has nothing registered under its id: the run stopped before it registered one, or
        it finished with none or withdrew it."""
        stop = self.stop_clause
        return NOT_REGISTERED_REASON if stop is None else f"The run had no property under this id when {stop}."

    @property
    def not_registered(self) -> list[PropertyResult]:
        """Manifest properties with nothing registered under their id, in a run that started."""
        return [p for p in self.properties if p.declaration is None and not p.never_started]

    @property
    def cut_short(self) -> bool:
        """The run ran out of budget, or this job's time budget ran out after it started, so its results are unchecked."""
        return self.out_of_budget or (self.timed_out and not self.never_started)

    @property
    def nothing_registered(self) -> bool:
        """The run has no property under any of the manifest's ids or, outside a manifest run, none at all."""
        return all(p.declaration is None for p in self.properties)

    @property
    def unchecked_reason(self) -> str | None:
        """Why the run's property results are unchecked, or why it has none, for report output; None unless the run
        failed, was cancelled or was cut short."""
        if self.task_failed:
            return TASK_FAILED_REASON
        if self.canceled:
            return CANCELED_REASON
        if not self.cut_short:
            return None
        if not self.nothing_registered:
            if self.budget_failure_reason is not None:
                return BILLING_STOP_REASON
            return OUT_OF_BUDGET_REASON if self.out_of_budget else TIME_BUDGET_REASON
        if self.budget_failure_reason is not None:
            ended = "Billing stopped the run"
        else:
            ended = "The run ran out of budget" if self.out_of_budget else "The time budget ran out"
        under = "under any of the manifest's ids" if self.from_manifest else "in the run"
        return f"{ended} with no property {under}."

    @property
    def cut_short_note(self) -> str | None:
        """unchecked_reason, then UNCHECKED_NOTE unless the run reached no property; None unless cut short."""
        if not self.cut_short:
            return None
        reason = self.unchecked_reason
        if reason is None or self.nothing_registered:
            return reason
        return f"{reason} {UNCHECKED_NOTE}"

    @property
    def stop_hint(self) -> str:
        """How to answer the open questions of a run Aristotle stopped, for report output."""
        if self.from_manifest:
            return (
                "A CI run takes no reply, whatever its summary says: answer them in the manifest, in a property's "
                "statement or its `assumptions`, the place for an `sva` property's answers, or in the code they "
                "concern, and run again."
            )
        return "Put the answers in --instructions (ARISTOTLE_INSTRUCTIONS) and run again."

    @property
    def nothing_verified(self) -> bool:
        """A manifest run that neither failed, was cancelled, was cut short nor was stopped, yet proved or refuted
        nothing."""
        return self.from_manifest and not (
            self.task_failed or self.canceled or self.cut_short or self.stopped_summary or self.passed or self.failed
        )

    @property
    def run_summary_excerpt(self) -> str | None:
        """run_summary, as excerpt() gives it."""
        return None if self.run_summary is None else excerpt(self.run_summary)

    @property
    def total(self) -> int:
        return len(self.properties)

    def summary_line(self) -> str:
        """The counts in the words `show` uses, deliberate counterexamples and unexpected proofs apart: proved,
        refuted, in progress, unresolved, not registered, unknown, and unchecked, with how many of those are
        refutations and proofs. In progress counts only IN_PROGRESS, so a property missing from the results doesn't read
        as work still going."""
        expected = sum(p.expect_refuted for p in self.passed)
        unexpected = sum(p.expect_refuted for p in self.failed)
        unchecked = [p.declaration for p in self.properties if p.unchecked_verdict and p.declaration]
        settled = [p for p in self.without_verdict if not (p.unchecked_verdict and p.declaration)]
        in_progress = sum(
            p.declaration is not None and p.declaration.status == DeclarationStatus.IN_PROGRESS for p in settled
        )
        unresolved = sum(
            p.declaration is not None and p.declaration.status == DeclarationStatus.FAILED for p in settled
        )
        not_registered = sum(p.declaration is None for p in settled)
        unknown = len(settled) - in_progress - unresolved - not_registered
        line = f"{len(self.passed) - expected} proved, "
        if expected:
            line += f"{expected} refuted as expected, "
        line += f"{len(self.failed) - unexpected} refuted, "
        if unexpected:
            line += f"{unexpected} unexpectedly proved, "
        line += f"{in_progress} in progress"
        for count, word in ((unresolved, "unresolved"), (not_registered, "not registered"), (unknown, "unknown")):
            if count:
                line += f", {count} {word}"
        if unchecked:
            counts = [(sum(map(refuted, unchecked)), "refuted"), (sum(map(proved, unchecked)), "proved")]
            line += f", {len(unchecked)} unchecked ({' and '.join(f'{n} {word}' for n, word in counts if n)})"
        return line

    def exit_code(self, fail_on: FailOn) -> int:
        """First match wins: a run still queued when the time budget ran out (6), a run the time budget cut after it
        started (3), NEVER (0), a failed property or a run Aristotle stopped for decisions only the user can make (1),
        a failed, cancelled or cut-short task (3), a manifest run that verified nothing (1), then NO_VERDICT (1). A
        cut-short run has no failed property: its results are unchecked. The first two hold whatever ``fail_on`` is,
        as neither run has finished."""
        if self.never_started:
            return EXIT_RATE_LIMITED
        if self.timed_out:
            return EXIT_INFRASTRUCTURE_ERROR
        if fail_on == FailOn.NEVER:
            return EXIT_SUCCESS
        if self.failed or self.stopped_summary:
            return EXIT_VERIFICATION_FAILED
        if self.task_failed or self.canceled or self.cut_short:
            return EXIT_INFRASTRUCTURE_ERROR
        if self.nothing_verified:
            return EXIT_VERIFICATION_FAILED
        if fail_on == FailOn.NO_VERDICT and (self.without_verdict or not self.properties):
            return EXIT_VERIFICATION_FAILED
        return EXIT_SUCCESS
