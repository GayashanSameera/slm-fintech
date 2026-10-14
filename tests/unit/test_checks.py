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
    data["seller"] = {"gstin": "NOT-A-GSTIN", "pan": "ABCCE1234F"}   # PAN fine, GSTIN wrong
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
        "transactions": [{"date": f"2026-09-{i:02d}", "debit": d, "credit": c, "balance": b}
                         for i, (d, c, b) in enumerate(rows, start=1)],
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
    # A wrong closing line is seen two ways: the sums do not fit, and it is not the last row's balance.
    assert set(_codes("bank_statement", _statement(rows, "9600.00"))) == {"closing_balance_mismatch", "closing_not_last_balance"}


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


def test_bank_statement_row_without_date_or_balance_is_flagged():
    st = _statement([("500.00", None, "9500.00"), (None, "2000.00", None)], "11500.00")
    st["transactions"][0]["date"] = None
    codes = _codes("bank_statement", st)
    assert "transaction_missing_date" in codes and "transaction_missing_balance" in codes


def test_bank_statement_without_any_balance_is_reported_not_climbed():
    from app.modules.extraction.checks import DOCUMENT_ONLY
    st = _statement([("500.00", None, None), (None, "2000.00", None)], "11500.00")
    assert "running_balance_not_printed" in _codes("bank_statement", st)
    assert "running_balance_not_printed" in DOCUMENT_ONLY


def test_balance_lines_read_as_rows_are_removed():
    from app.modules.extraction.schemas.bank_statement import BankStatement
    st = BankStatement.model_validate({"transactions": [
        {"date": "2025-04-01", "description": "Opening Balance", "balance": "250000.00"},
        {"date": "2025-04-02", "description": "ATM", "debit": "100.00", "balance": "249900.00"},
        {"date": "2025-04-30", "description": "Closing Balance", "balance": "249900.00"}]})
    assert [t.description for t in st.transactions] == ["ATM"]
    assert st.opening_balance == "250000.00" and st.closing_balance == "249900.00"


# --- identifier check digits and plausibility (checks.py + identifiers.py) -------------------

def test_invoice_gstin_checksum_state_and_embedded_pan():
    data = _invoice("1180.00")
    data["seller"] = {"gstin": "27AAPFU0939F1ZV", "pan": "AAPFU0939F"}       # valid and consistent
    assert "gstin_checksum" not in _codes("invoice", data)
    data["seller"] = {"gstin": "27AAPFU0939F1ZW", "pan": "AAPFU0939F"}       # last character wrong
    assert "gstin_checksum" in _codes("invoice", data)
    data["seller"] = {"gstin": "27AAPFU0939F1ZV", "pan": "AAPFU0939G"}       # PAN differs from the GSTIN's
    assert "gstin_pan" in _codes("invoice", data)


def test_identity_aadhaar_check_digit():
    from app.modules.extraction.identifiers import verhoeff_check_digit
    good = "23456789012" + verhoeff_check_digit("23456789012")
    bad = good[:-1] + str((int(good[-1]) + 1) % 10)
    assert "aadhaar_checksum" not in _codes("identity_document", {"id_type": "aadhaar", "id_number": good})
    assert "aadhaar_checksum" in _codes("identity_document", {"id_type": "aadhaar", "id_number": bad})


def test_impossible_dates_are_caught():
    assert "dob_implausible" in _codes("identity_document", {"date_of_birth": "2999-01-01"})
    assert "dob_implausible" in _codes("kyc_form", {"name": "A", "date_of_birth": "1700-01-01", "pan": "ABCPA1234F"})
    assert "date_in_future" in _codes("invoice", {"invoice_date": "2999-01-01"})
    assert "due_before_invoice_date" in _codes("invoice", {"invoice_date": "2026-09-07", "due_date": "2026-09-01"})


def test_payslip_total_deductions_must_match_the_listed_deductions():
    data = {"deductions": [{"name": "PF", "amount": "1000.00"}, {"name": "Tax", "amount": "500.00"}],
            "total_deductions": "1600.00"}
    assert "deductions_sum_mismatch" in _codes("payslip", data)


def test_kyc_mobile_email_and_pin_formats():
    codes = _codes("kyc_form", {"name": "A", "date_of_birth": "1990-01-01", "pan": "ABCPA1234F",
                                "mobile": "12345", "email": "not-an-email",
                                "current_address": {"pincode": "012345"}})
    assert {"mobile_format", "email_format", "pincode_format"} <= set(codes)
    ok = _codes("kyc_form", {"name": "A", "date_of_birth": "1990-01-01", "pan": "ABCPA1234F",
                             "mobile": "+91 98765 43210", "email": "a@b.co", "current_address": {"pincode": "560001"}})
    assert not {"mobile_format", "email_format", "pincode_format"} & set(ok)


def test_discount_style_line_item_mismatch_is_only_a_warning_never_a_reason_to_climb():
    from app.modules.extraction.checks import DOCUMENT_ONLY
    assert "line_item_amount_mismatch" in DOCUMENT_ONLY and "pan_name_mismatch" in DOCUMENT_ONLY


def test_payslip_totals_listed_as_components_are_a_misreading():
    # Seen with Nova Lite: the "Gross pay" and "Total deductions" LINES came back as list items.
    data = {"earnings": [{"name": "Basic", "amount": "100.00"}, {"name": "Gross pay", "amount": "100.00"}],
            "deductions": [{"name": "PF", "amount": "10.00"}, {"name": "Total deductions", "amount": "10.00"}]}
    assert "total_listed_as_component" in _codes("payslip", data)
    ok = {"earnings": [{"name": "Basic", "amount": "100.00"}], "deductions": [{"name": "Provident Fund", "amount": "10.00"}]}
    assert "total_listed_as_component" not in _codes("payslip", ok)


# --- bank statement dates (checks.py section 3) ----------------------------------------------

def _dated_statement(dates, period=("2026-08-01", "2026-08-31")):
    """Rows with consistent balances (each row debits 100) and the given dates."""
    rows, bal = [], 1000
    for d in dates:
        bal -= 100
        rows.append({"date": d, "description": "POS", "debit": "100.00", "balance": f"{bal}.00"})
    return {"opening_balance": "1000.00", "closing_balance": f"{bal}.00", "period_from": period[0],
            "period_to": period[1], "transactions": rows}


def test_statement_dates_ascending_or_descending_are_fine():
    assert _codes("bank_statement", _dated_statement(["2026-08-01", "2026-08-01", "2026-08-05"])) == []
    # Some banks list the newest row first; running balances are then read bottom-up, so use a
    # statement whose rows are consistent in that order: only the DATE rule is under test here.
    codes = _codes("bank_statement", _dated_statement(["2026-08-09", "2026-08-05", "2026-08-02"]))
    assert "dates_out_of_order" not in codes


def test_a_misread_date_digit_breaks_the_order_or_the_period():
    # 2026-08-17 misread as 2026-08-11 between 15 and 16, and a year misread as 2025.
    assert "dates_out_of_order" in _codes("bank_statement", _dated_statement(["2026-08-10", "2026-08-15", "2026-08-11", "2026-08-16"]))
    assert "date_outside_period" in _codes("bank_statement", _dated_statement(["2026-08-10", "2025-08-15"]))
    assert "date_outside_period" in _codes("bank_statement", _dated_statement(["2026-08-10", "2026-09-15"]))


def test_date_failures_name_the_rows_so_only_their_pages_are_reread():
    failures = run_checks("bank_statement", _parse("bank_statement", _dated_statement(["2026-08-10", "2026-08-15", "2026-08-11", "2026-08-16"])))
    order = next(f for f in failures if f.code == "dates_out_of_order")
    assert order.rows == (3,)


def test_closing_and_opening_balance_must_fit_the_rows():
    data = _dated_statement(["2026-08-01", "2026-08-05"])
    assert not {"closing_not_last_balance", "opening_not_first_row"} & set(_codes("bank_statement", data))
    data["closing_balance"] = "700.00"                       # last row's balance is 800.00
    assert "closing_not_last_balance" in _codes("bank_statement", data)
    data = _dated_statement(["2026-08-01", "2026-08-05"])
    data["opening_balance"] = "1500.00"                      # first row 1500 - 100 = 1400, but it shows 900
    assert "opening_not_first_row" in _codes("bank_statement", data)
