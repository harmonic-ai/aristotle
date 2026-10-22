"""Tests for aristotlelib.ci.github."""

import dataclasses
import time
from pathlib import Path

import pytest

from aristotlelib.ci import github, results
from aristotlelib.ci.manifest import Kind, RequestedProperty
from aristotlelib.ci.results import RunResults
from aristotlelib.declaration import DeclarationFailureReason, DeclarationStatus
from tests.ci.conftest import expecting_disproved, make_declaration

NO_OVERFLOW = RequestedProperty("no_overflow", Kind.NATURAL_LANGUAGE, "The FIFO never overflows.")


def test_a_failing_property_produces_an_error_annotation(capsys, tmp_path: Path):
    results = RunResults.from_declarations(
        [make_declaration("Spec.NoOverflow", DeclarationStatus.COUNTEREXAMPLE, description="Overflows at cycle 12.")],
        duration_seconds=1.0,
    )

    github.write_annotations(results, tmp_path)

    out = capsys.readouterr().out
    assert out.startswith("::error ")
    assert "title=Aristotle%3A Spec.NoOverflow" in out
    assert "::Overflows at cycle 12." in out


def test_a_property_in_progress_is_a_notice_not_an_error(capsys, tmp_path: Path):
    results = RunResults.from_declarations(
        [make_declaration("Spec.Hard", DeclarationStatus.IN_PROGRESS)],
        duration_seconds=1.0,
    )

    github.write_annotations(results, tmp_path)

    out = capsys.readouterr().out
    assert "::notice " in out
    assert "::error " not in out


def test_a_passing_property_is_not_annotated(capsys, tmp_path: Path):
    results = RunResults.from_declarations([make_declaration("Spec.Fine")], duration_seconds=1.0)

    github.write_annotations(results, tmp_path)

    assert capsys.readouterr().out == ""


def test_newlines_and_reserved_characters_are_escaped(capsys, tmp_path: Path):
    results = RunResults.from_declarations(
        [
            make_declaration(
                "Spec.Multi",
                DeclarationStatus.NEGATED,
                description="First line\nSecond line with 100% and, a comma",
            )
        ],
        duration_seconds=1.0,
    )

    github.write_annotations(results, tmp_path)

    out = capsys.readouterr().out.strip()
    # A raw newline would terminate the workflow command early and lose the rest.
    assert len(out.splitlines()) == 1
    assert "%0A" in out
    assert "100%25" in out


def test_control_characters_in_an_annotation_are_escaped(capsys, tmp_path: Path):
    results = RunResults.from_declarations(
        [make_declaration("Spec.Ctl", DeclarationStatus.NEGATED, description="red\x1b[31m\r\nnext\rcr\x85nel")],
        duration_seconds=1.0,
    )

    github.write_annotations(results, tmp_path)

    assert capsys.readouterr().out.endswith("::red\\x1b[31m%0Anext\\rcr\\x85nel\n")


def _levels(output: str) -> list[str]:
    return [line.split(" ", 1)[0].split("::", 2)[1] for line in output.strip().splitlines()]


@pytest.mark.parametrize(
    "failed,unresolved,errors,notices,further",
    [
        (15, 0, 10, 1, 5),
        (0, 15, 0, 10, 6),
        (15, 15, 10, 10, 11),
        (10, 10, 10, 10, 0),
        (11, 3, 10, 4, 1),
    ],
)
def test_annotations_stay_within_the_runners_cap_of_ten_per_level(
    capsys, tmp_path: Path, failed: int, unresolved: int, errors: int, notices: int, further: int
):
    results = RunResults.from_declarations(
        [make_declaration(f"Spec.F{i}", DeclarationStatus.NEGATED) for i in range(failed)]
        + [make_declaration(f"Spec.U{i}", DeclarationStatus.IN_PROGRESS) for i in range(unresolved)],
        duration_seconds=1.0,
    )

    github.write_annotations(results, tmp_path)

    output = capsys.readouterr().out
    levels = _levels(output)
    assert (levels.count("error"), levels.count("notice")) == (errors, notices)
    assert levels.count("notice") <= github.MAX_ANNOTATIONS_PER_LEVEL
    if further:
        assert output.strip().splitlines()[-1].startswith(f"::notice::{further} further Aristotle results")
    else:
        assert "further" not in output


def test_the_annotation_includes_the_file_when_one_resolves(capsys, tmp_path: Path):
    (tmp_path / "fifo.v").write_text("module fifo;\nendmodule\n")
    results = RunResults.from_declarations(
        [make_declaration("Spec.A", DeclarationStatus.NEGATED, filename="fifo.v")],
        duration_seconds=1.0,
    )

    github.write_annotations(results, tmp_path)

    assert "file=fifo.v" in capsys.readouterr().out


def test_the_annotation_points_at_the_source_line(capsys, tmp_path: Path):
    """A reviewer should land on the Verilog that failed, not on a generated file."""
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "counter.sv").write_text("module counter;\nendmodule\n")
    results = RunResults.from_declarations(
        [
            make_declaration(
                "Spec.A",
                DeclarationStatus.COUNTEREXAMPLE,
                filename="Spec/Counter.lean",
                source_file="rtl/counter.sv",
                source_line=2,
            )
        ],
        duration_seconds=1.0,
    )

    github.write_annotations(results, tmp_path)

    output = capsys.readouterr().out
    assert "file=rtl/counter.sv" in output
    assert "line=2" in output
    assert "Spec/Counter.lean" not in output


def test_the_summary_names_the_source_location():
    results = RunResults.from_declarations(
        [make_declaration("Spec.A", source_file="rtl/counter.sv", source_line=42)],
        duration_seconds=1.0,
    )

    assert "`rtl/counter.sv:42`" in github.build_summary(results)


def test_the_summary_renders_a_row_per_property():
    results = RunResults.from_declarations(
        [
            make_declaration("Spec.Good", DeclarationStatus.PROVED),
            make_declaration("Spec.Bad", DeclarationStatus.COUNTEREXAMPLE),
            make_declaration("Spec.Slow", DeclarationStatus.IN_PROGRESS),
        ],
        duration_seconds=1.0,
        project_url="https://aristotle.harmonic.fun/projects/abc",
    )

    summary = github.build_summary(results)

    assert "1 proved, 1 disproved, 1 in progress" in summary
    assert "`Spec.Good`" in summary
    assert "disproved by counterexample" in summary
    assert "https://aristotle.harmonic.fun/projects/abc" in summary


def test_the_summary_says_why_a_stopped_run_has_no_verdict():
    results = RunResults.from_declarations(
        [
            make_declaration(
                "Spec.Slow", DeclarationStatus.FAILED, failure_reason=DeclarationFailureReason.OUT_OF_BUDGET
            )
        ],
        duration_seconds=1.0,
    )

    summary = github.build_summary(results)

    assert "0 proved, 0 disproved, 0 in progress, 1 unresolved" in summary
    assert "unresolved (out of budget)" in summary


def test_a_stopped_run_is_a_notice_that_names_its_reason(capsys, tmp_path: Path):
    results = RunResults.from_declarations(
        [
            make_declaration(
                "Spec.Slow",
                DeclarationStatus.FAILED,
                failure_reason=DeclarationFailureReason.OUT_OF_BUDGET,
                description="The FIFO never overflows.",
            )
        ],
        duration_seconds=1.0,
    )

    github.write_annotations(results, tmp_path)

    out = capsys.readouterr().out
    assert out.startswith("::notice ")
    assert "unresolved (out of budget)" in out
    assert "::error" not in out


def test_a_property_whose_proof_no_longer_checks_is_a_notice_that_says_so(capsys, tmp_path: Path):
    declaration = make_declaration(
        "Spec.Broken", DeclarationStatus.FAILED, failure_reason=DeclarationFailureReason.PROOF_CHECK_FAILED
    )

    github.write_annotations(RunResults.from_declarations([declaration], duration_seconds=1.0), tmp_path)

    out = capsys.readouterr().out
    assert out.startswith("::notice ")
    assert "Spec.Broken unresolved (the proof no longer checks; re-run to re-prove)" in out


def test_the_summary_of_a_failed_run_says_its_proofs_are_unchecked():
    results = RunResults.from_declarations([make_declaration("Spec.Done")], duration_seconds=1.0, task_failed=True)

    summary = github.build_summary(results)

    assert "0 proved, 0 disproved, 0 in progress, 1 unchecked (1 proved)" in summary
    assert "a proof among them is unchecked, but a disproof counts" in summary


def test_the_summary_says_a_run_left_going_goes_on_and_how_to_report_on_it():
    note = "The run goes on, as --on-timeout leave is set. Report on it later by running the same command with --project p."
    results = RunResults.from_declarations([], duration_seconds=1.0, timed_out=True)

    assert "[!NOTE]" not in github.build_summary(results)
    assert f"> [!NOTE]\n> {note}\n" in github.build_summary(dataclasses.replace(results, left_note=note))


def test_errors_that_ended_the_step_are_error_annotations_within_the_cap(capsys):
    github.write_error_annotations(
        "aristotle ci", [f"error {i}\n100%" for i in range(github.MAX_ANNOTATIONS_PER_LEVEL + 1)]
    )

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == github.MAX_ANNOTATIONS_PER_LEVEL
    assert lines[0] == "::error title=aristotle ci::error 0%0A100%25"


def test_a_pipe_in_a_description_does_not_break_the_summary_table():
    results = RunResults.from_declarations(
        [make_declaration("Spec.A", description="holds when a | b is set\nand reset is low")],
        duration_seconds=1.0,
    )

    rows = [line for line in github.build_summary(results).splitlines() if "Spec.A" in line]

    # One row, and the pipe in the text is escaped so it does not open a sixth cell.
    assert len(rows) == 1
    assert rows[0].count("|") - rows[0].count("\\|") == 5
    assert "a \\| b" in rows[0]


@pytest.mark.parametrize("line_break", ["\r", "\r\n", "\u2028"], ids=["CR", "CRLF", "LS"])
def test_a_line_break_in_a_description_does_not_break_the_summary_table(line_break: str):
    results = RunResults.from_declarations(
        [make_declaration("Spec.A", description=f"holds when full{line_break}and reset is low")], duration_seconds=1.0
    )

    rows = [line for line in github.build_summary(results).splitlines() if "Spec.A" in line]

    assert len(rows) == 1
    assert "| holds when full and reset is low |" in rows[0]


@pytest.mark.parametrize(
    ("description", "detail"),
    [
        ("Holds </blockquote><img src=x>.", "Holds <\u200b/blockquote><\u200bimg src=x>."),
        ("Holds when `a | b` is set, <b>always</b>.", "Holds when `a \\| b` is set, <\u200bb>always<\u200b/b>."),
        ("`x | <h2>All proved</h2> | y`", "`x \\| <h2>All proved</h2> \\| y`"),
        ("```<img src=x>", "```<\u200bimg src=x>"),
        ("See [x](`) <img src=x> `.", "See [x]\u200b(`) <img src=x> `."),
        ("See https://example.com/a: `a <= b`.", "See https://example.com/a: `a <= b`."),
        (
            "See [the spec](https://example.com/a_(b)) for `req |-> ##[1:3] ack`.",
            "See [the spec](https://example.com/a_(b)) for `req \\|-> ##[1:3] ack`.",
        ),
        ("`f [x](y) <= www.z` holds.", "`f [x](y) <= www.z` holds."),
        ("See https://example.com/`a <= b`.", "See https:\u200b//example.com/`a <= b`."),
        ('See https://example.com/a](b "`a <= b`").', 'See https:\u200b//example.com/a]\u200b(b "`a <= b`").'),
        ("Holds\x1b[31m red.", "Holds\\x1b[31m red."),
        (
            "Holds ![x](https://example.com/x.png) and ![y][r].",
            "Holds !\u200b[x](https://example.com/x.png) and !\u200b[y][r].",
        ),
    ],
    ids=[
        "the ticket's html",
        "a table pipe in code",
        "html and pipes in code",
        "a fence, which a cell can't hold",
        "a link that would take in a backtick",
        "a URL beside code",
        "a link beside code",
        "links inside code",
        "a URL that runs into code",
        "a link's title in a URL",
        "a control character",
        "images",
    ],
)
def test_a_description_carries_no_html_or_image_into_the_summary_table_but_keeps_its_code(description, detail):
    results = RunResults.from_declarations([make_declaration("Spec.A", description=description)], duration_seconds=1.0)

    [row] = [line for line in github.build_summary(results).splitlines() if "Spec.A" in line]

    assert row == f"| ✅ | `Spec.A` | proved | {detail} |"


def test_a_description_and_its_assumptions_are_judged_as_one_cell():
    # Judged apart, the description's unpaired backtick would pair with the assumptions' and free their code's HTML.
    assumed = RequestedProperty(
        "no_overflow", Kind.NATURAL_LANGUAGE, "The FIFO never overflows.", assumptions="`<img src=x>` is set."
    )
    proved = make_declaration("Spec.NoOverflow", description="It never overflows `").model_copy(
        update={"declaration_id": "no_overflow"}
    )

    summary = github.build_summary(RunResults.from_declarations([proved], duration_seconds=1.0, requested=[assumed]))

    assert "| It never overflows `<br>Holds, assuming `<\u200bimg src=x>` is set. |" in summary


def test_a_long_description_of_many_code_spans_is_neutralized_in_well_under_5_seconds():
    description = "`a < b` and " * 150_000
    results = RunResults.from_declarations([make_declaration("Spec.A", description=description)], duration_seconds=1.0)

    started = time.monotonic()
    summary = github.build_summary(results)

    assert time.monotonic() - started < 5
    assert "`a < b` and `a < b`" in summary


@pytest.mark.parametrize("marker", ["](", "]((", "](a ", '](a "', "][", "www.", "://"])
def test_a_long_description_of_link_markers_is_neutralized_in_well_under_5_seconds(marker: str):
    results = RunResults.from_declarations(
        [make_declaration("Spec.A", description=marker * 100_000 + "`a < b`")], duration_seconds=1.0
    )

    started = time.monotonic()
    summary = github.build_summary(results)

    assert time.monotonic() - started < 5
    assert "`a < b` |" in summary


@pytest.mark.parametrize(
    ("name", "source_file", "cell"),
    [
        ("Spec.«x`<zq>`»", None, "``Spec.«x`<zq>`»``"),
        ("Spec.<img src=x>`", None, "`` Spec.<img src=x>` ``"),
        ("", "x``<img src=x>``y.v", "`  `<br>```x``<img src=x>``y.v:3```"),
        ("Spec.\x1bA", None, "`Spec.\\x1bA`"),
        ("Spec.A", "rtl/a`<zq>`.v", "`Spec.A`<br>``rtl/a`<zq>`.v:3``"),
        ("Spec.A", "rtl/``<zq>``.v", "`Spec.A`<br>```rtl/``<zq>``.v:3```"),
        ("Spec.A", "`rtl/a.v", "`Spec.A`<br>`` `rtl/a.v:3 ``"),
        ("Spec.A", "rtl/a|b.v", "`Spec.A`<br>`rtl/a\\|b.v:3`"),
        ("Spec.A", "rtl/a\nb.v", "`Spec.A`<br>`rtl/a b.v:3`"),
        ("Spec.A", "rtl/" + "`" * 16 + "<zq>.v", "`Spec.A`<br>rtl\\/" + "\\`" * 16 + "\\<zq\\>\\.v\\:3"),
        ("Spec.[x](" + "`" * 16, "rtl/a.v", "Spec\\.\\[x\\]\\(" + "\\`" * 16 + "<br>`rtl/a.v:3`"),
    ],
    ids=[
        "a backtick in the name",
        "a backtick that ends the name",
        "an empty name",
        "a control character in the name",
        "a backtick in the file path",
        "a run of two in the file path",
        "a backtick that starts it",
        "a pipe",
        "a line break",
        "a run of 16",
        "a link's start before the file path",
    ],
)
def test_a_name_or_file_path_stays_inside_its_code_span_in_the_summary_table(name, source_file, cell):
    declaration = make_declaration(
        name, description="Holds.", source_file=source_file, source_line=3 if source_file else None
    )

    summary = github.build_summary(RunResults.from_declarations([declaration], duration_seconds=1.0))

    [row] = [line for line in summary.splitlines() if line.startswith("| ✅")]
    assert row == f"| ✅ | {cell} | proved | Holds. |"


@pytest.mark.parametrize(
    ("cut", "reason"),
    [
        ({"out_of_budget": True}, "The run ran out of budget before Aristotle checked its results."),
        ({"timed_out": True}, "The time budget ran out before Aristotle checked the run's results."),
    ],
    ids=["out of budget", "out of time"],
)
def test_the_summary_cautions_that_a_cut_short_runs_results_are_unchecked(cut: dict[str, bool], reason: str):
    results = RunResults.from_declarations(
        [make_declaration("Spec.A", description="Holds.").model_copy(update={"declaration_id": "no_overflow"})],
        duration_seconds=1.0,
        requested=[NO_OVERFLOW],
        **cut,
    )

    summary = github.build_summary(results)

    assert "**0 proved, 0 disproved, 0 in progress, 1 unchecked (1 proved)**" in summary
    assert (
        f"> [!CAUTION]\n> {reason} Properties below are those it reached, unchecked: none counts as proved or disproved.\n"
    ) in summary
    assert "| ⬜ | `no_overflow` | proved, unchecked | Holds. |" in summary
    assert "language enabled" not in summary


def test_the_summary_says_when_a_run_was_cut_short_before_it_registered_a_property():
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=[NO_OVERFLOW], out_of_budget=True)

    summary = github.build_summary(results)

    assert (
        "> [!CAUTION]\n> The run ran out of budget with no property under any of the manifest's ids.\n\n"
    ) in summary
    assert "unchecked" not in summary


def test_a_cut_short_runs_proof_and_disproof_are_notices_with_their_claims(capsys, tmp_path: Path):
    results = RunResults.from_declarations(
        [
            make_declaration("Spec.Good", description="Holds."),
            make_declaration("Spec.Bad", DeclarationStatus.COUNTEREXAMPLE, description="Fails at cycle 3."),
        ],
        duration_seconds=1.0,
        out_of_budget=True,
    )

    github.write_annotations(results, tmp_path)

    lines = capsys.readouterr().out.splitlines()
    assert [line.split(" ", 1)[0] for line in lines] == ["::notice", "::notice"]
    assert lines[0].endswith("::Spec.Bad disproved by counterexample, unchecked: Fails at cycle 3.")
    assert lines[1].endswith("::Spec.Good proved, unchecked: Holds.")


def test_the_summary_says_a_run_that_never_left_the_queue_never_started():
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=[NO_OVERFLOW], never_started=True)

    summary = github.build_summary(results)

    assert "The run never started: it was still queued" in summary
    assert "language enabled" not in summary
    assert "time budget. Properties" not in summary


def test_the_summary_says_when_the_task_failed():
    results = RunResults.from_declarations([make_declaration()], duration_seconds=1.0, task_failed=True)

    assert "internal error" in github.build_summary(results)


@pytest.mark.parametrize("run", [{"task_failed": True}, {"canceled": True}], ids=["failed", "cancelled"])
def test_the_summary_of_a_failed_or_cancelled_run_that_reached_nothing_points_at_no_properties(run: dict):
    results = RunResults.from_declarations([], duration_seconds=1.0, **run)

    assert "Properties below" not in github.build_summary(results)


def test_the_summary_says_when_the_task_was_cancelled():
    results = RunResults.from_declarations([make_declaration()], duration_seconds=1.0, canceled=True)

    assert "cancelled outside this job" in github.build_summary(results)


def test_write_summary_appends_to_the_file_github_provides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    summary_file = tmp_path / "summary.md"
    summary_file.write_text("earlier step\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    results = RunResults.from_declarations([make_declaration("Spec.A")], duration_seconds=1.0)

    github.write_summary(results)

    content = summary_file.read_text()
    assert content.startswith("earlier step\n")
    assert "Spec.A" in content


def test_write_summary_is_a_no_op_outside_github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    results = RunResults.from_declarations([make_declaration()], duration_seconds=1.0)

    github.write_summary(results)


def test_an_unwritable_summary_does_not_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # A report we cannot write must never take down a build.
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "missing-dir" / "summary.md"))
    results = RunResults.from_declarations([make_declaration()], duration_seconds=1.0)

    github.write_summary(results)


def test_an_annotation_from_a_subproject_names_the_file_from_the_repository_top(capsys, tmp_path: Path):
    """GitHub places a relative file= from the repository top, not from the directory that was verified."""
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "counter.sv").write_text("module counter;\nendmodule\n")
    results = RunResults.from_declarations(
        [make_declaration("Spec.A", DeclarationStatus.COUNTEREXAMPLE, source_file="rtl/counter.sv", source_line=2)],
        duration_seconds=1.0,
        path_prefix="hw/",
    )

    github.write_annotations(results, tmp_path)

    assert "file=hw/rtl/counter.sv" in capsys.readouterr().out


def test_a_manifest_property_the_run_did_not_register_is_a_notice_with_no_file(capsys, tmp_path: Path):
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=[NO_OVERFLOW])

    github.write_annotations(results, tmp_path)

    assert capsys.readouterr().out == "::notice title=Aristotle%3A no_overflow::no_overflow not registered\n"


def test_the_summary_row_of_an_unregistered_manifest_property_shows_the_request():
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=[NO_OVERFLOW])

    summary = github.build_summary(results)

    assert "0 proved, 0 disproved, 0 in progress, 1 not registered" in summary
    assert "| ⬜ | `no_overflow` | not registered | The FIFO never overflows. |" in summary.splitlines()
    assert "No properties were registered" not in summary


def test_a_manifest_property_is_reported_under_its_id_not_the_declarations_name(capsys, tmp_path: Path):
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "fifo.sv").write_text("module fifo;\nendmodule\n")
    declaration = make_declaration(
        "Spec.NoOverflow",
        DeclarationStatus.COUNTEREXAMPLE,
        description="Overflows at cycle 12.",
        source_file="rtl/fifo.sv",
        source_line=2,
    ).model_copy(update={"declaration_id": "no_overflow"})
    results = RunResults.from_declarations([declaration], duration_seconds=1.0, requested=[NO_OVERFLOW])

    github.write_annotations(results, tmp_path)
    summary = github.build_summary(results)

    out = capsys.readouterr().out
    assert out == "::error title=Aristotle%3A no_overflow,file=rtl/fifo.sv,line=2::Overflows at cycle 12.\n"
    assert "| ❌ | `no_overflow`<br>`rtl/fifo.sv:2` | disproved by counterexample | Overflows at cycle 12. |" in summary
    assert "Spec.NoOverflow" not in summary


def test_suggestions_get_their_own_summary_table_and_no_annotations(capsys, tmp_path: Path):
    results = RunResults.from_declarations(
        [
            make_declaration("Spec.NoOverflow").model_copy(update={"declaration_id": "no_overflow"}),
            make_declaration("Spec.Extra", DeclarationStatus.COUNTEREXAMPLE, description="Wraps at cycle 12."),
            make_declaration("Spec.Idea", DeclarationStatus.IN_PROGRESS),
        ],
        duration_seconds=1.0,
        requested=[NO_OVERFLOW],
    )

    github.write_annotations(results, tmp_path)
    lines = github.build_summary(results).splitlines()

    assert capsys.readouterr().out == ""
    assert "**1 proved, 0 disproved, 0 in progress**" in lines
    heading = lines.index("### Suggested by Aristotle")
    assert lines.index("| ✅ | `no_overflow` | proved | The example property holds. |") < heading
    assert lines.index("| ❌ | `Spec.Extra` | disproved by counterexample | Wraps at cycle 12. |") > heading
    assert lines.index("| ⬜ | `Spec.Idea` | in progress | The example property holds. |") > heading


def test_an_open_ended_summary_has_no_suggestions_table():
    results = RunResults.from_declarations(
        [make_declaration("Spec.A"), make_declaration("Spec.B", DeclarationStatus.NEGATED)], duration_seconds=1.0
    )

    summary = github.build_summary(results)

    assert "Suggested by Aristotle" not in summary
    assert "`Spec.B`" in summary


def test_the_summary_cautions_when_a_manifest_run_verified_nothing():
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=[NO_OVERFLOW])

    summary = github.build_summary(results)

    assert (
        "> [!CAUTION]\n> No property in the manifest was verified. The step log names the likely causes.\n" in summary
    )


def test_an_open_ended_summary_never_says_nothing_was_verified():
    results = RunResults.from_declarations([], duration_seconds=1.0)

    assert "was verified" not in github.build_summary(results)


def test_a_failed_manifest_run_does_not_also_blame_the_accounts_languages():
    results = RunResults.from_declarations([], duration_seconds=1.0, requested=[NO_OVERFLOW], task_failed=True)

    assert "was verified" not in github.build_summary(results)


@pytest.mark.parametrize(
    ("run", "reason"),
    [
        ({"stopped_summary": "Which reset?"}, "Aristotle stopped the run for decisions only you can make."),
        ({"out_of_budget": True}, "The run ran out of budget before Aristotle checked its results."),
        ({"task_failed": True}, "The run failed due to an internal error before Aristotle checked its results."),
        ({}, "The run finished without checking it."),
    ],
    ids=["stopped for questions", "out of budget", "failed", "ended"],
)
def test_a_property_in_progress_says_so_and_why_before_its_statement(run, reason, capsys, tmp_path: Path):
    results = RunResults.from_declarations(
        [
            make_declaration(
                "Spec.Underflow", DeclarationStatus.IN_PROGRESS, description="The counter never underflows."
            )
        ],
        duration_seconds=1.0,
        **run,
    )

    github.write_annotations(results, tmp_path)

    notice = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("::notice"))
    assert notice.endswith(f"::Spec.Underflow in progress. {reason} The counter never underflows.")


def test_the_notice_cap_keeps_an_unchecked_disproof_ahead_of_unchecked_proofs(capsys, tmp_path: Path):
    proofs = [make_declaration(f"Spec.Proved{i:02}") for i in range(12)]
    disproof = make_declaration("Spec.Zz", DeclarationStatus.NEGATED, description="Overflows at cycle 9.")
    results = RunResults.from_declarations([*proofs, disproof], duration_seconds=1.0, out_of_budget=True)

    github.write_annotations(results, tmp_path)

    notices = [line for line in capsys.readouterr().out.splitlines() if line.startswith("::notice")]
    assert "Spec.Zz disproved, unchecked" in notices[0]
    assert len(notices) == github.MAX_ANNOTATIONS_PER_LEVEL


def test_a_stopped_run_is_one_error_annotation_pointing_at_the_log(capsys, tmp_path: Path):
    results = RunResults.from_declarations(
        [make_declaration("Spec.F", DeclarationStatus.NEGATED)],
        duration_seconds=1.0,
        stopped_summary="Which reset polarity is intended?\n- 100% high",
    )

    github.write_annotations(results, tmp_path)

    first, *rest = capsys.readouterr().out.strip().splitlines()
    assert first == (
        "::error title=Aristotle stopped before finishing::Aristotle stopped the run for decisions only you can make. "
        "Put the answers in --instructions (ARISTOTLE_INSTRUCTIONS) and run again. "
        "The run's summary, with its open questions, is in the step log."
    )
    assert _levels("\n".join(rest)) == ["error"]


def test_the_job_summary_of_a_stopped_run_says_so_without_agent_text():
    results = RunResults.from_declarations(
        [], duration_seconds=1.0, requested=[NO_OVERFLOW], stopped_summary="Open questions:\n1. Which reset?"
    )

    summary = github.build_summary(results)

    assert (
        "> [!CAUTION]\n> Aristotle stopped before finishing, for decisions only you can make. Its open questions are in"
        " the run's summary, printed in the step log.\n"
    ) in summary
    assert "Which reset?" not in summary


def _refuted(summary: str | None) -> RunResults:
    results = RunResults.from_declarations(
        [
            make_declaration(
                "Spec.NoOverflow",
                DeclarationStatus.NEGATED,
                description="Holds.",
                source_file="rtl/fifo.v",
                source_line=3,
            ).model_copy(update={"declaration_id": "no_overflow"})
        ],
        duration_seconds=1.0,
        requested=[NO_OVERFLOW],
    )
    return dataclasses.replace(results, run_summary=summary)


def test_the_job_summary_quotes_the_runs_summary_after_a_refuted_property():
    summary = github.build_summary(_refuted("no_overflow fails:\n\n| a | b |\n</details>\nat cycle 12\x1b[31m."))

    assert summary.endswith(
        "### Aristotle's summary of the run\n\n> no_overflow fails:\n>\n> | a | b |\n> <\u200b/details>\n"
        "> at cycle 12\\x1b[31m.\n"
    )


@pytest.mark.parametrize(
    "first_line", ["[\\!CAUTION]", "[&#x21;NOTE]", "[&excl;CAUTION]", "&#91;!CAUTION]", "[x]: https://example.com"]
)
def test_an_escaped_or_encoded_alert_marker_on_the_first_line_is_made_inert(first_line: str):
    summary = github.build_summary(_refuted(f"{first_line}\n[&#33;NOTE]\nAll properties proved."))

    quoted = summary.split("### Aristotle's summary of the run\n\n", 1)[1]
    assert quoted.startswith(f"> \u200b{first_line}\n")


def test_a_summary_that_opens_with_a_heading_keeps_it():
    summary = github.build_summary(_refuted("## Module `counter`\nFails at cycle 10."))

    assert "### Aristotle's summary of the run\n\n> ## Module `counter`\n> Fails at cycle 10.\n" in summary


def test_html_alerts_and_footnotes_in_the_runs_summary_stay_inert_inside_the_quote():
    summary = github.build_summary(
        _refuted("[!TIP]\n</blockquote>\n## Forged: all properties proved\n<!-- hides the rest\n[^1]: a footnote")
    )

    quoted = summary.split("### Aristotle's summary of the run\n\n", 1)[1]
    assert quoted == (
        "> \u200b[\u200b!TIP]\n> <\u200b/blockquote>\n> ## Forged: all properties proved\n> <\u200b!-- hides the rest\n"
        "> [\u200b^1]: a footnote\n"
    )


def test_code_in_the_runs_summary_keeps_its_text_while_html_around_it_stays_inert():
    summary = github.build_summary(
        _refuted("`count <= 9` fails <details>\n```sv\nassert property (count <= 9);\n```\nsee ``a`<b`` and <b>")
    )

    quoted = summary.split("### Aristotle's summary of the run\n\n", 1)[1]
    assert quoted == (
        "> `count <= 9` fails <\u200bdetails>\n> ```sv\n> assert property (count <= 9);\n> ```\n"
        "> see ``a`<b`` and <\u200bb>\n"
    )


@pytest.mark.parametrize(
    "before", ["", "| ID | Status |\n|---|---|\n| `p` | refuted |\n\n"], ids=["no table", "a table before a blank line"]
)
def test_a_pipe_in_code_keeps_its_text_where_no_table_could_split_it(before: str):
    summary = github.build_summary(_refuted(f"{before}- checked as `req |-> count <= 9`"))

    assert summary.endswith("> - checked as `req |-> count <= 9`\n")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("> | a |\n> | --- |\n>\n> `x | <= y`", "> > | a |\n> > | --- |\n> >\n> > `x | <= y`\n"),
        ("<zq> `a < b`", "> <\u200bzq> `a < b`\n"),
        ("`mem[addr][7:0] <= d` and `f](x)`", "> `mem[addr][7:0] <= d` and `f](x)`\n"),
        ("Result\n---\n`req |-> count <= 9`", "> Result\n> ---\n> `req |-> count <= 9`\n"),
        ("see " + "`" * 16 + "a < b" + "`" * 16, "> see " + "`" * 16 + "a < b" + "`" * 16 + "\n"),
    ],
    ids=[
        "a pipe after a quoted table's end",
        "html before code",
        "link markers inside code",
        "hyphens that underline a heading",
        "runs of 16",
    ],
)
def test_code_keeps_its_text_where_nothing_could_read_its_backticks_otherwise(text: str, expected: str):
    assert github.build_summary(_refuted(text)).endswith(expected)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("See ![x](https://example.com/x.png).", "> See !\u200b[x](https://example.com/x.png).\n"),
        (
            "See ![y][r].\n\n[r]: https://example.com/y.png",
            "> See !\u200b[y][r].\n>\n> [r]: https://example.com/y.png\n",
        ),
        ("Use `v = ![1, 2]` as ![v].", "> Use `v = ![1, 2]` as !\u200b[v].\n"),
    ],
    ids=["inline", "reference", "Mathlib's vector notation in code"],
)
def test_an_image_in_the_runs_summary_is_left_a_plain_link_while_code_keeps_its_text(text: str, expected: str):
    assert github.build_summary(_refuted(text)).endswith(expected)


def test_an_unclosed_fence_in_the_runs_summary_runs_to_its_end_as_markdown_reads_it():
    summary = github.build_summary(_refuted("Counterexample:\n~~~\ncount <= 9 fails\n<details>"))

    assert summary.endswith("> Counterexample:\n> ~~~\n> count <= 9 fails\n> <details>\n")


@pytest.mark.parametrize(
    "text",
    [
        "a `b\nc `<x>` d",
        "\\`<b>`",
        "  ```\n<x>\n```\n<details>",
        "```a`b\n<x>",
        "| a | b | c |\n| --- | --- | --- |\n| `x | </blockquote><h2>All proved</h2> | y` |",
        "> | a |\n> | --- |\n> | `x | <h2>All proved</h2> | y` |",
        "| a | b |\n| --- | --- |\u00a0\n| `x | <zq> | y` |",
        "| `x | <zq> | y` |\n| --- | --- | --- |",
        "| a | b | c |\n| --- | --- | --- |\n| p | q | r |\n| `x | <zq> | y` |",
        "a | b | c\n--- | :-: | ---:\n`x | <zq> | y`",
        "```\n~~~\n```\n<zq>",
        "````\n```\n````\n<zq>",
        "```\n``` x\n```\n<zq>",
        "see " + "`" * 81 + "<zq>" + "`" * 81,
        "see " + "`" * 17 + "<zq>" + "`" * 17,
    ],
    ids=[
        "a run unpaired on its line",
        "a run after a backslash",
        "an indented fence",
        "a backtick in a fence's info",
        "a table cell boundary in a code span",
        "a quoted table",
        "a delimiter row that ends in a no-break space",
        "a pipe in the header row's code",
        "a pipe in a later body row's code",
        "a delimiter row with colons and no outer pipes",
        "a fence a tilde line doesn't close",
        "a fence a shorter run doesn't close",
        "a fence a run with an info string doesn't close",
        "runs too long to make a code span",
        "runs longer than 16",
    ],
)
def test_markdown_that_could_read_code_otherwise_leaves_no_live_html(text: str):
    summary = github.build_summary(_refuted(text))

    quoted = summary.split("### Aristotle's summary of the run\n\n", 1)[1]
    assert "<" not in quoted.replace("<\u200b", "")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "See [the docs](`) </blockquote><h2>All proved</h2> `.",
            "> See [the docs]\u200b(`) </blockquote><h2>All proved</h2> `.\n",
        ),
        ("See [the docs](\n`) <img src=x> `", "> See [the docs]\u200b(\n> `) <img src=x> `\n"),
        ('[a](/u "x\n`") <img src=x> `', '> \u200b[a]\u200b(/u "x\n> `") <img src=x> `\n'),
        ("[a][`] <img src=x> `", "> \u200b[a]\u200b[`] <img src=x> `\n"),
        ("www.example.com/` </blockquote> `", "> w\u200bww.example.com/` </blockquote> `\n"),
        ("https://example.com/` <img src=x> `", "> https:\u200b//example.com/` <img src=x> `\n"),
        (
            "See [w](https://example.com/a_(b))`<img src=x>`",
            "> See [w]\u200b(https:\u200b//example.com/a_(b))`<img src=x>`\n",
        ),
        (
            'See https://example.com/a](b "`count <= 9`")',
            '> See https:\u200b//example.com/a]\u200b(b "`count <= 9`")\n',
        ),
        (
            "See [a](www.example.com/)][<b>`a <= b`].",
            "> See [a]\u200b(w\u200bww.example.com/)]\u200b[<\u200bb>`a <= b`].\n",
        ),
    ],
    ids=[
        "a link destination",
        "a link destination from the line before",
        "a link title from the line before",
        "a reference label",
        "a www autolink",
        "a URL autolink",
        "an autolink in a link's destination",
        "a link's title in a URL",
        "a link around a URL that runs into a label",
    ],
)
def test_a_link_that_could_take_in_a_backtick_is_left_text_and_code_keeps_its_text(text: str, expected: str):
    assert github.build_summary(_refuted(text)).endswith(expected)


@pytest.mark.parametrize(
    "text",
    [
        "Fails at https://example.com/runs/1: `req |-> ##[1:3] ack` and `count <= 9`.",
        'See [the docs](https://example.com/a_(b) "The docs") for `count <= 9`.',
        "`count <= 9` fails, see www.example.com.\n\n`a < b` holds, as [the spec][s] says.",
        "See [the docs](\n\n`count <= 9`",
        "`see [x](y) <= www.z` fails",
    ],
    ids=["a URL", "a link with a title", "a www autolink and a reference link", "a blank line after", "links in code"],
)
def test_code_keeps_its_text_beside_a_link_that_takes_in_no_backtick(text: str):
    summary = github.build_summary(_refuted(text))

    quoted = summary.split("### Aristotle's summary of the run\n\n", 1)[1]
    assert quoted == "".join(f"> {line}".rstrip() + "\n" for line in text.splitlines())


def test_the_job_summary_carries_an_excerpt_of_a_long_run_summary():
    summary = github.build_summary(_refuted("z" * (results.MAX_SUMMARY_CHARACTERS + 10)))

    assert "> … (cut at" in summary
    assert "z" * (results.MAX_SUMMARY_CHARACTERS + 1) not in summary


@pytest.mark.parametrize("summary", ["no_overflow fails at cycle 12.", None], ids=["summary", "no summary"])
def test_a_refuted_propertys_annotation_points_at_the_runs_summary_only_when_there_is_one(
    capsys, tmp_path: Path, summary: str | None
):
    github.write_annotations(_refuted(summary), tmp_path)

    [annotation] = capsys.readouterr().out.strip().splitlines()
    assert annotation.endswith(f"::Holds. {github.SUMMARY_POINTER}" if summary else "::Holds.")
    assert "cycle 12" not in annotation


def test_the_summary_says_what_a_verdict_assumes_in_its_cell():
    assumed = RequestedProperty(
        "no_overflow", Kind.NATURAL_LANGUAGE, "The FIFO never overflows.", assumptions="push | pop\nare one-hot."
    )
    proved = make_declaration("Spec.NoOverflow", description="It never overflows.").model_copy(
        update={"declaration_id": "no_overflow"}
    )

    summary = github.build_summary(RunResults.from_declarations([proved], duration_seconds=1.0, requested=[assumed]))
    unresolved = github.build_summary(RunResults.from_declarations([], duration_seconds=1.0, requested=[assumed]))

    assert "| It never overflows.<br>Holds, assuming push \\| pop are one-hot. |" in summary
    assert "| The FIFO never overflows.<br>Assuming: push \\| pop are one-hot. |" in unresolved


def test_an_expected_failure_is_not_annotated_and_an_unexpected_proof_is_an_error(capsys, tmp_path: Path):
    results = expecting_disproved(DeclarationStatus.NEGATED, DeclarationStatus.PROVED)

    github.write_annotations(results, tmp_path)
    summary = github.build_summary(results)

    out = capsys.readouterr().out
    assert out.count("::error") == 1 and "::notice" not in out
    assert "demo1 unexpectedly proved: update the manifest" in out
    assert "| ✅ | `demo0` | disproved, as expected |" in summary
    assert "| ❌ | `demo1` | unexpectedly proved: update the manifest |" in summary


def _deliberate_under_an_assumption(status: DeclarationStatus) -> RunResults:
    declaration = make_declaration("Spec.Demo", status).model_copy(update={"declaration_id": "demo"})
    requested = RequestedProperty(
        "demo", Kind.NATURAL_LANGUAGE, "A full FIFO takes no write.", assumptions="pop is low.", expect_disproved=True
    )
    return RunResults.from_declarations([declaration], duration_seconds=1.0, requested=[requested])


@pytest.mark.parametrize(
    ("status", "row"),
    [
        (DeclarationStatus.NEGATED, "| ✅ | `demo` | disproved, as expected | "),
        (DeclarationStatus.PROVED, "| ❌ | `demo` | unexpectedly proved: update the manifest | "),
    ],
)
def test_a_deliberate_counterexample_under_an_assumption_is_never_said_to_hold(status: DeclarationStatus, row: str):
    summary = github.build_summary(_deliberate_under_an_assumption(status))

    assert f"{row}The example property holds.<br>Assuming: pop is low. |" in summary


def _linked_failure(artifact_paths: list[str], summary: str | None = None) -> RunResults:
    declaration = make_declaration("Spec.NoOverflow", DeclarationStatus.COUNTEREXAMPLE, description="Overflows.")
    results = RunResults.from_declarations(
        [declaration.model_copy(update={"artifact_paths": artifact_paths})], duration_seconds=1.0
    )
    return dataclasses.replace(results, run_summary=summary)


def test_a_disproved_propertys_annotation_names_its_artifacts_and_the_download_command(capsys, tmp_path: Path):
    github.write_annotations(_linked_failure(["sim/cex.vcd", "sim/cex_tb.sv"], "It overflows."), tmp_path)

    [annotation] = capsys.readouterr().out.strip().splitlines()
    assert annotation.endswith(
        f"::Overflows. {github.SUMMARY_POINTER}%0AArtifacts:%0A  sim/cex.vcd%0A  sim/cex_tb.sv%0A"
        "Download them with:%0A  aristotle download project-1 --artifact sim/cex.vcd --artifact sim/cex_tb.sv"
    )


def test_a_disproved_propertys_annotation_without_artifacts_reads_as_it_did(capsys, tmp_path: Path):
    github.write_annotations(_linked_failure([]), tmp_path)

    [annotation] = capsys.readouterr().out.strip().splitlines()
    assert annotation.endswith("::Overflows.")
