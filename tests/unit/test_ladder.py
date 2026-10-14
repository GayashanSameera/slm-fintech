"""Unit tests for the model ladder (architecture §3.2, guide §6.1).

The FakeGateway plays the model: each test queues the answers the "model"
gives on attempt 1, 2, 3 and then checks WHICH model was called with WHICH
settings. With the default profile the ladder is:

    index 0 Nova Lite   index 1 Claude Haiku 5.5   index 2 Claude Sonnet 5.5

and every type starts on index 1 (Haiku), so the attempt plan is:
    attempt 1 Haiku  ->  attempt 2 Haiku WITH thinking  ->  attempt 3 Sonnet
"""

from __future__ import annotations

import json

import pytest

from app.core.errors import JobError
from app.gateway.fake import FakeGateway
from app.modules.extraction.pipeline import attempt_plan, extract_document, run_ladder
from app.modules.extraction.preprocess import Prepared
from app.modules.extraction.profiles import load_profile
from app.storage.tenants import TenantSettings

PROFILE = load_profile("default")
NOVA, HAIKU, SONNET = (r.model for r in PROFILE.ladder)

# A valid invoice whose numbers add up (subtotal 1000 + 18% GST = 1180).
GOOD_INVOICE = json.dumps({
    "invoice_number": "INV-1", "invoice_date": "07/09/2026",
    "seller": {"name": "Demo Traders", "pan": "ABCDE1234F"},
    "line_items": [{"description": "Widget", "amount": "1000.00"}],
    "subtotal": "1000.00", "cgst": "90.00", "sgst": "90.00", "total": "1180.00",
})
# Same invoice, but the total is wrong by one rupee: valid JSON, fails consistency checks.
WRONG_TOTAL = GOOD_INVOICE.replace('"total": "1180.00"', '"total": "1181.00"')
BAD_JSON = "Sure! Here is the invoice: {invoice_number: INV-1"

# A page as preprocessing would produce it (text only; the fake ignores content).
PAGES = [{"text": "TAX INVOICE ...", "image": None, "scanned": False}]


def _models(gw: FakeGateway) -> list[tuple[str, bool]]:
    return [(c["model_id"], c["thinking"]) for c in gw.calls]


# --- the plan itself ------------------------------------------------------------------------

@pytest.mark.parametrize("start, expected", [
    (0, [(0, False), (1, True), (2, False)]),    # Nova -> Haiku thinking -> Sonnet
    (1, [(1, False), (1, True), (2, False)]),    # Haiku -> Haiku thinking -> Sonnet
    (2, [(2, False)]),                           # already at the top: one attempt only
])
def test_attempt_plan(start, expected):
    assert attempt_plan(PROFILE, start) == expected


# --- climbing ---------------------------------------------------------------------------------

def test_good_first_answer_makes_one_call():
    gw = FakeGateway().queue(GOOD_INVOICE)
    parsed, failures, model_id, calls = run_ladder("invoice", PAGES, PROFILE, gw)
    assert failures == [] and model_id == HAIKU
    assert _models(gw) == [(HAIKU, False)]


def test_bad_json_climbs_and_tells_the_model_why():
    gw = FakeGateway().queue(BAD_JSON, GOOD_INVOICE)
    _, failures, model_id, _ = run_ladder("invoice", PAGES, PROFILE, gw)
    assert failures == []
    assert _models(gw) == [(HAIKU, False), (HAIKU, True)]     # retry with thinking
    assert "not valid JSON" in gw.calls[1]["instruction"]     # feedback reached attempt 2


def test_passes_on_top_rung_then_stops():
    # Two wrong answers, then Sonnet gets it right. The extra queued answer must NOT be used.
    gw = FakeGateway().queue(BAD_JSON, WRONG_TOTAL, GOOD_INVOICE, GOOD_INVOICE)
    _, failures, model_id, calls = run_ladder("invoice", PAGES, PROFILE, gw)
    assert failures == [] and model_id == SONNET
    assert len(calls) == 3


def test_check_failure_message_is_fed_back():
    gw = FakeGateway().queue(WRONG_TOTAL, GOOD_INVOICE)
    run_ladder("invoice", PAGES, PROFILE, gw)
    assert "1181.00" in gw.calls[1]["instruction"]     # the model is told what didn't add up


def test_never_passes_keeps_best_result_with_warnings():
    gw = FakeGateway().queue(WRONG_TOTAL, WRONG_TOTAL, WRONG_TOTAL)
    parsed, failures, _, calls = run_ladder("invoice", PAGES, PROFILE, gw)
    assert len(calls) == 3                               # 1 + max_climbs, never more
    assert parsed.invoice_number == "INV-1"
    assert [f.code for f in failures] == ["total_mismatch"]


def test_no_usable_answer_fails_job_but_keeps_calls_for_metering():
    gw = FakeGateway().queue(BAD_JSON, BAD_JSON, BAD_JSON)
    with pytest.raises(JobError) as exc:
        run_ladder("invoice", PAGES, PROFILE, gw)
    assert exc.value.code == "extraction_failed"
    assert len(exc.value.calls) == 3                     # tokens were spent; they're still billed


def test_schema_errors_name_fields_not_values():
    # "total" is not an amount: the retry feedback names the FIELD, never the bad value.
    bad = GOOD_INVOICE.replace('"total": "1180.00"', '"total": "SECRET-VALUE"')
    gw = FakeGateway().queue(bad, GOOD_INVOICE)
    run_ladder("invoice", PAGES, PROFILE, gw)
    feedback = gw.calls[1]["instruction"]
    assert "total" in feedback and "SECRET-VALUE" not in feedback


# --- the whole document (AI path) -------------------------------------------------------------

def _extract(gw: FakeGateway, requested_type: str | None, tenant: TenantSettings | None = None):
    prepared = Prepared(kind="png", pages=[{"text": None, "image": b"img", "scanned": True}])
    return extract_document(data=b"", prepared=prepared, requested_type=requested_type,
                            tenant=tenant or TenantSettings(tenant_id="t_test"),
                            profile=PROFILE, gateway=gw, job_id="j_test")


def test_extract_document_masks_and_reports_warnings():
    gw = FakeGateway().queue(WRONG_TOTAL, WRONG_TOTAL, WRONG_TOTAL)
    out = _extract(gw, "invoice")
    assert out.method == "ai" and out.doc_type_source == "client"
    assert out.warnings == ["total_mismatch"]
    assert out.result["seller"]["pan"] == "XXXXXX234F"   # masked before it leaves the pipeline
    assert out.result["invoice_date"] == "2026-09-07"
    assert len(out.calls) == 3


def test_empty_result_for_client_type_warns_mismatch():
    # Tenant says "invoice" but the model finds nothing at all: probably the wrong type.
    gw = FakeGateway().queue("{}")
    out = _extract(gw, "invoice")
    assert "document_type_mismatch" in out.warnings


def test_type_detection_uses_cheapest_model_first():
    # No document_type from the tenant: Nova classifies page 1, then Haiku extracts.
    gw = FakeGateway().queue('{"type": "invoice", "confidence": 0.95}', GOOD_INVOICE)
    out = _extract(gw, None)
    assert out.doc_type == "invoice" and out.doc_type_source == "detected"
    assert gw.calls[0]["model_id"] == NOVA


def test_unrecognised_type_fails_job():
    gw = FakeGateway().queue('{"type": "other", "confidence": 0.9}', '{"type": "other", "confidence": 0.9}')
    with pytest.raises(JobError) as exc:
        _extract(gw, None)
    assert exc.value.code == "unrecognised_document_type"
    assert len(exc.value.calls) == 2
