"""Tests for locating a declaration in the checkout."""

import logging
import os
from pathlib import Path

import pytest

from aristotlelib.ci import location
from tests.ci.conftest import make_declaration

COUNTER_SV = """module counter (
    input  logic clk,
    output logic [7:0] count
);
    always_ff @(posedge clk) begin
        count <= count + 1;
    end
endmodule
"""


def test_the_anchor_finds_the_line_that_the_recorded_number_has_lost(tmp_path: Path):
    """A header inserted above the property shifts the line; the anchor follows it."""
    (tmp_path / "counter.sv").write_text("// Copyright 2026 Example Inc.\n// SPDX: MIT\n\n" + COUNTER_SV)

    declaration = make_declaration(
        filename="",
        source_file="counter.sv",
        source_line=6,
        source_anchor="        count <= count + 1;",
    )

    assert location.resolve(declaration, tmp_path) == location.SourceLocation("counter.sv", 9)
    # The recorded number now names a different line.
    assert (tmp_path / "counter.sv").read_text().splitlines()[5].strip() == "output logic [7:0] count"


def test_an_anchor_that_moved_is_reported_the_same_way_by_every_format(tmp_path: Path):
    """resolve, line_in_checkout and describe agree on the line."""
    (tmp_path / "counter.sv").write_text("// header\n" + COUNTER_SV)
    declaration = make_declaration(
        filename="",
        source_file="counter.sv",
        source_line=6,
        source_anchor="count <= count + 1;",
    )

    resolved = location.resolve(declaration, tmp_path)
    assert resolved is not None
    assert resolved.line == 7
    assert location.line_in_checkout(declaration, tmp_path) == 7
    assert location.describe(declaration, tmp_path) == "counter.sv:7"


def test_an_anchor_quoted_without_its_indentation_still_matches(tmp_path: Path):
    """Surrounding whitespace is ignored when matching the anchor."""
    (tmp_path / "counter.sv").write_text(COUNTER_SV)

    declaration = make_declaration(filename="", source_file="counter.sv", source_anchor="count <= count + 1;")

    assert location.line_in_checkout(declaration, tmp_path) == 6


def test_an_anchor_that_is_gone_falls_back_to_the_recorded_line(tmp_path: Path):
    """No match falls back to the recorded line."""
    (tmp_path / "counter.sv").write_text(COUNTER_SV)

    declaration = make_declaration(
        filename="",
        source_file="counter.sv",
        source_line=6,
        source_anchor="        count <= count - 1;",
    )

    assert location.line_in_checkout(declaration, tmp_path) == 6


def test_an_anchor_that_now_matches_twice_falls_back_rather_than_picking(tmp_path: Path):
    """An ambiguous anchor falls back to the recorded line."""
    (tmp_path / "counter.sv").write_text(COUNTER_SV.replace("endmodule\n", "    end\nendmodule\n"))

    declaration = make_declaration(filename="", source_file="counter.sv", source_line=3, source_anchor="end")

    assert location.line_in_checkout(declaration, tmp_path) == 3


def test_a_declaration_with_no_anchor_still_uses_its_recorded_line(tmp_path: Path):
    (tmp_path / "counter.sv").write_text(COUNTER_SV)

    declaration = make_declaration(filename="", source_file="counter.sv", source_line=6)

    assert location.resolve(declaration, tmp_path) == location.SourceLocation("counter.sv", 6)


def test_without_a_checkout_the_recorded_line_is_all_there_is():
    declaration = make_declaration(
        filename="",
        source_file="counter.sv",
        source_line=6,
        source_anchor="        count <= count + 1;",
    )

    assert location.line_in_checkout(declaration) == 6
    assert location.describe(declaration) == "counter.sv:6"
    assert location.describe(make_declaration(filename="Spec/C.lean")) is None


def test_a_file_too_large_to_search_falls_back_instead_of_reading_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "counter.sv").write_text(COUNTER_SV)
    monkeypatch.setattr(location, "MAX_SEARCHED_FILE_BYTES", 10)

    declaration = make_declaration(
        filename="",
        source_file="counter.sv",
        source_line=6,
        source_anchor="        count <= count + 1;",
    )

    assert location.line_in_checkout(declaration, tmp_path) == 6


def test_the_source_file_wins_over_the_generated_artifact(tmp_path: Path):
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "counter.sv").write_text("module counter;\nendmodule\n")
    (tmp_path / "Spec").mkdir()
    (tmp_path / "Spec" / "Counter.lean").write_text("theorem t : True := trivial\n")

    declaration = make_declaration(
        filename="Spec/Counter.lean",
        source_file="rtl/counter.sv",
        source_line=2,
    )

    assert location.resolve(declaration, tmp_path) == location.SourceLocation("rtl/counter.sv", 2)


def test_the_generated_artifact_is_the_fallback(tmp_path: Path):
    """A project that submitted Lean directly still gets a location."""
    (tmp_path / "Spec").mkdir()
    (tmp_path / "Spec" / "Counter.lean").write_text("theorem t : True := trivial\n")

    declaration = make_declaration(filename="Spec/Counter.lean")

    assert location.resolve(declaration, tmp_path) == location.SourceLocation("Spec/Counter.lean", None)


def test_a_path_that_is_not_in_the_checkout_has_no_location(tmp_path: Path):
    declaration = make_declaration(filename="Spec/Counter.lean", source_file="rtl/counter.sv")

    assert location.resolve(declaration, tmp_path) is None


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permission bits, which root bypasses")
def test_a_source_file_in_a_folder_it_cant_enter_has_no_location_but_its_recorded_line(tmp_path: Path):
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "counter.sv").write_text(COUNTER_SV)
    declaration = make_declaration(filename="", source_file="rtl/counter.sv", source_line=6)
    (tmp_path / "rtl").chmod(0o600)
    try:
        assert location.resolve(declaration, tmp_path) is None
        assert location.describe(declaration, tmp_path) == "rtl/counter.sv:6"
    finally:
        (tmp_path / "rtl").chmod(0o700)


def test_a_source_file_that_is_missing_does_not_fall_back_to_the_artifact(tmp_path: Path):
    """The engineer's file is the claim; if it is gone, the Lean path is not a substitute."""
    (tmp_path / "Spec").mkdir()
    (tmp_path / "Spec" / "Counter.lean").write_text("theorem t : True := trivial\n")

    declaration = make_declaration(filename="Spec/Counter.lean", source_file="rtl/gone.sv")

    assert location.resolve(declaration, tmp_path) == location.SourceLocation("Spec/Counter.lean", None)


def test_paths_outside_the_workspace_are_refused(tmp_path: Path):
    outside = tmp_path.parent / "outside.sv"
    outside.write_text("module outside;\nendmodule\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    relative = make_declaration(filename="", source_file="../outside.sv")
    absolute = make_declaration(filename="", source_file=str(outside))

    assert location.resolve(relative, workspace) is None
    assert location.resolve(absolute, workspace) is None


WINDOWS_ESCAPES = [
    r"C:\Windows\System32\drivers\etc\hosts",  # drive-absolute
    r"C:hosts",  # drive-relative: no root, but `workspace / this` still drops the workspace
    r"\\server\share\secrets.sv",  # UNC
    r"\etc\passwd",  # rooted on the current drive
    r"..\..\outside.sv",  # climbing out with the other separator
    r"rtl\..\..\outside.sv",
]


@pytest.mark.parametrize("path", WINDOWS_ESCAPES)
def test_a_windows_style_escape_is_refused_on_every_platform(path: str, tmp_path: Path, caplog):
    """Asserted on the log line: on Linux each of these is simply a missing file, so None alone proves nothing."""
    declaration = make_declaration(filename="", source_file=path)

    with caplog.at_level(logging.DEBUG, logger="aristotle"):
        assert location.resolve(declaration, tmp_path) is None

    assert "Ignoring out-of-workspace declaration path" in caplog.text


def test_a_line_number_that_is_not_a_line_is_dropped(tmp_path: Path):
    (tmp_path / "counter.sv").write_text("module counter;\nendmodule\n")

    declaration = make_declaration(filename="", source_file="counter.sv", source_line=0)

    assert location.resolve(declaration, tmp_path) == location.SourceLocation("counter.sv", None)


def test_a_directory_is_not_a_location(tmp_path: Path):
    (tmp_path / "rtl").mkdir()

    declaration = make_declaration(filename="", source_file="rtl")

    assert location.resolve(declaration, tmp_path) is None


def test_the_reported_path_needs_no_checkout():
    """JUnit consumers group by path without ever opening the file."""
    assert location.reported_path(make_declaration(filename="Spec/C.lean")) == "Spec/C.lean"
    assert location.reported_path(make_declaration(filename="Spec/C.lean", source_file="c.sv")) == "c.sv"
    assert location.reported_path(make_declaration(filename="")) is None
