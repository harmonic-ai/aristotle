"""JUnit XML report; each property is one test case."""

import contextlib
import errno
import logging
import os
import re
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from pathlib import Path
from xml.dom import minidom

from aristotlelib import local_file_utils
from aristotlelib.ci import location
from aristotlelib.ci.results import (
    NEVER_STARTED_REASON,
    PROOF_CHECK_FAILED_REASON,
    STOPPED_REASON,
    UNCHECKED_NOTE,
    UNCHECKED_PROOFS_NOTE,
    PropertyResult,
    RunResults,
    artifact_lines,
    excerpt,
)
from aristotlelib.declaration import DeclarationFailureReason

logger = logging.getLogger("aristotle")

SUITE_NAME = "aristotle"

# Everything outside XML 1.0's Char production, which no escaping can represent.
_INVALID_XML_CHARS = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def _xml_safe(value: str) -> str:
    return _INVALID_XML_CHARS.sub("", value)


def _sanitize(element: ET.Element) -> None:
    """Strip characters XML 1.0 cannot carry from every attribute and text node under ``element``."""
    for node in element.iter():
        node.attrib = {key: _xml_safe(value) for key, value in node.attrib.items()}
        if node.text:
            node.text = _xml_safe(node.text)
        if node.tail:
            node.tail = _xml_safe(node.tail)


def _failure_text(result: PropertyResult, workspace: Path | None, run_summary: str | None = None) -> str:
    """The body of a <failure> element: what was claimed, and the run's summary, which usually says why not."""
    lines = [f"Property: {result.name}", f"Result: {result.label}"]
    declaration = result.declaration
    where = location.describe(declaration, workspace) if declaration else None
    if where:
        lines.append(f"Source: {where}")
    if result.requested and result.requested != result.description:
        lines.extend(["", f"Requested: {result.requested}"])
    if result.assumptions:
        lines.extend(["", f"Assuming: {result.assumptions}"])
    if result.description:
        lines.extend(["", result.description])
    if declaration and declaration.lean_code:
        lines.extend(["", "Formal statement:", declaration.lean_code])
    if artifacts := artifact_lines(result):
        lines.extend(["", *artifacts])
    if run_summary:
        heading = "Aristotle's summary of the run" + (
            "" if result.expect_refuted else ", which usually names the counterexample"
        )
        lines.extend(["", f"{heading}:", run_summary])
    return "\n".join(lines)


def _no_verdict_message(result: PropertyResult, results: RunResults) -> str:
    """The <skipped> message: the property's result and why it has no verdict, then its description."""
    if result.never_started:
        return NEVER_STARTED_REASON
    if result.unchecked_verdict:
        message = f"Reported {result.label}, unchecked. {results.unchecked_reason}"
        return f"{message} {result.description}" if result.description else message
    if result.declaration is None:
        return results.not_registered_reason
    if result.declaration.failure_reason == DeclarationFailureReason.PROOF_CHECK_FAILED:
        return f"Unresolved: {PROOF_CHECK_FAILED_REASON}"
    if reason := result.failure_reason:
        return f"Unresolved ({reason}): the run stopped before reaching a verdict."
    message = f"In progress. {results.in_progress_reason}"
    return f"{message} {result.description}" if result.description else message


def _add_testcase(suite: ET.Element, result: PropertyResult, results: RunResults, workspace: Path | None) -> None:
    # classname groups cases in the test tree, and with name keys their history; file/line are what CIs link to.
    declaration = result.declaration
    path = location.reported_path(declaration) if declaration and result.requested is None else None
    attributes = {"name": result.name, "classname": path or SUITE_NAME}
    if declaration and declaration.source_file:
        attributes["file"] = results.path_prefix + declaration.source_file
        line = location.line_in_checkout(declaration, workspace)
        if line:
            attributes["line"] = str(line)

    testcase = ET.SubElement(suite, "testcase", attributes)

    if result.failed:
        # A proof the manifest didn't expect says so; its description is what was proved.
        message = f"{result.name} {result.label}" if result.expect_refuted else result.description
        failure = ET.SubElement(
            testcase, "failure", {"message": message or f"{result.name} {result.label}", "type": result.label}
        )
        failure.text = _failure_text(result, workspace, results.run_summary_excerpt)
    elif not result.passed:
        ET.SubElement(testcase, "skipped", {"message": _no_verdict_message(result, results)})
    elif result.description or result.expect_refuted or result.assumptions:
        # system-out is the only place JUnit allows text on a passing case.
        system_out = ET.SubElement(testcase, "system-out")
        lines = [f"Result: {result.label}"] if result.expect_refuted else []
        head = "\n".join([*lines, *([result.description] if result.description else [])])
        verdict = "Assuming:" if result.expect_refuted else "Holds, assuming"
        assuming = f"{verdict} {result.assumptions}" if result.assumptions else ""
        system_out.text = "\n\n".join(part for part in (head, assuming) if part)


def _run_error(results: RunResults) -> tuple[str, str, str] | None:
    """The type, message and text of the error a run records that stopped short of checked verdicts: one cut short,
    failed, cancelled, never started or stopped for questions; None for a run that finished."""
    if results.stopped_summary is not None:
        return "stopped", STOPPED_REASON, f"{results.stop_hint}\n\n{excerpt(results.stopped_summary)}"
    if results.never_started:
        return "never started", NEVER_STARTED_REASON, ""
    reason = results.unchecked_reason
    if reason is None:
        return None
    if results.task_failed or results.canceled:
        kind, note = ("failed" if results.task_failed else "cancelled"), UNCHECKED_PROOFS_NOTE
    elif results.budget_failure_reason is not None:
        kind, note = "billing", UNCHECKED_NOTE
    else:
        kind, note = ("out of budget" if results.out_of_budget else "time budget"), UNCHECKED_NOTE
    notes = [] if results.nothing_registered else [note]
    if summary := results.run_summary_excerpt:
        notes.append(f"Aristotle's summary of the run:\n{summary}")
    return kind, reason, "\n\n".join(notes)


def build_root(results: RunResults, workspace: Path | None = None) -> ET.Element:
    """Render results as the root <testsuites> element, reading line numbers from ``workspace`` when given."""
    run_error = _run_error(results)
    suite = ET.Element(
        "testsuite",
        {
            "name": SUITE_NAME,
            "tests": str(results.total + bool(run_error)),
            "failures": str(len(results.failed)),
            "skipped": str(len(results.without_verdict)),
            "errors": str(int(bool(run_error))),
            "time": f"{results.duration_seconds:.3f}",
        },
    )

    if results.project_url:
        properties = ET.SubElement(suite, "properties")
        ET.SubElement(properties, "property", {"name": "aristotle.project_url", "value": results.project_url})

    if run_error is not None:
        # The run stopped short of checked verdicts: an error saying why, under a name no manifest id can have.
        kind, message, text = run_error
        stop = ET.SubElement(suite, "testcase", {"name": "Aristotle run", "classname": SUITE_NAME})
        error = ET.SubElement(stop, "error", {"message": message, "type": kind})
        error.text = text or None

    # The verdict's properties only; suggestions are never test cases.
    for result in results.properties:
        _add_testcase(suite, result, results, workspace)

    suites = ET.Element(
        "testsuites",
        {
            "name": SUITE_NAME,
            "tests": str(results.total + bool(run_error)),
            "failures": str(len(results.failed)),
            "errors": str(int(bool(run_error))),
            "time": f"{results.duration_seconds:.3f}",
        },
    )
    suites.append(suite)
    _sanitize(suites)
    return suites


def to_string(results: RunResults, workspace: Path | None = None) -> str:
    """Render results as pretty-printed JUnit XML."""
    raw = ET.tostring(build_root(results, workspace), encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ")


def write(results: RunResults, destination: Path, workspace: Path | None = None, checkout: Iterable[Path] = ()) -> None:
    """Write the JUnit report to ``destination``, creating parent directories owned like the nearest existing one.

    In ``workspace`` and the ``checkout`` folders, where a checkout's links can be, the report is a new file moved over
    whatever is at ``destination``; elsewhere it is written in place, links followed.

    Raises:
        OSError: If a folder on the way to ``destination`` is a symbolic link in one of those folders, before anything
            is written; or if the report can't be written.
    """
    text = to_string(results, workspace)
    inside = _holder([*([workspace] if workspace else []), *checkout])
    folder = _resolve(destination.parent, inside)
    replaces = inside(folder)
    path = os.path.join(folder, destination.name)
    existing = destination.parent
    while not existing.exists():
        existing = existing.parent
    # Made along the path as given, a folder before a ``..`` included, so that it names the report afterwards.
    destination.parent.mkdir(parents=True, exist_ok=True)
    if replaces:
        linked = os.path.islink(path)
        if not linked and os.path.isdir(path):
            raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(destination))
        _replace(path, text)
        if linked:
            logger.warning(
                f"Replaced the symbolic link at {destination} with the report, rather than write through it."
            )
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    _give_to_owner_of(existing, destination)
    logger.info(f"Wrote JUnit report to {destination}")


_MAX_LINKS = 40
"""The most links _resolve() follows, as Linux's MAXSYMLINKS."""


def _holder(folders: Iterable[Path]) -> Callable[[str], bool]:
    """Whether a folder, named by a path with no link on the way, is or is in one of ``folders``, by identity or by name;
    the file system's root never counts."""
    reals = [real for folder in folders if os.path.dirname(real := os.path.realpath(folder)) != real]
    names = {os.path.normcase(real) for real in reals}
    identities = {identity for real in reals if (identity := _identity(real)) is not None}

    def holds(folder: str) -> bool:
        return any(
            os.path.normcase(path) in names or _identity(path) in identities
            for path in (folder, *map(str, Path(folder).parents))
        )

    return holds


def _identity(path: str) -> tuple[int, int] | None:
    """The identity of the file or folder at ``path``, links followed; None if it has none or can't be read."""
    try:
        return local_file_utils.file_identity(os.stat(path))
    except OSError:
        return None


def _resolve(path: Path, inside: Callable[[str], bool]) -> str:
    """``path`` made absolute with each link followed, as os.path.realpath does, a ``..`` leaving the folder reached so
    far, so that a folder that doesn't exist yet hides no link after it.

    Raises:
        OSError: If a link on the way is in a folder ``inside`` holds, or links nest more than _MAX_LINKS deep.
    """
    folder = path.anchor or os.getcwd()
    names = list(path.parts[1:] if path.anchor else path.parts)
    followed = 0
    while names:
        name = names.pop(0)
        if name == os.pardir:
            folder = os.path.dirname(folder)
            continue
        candidate = os.path.join(folder, name)
        if not os.path.islink(candidate):
            folder = candidate
            continue
        if inside(folder):
            raise OSError(
                f"{candidate} is a symbolic link, which could lead the report out of the checkout. Remove the link, "
                "or pass --junit another path."
            )
        followed += 1
        if followed > _MAX_LINKS:
            raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), str(path))
        target = _link_target(candidate)
        folder = target.anchor or folder
        names[:0] = target.parts[1:] if target.anchor else target.parts
    return folder


def _link_target(link: str) -> Path:
    """Where the link at ``link`` points, without the \\\\?\\ prefix Windows gives a target on a drive or a share."""
    target = os.readlink(link)
    if os.name == "nt" and target.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + target[8:])
    if os.name == "nt" and target.startswith("\\\\?\\") and target[5:7] == ":\\":
        return Path(target[4:])
    return Path(target)


def _replace(path: str, text: str) -> None:
    """Write ``text`` to a new file beside ``path`` and move it over whatever is at ``path``, never through it."""
    temporary = os.path.join(os.path.dirname(path), f".aristotle-{uuid.uuid4().hex[:8]}.tmp")
    file = open(temporary, "x", encoding="utf-8")
    try:
        with file:
            file.write(text)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _give_to_owner_of(existing: Path, created: Path) -> None:
    """As root, chown ``created`` and its new parents up to ``existing`` to ``existing``'s owner, if that is not root."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    owner = existing.stat()
    if owner.st_uid == 0:
        return
    path = created
    while path != existing:
        os.chown(path, owner.st_uid, owner.st_gid, follow_symlinks=False)
        path = path.parent
