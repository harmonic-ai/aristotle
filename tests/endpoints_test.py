"""Tests for web origin selection."""

import pytest

from aristotlelib.endpoints import WEB_URL, get_web_url
from aristotlelib.project import ARISTOTLE_WEB_BASE_URL


@pytest.mark.parametrize("configured", [None, "", " \t "])
def test_unset_or_blank_origin_uses_default(monkeypatch, configured):
    if configured is None:
        monkeypatch.delenv("ARISTOTLE_WEB_URL", raising=False)
    else:
        monkeypatch.setenv("ARISTOTLE_WEB_URL", configured)

    assert get_web_url() == WEB_URL


def test_legacy_constant_does_not_follow_selected_origin(monkeypatch):
    monkeypatch.setenv("ARISTOTLE_WEB_URL", "https://alt.example.test")

    assert ARISTOTLE_WEB_BASE_URL == WEB_URL
    assert get_web_url() == "https://alt.example.test"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (" HTTPS://ALT.EXAMPLE.TEST:443/ ", "https://alt.example.test"),
        ("https://alt.example.test:8443/", "https://alt.example.test:8443"),
        ("https://ARISTOTLE.HARMONIC.FUN:443/", WEB_URL),
        ("http://LOCALHOST:80/", "http://localhost"),
        ("http://127.0.0.2:8000/", "http://127.0.0.2:8000"),
        ("http://[0:0:0:0:0:0:0:1]:8000/", "http://[::1]:8000"),
    ],
)
def test_origin_is_canonical(monkeypatch, configured, expected):
    monkeypatch.setenv("ARISTOTLE_WEB_URL", configured)

    assert get_web_url() == expected


@pytest.mark.parametrize(
    "configured",
    ["http://alt.example.test", "http://other.example.test", "http://0.0.0.0:8000"],
)
def test_http_requires_loopback(monkeypatch, configured):
    monkeypatch.setenv("ARISTOTLE_WEB_URL", configured)

    with pytest.raises(ValueError, match="must use HTTPS unless its host is loopback"):
        get_web_url()


@pytest.mark.parametrize(
    "configured",
    [
        "alt.example.test",
        "ftp://alt.example.test",
        "https://user:pass@alt.example.test",
        "https://alt.example.test/api/v3",
        "https://alt.example.test?next=other",
        "https://alt.example.test#other",
        "https://alt.example.test:bad",
        "https://alt.example.test:99999",
        "https://alt.example.test\n.evil.example.test",
    ],
)
def test_origin_rejects_unsafe_or_ambiguous_urls(monkeypatch, configured):
    monkeypatch.setenv("ARISTOTLE_WEB_URL", configured)

    with pytest.raises(ValueError, match="ARISTOTLE_WEB_URL must be an HTTP\\(S\\) origin"):
        get_web_url()
