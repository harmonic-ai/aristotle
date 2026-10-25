"""Native GitHub Actions reporting: workflow-command annotations and the job summary."""

import functools
import logging
import os
import re
from pathlib import Path
from typing import TextIO

from aristotlelib.ci import location
from aristotlelib.ci.results import (
    NEVER_STARTED_REASON,
    UNCHECKED_PROOFS_NOTE,
    PropertyResult,
    RunResults,
    artifact_lines,
)
from aristotlelib.printable import escape_controls

logger = logging.getLogger("aristotle")

MAX_ANNOTATIONS_PER_LEVEL = 10
"""The runner keeps at most this many annotations of each level per step and silently drops the rest."""


def _escape_data(value: str) -> str:
    """Escape a workflow command's message body, after escape_controls."""
    return escape_controls(value).replace("%", "%25").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    """Escape a workflow command's property value, which also reserves ':' and ','."""
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


SUMMARY_POINTER = "The run's summary, in the job summary and the step log, usually names the counterexample."
"""Ends a refuted property's annotation when the run has a summary; the annotation itself stays short."""


def _annotation(
    result: PropertyResult,
    workspace: Path,
    level: str,
    prefix: str = "",
    summarized: bool = False,
    in_progress_reason: str | None = None,
) -> str:
    properties = [f"title={_escape_property(f'Aristotle: {result.name}')}"]

    # Without file/line GitHub attaches the annotation to the workflow file.
    source = location.resolve(result.declaration, workspace) if result.declaration else None
    if source is not None:
        properties.append(f"file={_escape_property(prefix + source.path)}")
        if source.line is not None:
            properties.append(f"line={source.line}")

    # A property with no verdict to describe takes the reason it has none as the message.
    if result.unchecked_verdict:
        message = f"{result.name} {result.result_label}"
        if result.description:
            message += f": {result.description}"
    elif result.failure_reason or result.declaration is None or (result.expect_refuted and result.failed):
        message = f"{result.name} {result.result_label}"
    elif result.refuted:
        message = result.description or f"{result.name} {result.label}"
    else:
        # Still in progress: the description alone would read as though the property holds.
        message = f"{result.name} {result.result_label}. {in_progress_reason or ''}".rstrip()
        if result.description:
            message += f" {result.description}"
    if summarized and result.refuted:
        message += f" {SUMMARY_POINTER}"
    if artifacts := artifact_lines(result):
        message += "\n" + "\n".join(artifacts)
    return f"::{level} {','.join(properties)}::{_escape_data(message)}"


_FENCE = re.compile(r"[ \t]*(`{3,}|~{3,})(.*)")
_BACKTICKS = re.compile(r"`+")
_LONGEST_RUN = 16
"""The longest backtick run paired as Markdown would; cmark-gfm opens no code span with a run of more than 80."""
_DELIMITER_ROW = re.compile(
    r"(?:[ \t]*(?:>|(?:[-*+]|\d{1,9}[.)])(?=[ \t])))*(?=.*[|:])"
    r"[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$"
)
"""A table's delimiter row, such as ``| --- | :-: |``, in a quote or a list item too; hyphens alone make a thematic
break or a heading's underline, never one."""
_LINK = re.compile(r"\]\(|\]\[|www\.|://", re.IGNORECASE)
"""Opens an inline link's destination and title, a reference link's label or an extended autolink, which take in a
backtick before code spans are read."""
_PUNCTUATION = re.compile(r"[!-/:-@\[-`{-~]")
"""The ASCII punctuation Markdown lets a backslash escape."""


def _inert(line: str) -> str:
    """``line`` with a zero-width space after each ``<``, ``[!`` and ``[^``, and after the ``!`` of ``![``: no HTML,
    alert, footnote or image, the same text."""
    return line.replace("<", "<\u200b").replace("[!", "[\u200b!").replace("[^", "[\u200b^").replace("![", "!\u200b[")


def _fenced(lines: list[str]) -> list[bool] | None:
    """Which of ``lines`` belong to a fenced block, its fences included; None if a fence is indented, where Markdown
    could open or close one inside a list item."""
    fenced, fence = [], None
    for line in lines:
        match = _FENCE.match(line)
        if match and line[0] in " \t":
            return None
        if fence is None:
            # A backtick fence's info string holds no backtick.
            fence = match[1] if match and not (match[1][0] == "`" and "`" in match[2]) else None
            fenced.append(fence is not None)
        else:
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not match[2].strip():
                fence = None
            fenced.append(True)
    return fenced


def _code_spans(line: str) -> list[tuple[int, int]] | None:
    """Where ``line``'s code spans are, each backtick run closed by the next run of its length, as CommonMark pairs
    them; None if a run has no such run after it on the line, follows a backslash, or is longer than _LONGEST_RUN."""
    runs = [match.span() for match in _BACKTICKS.finditer(line)]
    if any((start and line[start - 1] == "\\") or end - start > _LONGEST_RUN for start, end in runs):
        return None
    spans = []
    i = 0
    while i < len(runs):
        start, end = runs[i]
        closer = next((j for j in range(i + 1, len(runs)) if runs[j][1] - runs[j][0] == end - start), None)
        if closer is None:
            return None
        spans.append((start, runs[closer][1]))
        i = closer + 1
    return spans


def _table_lines(lines: list[str], fenced: list[bool]) -> set[int]:
    """Which of ``lines`` a table could hold: from the row before a delimiter row to the next blank line or fence, a
    line of quote markers alone counting as blank."""
    held: set[int] = set()
    for i, line in enumerate(lines):
        if not fenced[i] and _DELIMITER_ROW.match(line):
            j = i - 1 if i and lines[i - 1].strip(" \t>") and not fenced[i - 1] else i
            while j < len(lines) and lines[j].strip(" \t>") and not fenced[j]:
                held.add(j)
                j += 1
    return held


def _inert_outside(line: str, spans: list[tuple[int, int]], breaks: list[int]) -> str:
    """``line`` made _inert but in its code ``spans``, where no HTML, alert or footnote acts, with a zero-width space
    before each of ``breaks``."""
    parts, end = [], 0
    for start, stop in sorted([*spans, *((at, at) for at in breaks)]):
        parts += [_inert(line[end:start]), line[start:stop] if stop > start else "\u200b"]
        end = stop
    return "".join([*parts, _inert(line[end:])])


def _outside(line: str, spans: list[tuple[int, int]]) -> str:
    """``line`` with its code ``spans`` blanked out."""
    parts, end = [], 0
    for start, stop in spans:
        parts += [line[end:start], " " * (stop - start)]
        end = stop
    return "".join([*parts, line[end:]])


@functools.cache
def _any_of(chars: str) -> re.Pattern[str]:
    """A pattern that matches any one of ``chars``."""
    return re.compile(f"[{re.escape(chars)}]")


def _first(line: str, at: int, chars: str, escapable: str = "") -> int | None:
    """Where the first of ``chars`` is in ``line`` from ``at`` on, passing over one of ``escapable`` after a backslash;
    None if nowhere."""
    pattern = _any_of(chars)
    while (match := pattern.search(line, at)) and match[0] in escapable and line.endswith("\\", 0, match.start()):
        at = match.end()
    return match.start() if match else None


def _skip_spaces(line: str, at: int) -> int:
    """Where the first character in ``line`` from ``at`` on that is no space or tab is; its length if none."""
    while at < len(line) and line[at] in " \t":
        at += 1
    return at


def _inline_link_end(line: str, at: int) -> int | None:
    """The end of what an inline link whose ``(`` is ``line[at]`` could take in, as cmark-gfm reads its destination and
    title: just past the first backtick they hold, else past the ``)`` or the title that ends them, or ``at`` if they
    make no link. None where they could run on past the line, start in angle brackets or reach a ``]``, where another
    _LINK marker could start. A ``(`` after a backslash counts toward the nesting and a ``)`` there doesn't, so it can
    only take in more than Markdown does."""
    start = _skip_spaces(line, at + 1)
    # NUL stands for a cell's <br>, itself a destination in angle brackets.
    if start == len(line) or line[start] in "<\0":
        return None
    depth, end = 0, start
    while (stop := _first(line, end, " \t()]`", escapable=")")) is not None and line[stop] in "()":
        if line[stop] == ")" and not depth:
            return stop + 1
        depth += 1 if line[stop] == "(" else -1
        end = stop + 1
    if stop is None or line[stop] == "]":
        return None
    if line[stop] == "`":
        return stop + 1
    title = _skip_spaces(line, stop)
    if title == len(line):
        return None
    if line[title] == ")":
        return title + 1
    if line[title] not in "\"'(":
        return at
    closer = ")" if line[title] == "(" else line[title]
    stop = _first(line, title + 1, "()`" if closer == ")" else closer + "`", escapable="()\"'")
    return None if stop is None else at if line[stop] == "(" else stop + 1


def _link_breaks(line: str, spans: list[tuple[int, int]], ahead: bool) -> list[int]:
    """Where on ``line`` a zero-width space keeps a link or extended autolink outside its code ``spans`` from taking in
    a backtick: after the first character of each _LINK marker whose link could hold one, ``ahead`` telling whether one
    follows on the lines a link could run on to, and of each _LINK marker of an inline link or extended autolink that
    would otherwise carry such a space in its URL or title."""
    breaks: set[int] = set()
    kept: list[tuple[int, int]] = []
    last = line.rfind("`")
    autolink_end, autolink_holds = -1, False
    for match in _LINK.finditer(_outside(line, spans)):
        at = match.start()
        end: int | None = None
        if match[0] == "](":
            end = _inline_link_end(line, at + 1)
            holds = None if end is None else line[end - 1] == "`"
        elif match[0] == "][":
            stop = _first(line, at + 2, "[]`", escapable="[]")
            holds = None if stop is None else line[stop] == "`"
        else:
            # An extended autolink runs to ASCII whitespace or a "<"; the next one before that end shares it.
            if at > autolink_end:
                stop = _first(line, at, " \t\n\v\f\r<`")
                autolink_end, autolink_holds = (len(line), False) if stop is None else (stop, line[stop] == "`")
            holds, end = autolink_holds, autolink_end
        if holds is None:
            holds = last > at or ahead
        if holds:
            # A kept link that holds a break goes too, latest first, so that one holding its break follows.
            lowest = at + 1
            breaks.add(lowest)
            for link, link_end in reversed(kept):
                if link_end > lowest:
                    lowest = link + 1
                    breaks.add(lowest)
            kept.clear()
        elif end is not None:
            kept.append((at, end))
    return sorted(breaks)


def _ticks_ahead(lines: list[str], fenced: list[bool]) -> list[bool]:
    """Whether a backtick follows each of ``lines`` before the next blank line or fence, where any link ends."""
    ahead, follows = [], False
    for line, inside in zip(reversed(lines), reversed(fenced)):
        ahead.append(follows)
        follows = not inside and bool(line.strip(" \t")) and (follows or "`" in line)
    return ahead[::-1]


def _neutralized(lines: list[str]) -> list[str]:
    """``lines`` made _inert but in fenced blocks and code spans, which stay as they are, with the _link_breaks that keep
    each link from taking in a backtick. Where Markdown could read a backtick otherwise than these line-by-line rules
    do, every line outside a fence is made _inert whole: a backtick unpaired on its line, after a backslash or in a run
    of more than 16, or a table, which splits a code span at its ``|``. An indented fence makes every line _inert."""
    fenced = _fenced(lines)
    if fenced is None:
        return [_inert(line) for line in lines]
    spans = [[] if inside else _code_spans(line) for line, inside in zip(lines, fenced)]
    if None in spans:
        return [line if inside else _inert(line) for line, inside in zip(lines, fenced)]
    if any("|" in lines[i][start:stop] for i in _table_lines(lines, fenced) for start, stop in spans[i] or []):
        return [line if inside else _inert(line) for line, inside in zip(lines, fenced)]
    return [
        line if inside else _inert_outside(line, s or [], _link_breaks(line, s or [], ahead))
        for line, inside, s, ahead in zip(lines, fenced, spans, _ticks_ahead(lines, fenced))
    ]


def _quoted(text: str) -> list[str]:
    """``text`` as Markdown quote lines, _neutralized, and a zero-width space before a first line that starts with
    ``[``, ``&`` or ``\\``, so no escaped or encoded marker makes the quote an alert."""
    # Judged as written: rstrip() drops a Unicode space that could hide a delimiter row from the rules.
    quoted = _neutralized([escape_controls(line).rstrip() for line in text.splitlines()])
    first = next((i for i, line in enumerate(quoted) if line.strip()), None)
    if first is not None and quoted[first].lstrip().startswith(("[", "&", "\\")):
        quoted[first] = "\u200b" + quoted[first]
    return [f"> {line}".rstrip() for line in quoted]


def _cell(*parts: str) -> str:
    """``parts`` as one table cell, a line break between them and each on one line: _inert but in code spans, which
    keep their text where every backtick pairs, with the _link_breaks that keep each link from taking one in, and its
    ``|`` escaped, so it splits no cell. The parts are judged together, as Markdown reads the cell, and a cell holds
    inline content only, so unlike the quote it has no fence to keep."""
    # NUL stands for the line breaks while the cell is judged: escape_controls leaves none, and it pairs nothing.
    line = "\0".join(" ".join(escape_controls(row) for row in part.splitlines()) for part in parts)
    spans = _code_spans(line)
    line = _inert(line) if spans is None else _inert_outside(line, spans, _link_breaks(line, spans, False))
    return line.replace("|", "\\|").replace("\0", "<br>")


def _code(text: str) -> str:
    """``text`` as a code span in a table cell, on one line: delimited by a backtick run longer than any in it, so none
    closes it early, with a space inside where it starts or ends with a backtick or is empty, and its ``|`` escaped. If
    it holds a run as long as _LONGEST_RUN, it is text instead with all its punctuation escaped, so nothing in it, a
    link's start or a backtick, reaches into a code span beside it."""
    line = " ".join(escape_controls(part) for part in text.splitlines())
    longest = max(map(len, _BACKTICKS.findall(line)), default=0)
    if longest >= _LONGEST_RUN:
        return _PUNCTUATION.sub(lambda match: "\\" + match[0], line)
    fence = "`" * (longest + 1)
    pad = " " if not line or line.startswith("`") or line.endswith("`") else ""
    return f"{fence}{pad}{line}{pad}{fence}".replace("|", "\\|")


def write_annotations(results: RunResults, workspace: Path) -> None:
    """Emit an error for an early stop and per failing property, and a notice per one without a verdict, within the
    runner's per-level cap."""
    stopped = results.stopped_summary is not None
    errors = results.failed[: MAX_ANNOTATIONS_PER_LEVEL - stopped]
    overflowing = len(results.failed) > len(errors) or len(results.without_verdict) > MAX_ANNOTATIONS_PER_LEVEL
    # The overflow message is itself a notice, so it takes the last notice slot.
    notices = results.without_verdict[: MAX_ANNOTATIONS_PER_LEVEL - overflowing]

    if results.stopped_summary is not None:
        # Fixed text: GitHub cuts an annotation at 4096 characters, and the summary is printed whole in the step log.
        message = (
            f"Aristotle stopped the run for decisions only you can make. {results.stop_hint} "
            "The run's summary, with its open questions, is in the step log."
        )
        print(f"::error title=Aristotle stopped before finishing::{_escape_data(message)}")
    for result in errors:
        print(_annotation(result, workspace, "error", results.path_prefix, results.run_summary is not None))
    for result in notices:
        print(
            _annotation(result, workspace, "notice", results.path_prefix, in_progress_reason=results.in_progress_reason)
        )

    remaining = len(results.failed) + len(results.without_verdict) - len(errors) - len(notices)
    if remaining > 0:
        print(f"::notice::{remaining} further Aristotle results are in the job summary and the JUnit report.")


def write_error_annotations(title: str, messages: list[str], stream: TextIO | None = None) -> None:
    """Emit each of ``messages``, the errors that ended a step before it reported a run, as an error annotation, within
    the runner's per-level cap, on ``stream``, by default stdout; the runner reads stderr too."""
    for message in messages[:MAX_ANNOTATIONS_PER_LEVEL]:
        print(f"::error title={_escape_property(title)}::{_escape_data(message)}", file=stream)


def _summary_row(result: PropertyResult, workspace: Path | None) -> str:
    if result.passed:
        icon = "✅"
    elif result.failed:
        icon = "❌"
    else:
        icon = "⬜"
    assuming = (
        f"{'Holds, assuming' if result.passed and not result.expect_refuted else 'Assuming:'} {result.assumptions}"
        if result.assumptions
        else ""
    )
    name = _code(result.name)
    where = location.describe(result.declaration, workspace) if result.declaration else None
    if where:
        name = f"{name}<br>{_code(where)}"

    detail = _cell(*(part for part in (result.description, assuming) if part))
    return f"| {icon} | {name} | {result.result_label} | {detail} |"


def build_summary(results: RunResults, workspace: Path | None = None) -> str:
    """Render the job summary as GitHub-flavored markdown."""
    lines = ["## Aristotle — formal verification", "", f"**{results.summary_line()}**", ""]

    # Properties below are those it reached, unless it reached none.
    reached = "" if results.nothing_registered else f" {UNCHECKED_PROOFS_NOTE}"
    if results.task_failed:
        lines.extend(
            [
                "> [!CAUTION]",
                f"> The verification run failed due to an internal error.{reached}",
                "",
            ]
        )

    if results.canceled:
        lines.extend(
            [
                "> [!CAUTION]",
                f"> The verification run was cancelled outside this job.{reached}",
                "",
            ]
        )

    if results.cut_short_note:
        lines.extend(["> [!CAUTION]", f"> {results.cut_short_note}", ""])

    if results.stopped_summary is not None:
        lines.extend(
            [
                "> [!CAUTION]",
                "> Aristotle stopped before finishing, for decisions only you can make. Its open questions are in the"
                " run's summary, printed in the step log.",
                "",
            ]
        )

    if results.never_started:
        lines.extend(
            [
                "> [!CAUTION]",
                f"> {NEVER_STARTED_REASON} Nothing was verified.",
                "",
            ]
        )
    elif results.nothing_verified:
        lines.extend(
            ["> [!CAUTION]", "> No property in the manifest was verified. The step log names the likely causes.", ""]
        )

    if results.left_note is not None:
        lines.extend(["> [!NOTE]", *_quoted(results.left_note), ""])

    if not results.properties:
        lines.append("No properties were registered for this run.")
    else:
        lines.extend(
            [
                "| | Property | Result | Detail |",
                "| --- | --- | --- | --- |",
                *(_summary_row(d, workspace) for d in results.properties),
            ]
        )

    if results.suggestions:
        lines.extend(
            [
                "",
                "### Suggested by Aristotle",
                "",
                "Not part of the verdict. To check one on every run, add it to `aristotle.toml`.",
                "",
                "| | Property | Result | Detail |",
                "| --- | --- | --- | --- |",
                *(_summary_row(r, workspace) for r in results.suggestions),
            ]
        )

    if results.run_summary_excerpt:
        lines.extend(["", "### Aristotle's summary of the run", "", *_quoted(results.run_summary_excerpt)])

    if results.project_url:
        lines.extend(["", f"[View the full run in Aristotle]({results.project_url})"])

    return "\n".join(lines) + "\n"


def write_summary(results: RunResults, workspace: Path | None = None) -> None:
    """Append the job summary to ``GITHUB_STEP_SUMMARY`` when it is set."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        logger.debug("GITHUB_STEP_SUMMARY is not set; skipping job summary")
        return

    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(build_summary(results, workspace))
    except OSError as e:
        # An unwritable summary must not fail the build.
        logger.warning(f"Could not write the GitHub job summary: {e}")
        return
    logger.info("Wrote GitHub job summary")


def write_output(name: str, value: str) -> None:
    """Append ``name=value`` to ``GITHUB_OUTPUT`` when it is set."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    try:
        with open(output_path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    except OSError as e:
        logger.warning(f"Could not write the {name} step output: {e}")


def report(results: RunResults, workspace: Path) -> None:
    """Write both GitHub-native outputs."""
    write_annotations(results, workspace)
    write_summary(results, workspace)
