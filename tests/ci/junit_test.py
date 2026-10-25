"""Tests for aristotlelib.ci.junit."""

import dataclasses
import errno
import logging
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from aristotlelib.ci import junit
from aristotlelib.ci.manifest import Kind, RequestedProperty
from aristotlelib.ci.results import (
    CANCELED_REASON,
    MAX_SUMMARY_CHARACTERS,
    NEVER_STARTED_REASON,
    OUT_OF_BUDGET_REASON,
    PROOF_CHECK_FAILED_REASON,
    TASK_FAILED_REASON,
    TIME_BUDGET_REASON,
    UNCHECKED_NOTE,
    UNCHECKED_PROOFS_NOTE,
    RunResults,
)
from aristotlelib.declaration import DeclarationEvidence, DeclarationFailureReason, DeclarationStatus, DeclarationType
from tests.ci.conftest import expecting_refuted, make_declaration
from tests.conftest import hard_link

NO_OVERFLOW = RequestedProperty("no_overflow", Kind.NATURAL_LANGUAGE, "The FIFO never overflows.")


def _suite(results: RunResults) -> ET.Element:
    suite = junit.build_root(results).find("testsuite")
    assert suite is not None
    return suite


def test_each_property_becomes_a_test_case():
    results = RunResults.from_declarations(
        [
            make_declaration("Spec.A", DeclarationStatus.PROVED),
            make_declaration("Spec.B", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE),
            make_declaration("Spec.C", DeclarationStatus.IN_PROGRESS),
        ],
        duration_seconds=12.5,
    )

    suite = _suite(results)

    assert suite.get("tests") == "3"
    assert suite.get("failures") == "1"
    assert suite.get("skipped") == "1"
    assert suite.get("time") == "12.500"
    assert [case.get("name") for case in suite.findall("testcase")] == ["Spec.A", "Spec.B", "Spec.C"]


def test_definitions_are_not_reported_as_tests():
    results = RunResults.from_declarations(
        [
            make_declaration("Spec.Prop", DeclarationStatus.PROVED),
            make_declaration("Spec.Helper", DeclarationStatus.IN_PROGRESS, DeclarationType.DEFINITION),
        ],
        duration_seconds=1.0,
    )

    assert [case.get("name") for case in _suite(results).findall("testcase")] == ["Spec.Prop"]


def test_withdrawn_declarations_are_not_reported():
    results = RunResults.from_declarations(
        [make_declaration("Spec.Gone", DeclarationStatus.REMOVED)],
        duration_seconds=1.0,
    )

    assert results.total == 0


def test_a_counterexample_becomes_a_failure_carrying_the_detail():
    declaration = make_declaration(
        "Spec.NoOverflow",
        DeclarationStatus.REFUTED,
        description="The write pointer wraps past the read pointer at cycle 12.",
        lean_code="def NoOverflow : Prop := ...",
        evidence=DeclarationEvidence.COUNTEREXAMPLE,
    )
    results = RunResults.from_declarations([declaration], duration_seconds=1.0)

    failure = _suite(results).find("testcase/failure")

    assert failure is not None
    assert failure.get("type") == "refuted"
    assert failure.get("message") == "The write pointer wraps past the read pointer at cycle 12."
    assert failure.text is not None
    assert "Spec.NoOverflow" in failure.text
    assert "def NoOverflow" in failure.text


def test_a_refuted_property_is_a_failure_not_a_skip():
    results = RunResults.from_declarations(
        [make_declaration("Spec.Wrong", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.NEGATION_PROVED)],
        duration_seconds=1.0,
    )

    assert _suite(results).find("testcase/failure") is not None
    assert _suite(results).find("testcase/skipped") is None


def test_a_property_in_progress_is_skipped_rather_than_failed():
    results = RunResults.from_declarations(
        [make_declaration("Spec.Hard", DeclarationStatus.IN_PROGRESS, description=None)],
        duration_seconds=1.0,
    )

    suite = _suite(results)
    skipped = suite.find("testcase/skipped")

    assert skipped is not None
    assert skipped.get("message") == "In progress. The run finished without checking it."
    assert (suite.get("tests"), suite.get("errors")) == ("1", "0")


def test_a_stopped_run_is_an_error_in_the_suite_and_its_properties_say_why_they_are_in_progress():
    results = RunResults.from_declarations(
        [
            make_declaration(
                "Spec.Underflow", DeclarationStatus.IN_PROGRESS, description="The counter never underflows."
            )
        ],
        duration_seconds=1.0,
        stopped_summary="Which reset polarity is intended?",
    )

    root = junit.build_root(results)
    suite = _suite(results)

    assert (suite.get("tests"), suite.get("errors"), root.get("tests"), root.get("errors")) == ("2", "1", "2", "1")
    error = suite.find("testcase[@name='Aristotle run']/error")
    assert error is not None
    assert error.get("message") == "Aristotle stopped the run for decisions only you can make."
    assert "Which reset polarity is intended?" in (error.text or "")
    skipped = suite.find("testcase[@name='Spec.Underflow']/skipped")
    assert skipped is not None
    assert skipped.get("message") == (
        "In progress. Aristotle stopped the run for decisions only you can make. The counter never underflows."
    )


def test_a_stopped_runs_error_carries_an_excerpt_of_its_open_questions():
    results = RunResults.from_declarations(
        [], duration_seconds=1.0, stopped_summary="Which reset polarity is intended?\n" + "w" * MAX_SUMMARY_CHARACTERS
    )

    error = _suite(results).find("testcase[@name='Aristotle run']/error")

    assert error is not None and error.text is not None
    assert "Which reset polarity is intended?\nwww" in error.text
    assert error.text.endswith(f"\n… (cut at {MAX_SUMMARY_CHARACTERS} characters; the job log has the whole summary)")
    assert len(error.text) < len(results.stop_hint) + MAX_SUMMARY_CHARACTERS + 100


def test_a_property_whose_proof_no_longer_checks_is_skipped_saying_so():
    declaration = make_declaration(
        "Spec.Broken", DeclarationStatus.FAILED, failure_reason=DeclarationFailureReason.PROOF_CHECK_FAILED
    )

    suite = _suite(RunResults.from_declarations([declaration], duration_seconds=1.0))
    skipped = suite.find("testcase/skipped")

    assert suite.find("testcase/failure") is None
    assert skipped is not None
    assert skipped.get("message") == f"Unresolved: {PROOF_CHECK_FAILED_REASON}"


def test_an_unchecked_proof_of_a_failed_run_is_skipped_with_why_it_is_unchecked():
    results = RunResults.from_declarations([make_declaration("Spec.Done")], duration_seconds=1.0, task_failed=True)

    skipped = _suite(results).find("testcase/skipped")

    assert skipped is not None
    assert (skipped.get("message") or "").startswith(
        "Reported proved, unchecked. The run failed due to an internal error before Aristotle checked its results."
    )


def test_a_run_that_stopped_is_skipped_with_its_reason():
    results = RunResults.from_declarations(
        [
            make_declaration(
                "Spec.Unlucky",
                DeclarationStatus.FAILED,
                failure_reason=DeclarationFailureReason.SYSTEM_ERROR,
                description="The FIFO never overflows.",
            )
        ],
        duration_seconds=1.0,
    )

    suite = _suite(results)
    skipped = suite.find("testcase/skipped")

    # No verdict, so not a <failure>; the reason, not the description, is the message.
    assert suite.find("testcase/failure") is None
    assert suite.get("failures") == "0"
    assert skipped is not None
    assert "system error" in (skipped.get("message") or "")
    assert "FIFO" not in (skipped.get("message") or "")


@pytest.mark.parametrize(
    ("reason", "label"),
    [(DeclarationFailureReason.INCOMPLETE, "incomplete"), (DeclarationFailureReason.CANCELED, "cancelled")],
)
def test_a_property_its_run_left_incomplete_or_cancelled_is_skipped_saying_so(
    reason: DeclarationFailureReason, label: str
):
    declaration = make_declaration("Spec.Unlucky", DeclarationStatus.FAILED, failure_reason=reason)

    skipped = _suite(RunResults.from_declarations([declaration], duration_seconds=1.0)).find("testcase/skipped")

    assert skipped is not None
    assert skipped.get("message") == f"Unresolved ({label}): the run stopped before reaching a verdict."


def test_the_test_case_is_grouped_by_its_file():
    results = RunResults.from_declarations(
        [make_declaration("Spec.A", filename="Spec/Fifo.lean")],
        duration_seconds=1.0,
    )

    case = _suite(results).find("testcase")
    assert case is not None
    assert case.get("classname") == "Spec/Fifo.lean"


def test_the_project_url_is_recorded_as_a_suite_property():
    results = RunResults.from_declarations(
        [make_declaration()],
        duration_seconds=1.0,
        project_url="https://aristotle.harmonic.fun/projects/abc",
    )

    prop = _suite(results).find("properties/property")
    assert prop is not None
    assert prop.get("value") == "https://aristotle.harmonic.fun/projects/abc"


def test_output_is_well_formed_xml_even_with_no_properties():
    results = RunResults.from_declarations([], duration_seconds=0.0)

    parsed = ET.fromstring(junit.to_string(results))

    assert parsed.tag == "testsuites"
    assert parsed.get("tests") == "0"


def test_write_creates_missing_parent_directories(tmp_path: Path):
    results = RunResults.from_declarations([make_declaration()], duration_seconds=1.0)
    destination = tmp_path / "reports" / "junit" / "aristotle.xml"

    junit.write(results, destination)

    assert ET.parse(destination).getroot().tag == "testsuites"


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership")
@pytest.mark.parametrize("euid,chowned", [(0, ["reports/junit/aristotle.xml", "reports/junit", "reports"]), (1000, [])])
@pytest.mark.parametrize("in_checkout", [False, True], ids=["written in place", "moved into place"])
def test_as_root_the_report_and_new_directories_go_to_the_workspace_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, euid: int, chowned: list[str], in_checkout: bool
):
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(junit.os, "geteuid", lambda: euid)
    monkeypatch.setattr(junit.os, "chown", lambda path, uid, gid, **_: calls.append((str(path), uid, gid)))
    owner = tmp_path.stat()
    monkeypatch.chdir(tmp_path)

    junit.write(
        RunResults.from_declarations([], duration_seconds=1.0),
        Path("reports/junit/aristotle.xml"),
        tmp_path if in_checkout else None,
    )

    assert calls == [(path, owner.st_uid, owner.st_gid) for path in chowned]


def _link(link: Path, target: Path, folder: bool = False) -> None:
    """Make ``link`` a symbolic link to ``target``, or skip the test where the platform can't."""
    try:
        link.symlink_to(target, target_is_directory=folder)
    except OSError:
        pytest.skip("this platform cannot create symlinks")


def _write(destination: Path, workspace: Path | None = None, *checkout: Path) -> None:
    junit.write(
        RunResults.from_declarations([make_declaration()], duration_seconds=1.0),
        destination,
        workspace,
        *([list(checkout)] if checkout else []),
    )


def _is_report(path: Path) -> bool:
    return not path.is_symlink() and path.is_file() and ET.parse(path).getroot().tag == "testsuites"


@pytest.mark.parametrize("dangling", [False, True], ids=["to a file", "dangling"])
@pytest.mark.parametrize("given", ["relative", "absolute"])
def test_a_link_at_the_reports_path_in_the_checkout_is_replaced_by_the_report_not_written_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog, dangling: bool, given: str
):
    target = tmp_path / "jenkins_home" / "config.xml"
    target.parent.mkdir()
    if not dangling:
        target.write_text("<hudson/>\n")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _link(checkout / "aristotle-results.xml", target)
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(checkout if given == "relative" else tmp_path / "elsewhere")
    destination = Path("aristotle-results.xml") if given == "relative" else checkout / "aristotle-results.xml"

    with caplog.at_level(logging.WARNING, logger="aristotle"):
        _write(destination, checkout)

    assert _is_report(checkout / "aristotle-results.xml")
    assert {p.name: p.read_text() for p in target.parent.iterdir()} == (
        {} if dangling else {"config.xml": "<hudson/>\n"}
    )
    assert caplog.messages == [
        f"Replaced the symbolic link at {destination} with the report, rather than write through it."
    ]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs")
@pytest.mark.parametrize("kind", ["hard link", "FIFO"])
def test_a_hard_link_or_a_fifo_at_the_reports_path_in_the_checkout_is_replaced_by_the_report(tmp_path: Path, kind: str):
    outside = tmp_path / "config.xml"
    outside.write_text("<hudson/>\n")
    report = tmp_path / "checkout" / "aristotle-results.xml"
    report.parent.mkdir()
    if kind == "hard link":
        hard_link(outside, report)
    else:
        os.mkfifo(report)
    # A reader, so that a write into the FIFO doesn't block.
    reader = os.open(report, os.O_RDONLY | os.O_NONBLOCK) if kind == "FIFO" else None
    try:
        _write(report, report.parent)
    finally:
        if reader is not None:
            os.close(reader)

    assert _is_report(report)
    assert outside.read_text() == "<hudson/>\n"


@pytest.mark.parametrize("linked", ["reports", "build/reports"])
def test_a_linked_folder_on_the_way_in_the_checkout_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked: str
):
    outside = tmp_path / "outside"
    outside.mkdir()
    checkout = tmp_path / "checkout"
    (checkout / linked).parent.mkdir(parents=True)
    _link(checkout / linked, outside, folder=True)
    monkeypatch.chdir(checkout)

    with pytest.raises(OSError, match=f"^{re.escape(str(checkout / linked))} is a symbolic link, which could lead "):
        _write(Path(linked, "aristotle.xml"), checkout)

    assert os.listdir(outside) == []


@pytest.mark.parametrize("above", [False, True], ids=["by its own path", "through a link above it"])
def test_a_linked_folder_in_the_workspace_is_refused_from_another_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, above: bool
):
    outside = tmp_path / "outside"
    outside.mkdir()
    checkout = tmp_path / "jenkins" / "workspace"
    checkout.mkdir(parents=True)
    _link(checkout / "reports", outside, folder=True)
    if above:
        _link(tmp_path / "jenkins_home", tmp_path / "jenkins", folder=True)
    workspace = tmp_path / ("jenkins_home" if above else "jenkins") / "workspace"
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")

    with pytest.raises(OSError, match=f"^{re.escape(str(checkout / 'reports'))} is a symbolic link, "):
        _write(workspace / "reports" / "aristotle.xml", checkout)

    assert os.listdir(outside) == []


@pytest.mark.parametrize("final", [False, True], ids=["a linked folder after it", "a link at the report's path"])
def test_a_dotdot_after_a_folder_that_does_not_exist_hides_no_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, final: bool
):
    outside = tmp_path / "outside"
    outside.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    if final:
        _link(checkout / "aristotle-results.xml", outside / "planted.xml")
    else:
        _link(checkout / "reports", outside, folder=True)
    monkeypatch.chdir(checkout)
    destination = Path("build/../aristotle-results.xml" if final else "build/../reports/aristotle.xml")

    if final:
        _write(destination, checkout)
        assert _is_report(checkout / "aristotle-results.xml") and _is_report(destination)
    else:
        with pytest.raises(OSError, match=f"^{re.escape(str(checkout / 'reports'))} is a symbolic link, "):
            _write(destination, checkout)
        assert not (checkout / "build").exists()
    assert os.listdir(outside) == []


def test_a_link_in_the_checkout_reached_through_a_link_outside_it_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    outside = tmp_path / "outside"
    outside.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _link(checkout / "build", outside, folder=True)
    _link(tmp_path / "ci-reports", checkout / "build", folder=True)
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")

    with pytest.raises(OSError, match=f"^{re.escape(str(checkout / 'build'))} is a symbolic link, "):
        _write(tmp_path / "ci-reports" / "aristotle.xml", checkout)

    assert os.listdir(outside) == []


def test_the_checkout_is_known_by_identity_however_its_path_is_spelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """As on a file system that ignores case."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    spelled = tmp_path / "CHECKOUT"
    if not spelled.exists():
        pytest.skip("this file system tells names apart by case")
    outside = tmp_path / "outside"
    outside.mkdir()
    _link(checkout / "reports", outside, folder=True)
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")

    with pytest.raises(OSError, match="is a symbolic link, "):
        _write(checkout / "reports" / "aristotle.xml", spelled)

    assert os.listdir(outside) == []


@pytest.mark.parametrize("folder", [True, False], ids=["a folder", "the path itself"])
@pytest.mark.parametrize("cwd", ["checkout", "root"], ids=["from the checkout", "from the file system's root"])
def test_a_link_outside_the_checkout_is_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: bool, cwd: str
):
    """As macOS's /tmp and Linux's /dev/stdout are."""
    report = tmp_path / "private" / "aristotle.xml"
    report.parent.mkdir()
    if folder:
        _link(tmp_path / "tmp", report.parent, folder=True)
    else:
        _link(tmp_path / "stdout", report)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout if cwd == "checkout" else Path(os.path.abspath(os.sep)))

    _write(
        tmp_path / "tmp" / "aristotle.xml" if folder else tmp_path / "stdout",
        checkout,
        *([] if cwd == "checkout" else [Path.cwd()]),
    )

    assert _is_report(report)


@pytest.mark.parametrize("swapped", ["folder", "report"])
def test_a_link_put_in_place_while_the_report_is_rendered_is_not_written_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swapped: str
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "config.xml").write_text("<hudson/>\n")
    checkout = tmp_path / "checkout"
    (checkout / "reports").mkdir(parents=True)
    report = checkout / "reports" / "aristotle.xml"
    render = junit.to_string

    def rendered_while_swapped(*args: object, **kwargs: object) -> str:
        if swapped == "folder":
            (checkout / "reports").rmdir()
            _link(checkout / "reports", outside, folder=True)
        else:
            _link(report, outside / "config.xml")
        return render(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(junit, "to_string", rendered_while_swapped)

    if swapped == "folder":
        with pytest.raises(OSError, match="is a symbolic link, "):
            _write(report, checkout)
    else:
        _write(report, checkout)
        assert _is_report(report)
    assert {p.name: p.read_text() for p in outside.iterdir()} == {"config.xml": "<hudson/>\n"}


def test_a_folder_at_the_reports_path_in_the_checkout_is_left_as_it_was(tmp_path: Path):
    checkout = tmp_path / "checkout"
    (checkout / "aristotle-results.xml").mkdir(parents=True)

    with pytest.raises(IsADirectoryError, match="aristotle-results.xml"):
        _write(checkout / "aristotle-results.xml", checkout)

    assert os.listdir(checkout) == ["aristotle-results.xml"]


def test_a_report_that_cannot_be_moved_into_place_leaves_the_path_as_it_was_and_nothing_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "aristotle-results.xml").write_text("<testsuites/>\n")

    def busy(source: str, destination: str, **kwargs: object) -> None:
        raise OSError(errno.EBUSY, os.strerror(errno.EBUSY), destination)

    monkeypatch.setattr(os, "replace", busy)

    with pytest.raises(OSError, match=os.strerror(errno.EBUSY)):
        _write(checkout / "aristotle-results.xml", checkout)

    assert os.listdir(checkout) == ["aristotle-results.xml"]
    assert (checkout / "aristotle-results.xml").read_text() == "<testsuites/>\n"


def test_a_testcase_carries_the_source_location():
    """GitLab and Jenkins link a failing case to file/line when the report names them."""
    results = RunResults.from_declarations(
        [
            make_declaration(
                "Spec.A",
                DeclarationStatus.REFUTED,
                filename="Spec/Counter.lean",
                source_file="rtl/counter.sv",
                source_line=42,
                evidence=DeclarationEvidence.COUNTEREXAMPLE,
            )
        ],
        duration_seconds=1.0,
    )

    testcase = _suite(results).find(".//testcase")
    assert testcase is not None
    assert testcase.get("file") == "rtl/counter.sv"
    assert testcase.get("line") == "42"
    assert testcase.get("classname") == "rtl/counter.sv"

    failure = testcase.find("failure")
    assert failure is not None
    assert failure.text is not None
    assert "Source: rtl/counter.sv:42" in failure.text


def test_a_testcase_without_a_source_location_omits_the_attributes():
    results = RunResults.from_declarations(
        [make_declaration("Spec.A", filename="Spec/Counter.lean")],
        duration_seconds=1.0,
    )

    testcase = _suite(results).find(".//testcase")
    assert testcase is not None
    assert testcase.get("file") is None
    assert testcase.get("line") is None
    assert testcase.get("classname") == "Spec/Counter.lean"


def test_characters_xml_cannot_carry_are_stripped_from_every_field():
    results = RunResults.from_declarations(
        [
            make_declaration(
                "Spec.\x07Bell",
                DeclarationStatus.REFUTED,
                description="\x1b[31mred\x1b[0m and a \x00null",
                lean_code="theorem t : \x0bTrue",
                evidence=DeclarationEvidence.COUNTEREXAMPLE,
            ),
            make_declaration("Spec.Ok", DeclarationStatus.PROVED, description="tab\tstays\x1f"),
        ],
        duration_seconds=1.0,
    )

    xml = junit.to_string(results)

    assert not any(ch in xml for ch in "\x00\x07\x0b\x1b\x1f")
    suite = ET.fromstring(xml).find("testsuite")
    failure = suite.find("testcase/failure")
    assert failure.get("message") == "[31mred[0m and a null"
    assert "theorem t : True" in failure.text
    assert suite.findall("testcase")[0].get("name") == "Spec.Bell"
    assert suite.find("testcase/system-out").text == "tab\tstays"


def test_a_test_case_from_a_subproject_names_the_file_from_the_repository_top():
    results = RunResults.from_declarations(
        [
            make_declaration(
                "Spec.A",
                DeclarationStatus.REFUTED,
                source_file="rtl/counter.sv",
                source_line=42,
                evidence=DeclarationEvidence.COUNTEREXAMPLE,
            )
        ],
        duration_seconds=1.0,
        path_prefix="hw/",
    )

    testcase = _suite(results).find(".//testcase")
    assert testcase is not None
    assert testcase.get("file") == "hw/rtl/counter.sv"


def test_a_manifest_property_the_run_did_not_register_is_a_skipped_case_with_no_file():
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=[NO_OVERFLOW])

    suite = _suite(results)
    testcase = suite.find("testcase")

    assert (suite.get("tests"), suite.get("failures"), suite.get("skipped")) == ("1", "0", "1")
    assert testcase is not None
    assert testcase.get("name") == "no_overflow"
    assert testcase.get("classname") == "aristotle"
    assert testcase.get("file") is None
    assert testcase.find("skipped").get("message") == "Aristotle registered no property under this id, or withdrew it."


def test_a_refuted_manifest_property_is_named_by_its_id_and_its_failure_quotes_the_request():
    declaration = make_declaration(
        "Spec.NoOverflow",
        DeclarationStatus.REFUTED,
        description="The write pointer wraps past the read pointer at cycle 12.",
        source_file="rtl/fifo.sv",
        evidence=DeclarationEvidence.COUNTEREXAMPLE,
    ).model_copy(update={"declaration_id": "no_overflow"})
    results = RunResults.from_declarations([declaration], duration_seconds=1.0, requested=[NO_OVERFLOW])

    testcase = _suite(results).find("testcase")

    assert testcase is not None
    assert testcase.get("name") == "no_overflow"
    assert testcase.get("file") == "rtl/fifo.sv"
    failure = testcase.find("failure")
    assert failure.get("message") == "The write pointer wraps past the read pointer at cycle 12."
    assert "Property: no_overflow" in failure.text
    assert "Requested: The FIFO never overflows." in failure.text
    assert "The write pointer wraps past the read pointer at cycle 12." in failure.text
    assert "Spec.NoOverflow" not in failure.text


@pytest.mark.parametrize(
    "where",
    [{"source_file": "rtl/fifo.sv"}, {"filename": "Spec/Fifo.lean"}],
    ids=["source file", "generated file"],
)
def test_a_manifest_property_keeps_one_classname_wherever_the_run_put_it(where: dict[str, str]):
    declaration = make_declaration("Spec.NoOverflow", **where).model_copy(update={"declaration_id": "no_overflow"})
    results = RunResults.from_declarations([declaration], duration_seconds=1.0, requested=[NO_OVERFLOW])

    testcase = _suite(results).find("testcase")

    assert testcase is not None
    assert testcase.get("classname") == "aristotle"
    assert testcase.get("file") == where.get("source_file")


def test_a_refuted_manifest_property_without_a_description_states_the_request_once():
    declaration = make_declaration(
        "Spec.NoOverflow", DeclarationStatus.REFUTED, description=None, evidence=DeclarationEvidence.NEGATION_PROVED
    ).model_copy(update={"declaration_id": "no_overflow"})
    results = RunResults.from_declarations([declaration], duration_seconds=1.0, requested=[NO_OVERFLOW])

    failure = _suite(results).find("testcase/failure")

    assert failure.get("message") == "The FIFO never overflows."
    assert "Requested:" not in failure.text
    assert failure.text.count("The FIFO never overflows.") == 1


def test_suggestions_are_not_test_cases():
    results = RunResults.from_declarations(
        [
            make_declaration("Spec.NoOverflow").model_copy(update={"declaration_id": "no_overflow"}),
            make_declaration("Spec.Extra", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE),
            make_declaration("Spec.Idea", DeclarationStatus.IN_PROGRESS),
        ],
        duration_seconds=1.0,
        requested=[NO_OVERFLOW],
    )

    root = junit.build_root(results)
    suite = root.find("testsuite")

    # A CI that marks a build unstable on any <failure> would otherwise gate on a suggestion.
    assert (root.get("tests"), root.get("failures")) == ("1", "0")
    assert (suite.get("tests"), suite.get("failures"), suite.get("skipped")) == ("1", "0", "0")
    assert [case.get("name") for case in suite.findall("testcase")] == ["no_overflow"]


@pytest.mark.parametrize(
    ("cut", "reason"),
    [
        ({"out_of_budget": True}, "The run ran out of budget before Aristotle checked its results."),
        ({"timed_out": True}, "The time budget ran out before Aristotle checked the run's results."),
    ],
    ids=["out of budget", "out of time"],
)
def test_a_cut_short_runs_proofs_and_refutations_are_skipped_as_unchecked(cut: dict[str, bool], reason: str):
    results = RunResults.from_declarations(
        [
            make_declaration("Spec.Good", description="Holds."),
            make_declaration(
                "Spec.Bad", DeclarationStatus.REFUTED, description=None, evidence=DeclarationEvidence.NEGATION_PROVED
            ),
        ],
        duration_seconds=1.0,
        **cut,
    )

    root = junit.build_root(results)
    suite = root.find("testsuite")
    assert suite is not None
    skipped = {case.get("name"): case.find("skipped") for case in suite.findall("testcase")}

    assert root.get("failures") == suite.get("failures") == "0"
    assert suite.get("skipped") == "2"
    assert suite.find("testcase/failure") is None
    assert skipped["Spec.Good"].get("message") == f"Reported proved, unchecked. {reason} Holds."
    assert skipped["Spec.Bad"].get("message") == f"Reported refuted, unchecked. {reason}"


@pytest.mark.parametrize(
    ("run", "stop"),
    [
        ({"timed_out": True}, "the time budget ran out"),
        ({"out_of_budget": True}, "it ran out of budget"),
        ({"task_failed": True}, "it failed due to an internal error"),
        ({"canceled": True}, "it was cancelled outside this job"),
        ({"stopped_summary": "Which reset?"}, "Aristotle stopped it for decisions only you can make"),
    ],
    ids=["out of time", "out of budget", "failed", "cancelled", "stopped for questions"],
)
def test_a_manifest_property_a_stopped_run_has_nothing_under_says_when_it_stopped(run: dict, stop: str):
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=[NO_OVERFLOW], **run)

    skipped = _suite(results).find("testcase[@name='no_overflow']/skipped")

    assert skipped is not None
    assert skipped.get("message") == f"The run had no property under this id when {stop}."


def test_a_manifest_property_a_finished_run_has_nothing_under_is_not_registered():
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=[NO_OVERFLOW])

    skipped = _suite(results).find("testcase[@name='no_overflow']/skipped")

    assert skipped is not None
    assert skipped.get("message") == "Aristotle registered no property under this id, or withdrew it."


@pytest.mark.parametrize(
    ("run", "kind", "message", "text"),
    [
        ({"out_of_budget": True}, "out of budget", OUT_OF_BUDGET_REASON, UNCHECKED_NOTE),
        ({"timed_out": True}, "time budget", TIME_BUDGET_REASON, UNCHECKED_NOTE),
        ({"task_failed": True}, "failed", TASK_FAILED_REASON, UNCHECKED_PROOFS_NOTE),
        ({"canceled": True}, "cancelled", CANCELED_REASON, UNCHECKED_PROOFS_NOTE),
        ({"never_started": True}, "never started", NEVER_STARTED_REASON, None),
    ],
    ids=["out of budget", "out of time", "failed", "cancelled", "never started"],
)
def test_a_run_that_stopped_short_of_checked_verdicts_is_an_error_in_the_suite_saying_why(
    run: dict, kind: str, message: str, text: str | None
):
    proved = make_declaration("Spec.NoOverflow", DeclarationStatus.PROVED).model_copy(
        update={"declaration_id": "no_overflow"}
    )
    results = RunResults.from_declarations([proved], duration_seconds=1.0, requested=[NO_OVERFLOW], **run)

    root = junit.build_root(results)
    suite = _suite(results)

    assert (suite.get("tests"), suite.get("errors"), root.get("tests"), root.get("errors")) == ("2", "1", "2", "1")
    error = suite.find("testcase[@name='Aristotle run']/error")
    assert error is not None
    assert (error.get("type"), error.get("message"), error.text) == (kind, message, text)


@pytest.mark.parametrize(
    ("run", "message"),
    [
        ({"out_of_budget": True}, "The run ran out of budget with no property under any of the manifest's ids."),
        ({"timed_out": True}, "The time budget ran out with no property under any of the manifest's ids."),
        ({"task_failed": True}, TASK_FAILED_REASON),
        ({"canceled": True}, CANCELED_REASON),
    ],
    ids=["out of budget", "out of time", "failed", "cancelled"],
)
def test_the_error_of_a_run_that_reached_no_property_points_at_none(run: dict, message: str):
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=[NO_OVERFLOW], **run)

    error = _suite(results).find("testcase[@name='Aristotle run']/error")

    assert error is not None
    assert (error.get("message"), error.text) == (message, None)


def test_a_cut_short_runs_error_carries_an_excerpt_of_its_summary():
    results = dataclasses.replace(
        RunResults.from_declarations([], duration_seconds=1.0, requested=[NO_OVERFLOW], out_of_budget=True),
        run_summary="The model drops the assertion.\n" + "w" * MAX_SUMMARY_CHARACTERS,
    )

    error = _suite(results).find("testcase[@name='Aristotle run']/error")

    assert error is not None
    assert (error.text or "").startswith("Aristotle's summary of the run:\nThe model drops the assertion.\n")
    assert "the job log has the whole summary" in (error.text or "")


def test_a_manifest_property_of_a_run_that_never_started_is_skipped_with_that_reason():
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=[NO_OVERFLOW], never_started=True)

    skipped = _suite(results).find("testcase[@name='no_overflow']/skipped")

    assert skipped is not None
    assert skipped.get("message") == (
        "The run never started: it was still queued, behind your other runs in progress or waiting for capacity, "
        "when the time budget ran out."
    )


def test_a_failure_carries_an_excerpt_of_the_runs_summary():
    refuted = make_declaration(
        "Spec.NoOverflow", DeclarationStatus.REFUTED, evidence=DeclarationEvidence.COUNTEREXAMPLE
    ).model_copy(update={"declaration_id": "no_overflow"})
    results = dataclasses.replace(
        RunResults.from_declarations([refuted], duration_seconds=1.0, requested=[NO_OVERFLOW]),
        run_summary="no_overflow fails at cycle 12.\n" + "w" * MAX_SUMMARY_CHARACTERS,
    )

    failure = _suite(results).find("testcase/failure")

    assert failure is not None and failure.text is not None
    _, carried = failure.text.split("Aristotle's summary of the run, which usually names the counterexample:\n")
    assert carried.startswith("no_overflow fails at cycle 12.\nwww")
    assert carried.endswith(f"\n… (cut at {MAX_SUMMARY_CHARACTERS} characters; the job log has the whole summary)")
    assert len(carried) < MAX_SUMMARY_CHARACTERS + 100


ASSUMED = dataclasses.replace(NO_OVERFLOW, assumptions="push is low while full.")


def test_a_verdict_under_assumptions_says_so():
    proved = make_declaration("Spec.NoOverflow", description="It never overflows.").model_copy(
        update={"declaration_id": "no_overflow"}
    )
    refuted = proved.model_copy(
        update={
            "declaration_id": "full_drops",
            "status": DeclarationStatus.REFUTED,
            "evidence": DeclarationEvidence.NEGATION_PROVED,
        }
    )
    full_drops = dataclasses.replace(ASSUMED, id="full_drops")

    root = junit.build_root(
        RunResults.from_declarations([proved, refuted], duration_seconds=1.0, requested=[ASSUMED, full_drops])
    )

    cases = {case.get("name"): case for case in root.iter("testcase")}
    assert (
        cases["no_overflow"].find("system-out").text == "It never overflows.\n\nHolds, assuming push is low while full."
    )
    assert "\n\nAssuming: push is low while full.\n" in cases["full_drops"].find("failure").text


def test_an_expected_failure_passes_and_an_unexpected_proof_fails():
    suite = _suite(expecting_refuted(DeclarationStatus.REFUTED, DeclarationStatus.PROVED))
    expected, unexpected = suite.findall("testcase")

    assert suite.get("failures") == "1"
    assert expected.find("failure") is None and expected.find("skipped") is None
    system_out = expected.find("system-out")
    assert system_out is not None and (system_out.text or "").startswith("Result: refuted, as expected\n")
    failure = unexpected.find("failure")
    assert failure is not None
    assert failure.get("message") == "demo1 unexpectedly proved: update the manifest"
    assert "Result: unexpectedly proved: update the manifest" in (failure.text or "")


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


def test_a_deliberate_counterexample_under_an_assumption_says_what_it_assumed():
    suite = _suite(_deliberate_under_an_assumption(DeclarationStatus.REFUTED))

    system_out = suite.find("testcase").find("system-out")
    assert system_out is not None
    assert system_out.text == "Result: refuted, as expected\nThe example property holds.\n\nAssuming: pop is low."


def _linked_failure_text(artifact_paths: list[str]) -> str:
    declaration = make_declaration(
        "Spec.NoOverflow",
        DeclarationStatus.REFUTED,
        description="Overflows.",
        lean_code="def NoOverflow : Prop",
        evidence=DeclarationEvidence.NEGATION_PROVED,
    ).model_copy(update={"artifact_paths": artifact_paths})
    results = dataclasses.replace(
        RunResults.from_declarations([declaration], duration_seconds=1.0), run_summary="It overflows at cycle 12."
    )
    failure = _suite(results).find("testcase/failure")
    assert failure is not None and failure.text is not None
    return failure.text


def test_a_refuted_propertys_failure_names_its_artifacts_and_the_download_command_before_the_summary():
    text = _linked_failure_text(["sim/cex.vcd", "sim/cex_tb.sv"])

    assert (
        "def NoOverflow : Prop\n\n"
        "Artifacts:\n"
        "  sim/cex.vcd\n"
        "  sim/cex_tb.sv\n"
        "Download them with:\n"
        "  aristotle download project-1 --artifact sim/cex.vcd --artifact sim/cex_tb.sv\n"
        "\n"
        "Aristotle's summary of the run, which usually names the counterexample:\n"
        "It overflows at cycle 12."
    ) in text


def test_a_refuted_propertys_failure_without_artifacts_reads_as_it_did():
    text = _linked_failure_text([])

    assert "Artifacts" not in text
    assert "def NoOverflow : Prop\n\nAristotle's summary of the run" in text
