"""Tests for aristotlelib.printable."""

import io
import logging
import time

import pytest

from aristotlelib.printable import PrintableFormatter, escape_controls, escape_logged_text, print_line, printable


@pytest.fixture
def off_github(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture
def on_github(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")


def test_control_characters_are_escaped_but_tab_and_newline(off_github):
    text = "a\x1b[2Jb\x00c\x07d\x7fe\x85f\x9bg\u2028h\u2029i\tj\nk"

    assert printable(text) == "a\\x1b[2Jb\\x00c\\x07d\\x7fe\\x85f\\x9bg\\u2028h\\u2029i\tj\nk"


def test_a_carriage_return_is_escaped_unless_a_newline_follows_it(off_github):
    assert printable("one\r\ntwo\rthree") == "one\ntwo\\rthree"


def test_off_github_a_line_may_start_with_two_colons(off_github):
    assert printable("::error::x\n  ::notice::y\n##[group]z") == "::error::x\n  ::notice::y\n##[group]z"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("::error::x", "\\::error::x"),
        ("one\n::stop-commands::t", "one\n\\::stop-commands::t"),
        ("  ::warning::x", "  \\::warning::x"),
        ("\t::warning::x", "\t\\::warning::x"),
        ("\u3000::warning::x", "\u3000\\::warning::x"),
        ("one\r::error::x", "one\\r::error::x"),
        ("one\r\n::error::x", "one\n\\::error::x"),
        ("\x0b::error::x", "\\x0b::error::x"),
        ("é::error::x", "é\\::error::x"),
        ("a ::error::x", "a ::error::x"),
        ("see ##[set-output name=verdict]pass", "see ##[\\set-output name=verdict]pass"),
        ("##[ERROR]x", "##[\\ERROR]x"),
        ("##[ſet-output]x", "##[\\ſet-output]x"),
        ("a ##[1:3] b ##[0:$] c ##[*] d ##[+] e ##[2: 5]", "a ##[1:3] b ##[0:$] c ##[*] d ##[+] e ##[2: 5]"),
        ("##[1:LATENCY] ##[MIN:MAX] ##[0:DEPTH-1] ##[ 1 : 3 ]", "##[1:LATENCY] ##[MIN:MAX] ##[0:DEPTH-1] ##[ 1 : 3 ]"),
        ("##[1234567890 :1]", "##[\\1234567890 :1]"),
        ("##[LATENCY : 1]", "##[\\LATENCY : 1]"),
        ("##[7f3a9c21-4d6e-4b8a-9c3f-2e1d0a5b6c7d]", "##[\\7f3a9c21-4d6e-4b8a-9c3f-2e1d0a5b6c7d]"),
        ("##[1234567890]", "##[\\1234567890]"),
        ("##[$TOKEN]", "##[\\$TOKEN]"),
    ],
)
def test_on_github_no_line_is_a_workflow_command(on_github, text: str, expected: str):
    assert printable(text) == expected


def test_a_long_line_of_bracketed_commands_is_escaped_in_linear_time(on_github):
    started = time.monotonic()

    escaped = printable("##[" * 50_000)

    assert escaped == "##[\\" * 50_000
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    ("prefix", "printed", "read"),
    [("Â…", "cp1252", "utf-8"), ("Â\xa0", "cp1252", "utf-8"), ("ã€€", "cp1252", "utf-8"), ("ÿ", "cp1252", "cp437")],
    ids=["NEL", "NBSP", "U+3000", "cp437 NBSP"],
)
def test_on_github_text_that_decodes_to_whitespace_cannot_start_a_workflow_command(
    on_github, prefix: str, printed: str, read: str
):
    received = printable(f"k\n{prefix}::error::forged").encode(printed).decode(read)

    # The runner ends lines only at a carriage return or a newline; .NET's TrimStart() strips no more than lstrip().
    assert received.split("\n")[1].lstrip().startswith("\\::error::forged")


def test_azure_and_teamcity_commands_are_broken_everywhere(off_github):
    assert printable("##vso[task.complete result=Succeeded] ##teamcity[buildStatus text='ok']") == (
        "##vso\u200b[task.complete result=Succeeded] ##teamcity\u200b[buildStatus text='ok']"
    )


def test_print_line_prints_the_text_printable(on_github, capsys):
    print_line("k\n::error::forged\x1b[31m")

    assert capsys.readouterr().out == "k\n\\::error::forged\\x1b[31m\n"


def test_the_formatter_makes_the_text_of_another_formatter_printable(on_github):
    record = logging.LogRecord("aristotle", logging.ERROR, __file__, 1, "k\n::error::forged\x1b[31m", None, None)

    formatted = PrintableFormatter(logging.Formatter("%(levelname)s - %(message)s")).format(record)

    assert formatted == "ERROR - k\n\\::error::forged\\x1b[31m"


def test_logged_text_is_escaped_once_however_often_escaping_is_turned_on(on_github):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logging.root.addHandler(handler)
    try:
        escape_logged_text()
        escape_logged_text()
        logging.getLogger("aristotle").warning("##[group]x")
    finally:
        logging.root.removeHandler(handler)

    assert stream.getvalue() == "##[\\group]x\n"


def test_a_byte_that_is_not_utf8_prints_as_its_hex_and_another_lone_surrogate_as_its_code_point(off_github):
    assert escape_controls("caf\udce9.v") == "caf\\xe9.v"
    assert escape_controls("x\ud800y") == "x\\ud800y"
