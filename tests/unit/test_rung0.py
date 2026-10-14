"""Unit tests for Rung 0: Python parsers for bank statements, verified by AI
(FR-9, architecture §3.3, guide §4.10).

Every test builds a SYNTHETIC "DEMO BANK LTD" statement PDF with
tests/helpers/simple_pdf.py, runs the real preprocessing (pypdfium2 +
pdfplumber), and drives the full `extract_document` so we test what the worker
actually does:

    accepted  -> method "parser", no extraction model call, result masked
    rejected  -> falls back to the AI ladder, starting at Haiku (fallback_rung)
"""

from __future__ import annotations

import json

from app.gateway.fake import FakeGateway
from app.modules.extraction.pipeline import extract_document
from app.modules.extraction.preprocess import preprocess
from app.modules.extraction.profiles import load_profile
from app.storage.tenants import TenantSettings
from tests.helpers.simple_pdf import demo_statement, sample_transactions

PROFILE = load_profile("default")
NOVA, HAIKU, _ = (r.model for r in PROFILE.ladder)
AGREE = '{"agree": true}'


def _run(pdf: bytes, gw: FakeGateway, *, requested_type: str | None = None, **tenant_settings):
    tenant = TenantSettings(tenant_id="t_test", **tenant_settings)
    return extract_document(data=pdf, prepared=preprocess(pdf), requested_type=requested_type,
                            tenant=tenant, profile=PROFILE, gateway=gw, job_id="j_test")


def _ai_statement(rows: list[dict]) -> str:
    """What the AI ladder returns when Rung 0 is rejected (a consistent statement)."""
    return json.dumps({
        "account_number": "001234567890", "ifsc": "DEMO0001234",
        "opening_balance": "10000.00", "closing_balance": rows[-1]["balance"],
        "transactions": [{k: r[k] or None for k in ("date", "debit", "credit", "balance")} for r in rows],
    })


def test_accepted_statement_uses_parser_and_one_cheap_verify_call():
    gw = FakeGateway().queue(AGREE)
    out = _run(demo_statement(sample_transactions(5)), gw)

    assert out.method == "parser" and out.parser_id == "demo_bank_v1"
    assert out.doc_type == "bank_statement" and out.doc_type_source == "parser"
    assert out.verified_by == NOVA
    assert [c["model_id"] for c in gw.calls] == [NOVA]      # only the verifier ran
    assert len(out.result["transactions"]) == 5
    assert out.result["closing_balance"] == "11210.00"
    assert out.result["account_number"] == "XXXXXXXX7890"   # masked like any AI result


def test_multi_page_statement():
    gw = FakeGateway().queue(AGREE)
    out = _run(demo_statement(sample_transactions(28), rows_per_page=10), gw)
    assert out.method == "parser"
    assert len(out.result["transactions"]) == 28


def test_checks_only_tenant_never_calls_a_model():
    # FR-9.4: the tenant chose that digital statements never go to any AI service.
    gw = FakeGateway()
    out = _run(demo_statement(sample_transactions(5)), gw, verification_mode="checks_only")
    assert out.method == "parser" and out.verified_by is None
    assert gw.calls == []


def test_verifier_disagreement_falls_back_to_haiku():
    rows = sample_transactions(5)
    gw = FakeGateway().queue('{"agree": false, "issues": [{"path": "account_holder"}]}', _ai_statement(rows))
    out = _run(demo_statement(rows), gw)
    assert out.method == "ai"
    # Nova has already seen the document as verifier, so the ladder starts at Haiku.
    assert [c["model_id"] for c in gw.calls] == [NOVA, HAIKU]
    assert out.doc_type == "bank_statement"


def test_unreadable_verifier_answer_counts_as_disagree():
    rows = sample_transactions(5)
    gw = FakeGateway().queue("I think it looks fine", _ai_statement(rows))
    assert _run(demo_statement(rows), gw).method == "ai"


def test_inconsistent_balances_reject_parser_without_verifying():
    rows = sample_transactions(5)
    rows[2]["balance"] = "99,999.00"                         # a printed balance that doesn't add up
    gw = FakeGateway().queue(_ai_statement(sample_transactions(5)))
    out = _run(demo_statement(rows, closing="11,210.00"), gw)
    assert out.method == "ai"
    assert [c["model_id"] for c in gw.calls] == [HAIKU]      # no verifier call was wasted


def test_requested_other_type_skips_rung0():
    # The tenant says it's an invoice: Rung 0 (bank statements only) must not run.
    gw = FakeGateway().queue("{}")
    out = _run(demo_statement(sample_transactions(3)), gw, requested_type="invoice")
    assert out.method == "ai" and out.doc_type == "invoice"
    assert [c["model_id"] for c in gw.calls] == [HAIKU]
