"""Exercise the real CLI and request client, with no outside network or writes."""

import json
from unittest.mock import Mock

import httpx
import pytest

from aristotlelib import api_request
from aristotlelib.billing import BalanceResult, CreditDisplayConfig, format_credits
from aristotlelib.cli.command.balance import BalanceAction
from aristotlelib.cli.controller import main

BALANCE = {
    "credits": 51_234_567,
    "paid_credits": 1_234_567,
    "free_credits": 50_000_000,
    "granted_credits": 60_000_000,
    "observed_at": "2026-10-28T03:00:00Z",
    "next_expiry_at": "2027-10-28T03:00:00Z",
    "credit_display_config": {"small_credits_per_credit": 1_000_000, "decimal_places": 2},
}


def invoke(monkeypatch, *arguments):
    monkeypatch.setattr("sys.argv", ["aristotle", "balance", *arguments])
    return main()


def assert_reads(requests, *paths):
    assert [(r.method, r.url.path) for r in requests] == [("GET", f"/api/v3/{p}") for p in paths]
    assert all(r.headers["X-API-Key"] == "test-key" and not r.content and not r.url.query for r in requests)


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize(
    "divisor,places,amounts",
    [
        (1_000_000, 2, ("51.23", "50.00", "1.23", "60.00")),
        (10_000, 0, ("5,123", "5,000", "123", "6,000")),
        (1_000, 1, ("51,234.5", "50,000.0", "1,234.5", "60,000.0")),
        (10_000, 2, ("5,123.45", "5,000.00", "123.45", "6,000.00")),
        (10_000, 3, ("5,123.456", "5,000.000", "123.456", "6,000.000")),
    ],
)
def test_admin_balance(api_server, monkeypatch, capsys, json_output, divisor, places, amounts):
    config = {"small_credits_per_credit": divisor, "decimal_places": places}
    requests = api_server(
        httpx.Response(200, json={**BALANCE, "credit_display_config": {**config, "unit_label": "future field"}})
    )
    assert invoke(monkeypatch, *(["--json"] if json_output else [])) == 0
    assert_reads(requests, "billing/balance")
    captured = capsys.readouterr()
    if json_output:
        payload = json.loads(captured.out)
        result = BalanceResult.model_validate(payload)
        assert result.status == "available"
        assert result.balance.credits == 51_234_567
        assert result.balance.granted_credits == 60_000_000
        assert result.unit == "small_credits"
        assert payload["small_credits_per_credit"] == divisor
        assert payload["decimal_places"] == places
        assert result.balance.credit_display_config.model_dump() == config
        for field in ("credits", "paid_credits", "free_credits", "granted_credits"):
            assert payload["balance"][field] == BALANCE[field]
        assert result.credit_status is None and result.error is None
    else:
        available, free, paid, granted = amounts
        assert f"Available balance: {available} Credits" in captured.out
        assert f"Free: {free} Credits" in captured.out
        assert f"Purchased: {paid} Credits" in captured.out
        assert f"Granted (active grants): {granted} Credits" in captured.out
        assert f"Credit unit: 1 Credit = {divisor:,} small credits" in captured.out
        assert "Observed at: 2026-10-28T03:00:00+00:00" in captured.out
        assert "Next expiry: 2027-10-28T03:00:00+00:00" in captured.out
    assert not captured.err


@pytest.mark.parametrize(
    "divisor,places,amount,expected",
    [
        (1_000_000, 2, 0, "0.00"),
        (1_000_000, 2, 1, "<0.01"),
        (1_000_000, 2, 9_999, "<0.01"),
        (1_000_000, 2, 10_000, "0.01"),
        (1_000_000, 2, 999_999, "0.99"),
        (1_000_000, 2, 1_234_567_890, "1,234.56"),
        (10_000, 0, 0, "0"),
        (10_000, 0, 9_999, "<1"),
        (10_000, 0, 10_000, "1"),
        (1_000, 1, 0, "0.0"),
        (1_000, 1, 99, "<0.1"),
        (1_000, 1, 100, "0.1"),
        (10_000, 2, 10**30 + 9_999, "100,000,000,000,000,000,000,000,000.99"),
        (10_000, 3, 1, "<0.001"),
        (10_000, 3, 10, "0.001"),
        (101, 3, 102, "1.009"),
        (9_007_199_254_740_992, 0, 9_007_199_254_740_993, "1"),
    ],
)
def test_credit_units(divisor, places, amount, expected):
    config = CreditDisplayConfig(small_credits_per_credit=divisor, decimal_places=places)
    assert format_credits(amount, config) == expected


@pytest.mark.parametrize(
    "state,code", [("available", 0), ("low", 0), ("out_of_credits", 0), ("disabled", 0), ("unavailable", 3)]
)
@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("status", [403, 404, 409])
def test_credit_status_fallback(api_server, monkeypatch, capsys, caplog, state, code, json_output, status):
    requests = api_server(
        httpx.Response(status, json={"detail": "Exact balance is unavailable"}),
        httpx.Response(200, json={"state": state, "credits": 987654321}),
    )
    assert invoke(monkeypatch, *(["--json"] if json_output else [])) == code
    captured = capsys.readouterr()
    assert "987654321" not in captured.out + captured.err
    if json_output:
        result = json.loads(captured.out)
        assert result["status"] == ("restricted" if status == 403 else "unavailable")
        assert result["balance"] is None
        assert result["credit_status"] == state
        assert result["small_credits_per_credit"] is None and result["decimal_places"] is None
        assert result["error"] == (
            "Exact credit balance is restricted to organization admins." if status == 403 else None
        )
    elif code == 0:
        assert ("Exact credit balance is restricted to organization admins." in captured.out) == (status == 403)
        assert "Available balance:" not in captured.out
        assert "Credit unit:" not in captured.out
        if state == "disabled":
            assert "Credit consumption is not billed for this organization." in captured.out
            assert "Billing is disabled" not in captured.out
    else:
        assert not captured.out
    if code:
        assert "Credit status is unavailable" in caplog.text
    assert_reads(requests, "billing/balance", "billing/credit-status")


@pytest.mark.parametrize("status,code", [(503, 3), (429, 6)])
@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.usefixtures("frozen_api_clock")
def test_unavailable_balance(api_server, monkeypatch, capsys, caplog, status, code, json_output):
    response = httpx.Response(status, json={"detail": "Billing balance is unavailable"})
    requests = api_server(*([response] * api_request.RETRY_ATTEMPTS))
    assert invoke(monkeypatch, *(["--json"] if json_output else [])) == code
    captured = capsys.readouterr()
    if json_output:
        result = json.loads(captured.out)
        assert result["status"] == "unavailable" and result["balance"] is None
        assert result["small_credits_per_credit"] is None and result["decimal_places"] is None
        assert "Credit balance unavailable" in result["error"]
    else:
        assert not captured.out
    assert "Credit balance unavailable" in caplog.text
    assert_reads(requests, *(["billing/balance"] * len(requests)))


@pytest.mark.parametrize("status,code", [(403, 4), (404, 4), (409, 3)])
@pytest.mark.parametrize("content_type", ["text/html; charset=utf-8", "text/plain"])
def test_proxy_refusal_does_not_claim_admin_restriction(
    api_server, monkeypatch, capsys, caplog, status, code, content_type
):
    requests = api_server(
        httpx.Response(status, text="<title>Denied by proxy</title>", headers={"content-type": content_type})
    )
    assert invoke(monkeypatch, "--json") == code
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["status"] == "unavailable"
    assert "proxy" in result["error"]
    assert "organization admins" not in result["error"]
    assert "proxy" in caplog.text
    assert_reads(requests, "billing/balance")


@pytest.mark.parametrize(
    "field,value",
    [
        ("credits", None),
        ("credits", True),
        ("credits", -1),
        ("credits", 1.5),
        ("credits", "5000000"),
        ("observed_at", "invalid"),
    ],
)
def test_invalid_balance_is_unavailable(api_server, monkeypatch, capsys, field, value):
    requests = api_server(httpx.Response(200, json={**BALANCE, field: value}))
    assert invoke(monkeypatch, "--json") == 3
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "unavailable" and result["balance"] is None
    assert "invalid credit balance response" in result["error"]
    assert_reads(requests, "billing/balance")


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"small_credits_per_credit": 10_000},
        {"decimal_places": 2},
        *({"small_credits_per_credit": divisor, "decimal_places": 2} for divisor in [True, "10000", 10000.0, 0, -1]),
        *({"small_credits_per_credit": 10_000, "decimal_places": places} for places in [True, "2", 2.0, -1]),
    ],
)
def test_invalid_display_config_is_unavailable(api_server, monkeypatch, capsys, caplog, config):
    requests = api_server(httpx.Response(200, json={**BALANCE, "credit_display_config": config}))
    assert invoke(monkeypatch, "--json") == 3
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "unavailable" and result["balance"] is None
    assert result["small_credits_per_credit"] is None and result["decimal_places"] is None
    assert "invalid credit balance response" in result["error"]
    assert "invalid credit balance response" in caplog.text
    assert_reads(requests, "billing/balance")


def test_missing_display_config_is_unavailable(api_server, monkeypatch, capsys):
    response = {key: value for key, value in BALANCE.items() if key != "credit_display_config"}
    requests = api_server(httpx.Response(200, json=response))
    assert invoke(monkeypatch, "--json") == 3
    result = json.loads(capsys.readouterr().out)
    assert result["balance"] is None
    assert result["small_credits_per_credit"] is None and result["decimal_places"] is None
    assert_reads(requests, "billing/balance")


@pytest.mark.parametrize(
    "outcome,code",
    [
        (httpx.Response(200, json={"state": "future-state"}), 3),
        (httpx.Response(403, json={"detail": "Account not approved"}), 4),
        (httpx.Response(401, json={"detail": "Invalid key"}), 5),
    ],
)
@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("status", [403, 404, 409])
def test_failed_credit_status(api_server, monkeypatch, capsys, caplog, outcome, code, json_output, status):
    requests = api_server(httpx.Response(status, json={"detail": "Exact balance is unavailable"}), outcome)
    assert invoke(monkeypatch, *(["--json"] if json_output else [])) == code
    captured = capsys.readouterr()
    if json_output and code != 5:
        result = json.loads(captured.out)
        assert result["status"] == "unavailable" and result["balance"] is None
        assert result["credit_status"] is None
        assert "Credit balance unavailable" in result["error"]
    else:
        assert not captured.out
    assert "restricted to organization admins" not in captured.out + captured.err + caplog.text
    assert caplog.records
    assert_reads(requests, "billing/balance", "billing/credit-status")


@pytest.mark.parametrize("json_output", [False, True])
def test_missing_key_never_offers_login(monkeypatch, tmp_path, caplog, capsys, json_output):
    monkeypatch.setattr(api_request, "API_KEY", None)
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    login = Mock(side_effect=AssertionError("A read must not start a login"))
    monkeypatch.setattr(BalanceAction, "_offer_login", login)
    assert invoke(monkeypatch, *(["--json"] if json_output else [])) == 5
    assert not capsys.readouterr().out
    assert "aristotle login" in caplog.text
    login.assert_not_called()


@pytest.mark.parametrize("json_output", [False, True])
def test_rejected_key_leaves_stdout_empty(api_server, monkeypatch, capsys, caplog, json_output):
    requests = api_server(httpx.Response(401, json={"detail": "Invalid key"}))
    assert invoke(monkeypatch, *(["--json"] if json_output else [])) == 5
    assert not capsys.readouterr().out
    assert caplog.records
    assert_reads(requests, "billing/balance")


def test_balance_is_discoverable_without_key_or_network(api_server, monkeypatch, capsys):
    requests = api_server()
    monkeypatch.setattr(api_request, "API_KEY", None)
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["aristotle", "--help"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert "balance" in capsys.readouterr().out
    assert not requests
