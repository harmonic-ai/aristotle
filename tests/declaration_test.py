"""Tests for aristotlelib.declaration."""

import html
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pydantic
import pytest

from aristotlelib.api_request import AristotleAPIError, set_api_key
from aristotlelib.declaration import (
    Declaration,
    DeclarationEvidence,
    DeclarationFailureReason,
    DeclarationSource,
    DeclarationStatus,
    DeclarationType,
    decode_html_entities,
)
from aristotlelib.results import PropertyCounts

PROJECT_ID = "b2396058-7c1e-4d7a-9f3b-2a6c8e1d4f50"

# Shaped like a real /project/{id}/declarations response.
SAMPLE_DECLARATION = {
    "project_id": "b2396058",
    "declaration_id": "pcpi-div-ready-single-cycle",
    "declaration_type": 1,
    "source": 1,
    "name": "Spec.PcpiDivReadyIsSingleCycle",
    "status": 3,
    "filename": "Spec/Property.lean",
    "lean_code": "def PcpiDivReadyIsSingleCycle : Prop := ...",
    "description": "pcpi_ready is never asserted on two consecutive cycles.",
    "created_at": "2026-08-06T18:13:38.371433",
    "last_updated": "2026-08-06T19:07:05.079910",
}


def _mock_client(*pages: dict) -> Mock:
    responses = []
    for page in pages:
        response = Mock()
        response.json.return_value = page
        responses.append(response)

    client = AsyncMock()
    client.get = AsyncMock(side_effect=responses)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


async def test_list_for_project_parses_a_response():
    client = _mock_client({"declarations": [SAMPLE_DECLARATION], "pagination_key": None})

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        declarations, pagination_key = await Declaration.list_for_project(PROJECT_ID)

    assert pagination_key is None
    assert len(declarations) == 1
    declaration = declarations[0]
    assert declaration.name == "Spec.PcpiDivReadyIsSingleCycle"
    assert declaration.declaration_type == DeclarationType.PROPERTY
    assert declaration.source == DeclarationSource.AGENT
    assert declaration.status == DeclarationStatus.PROVED


@pytest.mark.parametrize(
    ("given", "read"),
    [
        ({}, []),
        ({"artifact_paths": None}, []),
        ({"artifact_paths": ["sim/cex.vcd", "sim/cex_tb.sv"]}, ["sim/cex.vcd", "sim/cex_tb.sv"]),
    ],
    ids=["absent", "null", "listed"],
)
def test_artifact_paths_are_read_and_an_absent_or_null_list_is_empty(given: dict, read: list[str]):
    declaration = Declaration.model_validate({**SAMPLE_DECLARATION, **given})

    assert declaration.artifact_paths == read
    assert declaration.model_dump(mode="json")["artifact_paths"] == read


@pytest.mark.parametrize(
    ("fields", "evidence"),
    [
        ({"status": 2, "evidence": 1}, DeclarationEvidence.COUNTEREXAMPLE),
        ({"status": 2, "evidence": 2}, DeclarationEvidence.NEGATION_PROVED),
        ({"status": 2, "evidence": 3}, DeclarationEvidence.BOTH),
        ({"status": 6, "failure_reason": 3}, None),
    ],
    ids=["cex", "negation", "both", "failed"],
)
def test_refutation_responses_keep_the_evidence_kind(fields, evidence):
    message = "Refuted by a counterexample: x < y & y > 0. See the trace in Artifacts."
    declaration = Declaration.model_validate({**SAMPLE_DECLARATION, **fields, "evidence_message": message})

    assert declaration.evidence is evidence
    payload = declaration.model_dump(mode="json")
    assert payload["status"] == fields["status"]
    assert payload["evidence_message"] == message
    assert payload["evidence"] == evidence
    if fields["status"] == 2:
        assert payload["status_name"] == "REFUTED"
    elif fields["status"] == 6:
        assert declaration.status is DeclarationStatus.FAILED


@pytest.mark.parametrize(
    "fields", [{}, {"evidence": None}, {"evidence": 0}, {"evidence": 99}, {"evidence": DeclarationEvidence.UNKNOWN}]
)
def test_refuted_declarations_require_a_known_evidence_kind(fields):
    with pytest.raises(pydantic.ValidationError):
        Declaration.model_validate({**SAMPLE_DECLARATION, "status": 2, **fields})


@pytest.mark.parametrize("evidence", [0, 4, DeclarationEvidence.UNKNOWN])
def test_a_declaration_made_in_python_refuses_an_unknown_evidence_kind_whatever_its_status(evidence):
    with pytest.raises(pydantic.ValidationError, match="Evidence must be COUNTEREXAMPLE, NEGATION_PROVED or BOTH"):
        Declaration.model_validate({**SAMPLE_DECLARATION, "evidence": evidence})


@pytest.mark.parametrize(
    ("fields", "evidence"),
    [
        ({"status": 2}, DeclarationEvidence.NEGATION_PROVED),
        ({"status": 5}, DeclarationEvidence.COUNTEREXAMPLE),
        ({"status": 2, "evidence": None, "evidence_message": None}, DeclarationEvidence.NEGATION_PROVED),
        ({"status": 5, "evidence": None, "evidence_message": None}, DeclarationEvidence.COUNTEREXAMPLE),
        ({"status": 2, "evidence": 1, "evidence_message": "CEX at `x = 1`."}, DeclarationEvidence.COUNTEREXAMPLE),
        ({"status": 2, "evidence": 2, "evidence_message": "Negation proved."}, DeclarationEvidence.NEGATION_PROVED),
        ({"status": 2, "evidence": 3, "evidence_message": "Proof and CEX."}, DeclarationEvidence.BOTH),
        ({"status": 5, "evidence": 3, "evidence_message": "Proof and CEX."}, DeclarationEvidence.BOTH),
    ],
)
async def test_api_refutations_normalize_without_changing_the_response(fields, evidence):
    raw = {**SAMPLE_DECLARATION, **fields}
    original = raw.copy()
    client = _mock_client({"declarations": [raw], "pagination_key": None})

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        declarations, _ = await Declaration.list_for_project(PROJECT_ID)

    declaration = declarations[0]
    assert declaration.status is DeclarationStatus.REFUTED
    assert declaration.evidence is evidence
    assert declaration.evidence_message == raw.get("evidence_message")
    assert declaration.model_dump(mode="json")["status"] == 2
    assert raw == original


@pytest.mark.parametrize("status", [2, 5])
@pytest.mark.parametrize("evidence", [0, 4, 99])
async def test_api_refutations_read_an_unknown_evidence_kind_as_unknown_instead_of_inferring_one(status, evidence):
    raw = {**SAMPLE_DECLARATION, "status": status, "evidence": evidence}
    client = _mock_client({"declarations": [raw], "pagination_key": None})

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        [declaration], _ = await Declaration.list_for_project(PROJECT_ID)

    assert declaration.status is DeclarationStatus.REFUTED
    assert declaration.evidence is DeclarationEvidence.UNKNOWN
    assert declaration.model_dump(mode="json")["evidence"] == 0


async def test_mixed_api_pages_preserve_every_refutation_in_counts():
    fields = [{"status": 5}, {"status": 2}]
    fields += [{"status": 2, "evidence": evidence} for evidence in DeclarationEvidence]
    fields += [{"status": 3}, {"status": 6}]
    rows = [{**SAMPLE_DECLARATION, "declaration_id": str(i), **values} for i, values in enumerate(fields)]
    client = _mock_client(
        {"declarations": rows[:2], "pagination_key": "next"},
        {"declarations": rows[2:], "pagination_key": None},
    )

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.declaration.asyncio.sleep", AsyncMock()),
    ):
        declarations = await Declaration.list_all_for_project(PROJECT_ID)

    assert [d.declaration_id for d in declarations] == [str(i) for i in range(8)]
    assert PropertyCounts.from_declarations(declarations, []) == PropertyCounts(proved=1, refuted=6, unresolved=1)
    assert [d.evidence for d in declarations[:6]] == [
        DeclarationEvidence.COUNTEREXAMPLE,
        DeclarationEvidence.NEGATION_PROVED,
        *DeclarationEvidence,
    ]


async def test_list_for_project_requests_the_right_endpoint():
    client = _mock_client({"declarations": [], "pagination_key": None})

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        await Declaration.list_for_project(PROJECT_ID, declaration_type=DeclarationType.PROPERTY, limit=50)

    endpoint, kwargs = client.get.call_args[0][0], client.get.call_args[1]
    assert endpoint == f"/project/{PROJECT_ID}/declarations"
    assert kwargs["params"]["limit"] == 50
    assert kwargs["params"]["declaration_type"] == DeclarationType.PROPERTY.value


@pytest.mark.parametrize("listing", [Declaration.list_for_project, Declaration.list_all_for_project])
@pytest.mark.parametrize(
    "project_id",
    ["", "..", "b2396058", f"junk/../{PROJECT_ID}", f"{PROJECT_ID}?x=1", f"{PROJECT_ID}#x", f"{PROJECT_ID}/"],
)
async def test_list_for_project_rejects_an_id_that_is_not_a_uuid_without_a_request(project_id, listing):
    client = _mock_client()

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        with pytest.raises(AristotleAPIError) as raised:
            await listing(project_id)

    assert raised.value.status_code == 404
    client.get.assert_not_called()


async def test_first_page_sends_no_pagination_key_on_the_wire():
    # An unset pagination_key must not reach the query string; the server rejects `pagination_key=`.
    set_api_key("test-key")
    response = Mock(spec=httpx.Response)
    response.status_code = 200
    response.headers = httpx.Headers({"Content-Type": "application/json"})
    response.json.return_value = {"declarations": [], "pagination_key": None}
    http = AsyncMock(spec=httpx.AsyncClient)
    http.request = AsyncMock(return_value=response)

    with patch("httpx.AsyncClient", return_value=http):
        await Declaration.list_for_project(PROJECT_ID, declaration_type=DeclarationType.PROPERTY)

    assert http.request.call_args.kwargs["params"] == {"limit": 30, "declaration_type": 1}


async def test_list_all_for_project_follows_pagination():
    page_one = {"declarations": [SAMPLE_DECLARATION], "pagination_key": "2026-08-06T18:13:38"}
    page_two = {"declarations": [{**SAMPLE_DECLARATION, "declaration_id": "second"}], "pagination_key": None}
    client = _mock_client(page_one, page_two)

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.declaration.asyncio.sleep", AsyncMock()),
    ):
        declarations = await Declaration.list_all_for_project(PROJECT_ID)

    assert [d.declaration_id for d in declarations] == ["pcpi-div-ready-single-cycle", "second"]
    assert client.get.await_count == 2
    second_call_params = client.get.call_args_list[1][1]["params"]
    assert second_call_params["pagination_key"] == "2026-08-06T18:13:38"


async def test_list_all_for_project_reads_every_page_over_one_connection():
    page = {"declarations": [SAMPLE_DECLARATION], "pagination_key": "next"}
    client = _mock_client(page, page, {**page, "pagination_key": None})

    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client) as connect,
        patch("aristotlelib.declaration.asyncio.sleep", AsyncMock()),
    ):
        declarations = await Declaration.list_all_for_project(PROJECT_ID)

    assert len(declarations) == 3
    connect.assert_called_once_with()


@pytest.mark.parametrize("request_seconds", [0.0, 0.3, 0.8])
async def test_list_all_for_project_spaces_its_pages_out(request_seconds):
    now = [0.0]
    started: list[float] = []
    page = {"declarations": [SAMPLE_DECLARATION], "pagination_key": "next"}
    client = _mock_client(page, page, {**page, "pagination_key": None})
    serve = client.get

    async def get(*args, **kwargs):
        started.append(now[0])
        now[0] += request_seconds
        return await serve(*args, **kwargs)

    async def sleep(seconds):
        now[0] += seconds

    client.get = get
    with (
        patch("aristotlelib.api_request.AristotleRequestClient", return_value=client),
        patch("aristotlelib.declaration.time", Mock(monotonic=lambda: now[0])),
        patch("aristotlelib.declaration.asyncio.sleep", sleep),
    ):
        await Declaration.list_all_for_project(PROJECT_ID)

    # At most two pages a second, a fifth of the per-second request limit.
    gap = max(0.5, request_seconds)
    assert started == pytest.approx([0, gap, 2 * gap])


async def test_list_for_project_rejects_an_out_of_range_limit():
    with pytest.raises(ValueError, match="between 1 and 100"):
        await Declaration.list_for_project(PROJECT_ID, limit=101)


async def test_an_unrecognized_status_degrades_to_unknown():
    # A status this client predates parses as UNKNOWN rather than failing validation.
    client = _mock_client({"declarations": [{**SAMPLE_DECLARATION, "status": 99}], "pagination_key": None})

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        declarations, _ = await Declaration.list_for_project(PROJECT_ID)

    assert declarations[0].status == DeclarationStatus.UNKNOWN


async def test_optional_fields_may_be_absent():
    minimal = {k: v for k, v in SAMPLE_DECLARATION.items() if k not in {"lean_code", "description"}}
    client = _mock_client({"declarations": [minimal], "pagination_key": None})

    with patch("aristotlelib.api_request.AristotleRequestClient", return_value=client):
        declarations, _ = await Declaration.list_for_project(PROJECT_ID)

    assert declarations[0].lean_code is None
    assert declarations[0].description is None
    assert declarations[0].failure_reason is None
    assert declarations[0].source_file is None


def test_a_failed_declaration_carries_its_reason():
    declaration = Declaration.model_validate({**SAMPLE_DECLARATION, "status": 6, "failure_reason": 1})

    assert declaration.status == DeclarationStatus.FAILED
    assert declaration.failure_reason == DeclarationFailureReason.OUT_OF_BUDGET


def test_a_proof_that_no_longer_checks_is_its_own_failure_reason():
    declaration = Declaration.model_validate({**SAMPLE_DECLARATION, "status": 6, "failure_reason": 3})

    assert declaration.failure_reason == DeclarationFailureReason.PROOF_CHECK_FAILED


@pytest.mark.parametrize(("code", "name"), [(4, "INCOMPLETE"), (5, "CANCELED")])
def test_a_run_left_incomplete_or_cancelled_is_its_own_failure_reason(code: int, name: str):
    declaration = Declaration.model_validate({**SAMPLE_DECLARATION, "status": 6, "failure_reason": code})

    assert declaration.failure_reason_name == name
    assert declaration.model_dump(mode="json")["failure_reason"] == code


def test_an_unrecognized_failure_reason_degrades_to_unknown():
    declaration = Declaration.model_validate({**SAMPLE_DECLARATION, "status": 6, "failure_reason": 42})

    assert declaration.failure_reason == DeclarationFailureReason.UNKNOWN


@pytest.mark.parametrize(
    "escaped,expected",
    [
        # Named and numeric entities decode; `&not` without a semicolon is left alone.
        (
            "accepts mmu2carb_&lt;c&gt;_rf_pvld &amp; arb_h0_&#60;c&#62;_gnt",
            "accepts mmu2carb_<c>_rf_pvld & arb_h0_<c>_gnt",
        ),
        ("asserted while grant&not_ready holds", "asserted while grant&not_ready holds"),
        # An unknown name is not decoded by its known prefix (`&not`, `&copy`).
        ("grant&notready; and &copyright;", "grant&notready; and &copyright;"),
        # One decoding pass only.
        ("a &amp;lt; b", "a &lt; b"),
    ],
)
def test_description_is_decoded_the_way_the_web_shows_it(escaped: str, expected: str):
    declaration = Declaration.model_validate({**SAMPLE_DECLARATION, "description": escaped})

    assert declaration.description == expected
    assert declaration.model_dump(mode="json")["description"] == expected


@pytest.mark.parametrize(
    "reference, decoded",
    [
        ("&#" + "9" * 5000 + ";", "\ufffd"),
        ("&#x" + "F" * 5000 + ";", "\ufffd"),
        ("&#" + "0" * 5000 + "65;", "A"),
        ("&#X" + "0" * 5000 + "41;", "A"),
    ],
    ids=["decimal past U+10FFFF", "hexadecimal past U+10FFFF", "decimal after zeros", "hexadecimal after zeros"],
)
def test_a_numeric_reference_too_long_for_int_decodes(reference: str, decoded: str):
    declaration = Declaration.model_validate({**SAMPLE_DECLARATION, "description": f"x {reference} y"})

    assert declaration.description == f"x {decoded} y"


def test_a_numeric_reference_decodes_as_html_unescape_does():
    """0, the C1 controls html.unescape remaps, surrogates, noncharacters, and either side of U+10FFFF and the cut."""
    numbers = [*range(0x100), 0xD7FF, 0xD800, 0xDFFF, 0xE000, 0xFDD0, 0xFFFE, 0x10000, 0x10FFFF, 0x110000]
    for number in [*numbers, 9_999_999, 10_000_000, 0xFFFFFF, 0x1000000]:
        for reference in (f"&#{number};", f"&#000{number};", f"&#x{number:x};", f"&#X00{number:X};"):
            assert decode_html_entities(reference) == html.unescape(reference), reference


def test_lean_code_keeps_whatever_it_arrived_with():
    source = 'theorem base_case : render "a &lt; b" = ok'
    declaration = Declaration.model_validate({**SAMPLE_DECLARATION, "lean_code": source, "description": None})

    assert declaration.lean_code == source
    assert declaration.description is None
