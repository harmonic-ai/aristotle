"""Tests for selected CLI web origins."""

from unittest.mock import patch

import pytest

from aristotlelib.cli.controller import main
from aristotlelib.cli.version import get_version


def test_help_uses_selected_web_origin(monkeypatch, capsys):
    configured_url = "https://alt.example.test"
    monkeypatch.setenv("ARISTOTLE_WEB_URL", f"{configured_url}/")

    with patch("sys.argv", ["aristotle"]):
        assert main() == 0

    bare = capsys.readouterr().out
    assert f"For more information, visit {configured_url}" in bare
    assert "https://aristotle.harmonic.fun" not in bare

    with patch("sys.argv", ["aristotle", "--help"]), pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == bare


def test_version_does_not_require_a_valid_web_origin(monkeypatch, capsys):
    monkeypatch.setenv("ARISTOTLE_WEB_URL", "http://alt.example.test")

    with patch("sys.argv", ["aristotle", "--version"]):
        assert main() == 0

    output = capsys.readouterr()
    assert output.out == f"aristotlelib {get_version()}\n"
    assert output.err == ""


@pytest.mark.parametrize(
    "argv",
    [
        ["aristotle"],
        ["aristotle", "--help"],
        ["aristotle", "login", "--no-browser"],
        ["aristotle", "list", "--api-key", "secret"],
    ],
)
def test_invalid_web_origin_is_a_clean_cli_error(monkeypatch, capsys, argv):
    monkeypatch.setenv("ARISTOTLE_WEB_URL", "http://alt.example.test")

    with patch("sys.argv", argv):
        assert main() == 2

    error = capsys.readouterr().err
    assert "ARISTOTLE_WEB_URL must use HTTPS unless its host is loopback" in error
    assert "secret" not in error
    assert "Traceback" not in error
