"""Plain-text run summary for the build log."""

import logging
from pathlib import Path

from aristotlelib.ci import location
from aristotlelib.ci.results import (
    NEVER_STARTED_REASON,
    UNCHECKED_PROOFS_NOTE,
    PropertyResult,
    RunResults,
    artifact_lines,
)

logger = logging.getLogger("aristotle")

MAX_DETAIL_LINES = 3


def _wrap_detail(text: str, width: int = 92) -> list[str]:
    """Soft-wrap a description into a few indented lines."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
            if len(lines) == MAX_DETAIL_LINES:
                return [*lines[:-1], lines[-1].rstrip() + " ..."]
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def _format(result: PropertyResult, marker: str, workspace: Path | None) -> list[str]:
    lines = [f"  {marker} {result.name}"]
    where = location.describe(result.declaration, workspace) if result.declaration else None
    if where:
        lines.append(f"      {where}")
    if result.description:
        lines.extend(f"      {line}" for line in _wrap_detail(result.description))
    if result.assumptions:
        lines.extend(f"      {line}" for line in _wrap_detail(f"Assuming: {result.assumptions}"))
    return lines


def _suggestion(result: PropertyResult) -> list[str]:
    lines = [f"  -     {result.name}  ({result.result_label})"]
    if result.description:
        lines.extend(f"        {line}" for line in _wrap_detail(result.description, width=90))
    return lines


def render(results: RunResults, workspace: Path | None = None) -> str:
    """Render the full summary, reading line numbers out of ``workspace`` when given."""
    lines = ["", "Aristotle — formal verification", f"  {results.summary_line()}", ""]

    if results.stopped_summary:
        lines.extend(["  Aristotle stopped before finishing:", ""])
        lines.extend(f"    {line}" if line.strip() else "" for line in results.stopped_summary.splitlines())
        lines.extend(["", f"  {results.stop_hint}", ""])

    # Properties below are those it reached, unless it reached none.
    reached = "" if results.nothing_registered else f" {UNCHECKED_PROOFS_NOTE}"
    if results.task_failed:
        note = f"The run failed due to an internal error.{reached}"
        lines.extend([f"  {line}" for line in _wrap_detail(note, width=80)] + [""])

    if results.canceled:
        note = f"The run was cancelled outside this job.{reached}"
        lines.extend([f"  {line}" for line in _wrap_detail(note, width=80)] + [""])

    if results.cut_short_note:
        lines.extend([f"  {line}" for line in _wrap_detail(results.cut_short_note, width=80)] + [""])

    if results.nothing_verified:
        lines.extend(["  No property in the manifest was verified.", ""])

    if results.never_started:
        lines.extend([f"  {line}" for line in _wrap_detail(NEVER_STARTED_REASON, width=80)] + [""])

    if not results.properties:
        lines.append("  No properties were registered for this run.")

    for result in results.passed:
        lines.append(
            f"  PASS  {result.name}  ({result.label})" if result.expect_disproved else f"  PASS  {result.name}"
        )
        if result.assumptions:
            # A deliberate counterexample doesn't hold; it is shown under them.
            assuming = f"{'assuming' if result.expect_disproved else 'holds, assuming'} {result.assumptions}"
            lines.extend(f"        {line}" for line in _wrap_detail(assuming, width=90))

    for result in results.failed:
        lines.append("")
        lines.extend(_format(result, f"FAIL  [{result.label}]", workspace))
        lines.extend(f"      {line}" for line in artifact_lines(result))

    for result in results.without_verdict:
        if result.unchecked_verdict:
            # The agent's claim, such as a counterexample, is still worth reading, so it gets a FAIL row's detail.
            lines.append("")
            lines.extend(_format(result, f"SKIP  [{result.result_label}]", workspace))
        else:
            lines.append(f"  SKIP  {result.name}  ({result.result_label})")

    if results.suggestions:
        lines.extend(["", "  Suggested by Aristotle, not part of the verdict:"])
        for result in results.suggestions:
            lines.extend(_suggestion(result))

    # A stopped run's summary is printed above, with its open questions.
    if results.run_summary and not results.stopped_summary:
        lines.extend(["", "  Aristotle's summary of the run:", ""])
        lines.extend(f"    {line}" if line.strip() else "" for line in results.run_summary.splitlines())

    if results.project_url:
        lines.extend(["", f"  Full run: {results.project_url}"])

    lines.append("")
    return "\n".join(lines)


def report(results: RunResults, workspace: Path | None = None) -> None:
    """Log the summary."""
    logger.info(render(results, workspace))
