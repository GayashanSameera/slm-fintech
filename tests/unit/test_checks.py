"""Unit tests for consistency checks (FR-3, guide §6.1).

Checks are the strongest quality signal: they decide whether the ladder climbs
and whether a Rung 0 parser result is accepted. The tolerance is exactly one
paisa (0.01): a difference of 0.01 passes, anything more fails.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.modules.extraction.checks import DOCUMENT_ONLY, run_checks
from app.modules.extraction.schemas import REGISTRY


def _parse(doc_type: str, data: dict):
    """Validate a dict with the real schema, as the pipeline does."""
    return REGISTRY[doc_type].model.model_validate(data)


def _codes(doc_type: str, data: dict) -> list[str]:
    """Validate `data` with the type's schema, run its checks, return the failure codes."""
    return [f.code for f in run_checks(doc_type, _parse(doc_type, data))]


# --- invoice --------------------------------------------------------------------------------

def _invoice(total: str) -> dict:
    # 600 + 400 = 1000 subtotal; 9% CGST + 9% SGST = 180; correct total = 1180.00
    """An invoice that adds up (600 + 400 = 1000; +18% GST = 1180) except for `total`."""
    return {
        "line_items": [{"description": "Widget", "amount": "600.00"},
                       {"description": "Service", "amount": "400.00"}],
        "subtotal": "1000.00", "cgst": "90.00", "sgst": "90.00", "total": total,
    }


@pytest.mark.parametrize("total, expected", [
    ("1180.00", []),                      # sums right
    ("1180.01", []),                      # off by one paisa: within tolerance (rounding on paper)
    ("1181.00", ["total_mismatch"]),      # off by one rupee: wrong
])
def test_invoice_total(total, expected):
    assert _codes("invoice", _invoice(total)) == expected


def test_invoice_line_items_must_add_up_to_subtotal():
    data = _invoice("1180.00")
    data["line_items"][0]["amount"] = "650.00"
    assert "line_items_sum_mismatch" in _codes("invoice", data)


def test_invoice_bad_gstin_and_pan_formats():
    data = _invoice("1180.00")
    data["seller"] = {"gstin": "NOT-A-GSTIN", "pan": "ABCDE1234F"}   # PAN fine, GSTIN wrong
    data["buyer"] = {"pan": "12345"}
    assert set(_codes("invoice", data)) == {"gstin_format", "pan_format"}


def test_check_messages_carry_numbers_for_the_model():
    # Messages go ONLY to the model's retry prompt; they're useful because they name the numbers.
    failures = run_checks("invoice", _parse("invoice", _invoice("1181.00")))
    assert "1180.00" in failures[0].message


# --- bank statement -------------------------------------------------------------------------

def _statement(rows: list[tuple[str | None, str | None, str]], closing: str) -> dict:
    """A statement opening at 10,000 with the given (debit, credit, balance) rows."""
    return {
        "opening_balance": "10000.00", "closing_balance": closing, "ifsc": "HDFC0001234",
        "transactions": [{"debit": d, "credit": c, "balance": b} for d, c, b in rows],
    }


def test_bank_statement_consistent():
    rows = [("500.00", None, "9500.00"), (None, "2000.00", "11500.00")]
    assert _codes("bank_statement", _statement(rows, "11500.00")) == []


def test_bank_statement_running_balance_reported_once():
    # Row 1 is wrong; rows after it would all cascade, but only ONE failure is reported.
    rows = [("500.00", None, "9400.00"), (None, "2000.00", "11400.00"), ("100.00", None, "11300.00")]
    codes = _codes("bank_statement", _statement(rows, "11300.00"))
    assert codes.count("running_balance_mismatch") == 1


def test_bank_statement_closing_balance():
    rows = [("500.00", None, "9500.00")]
    assert _codes("bank_statement", _statement(rows, "9600.00")) == ["closing_balance_mismatch"]


def test_bank_statement_bad_ifsc():
    data = _statement([("500.00", None, "9500.00")], "9500.00")
    data["ifsc"] = "HDFC1234"
    assert _codes("bank_statement", data) == ["ifsc_format"]


# --- payslip, identity, kyc -----------------------------------------------------------------

def test_payslip_sums():
    good = {"earnings": [{"amount": "30000"}, {"amount": "10000"}], "gross_pay": "40000",
            "deductions": [{"amount": "1800"}, {"amount": "200"}], "net_pay": "38000"}
    assert _codes("payslip", good) == []
    bad = dict(good, net_pay="39000")
    assert _codes("payslip", bad) == ["net_pay_mismatch"]


def test_expired_identity_document_is_a_document_only_warning():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    codes = _codes("identity_document", {"id_type": "passport", "expiry_date": yesterday})
    assert codes == ["document_expired"]
    # Re-extracting with a stronger model can't "fix" an expired document, so no climb.
    assert "document_expired" in DOCUMENT_ONLY


def test_kyc_required_fields():
    assert _codes("kyc_form", {"name": "Asha Verma"}) == ["required_fields_missing"]
