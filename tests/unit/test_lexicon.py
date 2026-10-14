"""Shared reading rules (parsers/lexicon.py): dates, amounts, periods, row starts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.modules.extraction.parsers.lexicon import find_period, leading_date, parse_amount, text_row_starts

AUG = date(2026, 8, 31)


@pytest.mark.parametrize("token, value", [
    ("1,245.55", "1245.55"), ("1245.55", "1245.55"), ("1,18,000.50", "118000.50"), ("1,234,567.00", "1234567.00"),
    ("10.481,44", "10481.44"), ("(11,452.30)", "-11452.30"), ("-111,060.78", "-111060.78"),
    ("9,750.00Cr", "9750.00"), ("9.00Dr", "-9.00"), ("0.00", "0.00"),
])
def test_amounts(token, value):
    assert parse_amount(token) == Decimal(value)


@pytest.mark.parametrize("token", ["2026", "01.08.26", "MOB-3901", "1234", "—", "12/08/2026"])
def test_not_amounts(token):
    assert parse_amount(token) is None


@pytest.mark.parametrize("text, expected, used", [
    ("01/08/2026 Grocery", date(2026, 8, 1), 1), ("01-08-26 x", date(2026, 8, 1), 1),
    ("01.08.2026", date(2026, 8, 1), 1), ("01-Oct-2026 x", date(2026, 10, 1), 1),
    ("03 Aug 2026 Refund", date(2026, 8, 3), 3), ("1st Aug 2026", date(2026, 8, 1), 3),
    ("01 août 2026", date(2026, 8, 1), 3),
    ("01 Aug Mobile", date(2026, 8, 1), 2),        # no year: the statement's year
    ("02/08 Interest", date(2026, 8, 2), 1),
])
def test_leading_dates(text, expected, used):
    assert leading_date(text.split(), AUG) == (expected, used)


def test_a_yearless_date_after_the_period_end_month_is_from_the_year_before():
    # A December-January statement ending 15 Jan 2027: "20 Dec" is December 2026.
    assert leading_date(["20", "Dec"], date(2027, 1, 15)) == (date(2026, 12, 20), 2)


@pytest.mark.parametrize("text", ["14/10/2026 10:22 Page 1", "MONDAY, 10 AUGUST 2026", "Statement 01/08/2026"])
def test_not_row_dates(text):
    assert leading_date(text.split(), AUG) is None


@pytest.mark.parametrize("text", [
    "Statement Period : 01-08-2026 to 31-08-2026", "01 Aug – 31 Aug 2026", "01–31 Aug 2026",
    "01 August 2026 – 31 August 2026", "STATEMENT PERIOD 01 AUGUST — 31 AUGUST 2026", "PÉRIODE 01–31 août 2026",
])
def test_periods(text):
    assert find_period(text) == (date(2026, 8, 1), date(2026, 8, 31))


def test_month_only_period_needs_permission():
    assert find_period("Period: August 2026") is None
    assert find_period("Period: August 2026", allow_month_only=True) == (date(2026, 8, 1), date(2026, 8, 31))


def test_row_starts_skip_balance_lines_ranges_and_wrapped_range_tails():
    lines = [
        "01 Aug – 31 Aug 2026",                       # a period
        "01/08/2026 Opening Balance 1,000.00",        # a balance line
        "02/08/2026 Fuel 100.00 900.00",              # row
        "03/08/2026 Interest Pd:01-08-2026 to",       # row, its description wraps ...
        "31-08-2026",                                 # ... the rest of the range, not a row
        "S0001 10.00 910.00",
        "04/08/2026 Salary 2,000.00 2,910.00",        # row
    ]
    assert text_row_starts(lines, AUG) == [2, 3, 6]
