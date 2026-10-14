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
from tests.helpers.simple_pdf import demo_reading, demo_statement, sample_transactions

PROFILE = load_profile("default")
NOVA, HAIKU, SONNET = (r.model for r in PROFILE.ladder)


def _run(pdf: bytes, gw: FakeGateway, *, requested_type: str | None = None, **tenant_settings):
    """Run the full extract_document on `pdf` for a test tenant, with the given tenant settings."""
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
    rows = sample_transactions(5)
    gw = FakeGateway().queue(demo_reading(rows))
    out = _run(demo_statement(rows), gw)

    assert out.method == "parser" and out.parser_id == "demo_bank_v1"
    assert out.doc_type == "bank_statement" and out.doc_type_source == "parser"
    assert out.verified_by == NOVA
    assert [c["model_id"] for c in gw.calls] == [NOVA]      # only the verifier ran
    assert len(out.result["transactions"]) == 5
    assert out.result["closing_balance"] == "11210.00"
    assert out.result["account_number"] == "XXXXXXXX7890"   # masked like any AI result


def test_multi_page_statement():
    rows = sample_transactions(28)
    gw = FakeGateway().queue(demo_reading(rows))
    out = _run(demo_statement(rows, rows_per_page=10), gw)
    assert out.method == "parser"
    assert len(out.result["transactions"]) == 28


def test_checks_only_tenant_never_calls_a_model():
    # FR-9.4: the tenant chose that digital statements never go to any AI service.
    gw = FakeGateway()
    out = _run(demo_statement(sample_transactions(5)), gw, verification_mode="checks_only")
    assert out.method == "parser" and out.verified_by is None
    assert gw.calls == []


def test_header_disagreement_is_a_warning_not_a_rejection():
    # Owner decision 2026-10-15: transactions are what matters. A different account holder is
    # reported, and the (row-proven) parser result is kept: no expensive AI extraction.
    rows = sample_transactions(5)
    gw = FakeGateway().queue(demo_reading(rows, holder="Someone Else"))
    out = _run(demo_statement(rows), gw)
    assert out.method == "parser" and [c["model_id"] for c in gw.calls] == [NOVA]
    assert out.warnings == ["header_unverified:account_holder"]


def test_transaction_disagreement_falls_back_to_haiku():
    rows = sample_transactions(5)
    reading = json.loads(demo_reading(rows))
    reading["first_transaction"]["description"] = "SOMETHING ELSE ENTIRELY"
    gw = FakeGateway().queue(json.dumps(reading), _ai_statement(rows))
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
    # The "AI" answers with tidy numbers that are NOT what the document prints.
    tidy = _ai_statement(sample_transactions(5))
    gw = FakeGateway().queue(tidy, tidy, tidy)
    out = _run(demo_statement(rows, closing="11,210.00"), gw)
    assert out.method == "ai"
    assert gw.calls[0]["model_id"] == HAIKU                  # no verifier call was wasted on the parser
    # Text reconciliation notices the AI's rows differ from the printed document: it climbs the
    # ladder and, still not matching, the result carries a warning instead of passing silently.
    assert [c["model_id"] for c in gw.calls] == [HAIKU, HAIKU, SONNET]
    assert out.warnings == ["text_reconciliation_failed"]


def test_requested_other_type_skips_rung0():
    # The tenant says it's an invoice: Rung 0 (bank statements only) must not run.
    gw = FakeGateway().queue("{}")
    out = _run(demo_statement(sample_transactions(3)), gw, requested_type="invoice")
    assert out.method == "ai" and out.doc_type == "invoice"
    # A DIGITAL document: its values are proven by the text (grounding.py), so no second reading.
    assert [c["model_id"] for c in gw.calls] == [HAIKU]


# --- the verifier: "read independently, compare in code" (parsers/verify.py) ----------------
# Redesigned after the first real AWS test, where Nova Lite asked to "list differences"
# reported identical values and ran out of tokens on two-page statements.

def _statement(n: int = 6):
    """A synthetic statement: (rows, prepared pages, parser result)."""
    from app.modules.extraction.parsers.fingerprint import pick_parser
    from app.modules.extraction.schemas.bank_statement import BankStatement

    rows = sample_transactions(n)
    pdf = demo_statement(rows)
    prepared = preprocess(pdf)
    return rows, prepared, BankStatement.model_validate(pick_parser(prepared.pages[0]["text"]).parse(pdf))


def _verify(answer: str, statement=None, n: int = 6):
    """Run the verifier with a queued model answer. Returns (result, gateway, rows, parsed)."""
    from app.modules.extraction.parsers.verify import verify

    rows, prepared, parsed = _statement(n)
    gw = FakeGateway().queue(answer)
    return verify(prepared.pages, statement or parsed, gw, NOVA), gw, rows, parsed


def test_verifier_never_sees_the_parser_output():
    from app.modules.extraction.prompts import VERIFY_INSTRUCTION

    rows, *_ = _statement()
    _, gw, _, _ = _verify(demo_reading(rows))
    assert gw.calls[0]["instruction"] == VERIFY_INSTRUCTION          # an independent reading
    assert "001234567890" not in gw.calls[0]["instruction"]


def test_reading_in_other_formats_still_matches():
    rows, *_ = _statement()
    answer = json.loads(demo_reading(rows))
    answer.update(account_holder="ASHA  VERMA", account_number="0012 3456 7890", period_from="2026-09-01")
    v, *_ = _verify(json.dumps(answer))
    assert v.agree is True and v.issue_paths == []


def test_wrong_account_number_is_reported_not_rejected():
    rows, *_ = _statement()
    v, *_ = _verify(demo_reading(rows, account="999988887777"))
    assert v.agree is True and v.header_issues == ["account_number"]


def test_unreadable_required_field_rejects():
    rows, *_ = _statement()
    answer = json.loads(demo_reading(rows))
    answer["account_number"] = None                                    # the model couldn't find it
    v, *_ = _verify(json.dumps(answer))
    assert v.agree is True and v.header_issues == ["account_number"]   # header: reported only


def test_optional_field_not_printed_is_not_a_mismatch():
    # Many statements (e.g. Sri Lankan ones) print no IFSC; some layouts no branch or holder.
    rows, *_ = _statement()
    answer = json.loads(demo_reading(rows))
    answer.update(ifsc=None, branch=None, account_holder=None)
    v, *_ = _verify(json.dumps(answer))
    assert v.agree is True


def test_header_field_read_differently_is_reported():
    rows, *_ = _statement()
    answer = json.loads(demo_reading(rows))
    answer["account_holder"] = "SOMEONE ELSE"
    v, *_ = _verify(json.dumps(answer))
    assert v.agree is True and v.header_issues == ["account_holder"]


def test_last_transaction_read_differently_rejects():
    rows, *_ = _statement()
    answer = json.loads(demo_reading(rows))
    answer["last_transaction"]["date"] = "2026-01-01"
    v, *_ = _verify(json.dumps(answer))
    assert v.agree is False and v.issue_paths == ["last_transaction.date"]


def test_dates_without_a_year_take_the_statement_year():
    from app.modules.extraction.parsers.verify import compare
    parsed = {"account_number": "12345678", "period_from": "2026-08-01", "period_to": "2026-08-31"}
    reading = {"account_number": "12345678", "period_from": "01 Aug", "period_to": "31 Aug 2026"}
    assert compare(reading, parsed) == []


def test_parser_that_missed_the_last_row_is_rejected():
    rows, _, parsed = _statement()
    short = parsed.model_copy(update={"transactions": parsed.transactions[:-1]})
    v, *_ = _verify(demo_reading(rows), statement=short)
    assert v.agree is False
    assert v.issue_paths == ["last_transaction.date", "last_transaction.description"] or \
        v.issue_paths == ["last_transaction.description"]          # same date can repeat


def test_answer_that_is_not_an_object_counts_as_unreadable():
    v, *_ = _verify('["agree"]')
    assert v.agree is False and v.issue_paths == ["_verifier_unreadable"]

