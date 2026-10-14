"""Unit tests for date and amount normalisation (FR-3.7, FR-3.8, guide §6.1).

Whatever format the document (or the model) uses, results always come out as:
  * dates   -> "YYYY-MM-DD"
  * amounts -> a 2-decimal string such as "118000.50" (Decimal, never float)
"""

from __future__ import annotations

from datetime import date

import pytest

from app.modules.extraction.normalise import to_result
from app.modules.extraction.schemas.base import _DATE_FORMATS, parse_date, parse_money
from app.modules.extraction.schemas.invoice import Invoice

SAMPLE_DAY = date(2026, 9, 7)


@pytest.mark.parametrize("fmt", _DATE_FORMATS)
def test_every_supported_date_format(fmt):
    # Format a known date with each pattern, then make sure we parse it back.
    # Looping over _DATE_FORMATS itself means a newly added format is tested automatically.
    assert parse_date(SAMPLE_DAY.strftime(fmt)) == SAMPLE_DAY


@pytest.mark.parametrize("value", [None, ""])
def test_empty_date_is_none(value):
    assert parse_date(value) is None


def test_unknown_date_format_raises_without_echoing_value():
    with pytest.raises(ValueError) as exc:
        parse_date("the seventh of September")
    assert "September" not in str(exc.value)          # NFR-S8: no document values in errors


@pytest.mark.parametrize("raw, expected", [
    ("₹1,18,000.50", "118000.50"),      # Indian lakh grouping + rupee sign
    ("Rs. 500", "500.00"),
    ("INR 2,500", "2500.00"),
    (11800, "11800.00"),                # numbers from JSON
    ("(250.00)", "-250.00"),            # accounting-style negative
    ("1,234.50 Cr", "1234.50"),         # balance suffixes used by some banks
    ("1,234.50 Dr", "-1234.50"),
    ("0.1", "0.10"),
])
def test_money_formats(raw, expected):
    assert parse_money(raw) == expected


@pytest.mark.parametrize("value", [None, ""])
def test_empty_money_is_none(value):
    assert parse_money(value) is None


def test_non_amount_raises():
    with pytest.raises(ValueError):
        parse_money("twelve rupees")


def test_to_result_fills_missing_fields_with_null_and_iso_dates():
    # The model left most fields out (to save tokens). FR-3.6: they come back as null.
    inv = Invoice.model_validate({"invoice_number": "INV-1", "invoice_date": "07/09/2026", "total": "₹1,180"})
    out = to_result(inv)
    assert out["invoice_date"] == "2026-09-07"
    assert out["total"] == "1180.00"
    assert out["due_date"] is None and out["subtotal"] is None
    assert out["line_items"] == []


def test_unknown_fields_from_model_are_dropped():
    # extra="ignore": model output can never add fields we didn't define (security.md §7.2).
    inv = Invoice.model_validate({"invoice_number": "INV-1", "secret_note": "ignore me"})
    assert "secret_note" not in to_result(inv)
