import json
import os
import random
import re
import time
from pathlib import Path

import pytest

from aristotlelib.verilog import filelist
from aristotlelib.verilog.filelist import FilelistError, Kind, Origin, Reader
from tests.conftest import NEVER_READ, UNREACHABLE_HOST, WILDCARD_FOLDERS

FIXTURES = Path(__file__).parent / "filelists"


def _projects() -> list[Path]:
    return sorted(p for p in FIXTURES.iterdir() if (p / "expected.json").is_file())


def _read_arguments(arguments: list[str], cwd: Path, environ: dict[str, str] | None = None) -> filelist.Filelist:
    reader = Reader(environ or {})
    reader.read_arguments(arguments, cwd)
    return reader.result


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())
    return path


def _relative(entries: list[filelist.Entry], root: Path) -> list[str]:
    return [Path(os.path.relpath(e.path, root)).as_posix() for e in entries]


@pytest.mark.parametrize("project", _projects(), ids=lambda p: p.name)
def test_reads_2x_fixture(project: Path):
    """Each 2.x fixture's entry point gives the defines, include folders and sources it expects."""
    expected = json.loads((project / "expected.json").read_text())
    result = _read_arguments(expected["entry_point"], project / expected.get("work_dir", "."))

    defines = dict(d.value.split("=", 1) if "=" in d.value else (d.value, None) for d in result.defines)
    assert defines == expected["defines"]
    assert sorted(_relative(result.of_kind(Kind.INCLUDE_DIR), project)) == sorted(expected["include_dirs"])
    assert sorted(_relative(result.of_kind(Kind.SOURCE), project)) == sorted(expected["source_files"])
    assert [t.value for t in result.tops] == ["top"]


def test_keeps_the_order_defines_are_named_in():
    """Defines come out in the order slang applies them, nested filelists in place."""
    result = _read_arguments(["-f", "main.f"], FIXTURES / "project3_chain")
    assert [d.value for d in result.defines] == ["MAIN_FIRST=1", "A_MIDDLE=1", "FROM_B=1", "A_AFTER_B=2", "MAIN_LAST=2"]


def test_names_the_line_of_each_value(tmp_path: Path):
    """An entry's origin is the filelist and line that named it."""
    _write(tmp_path / "a.v", "")
    f = _write(tmp_path / "list.f", "# header\n\n-D A=1\n  a.v  // trailing\n")
    result = filelist.read(f, environ={})
    assert result.defines[0].origin == Origin(f, 3)
    assert result.entries[0].origin == Origin(f, 4)
    assert str(result.entries[0].origin) == f"{f}:4"


def test_read_file_rebases_to_the_filelists_folder(tmp_path: Path):
    """read() reads paths relative to the filelist, as -F does, whatever the working directory."""
    _write(tmp_path / "rtl" / "a.v", "")
    f = _write(tmp_path / "rtl" / "list.f", "a.v\n")
    result = filelist.read(f, environ={})
    assert result.entries[0].path == tmp_path / "rtl" / "a.v"


def test_small_f_inside_big_f_is_relative_to_the_big_fs_folder(tmp_path: Path):
    """A -f nested in a -F file resolves from the -F file's folder; after it, the outer base is back."""
    _write(tmp_path / "ip" / "inner.f", "core.v\n")
    _write(tmp_path / "ip" / "core.v", "")
    _write(tmp_path / "ip" / "outer.f", "-f inner.f\n")
    _write(tmp_path / "top.v", "")
    result = _read_arguments(["-F", "ip/outer.f", "top.v"], tmp_path)
    assert _relative(result.of_kind(Kind.SOURCE), tmp_path) == ["ip/core.v", "top.v"]


def test_small_f_keeps_the_callers_base(tmp_path: Path):
    """Paths in a -f file resolve from the folder in force, not the file's own."""
    _write(tmp_path / "lists" / "list.f", "rtl/a.v\n")
    _write(tmp_path / "rtl" / "a.v", "")
    result = _read_arguments(["-f", "lists/list.f"], tmp_path)
    assert _relative(result.entries, tmp_path) == ["rtl/a.v"]


def test_absolute_paths(tmp_path: Path):
    """An absolute path is taken as it is."""
    a = _write(tmp_path / "x" / "a.v", "")
    f = _write(tmp_path / "lists" / "list.f", f"{a.as_posix()}\n-I {(tmp_path / 'x').as_posix()}\n")
    result = filelist.read(f, environ={})
    assert [e.path for e in result.entries] == [a, tmp_path / "x"]


def test_expands_each_form_of_variable(tmp_path: Path):
    """$VAR, ${VAR} and $(VAR) expand from the environment, and each variable used is recorded."""
    for name in ("a", "b", "c"):
        _write(tmp_path / "wa" / f"{name}.v", "")
    f = _write(tmp_path / "list.f", "$WA/a.v\n${WA}/b.v\n$(OTHER)/c.v\n")
    result = filelist.read(f, environ={"WA": str(tmp_path / "wa"), "OTHER": str(tmp_path / "wa")})
    assert _relative(result.entries, tmp_path) == ["wa/a.v", "wa/b.v", "wa/c.v"]
    assert result.variables == {"WA": str(tmp_path / "wa"), "OTHER": str(tmp_path / "wa")}


@pytest.mark.parametrize(
    ("text", "written"),
    [("$WORKAREA/a.v", "$WORKAREA"), ("${WORKAREA}/a.v", "${WORKAREA}"), ("+incdir+$(WORKAREA)/inc", "$(WORKAREA)")],
)
def test_an_unset_variable_stops_naming_the_line(tmp_path: Path, text: str, written: str):
    """An unset variable, which slang would expand to nothing, is an error naming its line."""
    f = _write(tmp_path / "list.f", f"// header\n{text}\n")
    with pytest.raises(FilelistError) as caught:
        filelist.read(f, environ={})
    assert str(caught.value) == f"{f}:2: the environment variable {written} is not set"


@pytest.mark.parametrize("written", ["${..}", "$(../..)", "${A/B}", "${A\\B}", "${.}", "${}"])
def test_a_braced_name_that_cant_name_a_folder_stops(tmp_path: Path, written: str):
    """Its name would name the file's folder in the upload, and .. would put it outside _ext."""
    f = _write(tmp_path / "list.f", f"{written}/lib/top.v\n")
    name = written[2:-1]
    with pytest.raises(FilelistError) as caught:
        filelist.read(f, environ={name: str(tmp_path)})
    assert str(caught.value) == f"{f}:1: {written} can't name a folder of the upload, as a variable's name must"


def test_a_braced_name_windows_sets_with_parentheses_expands(tmp_path: Path):
    _write(tmp_path / "pf" / "x.v", "")
    f = _write(tmp_path / "list.f", "${ProgramFiles(x86)}/x.v\n")
    result = filelist.read(f, environ={"ProgramFiles(x86)": str(tmp_path / "pf")})
    assert _relative(result.entries, tmp_path) == ["pf/x.v"]


def test_an_empty_variable_stops(tmp_path: Path):
    """An empty variable is an error too, since $WA/a.v would name /a.v."""
    f = _write(tmp_path / "list.f", "$WA/a.v\n")
    with pytest.raises(FilelistError, match=r"list.f:1: the environment variable \$WA is empty"):
        filelist.read(f, environ={"WA": ""})


def test_variables_expand_in_double_quotes_only(tmp_path: Path):
    """Double quotes expand a variable into one argument; single quotes keep it as written."""
    _write(tmp_path / "my dir" / "a.v", "")
    _write(tmp_path / "$WA" / "b.v", "")
    f = _write(tmp_path / "list.f", "\"$WA/a.v\"\n'$WA/b.v'\n")
    result = filelist.read(f, environ={"WA": str(tmp_path / "my dir")})
    assert _relative(result.entries, tmp_path) == ["my dir/a.v", "$WA/b.v"]


def test_a_dollar_without_a_name_is_kept(tmp_path: Path):
    """A $ not followed by a variable name, or an unclosed ${, stays in the argument."""
    _write(tmp_path / "a$.v", "")
    f = _write(tmp_path / "list.f", "a$.v\n-D X=${UNCLOSED\n")
    result = filelist.read(f, environ={})
    assert _relative(result.entries, tmp_path) == ["a$.v"]
    assert result.defines[0].value == "X=${UNCLOSED"


def test_comments(tmp_path: Path):
    """#, // and /* */ start comments; # even inside an argument, // only at its start."""
    for name in ("a.v", "b.v", "c.v", "d.v"):
        _write(tmp_path / "rtl" / name, "")
    text = "rtl/a.v# hash comment\n// line comment\n/* block\n rtl/x.v */ rtl//b.v\nrtl/c.v // rtl/x.v\nrtl/d.v\n"
    result = filelist.read(_write(tmp_path / "list.f", text), environ={})
    assert _relative(result.entries, tmp_path) == ["rtl/a.v", "rtl/b.v", "rtl/c.v", "rtl/d.v"]
    assert [e.origin.line for e in result.entries] == [1, 4, 5, 6]


def test_quotes_and_escapes(tmp_path: Path):
    """Quotes keep spaces in one argument; a backslash keeps the next character, and in double quotes escapes only
    a quote or a backslash."""
    _write(tmp_path / "a b.v", "")
    _write(tmp_path / "c d.v", "")
    text = '"a b.v"\nc\\ d.v\n-D \'MSG="hi there"\'\n-D "P=a\\"b\\\\c\\d"\n'
    result = filelist.read(_write(tmp_path / "list.f", text), environ={})
    assert _relative(result.entries, tmp_path) == ["a b.v", "c d.v"]
    assert [d.value for d in result.defines] == ['MSG="hi there"', 'P=a"b\\c\\d']


def test_joined_and_long_forms(tmp_path: Path):
    """-Idir, -DNAME=V, --top=x, -I=dir, the long names and the plus forms all read as their options."""
    for name in ("i1", "i2", "i3", "i4", "i5", "i6"):
        (tmp_path / name).mkdir()
    text = (
        "-Ii1\n-I=i2\n--include-directory i3\n+incdir+i4+i5\n-include-directory=i6\n"
        "-DA=1\n--define-macro B\n+define+C=3+D\n--top=t\n-top t2\n--undefine-macro=E,F\n-UG\n"
    )
    result = filelist.read(_write(tmp_path / "list.f", text), environ={})
    assert _relative(result.entries, tmp_path) == ["i1", "i2", "i3", "i4", "i5", "i6"]
    assert [d.value for d in result.defines] == ["A=1", "B", "C=3", "D"]
    assert [t.value for t in result.tops] == ["t", "t2"]
    assert [u.value for u in result.undefines] == ["E", "F", "G"]


def test_comma_lists(tmp_path: Path):
    """Options slang reads as comma lists split on commas; -D doesn't."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    text = "-I a,b\n-D X=1,2\n+libext+.v,.sv+.vh\n--top t1,t2\n"
    result = filelist.read(_write(tmp_path / "list.f", text), environ={})
    assert _relative(result.entries, tmp_path) == ["a", "b"]
    assert [d.value for d in result.defines] == ["X=1,2"]
    assert [e.value for e in result.library_extensions] == [".v", ".sv", ".vh"]
    assert [t.value for t in result.tops] == ["t1", "t2"]


def test_library_options(tmp_path: Path):
    """-v names a library file, -y a library folder and -Y an extension."""
    _write(tmp_path / "cells" / "and2.v", "")
    _write(tmp_path / "lib" / "x.v", "")
    text = "-v lib/x.v\n-y cells\n-Y .v\n--libdir=cells\n--libfile lib/x.v\n"
    result = filelist.read(_write(tmp_path / "list.f", text), environ={})
    assert [(e.kind, os.path.relpath(e.path, tmp_path)) for e in result.entries] == [
        (Kind.LIBRARY_FILE, os.path.join("lib", "x.v")),
        (Kind.LIBRARY_DIR, "cells"),
    ]
    assert [e.value for e in result.library_extensions] == [".v"]


def test_parameters_timescale_and_single_unit(tmp_path: Path):
    """-G takes NAME=VALUE; the first --timescale applies and later ones are listed as ignored."""
    text = "-G W=8\n--timescale 1ns/1ps\n-timescale=1ps/1ps\n--single-unit\n"
    f = _write(tmp_path / "list.f", text)
    result = filelist.read(f, environ={})
    assert [p.value for p in result.parameters] == ["W=8"]
    assert result.timescale == filelist.Named("1ns/1ps", Origin(f, 2))
    assert result.ignored == [filelist.Ignored("-timescale=1ps/1ps", Origin(f, 3), f"the --timescale at {f}:2 applies")]


def test_a_parameter_needs_a_value(tmp_path: Path):
    f = _write(tmp_path / "list.f", "-G WIDTH\n")
    with pytest.raises(FilelistError, match=r"list.f:1: -G WIDTH needs the form NAME=VALUE"):
        filelist.read(f, environ={})


def test_blackbox_is_recorded(tmp_path: Path):
    f = _write(tmp_path / "list.f", "--blackbox rom_big\n")
    assert filelist.read(f, environ={}).blackboxes == [filelist.Named("rom_big", Origin(f, 1))]


def test_simulator_options_are_ignored_with_their_arguments(tmp_path: Path):
    """Known simulator options are listed as ignored, with the arguments they take; renamed ones apply."""
    (tmp_path / "inc").mkdir()
    _write(tmp_path / "a.v", "")
    text = (
        "-sv\n-access +rwc\n-full64 -v2k\n+lint=all\n-debug_access+all\n-l sim.log\n-suppress=X\n"
        "-incdir inc\n-define WIDTH=8\n--top-module top\n--std 1800-2017\n-Wno-width\na.v\n"
    )
    f = _write(tmp_path / "list.f", text)
    result = filelist.read(f, environ={})
    assert [(i.text, i.origin.line) for i in result.ignored] == [
        ("-sv", 1),
        ("-access +rwc", 2),
        ("-full64", 3),
        ("-v2k", 3),
        ("+lint=all", 4),
        ("-debug_access+all", 5),
        ("-l sim.log", 6),
        ("-suppress=X", 7),
        ("--std 1800-2017", 11),
        ("-Wno-width", 12),
    ]
    assert _relative(result.entries, tmp_path) == ["inc", "a.v"]
    assert [d.value for d in result.defines] == ["WIDTH=8"]
    assert [t.value for t in result.tops] == ["top"]


def test_common_vcs_xcelium_and_questa_options_are_ignored(tmp_path: Path):
    """Including a code joined to +nowarn, and -assert's keyword, which a file name after it isn't taken for."""
    _write(tmp_path / "a.v", "")
    _write(tmp_path / "b.v", "")
    text = (
        "-assert svaext\n+notimingcheck\n+nowarnTFIPC\n-v2k_generate\n-Mdir=csrc\n-xlrm uniq_prior_final\n"
        "-notimingchecks -nocopyright\n-coverage all\n-svinputport=relaxed\n-assert\na.v\n-cover b:e:f\n-coverage\nb.v\n"
        "-override_timescale=1ns/1ps\n-assert\n"
    )
    f = _write(tmp_path / "list.f", text)
    result = filelist.read(f, environ={})
    assert [(i.text, i.origin.line, i.reason) for i in result.ignored] == [
        ("-assert svaext", 1, "a simulator option"),
        ("+notimingcheck", 2, "a simulator option"),
        ("+nowarnTFIPC", 3, "a simulator option"),
        ("-v2k_generate", 4, "a simulator option"),
        ("-Mdir=csrc", 5, "a simulator option"),
        ("-xlrm uniq_prior_final", 6, "a simulator option"),
        ("-notimingchecks", 7, "a simulator option"),
        ("-nocopyright", 7, "a simulator option"),
        ("-coverage all", 8, "a simulator option"),
        ("-svinputport=relaxed", 9, "a simulator option"),
        ("-assert", 10, "a simulator option"),
        ("-cover b:e:f", 12, "a simulator option"),
        ("-coverage", 13, "a simulator option"),
        ("-assert", 16, "a simulator option"),
    ]
    assert _relative(result.entries, tmp_path) == ["a.v", "b.v"]
    # Read as slang's default timescale, which clears its error for a design that mixes elements with and without one.
    assert result.timescale == filelist.Named("1ns/1ps", Origin(f, 15))


@pytest.mark.parametrize(
    "text",
    ["-timescale 1ns/1ps -override_timescale\ntop.v\n", "-override_timescale -timescale 1ns/1ps\ntop.v\n"],
)
def test_xceliums_bare_override_timescale_takes_no_argument(tmp_path: Path, text: str):
    _write(tmp_path / "top.v", "")
    f = _write(tmp_path / "list.f", text)
    result = filelist.read(f, environ={})
    assert result.timescale is not None and result.timescale.value == "1ns/1ps"
    assert [(i.text, i.reason) for i in result.ignored] == [("-override_timescale", "a simulator option")]
    assert _relative(result.entries, tmp_path) == ["top.v"]


def test_an_unknown_option_read_as_a_missing_joined_file_says_so(tmp_path: Path):
    """slang reads -v2k_gen as -v 2k_gen, so the message names both readings."""
    f = _write(tmp_path / "list.f", "-v2k_gen\n")
    with pytest.raises(FilelistError) as caught:
        filelist.read(f, environ={})
    assert (
        str(caught.value) == f"{f}:1: -v2k_gen is not an option Aristotle knows, and read as -v 2k_gen it names no file"
    )


def test_a_byte_order_mark_is_not_part_of_the_first_argument(tmp_path: Path):
    _write(tmp_path / "a.v", "")
    f = tmp_path / "list.f"
    f.write_bytes(b"\xef\xbb\xbf-sv\na.v\n")
    result = filelist.read(f, environ={})
    assert [i.text for i in result.ignored] == ["-sv"]
    assert _relative(result.entries, tmp_path) == ["a.v"]


@pytest.mark.parametrize("option", ["--ignore-unknown-modules", "-ignore-unknown-modules"])
def test_ignore_unknown_modules_stops_rather_than_being_dropped(tmp_path: Path, option: str):
    """The uploaded filelist can't carry it, and leaving it out would fail the run on each module with no source."""
    f = _write(tmp_path / "list.f", f"{option}\n")
    with pytest.raises(FilelistError) as caught:
        filelist.read(f, environ={})
    assert str(caught.value) == (
        f"{f}:1: {option} can't be sent: Aristotle needs a source for every module the design instantiates. Drop the "
        "option, then add the missing sources, or name each missing module with --blackbox <module> for Aristotle to "
        "treat as a black box with unconstrained outputs."
    )


@pytest.mark.parametrize("option", ["-unknown", "--incdir-first", "+foo+bar", "-pvalue+W=1", "--isystem"])
def test_an_unknown_option_stops(tmp_path: Path, option: str):
    f = _write(tmp_path / "list.f", f"\n{option}\n")
    with pytest.raises(FilelistError) as caught:
        filelist.read(f, environ={})
    assert str(caught.value) == f"{f}:2: {option} is not an option Aristotle knows"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("missing.v\n", "missing.v: no such file"),
        ("-v missing.v\n", "missing.v: no such file"),
        ("-y nolib\n", "nolib: no such folder"),
        ("-f missing.f\n", "missing.f: no such file"),
        ("sub\n", "sub is not a file"),
        ("rtl/*.sv\n", "rtl/*.sv matches no file"),
    ],
)
def test_a_missing_file_stops(tmp_path: Path, text: str, message: str):
    """A file, library folder or filelist that isn't there stops with its line, rather than failing in the run."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "rtl").mkdir()
    f = _write(tmp_path / "list.f", text)
    with pytest.raises(FilelistError) as caught:
        filelist.read(f, environ={})
    assert str(caught.value).startswith(f"{f}:1: ")
    assert str(caught.value).endswith(message)


def test_a_missing_include_folder_is_listed_as_ignored(tmp_path: Path):
    """slang only warns about a missing -I folder, so it's listed, not an error."""
    f = _write(tmp_path / "list.f", "-I nope\n")
    result = filelist.read(f, environ={})
    assert result.entries == []
    assert result.ignored == [filelist.Ignored("-I nope", Origin(f, 1), "no such folder")]


@pytest.mark.parametrize(("text", "message"), [("-I\n", "-I needs a value"), ("+incdir+\n", "+incdir+ needs a value")])
def test_an_option_without_its_value_stops(tmp_path: Path, text: str, message: str):
    f = _write(tmp_path / "list.f", text)
    with pytest.raises(FilelistError, match=f"list.f:1: {message.replace('+', '[+]')}"):
        filelist.read(f, environ={})


def test_an_option_at_the_end_of_a_nested_file_takes_no_value_from_its_caller(tmp_path: Path):
    """Each filelist is read on its own, as slang does: an option at its end doesn't take the next line of the
    caller."""
    _write(tmp_path / "inner.f", "-I\n")
    (tmp_path / "inc").mkdir()
    f = _write(tmp_path / "list.f", "-f inner.f\ninc\n")
    with pytest.raises(FilelistError, match=r"inner.f:1: -I needs a value"):
        filelist.read(f, environ={})


def test_a_filelist_that_names_itself_stops(tmp_path: Path):
    _write(tmp_path / "b.f", "-f a.f\n")
    f = _write(tmp_path / "a.f", "-f b.f\n")
    with pytest.raises(FilelistError, match=r"b.f:1: .*a.f names itself"):
        filelist.read(f, environ={})


def test_a_filelist_named_twice_in_sequence_is_read_twice(tmp_path: Path):
    """Only a cycle stops; the same filelist named twice is read twice, as slang does."""
    _write(tmp_path / "defs.f", "-D A\n")
    f = _write(tmp_path / "list.f", "-f defs.f\n-f defs.f\n")
    assert [d.value for d in filelist.read(f, environ={}).defines] == ["A", "A"]


def test_an_unreadable_filelist_stops(tmp_path: Path):
    inner = _write(tmp_path / "inner.f", "")
    f = _write(tmp_path / "list.f", "-f inner.f\n")
    inner.chmod(0)
    try:
        if os.access(inner, os.R_OK):
            pytest.skip("running as a user who can read any file")
        with pytest.raises(FilelistError, match=r"list.f:1: can't read .*inner.f: Permission denied"):
            filelist.read(f, environ={})
    finally:
        inner.chmod(0o644)


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permission bits, which root bypasses")
@pytest.mark.parametrize(
    ("line", "named"), [("locked/a.v", "a.v"), ("-f locked/inner.f", "inner.f")], ids=["a source", "a filelist"]
)
def test_a_file_in_a_folder_it_cant_enter_cant_be_read_rather_than_missing(tmp_path: Path, line: str, named: str):
    _write(tmp_path / "locked" / "a.v", "")
    _write(tmp_path / "locked" / "inner.f", "")
    f = _write(tmp_path / "list.f", f"{line}\n")
    (tmp_path / "locked").chmod(0o600)
    try:
        with pytest.raises(FilelistError, match=rf"list.f:1: can't read .*locked/{named}: Permission denied$"):
            filelist.read(f, environ={})
    finally:
        (tmp_path / "locked").chmod(0o700)


@pytest.mark.parametrize(
    ("state", "from_file", "from_command_line"),
    [
        ("missing", "list.f: no such file", "the command line: list.f: no such file"),
        ("a folder", "list.f is not a file", "the command line: list.f is not a file"),
        (
            "unreadable",
            "Can't read list.f: Permission denied",
            "the command line: can't read list.f: Permission denied",
        ),
        (
            "linked into a folder it can't enter",
            "Can't read list.f: Permission denied",
            "the command line: can't read list.f: Permission denied",
        ),
    ],
    ids=["missing", "a folder", "unreadable", "linked into a folder it can't enter"],
)
def test_the_filelist_read_file_reads_is_named_without_the_command_line(
    tmp_path: Path, monkeypatch, state: str, from_file: str, from_command_line: str
):
    """The command line names only the filelists -f and -F name there."""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "list.f"
    locked = tmp_path / "locked"
    if state == "a folder":
        f.mkdir()
    elif state == "unreadable":
        _write(f, "").chmod(0)
    elif state == "linked into a folder it can't enter":
        if os.name == "nt":
            pytest.skip("POSIX permission bits")
        f.symlink_to(_write(locked / "list.f", ""))
        locked.chmod(0o600)
    try:
        if state != "missing" and state != "a folder" and os.access(f, os.R_OK):
            pytest.skip("running as a user who can read any file")
        with pytest.raises(FilelistError) as read_file:
            filelist.read(f, environ={})
        with pytest.raises(FilelistError) as named:
            _read_arguments(["-f", "list.f"], tmp_path)
    finally:
        if state == "unreadable":
            f.chmod(0o644)
        elif state == "linked into a folder it can't enter":
            locked.chmod(0o700)
    assert str(read_file.value) == from_file
    assert str(named.value) == from_command_line


def test_a_file_named_twice_is_listed_once(tmp_path: Path):
    """The same file, however named, is one entry, at its first mention."""
    _write(tmp_path / "rtl" / "a.v", "")
    f = _write(tmp_path / "list.f", "rtl/a.v\n./rtl/../rtl/a.v\n")
    result = filelist.read(f, environ={})
    assert [(e.path, e.origin.line) for e in result.entries] == [(tmp_path / "rtl" / "a.v", 1)]


def test_globs(tmp_path: Path):
    """*, ? and ... match as slang's globs do, in sorted order; a trailing slash names every file in a folder."""
    for name in ("rtl/a.sv", "rtl/b.sv", "rtl/c.v", "rtl/sub/d.sv", "rtl/sub/deep/e.sv", "gen/x1.v", "gen/x22.v"):
        _write(tmp_path / name, "")
    text = "rtl/*.sv\ngen/x?.v\nrtl/.../*.sv\ngen/\n-I rtl/...\n"
    result = filelist.read(_write(tmp_path / "list.f", text), environ={})
    assert _relative(result.of_kind(Kind.SOURCE), tmp_path) == [
        "rtl/a.sv",
        "rtl/b.sv",
        "gen/x1.v",
        "rtl/sub/d.sv",
        "rtl/sub/deep/e.sv",
        "gen/x22.v",
    ]
    assert sorted(_relative(result.of_kind(Kind.INCLUDE_DIR), tmp_path)) == ["rtl", "rtl/sub", "rtl/sub/deep"]


def _regex_matches(name: str, pattern: str) -> bool:
    """Whether all of ``name`` matches ``pattern`` as a regex, with ``*`` as ``.*`` and ``?`` as ``.``."""
    regex = "".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pattern)
    return re.fullmatch(regex, name, re.DOTALL) is not None


def test_a_pattern_matches_the_names_its_regex_matches():
    """On generated patterns, each against a name filled in from it, then changed at one place half the time."""
    rng = random.Random(0)
    differ = []
    for _ in range(20000):
        pattern = "".join(rng.choices("ab*?./\\\n", k=rng.randint(0, 8)))
        name = "".join(
            rng.choice(["", "a", "b.", "\n/"]) if ch == "*" else rng.choice("ab.\n") if ch == "?" else ch
            for ch in pattern
        )
        if rng.random() < 0.5:
            i = rng.randint(0, len(name))
            name = name[:i] + rng.choice(["", "a", "\n"]) + name[i + 1 :]
        if filelist._matches(name, pattern) != _regex_matches(name, pattern):
            differ.append((pattern, name))
    assert differ == []


def test_a_pattern_of_many_stars_matches_in_time_linear_in_the_name():
    started = time.perf_counter()
    assert not filelist._matches("a" * 1000, "*a" * 30 + "b")
    assert time.perf_counter() - started < 1


def test_a_line_of_many_stars_reads_at_once(tmp_path: Path):
    for name in ("a" * 100 + ".v", "ab" * 15 + ".v"):
        _write(tmp_path / name, "")
    f = _write(tmp_path / "list.f", "*a" * 15 + "*b.v\n")
    started = time.perf_counter()
    result = filelist.read(f, environ={})
    assert time.perf_counter() - started < 1
    assert [e.path.name for e in result.entries] == ["ab" * 15 + ".v"]


@pytest.mark.parametrize(("folder", "sibling"), WILDCARD_FOLDERS)
def test_the_filelist_read_names_is_a_path_not_a_pattern(tmp_path: Path, folder: str, sibling: str):
    """Its folder's name matches no other folder, and the patterns in it match from that folder."""
    for name, define in ((folder, "OWN"), (sibling, "SIBLING")):
        _write(tmp_path / name / "core.v", "")
        _write(tmp_path / name / "list.f", f"+define+{define}\n*.v\n")
    result = filelist.read(tmp_path / folder / "list.f", environ={})
    assert [d.value for d in result.defines] == ["OWN"]
    assert [e.path for e in result.entries] == [tmp_path / folder / "core.v"]


@pytest.mark.parametrize("option", ["-f", "-F"])
@pytest.mark.parametrize(("folder", "sibling"), WILDCARD_FOLDERS)
def test_a_filelist_the_command_line_names_is_a_path_not_a_pattern(
    tmp_path: Path, folder: str, sibling: str, option: str
):
    for name, define in ((folder, "OWN"), (sibling, "SIBLING")):
        _write(tmp_path / name / "list.f", f"+define+{define}\n")
    result = _read_arguments([option, str(Path(folder, "list.f"))], tmp_path)
    assert [d.value for d in result.defines] == ["OWN"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["-F", "ip/"], "{} is not a file"),
        (["*.v"], "{}: no such file"),
        (["-v", "*.v"], "{}: no such file"),
        (["-y", "i*"], "{}: no such folder"),
    ],
    ids=["-F folder/", "source", "-v", "-y"],
)
def test_a_path_the_command_line_names_matches_nothing_else(tmp_path: Path, arguments: list[str], message: str):
    """Not each file a pattern matches, nor, after a trailing slash, every file in a folder, as a filelist's line would."""
    _write(tmp_path / "ip" / "list.f", "+define+FROM_IP\n")
    _write(tmp_path / "a.v", "")
    with pytest.raises(FilelistError) as caught:
        _read_arguments(arguments, tmp_path)
    assert str(caught.value) == "the command line: " + message.format(tmp_path / arguments[-1])


def test_an_include_folder_the_command_line_names_matches_nothing_else(tmp_path: Path):
    (tmp_path / "inc").mkdir()
    result = _read_arguments(["-I", "i*"], tmp_path)
    assert result.of_kind(Kind.INCLUDE_DIR) == []
    assert [(i.text, i.reason) for i in result.ignored] == [("-I i*", "no such folder")]


@pytest.mark.parametrize("option", ["-f", "-F"])
def test_a_comma_splits_a_filelists_line_but_not_a_filelist_the_command_line_names(tmp_path: Path, option: str):
    """On the command line the value is one filelist's path; in a filelist's line it is a list, as slang reads it."""
    for name, define in (("a,b.f", "WHOLE"), ("a", "A"), ("b.f", "B")):
        _write(tmp_path / name, f"+define+{define}\n")
    on_the_command_line = _read_arguments([option, "a,b.f"], tmp_path)
    in_a_filelist = filelist.read(_write(tmp_path / "list.f", f"{option} a,b.f\n"), environ={})
    assert [d.value for d in on_the_command_line.defines] == ["WHOLE"]
    assert [d.value for d in in_a_filelist.defines] == ["A", "B"]


@pytest.mark.parametrize(("option", "folders"), [("-v", False), ("-y", True), ("-I", True)])
def test_a_path_the_command_line_names_keeps_its_commas(tmp_path: Path, option: str, folders: bool):
    for name in ("a,b", "a", "b"):
        if folders:
            (tmp_path / name).mkdir()
        else:
            _write(tmp_path / name, "")
    result = _read_arguments([option, "a,b"], tmp_path)
    assert _relative(result.entries, tmp_path) == ["a,b"]


@pytest.mark.parametrize("where", ["the command line", "a filelist"])
@pytest.mark.parametrize("option", ["-f", "-F", "-v", None], ids=["-f", "-F", "-v", "source"])
def test_a_file_named_with_a_trailing_slash_is_told_to_drop_it(tmp_path: Path, option: str | None, where: str):
    """It is a file, so the slash is what's wrong."""
    ip = _write(tmp_path / "ip.f", "+define+FROM_IP\n")
    arguments = [option, "ip.f/"] if option else ["ip.f/"]
    listing = _write(tmp_path / "list.f", " ".join(arguments) + "\n")
    with pytest.raises(FilelistError) as caught:
        if where == "a filelist":
            filelist.read(listing, environ={})
        else:
            _read_arguments(arguments, tmp_path)
    origin = "the command line" if where == "the command line" else f"{listing}:1"
    assert str(caught.value) == f"{origin}: ip.f/: {ip} is a file; drop the trailing slash"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_file_keeps_the_path_it_is_named_by(tmp_path: Path):
    """An entry's path is the one the filelist names; real is where the link leads."""
    target = _write(tmp_path / "shared" / "common.v", "")
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "common").symlink_to(tmp_path / "shared")
    f = _write(tmp_path / "list.f", "rtl/common/common.v\n")
    entry = filelist.read(f, environ={}).entries[0]
    assert entry.path == tmp_path / "rtl" / "common" / "common.v"
    assert entry.real == target.resolve()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_big_f_through_a_link_rebases_to_where_the_link_leads(tmp_path: Path):
    """Like slang, -F moves to the filelist's real folder, so its ../ goes up from there."""
    _write(tmp_path / "real" / "ip" / "list.f", "../core.v\n")
    _write(tmp_path / "real" / "core.v", "")
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "ip").symlink_to(tmp_path / "real" / "ip")
    result = _read_arguments(["-F", "work/ip/list.f"], tmp_path)
    assert [e.real for e in result.entries] == [(tmp_path / "real" / "core.v").resolve()]
    assert [e.path for e in result.entries] == [(tmp_path / "real" / "core.v").resolve()]


def test_double_dash_makes_the_rest_files(tmp_path: Path):
    _write(tmp_path / "-odd.v", "")
    f = _write(tmp_path / "list.f", "--\n-odd.v\n")
    assert _relative(filelist.read(f, environ={}).entries, tmp_path) == ["-odd.v"]


@pytest.mark.parametrize(
    ("text", "message"), [('""\n', "an empty file name names no file"), ("-\n", "- (standard input)")]
)
def test_an_empty_or_stdin_file_name_stops(tmp_path: Path, text: str, message: str):
    f = _write(tmp_path / "list.f", text)
    with pytest.raises(FilelistError, match=message.replace("(", r"\(").replace(")", r"\)")):
        filelist.read(f, environ={})


def test_arguments_from_the_command_line(tmp_path: Path):
    """read_arguments() takes arguments as the shell passes them, from a working directory, with no expansion."""
    _write(tmp_path / "ip" / "ip.f", "core.v\n")
    _write(tmp_path / "ip" / "core.v", "")
    result = _read_arguments(["--top", "alu", "-F", str(tmp_path / "ip" / "ip.f"), "--blackbox", "rom"], tmp_path)
    assert [t.value for t in result.tops] == ["alu"]
    assert result.tops[0].origin == Origin()
    assert str(result.tops[0].origin) == "the command line"
    assert _relative(result.entries, tmp_path) == ["ip/core.v"]
    assert [b.value for b in result.blackboxes] == ["rom"]


def test_a_backslash_in_a_variables_value_is_kept(tmp_path: Path):
    """A variable's value isn't unescaped, so a Windows path in one survives."""
    folder = tmp_path / ("wa" if os.name == "nt" else "w\\a")
    _write(folder / "a.v", "")
    f = _write(tmp_path / "list.f", "$WA/a.v\n")
    assert filelist.read(f, environ={"WA": str(folder)}).entries[0].path == folder / "a.v"


@pytest.mark.parametrize(
    ("path", "form"),
    [
        (r"\\host\share\x.v", "a network path"),
        ("//host/share/x.v", "a network path"),
        (r"\/host/share", "a network path"),
        ("///host/share", "a network path"),
        (r"\\", "a network path"),
        (r"\\?\C:\x.v", "a Windows device path"),
        ("//?/C:/x.v", "a Windows device path"),
        (r"\\.\pipe\x", "a Windows device path"),
        (r"\\?", "a Windows device path"),
        (r"\??\C:\x.v", "a Windows device path"),
        ("/??/C:/x.v", "a Windows device path"),
        ("C:", "a drive-relative path"),
        ("c:x.v", "a drive-relative path"),
        ("C:..", "a drive-relative path"),
        ("rtl/top.v", None),
        ("/work/top.v", None),
        (r"C:\work\top.v", None),
        ("C:/work/top.v", None),
        (r"\work\top.v", None),
        (r"rtl\\share\x.v", None),
        (r".\\host\share", None),
        (r"\?\x.v", None),
        (r"\??x.v", None),
        ("a.v:1", None),
        ("", None),
    ],
)
def test_never_read_judges_a_path_as_written(path: str, form: str | None):
    why = filelist.never_read(path)
    assert why is None if form is None else why is not None and why.startswith(f"{form}, which is never read: ")


PATH_ROUTES = {
    "source": ("'{}'", "file"),
    "glob": ("'{}*'", "file"),
    "-v": ("-v '{}'", "file"),
    "-v joined": ("'-v{}'", "file"),
    "-y": ("-y '{}'", "folder"),
    "-I": ("-I '{}'", "folder"),
    "-I list": ("-I 'missing,{}'", "folder"),
    "+incdir+": ("'+incdir+{}'", "folder"),
    "-f": ("-f '{}'", "filelist"),
    "-F": ("-F '{}'", "filelist"),
}
"""Each way a filelist names a file or folder: the line, with {} for the path, and what it names."""


@pytest.mark.parametrize("value", NEVER_READ.values(), ids=NEVER_READ.keys())
@pytest.mark.parametrize(("line", "named"), PATH_ROUTES.values(), ids=PATH_ROUTES.keys())
def test_a_path_never_read_refuses_stops_before_anything_looks_it_up(
    tmp_path: Path, no_lookup_of, value: str, line: str, named: str
):
    """On every platform, though only Windows would reach another computer looking it up."""
    f = _write(tmp_path / "list.f", f"// header\n{line.format(value)}\n")
    no_lookup_of(UNREACHABLE_HOST)
    with pytest.raises(FilelistError) as caught:
        filelist.read(f, environ={})
    assert str(caught.value) == f"{f}:2: the {named} named is {filelist.never_read(value)}"


def test_a_path_never_read_refuses_is_judged_as_a_variable_expands_it(tmp_path: Path, no_lookup_of):
    f = _write(tmp_path / "list.f", "$SHARE/x.v\n")
    no_lookup_of(UNREACHABLE_HOST)
    with pytest.raises(FilelistError) as caught:
        filelist.read(f, environ={"SHARE": rf"\\{UNREACHABLE_HOST}\share"})
    assert str(caught.value) == f"{f}:1: the file named is {filelist.never_read(NEVER_READ['network'])}"


@pytest.mark.parametrize("value", NEVER_READ.values(), ids=NEVER_READ.keys())
def test_a_filelist_the_command_line_names_by_a_path_never_read_refuses_stops(tmp_path: Path, no_lookup_of, value: str):
    no_lookup_of(UNREACHABLE_HOST)
    with pytest.raises(FilelistError) as caught:
        _read_arguments(["-f", value], tmp_path)
    assert str(caught.value) == f"the command line: the filelist named is {filelist.never_read(value)}"


@pytest.mark.skipif(os.name == "nt", reason="the name is a drive-relative path on Windows")
def test_the_filelist_the_caller_reads_is_not_judged(tmp_path: Path, monkeypatch):
    """Its path is the caller's, as --project-dir names it, not one a filelist names."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "C:block" / "a.v", "")
    _write(tmp_path / "C:block" / "list.f", "a.v\n")
    assert [e.path for e in filelist.read(Path("C:block/list.f"), environ={}).entries] == [tmp_path / "C:block" / "a.v"]


@pytest.mark.skipif(os.name != "nt", reason="only Windows takes the path for another computer's")
@pytest.mark.parametrize(
    "value", [NEVER_READ["network"], NEVER_READ["network, slashes"]], ids=["backslashes", "slashes"]
)
def test_on_windows_a_network_path_is_refused_without_reaching_the_computer(tmp_path: Path, no_lookup_of, value: str):
    """Joined to the filelist's folder it is still the share, which Windows would connect to; the address reaches no
    computer, conftest refuses resolving it, and no lookup of the path happens."""
    share = os.path.splitdrive(value)[0]
    assert share and os.path.splitdrive(os.path.join(tmp_path, value))[0] == share != tmp_path.drive
    f = _write(tmp_path / "list.f", f"'{value}'\n")
    no_lookup_of(UNREACHABLE_HOST)
    with pytest.raises(FilelistError) as caught:
        filelist.read(f, environ={})
    assert str(caught.value) == f"{f}:1: the file named is {filelist.never_read(value)}"
