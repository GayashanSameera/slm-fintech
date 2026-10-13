"""Document schemas: value parsing, consistency checks, masking, and the model schema."""

import json
from decimal import Decimal

import pytest

from app.modules.extraction.masking import mask_result
from app.modules.extraction.schemas import DOCUMENT_TYPES
from app.modules.extraction.schemas.types import parse_amount, parse_date

# ---- parsing ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(11800, "11800"), ("₹11,800.00", "11800.00"), ("Rs. 500", "500"), ("(1,000.00)", "-1000.00"), (0.1, "0.1")],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["2026-09-30", "30/09/2026", "30-09-2026", "30 Sep 2026", "30-Sep-2026"])
def test_parse_date_indian_formats(raw):
    assert parse_date(raw).isoformat() == "2026-09-30"


def test_parse_date_rejects_nonsense():
    with pytest.raises(ValueError):
        parse_date("31/02/2026")


# ---- checks ----


def test_invoice_checks():
    invoice = DOCUMENT_TYPES["invoice"].model
    ok = invoice.model_validate(
        {
            "line_items": [{"amount": 6000}, {"amount": 4000}],
            "subtotal": 10000,
            "cgst": 900,
            "sgst": 900,
            "total_amount": 11800,
            "seller": {"pan": "AABCS1429B"},
        }
    )
    assert ok.consistency_warnings() == []
    bad = invoice.model_validate(
        {
            "line_items": [{"amount": 6000}],
            "subtotal": 10000,
            "total_tax": 1700,
            "total_amount": 11800,
            "seller": {"pan": "BAD"},
        }
    )
    assert len(bad.consistency_warnings()) == 3


def test_bank_statement_checks():
    statement = DOCUMENT_TYPES["bank_statement"].model
    ok = statement.model_validate(
        {
            "opening_balance": 1000,
            "closing_balance": 1300,
            "transactions": [{"credit": 500, "balance": 1500}, {"debit": 200, "balance": 1300}],
        }
    )
    assert ok.consistency_warnings() == []
    bad = statement.model_validate(
        {
            "opening_balance": 1000,
            "closing_balance": 1400,
            "transactions": [{"credit": 500, "balance": 1500}, {"debit": 200, "balance": 1400}],
        }
    )
    assert len(bad.consistency_warnings()) == 2


def test_payslip_net_pay_check():
    payslip = DOCUMENT_TYPES["payslip"].model
    doc = payslip.model_validate({"gross_earnings": 50000, "total_deductions": 1800, "net_pay": 48250})
    assert doc.consistency_warnings() == ["Gross − deductions is 48,200.00, but net pay is 48,250.00."]


def test_expired_identity_document():
    identity = DOCUMENT_TYPES["identity_document"].model
    doc = identity.model_validate({"id_type": "Passport", "expiry_date": "01/01/2001"})
    assert doc.id_type == "passport"
    assert "expired" in doc.consistency_warnings()[0]


# ---- masking ----


def test_payslip_masking_and_unmask():
    t = DOCUMENT_TYPES["payslip"]
    data = {"pan": "ABCDE1234F", "uan": "100123456789", "bank_account_number": "50100123456789"}
    masked = mask_result(t, data)
    assert masked == {"pan": "XXXXXX234F", "uan": "XXXXXXXX6789", "bank_account_number": "XXXXXXXXXX6789"}
    assert mask_result(t, data, frozenset({"pan"}))["pan"] == "ABCDE1234F"
    assert data["pan"] == "ABCDE1234F"  # the original is not modified


def test_aadhaar_always_masked():
    t = DOCUMENT_TYPES["identity_document"]
    data = {"id_type": "aadhaar", "document_number": "2345 6789 0123", "address": "x"}
    out = mask_result(t, data, frozenset({"id_number", "aadhaar"}))
    assert out["document_number"] == "XXXX XXXX 0123"


def test_aadhaar_scan_in_any_field():
    t = DOCUMENT_TYPES["kyc_form"]
    out = mask_result(t, {"aadhaar_number": "234567890123", "permanent_address": "near 3456 7890 1234 road"})
    assert out["aadhaar_number"] == "XXXX XXXX 0123"
    assert out["permanent_address"] == "near XXXX XXXX 1234 road"


# ---- schema sent to the model ----


@pytest.mark.parametrize("name", list(DOCUMENT_TYPES))
def test_model_schema_is_flat_and_complete(name):
    schema = DOCUMENT_TYPES[name].json_schema
    text = json.dumps(schema)
    assert "$ref" not in text and "anyOf" not in text
    assert schema["required"] == list(schema["properties"])
    assert schema["additionalProperties"] is False
