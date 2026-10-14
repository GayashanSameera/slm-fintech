"""Deterministic reconciliation of a statement with the PDF text (parsers/reconcile.py).

Statement rows are reconciled with real transactions downstream, so every field of
every row must match the document; each kind of mismatch below must be caught and
named by row and field (never by value).
"""

from __future__ import annotations

import pytest

from app.modules.extraction.parsers.fingerprint import pick_parser
from app.modules.extraction.parsers.reconcile import reconcile
from app.modules.extraction.preprocess import preprocess
from app.modules.extraction.schemas.bank_statement import BankStatement
from tests.helpers.simple_pdf import demo_statement, sample_transactions


def _parsed(n: int = 8, rows_per_page: int = 25):
    """A synthetic statement parsed by the DEMO BANK parser, plus its page texts."""
    rows = sample_transactions(n)
    rows[1]["description"] = "NEFT/ACME PAYROLL SEP"            # spaces inside a description
    pdf = demo_statement(rows, rows_per_page=rows_per_page)
    prepared = preprocess(pdf)
    statement = BankStatement.model_validate(pick_parser(prepared.pages[0]["text"]).parse(pdf))
    return statement, [p["text"] for p in prepared.pages]


@pytest.mark.parametrize("n, per_page", [(8, 25), (28, 10), (100, 10)])
def test_correct_statement_reconciles_fully(n, per_page):
    statement, texts = _parsed(n, per_page)
    assert reconcile(statement, texts) == []


def _change_row(statement, index: int, **changes):
    """A copy of the statement with row `index` changed (to simulate an extraction mistake)."""
    rows = list(statement.transactions)
    rows[index] = rows[index].model_copy(update=changes)
    return statement.model_copy(update={"transactions": rows})


@pytest.mark.parametrize("change, expected", [
    ({"date": None}, "row_3_date"),
    ({"description": "NEFT/SOMEONE ELSE"}, "row_3_description"),
    ({"reference": "REF9999"}, "row_3_reference"),
    ({"balance": "1.00"}, "row_3_amounts"),
])
def test_every_row_field_is_checked(change, expected):
    statement, texts = _parsed()
    assert reconcile(_change_row(statement, 3, **change), texts) == [expected]


def test_missing_or_extra_row_is_caught():
    statement, texts = _parsed()
    assert reconcile(statement.model_copy(update={"transactions": statement.transactions[:-1]}), texts) == ["row_count"]
    extra = statement.transactions + [statement.transactions[-1]]
    assert reconcile(statement.model_copy(update={"transactions": extra}), texts) == ["row_count"]


def test_rows_out_of_order_are_caught():
    statement, texts = _parsed()
    rows = list(statement.transactions)
    rows[2], rows[3] = rows[3], rows[2]
    issues = reconcile(statement.model_copy(update={"transactions": rows}), texts)
    assert "row_2_date" in issues or "row_2_amounts" in issues


@pytest.mark.parametrize("field, value", [
    ("account_number", "999999999999"), ("ifsc", "HDFC0000001"), ("account_holder", "Rahul Mehta"),
    ("opening_balance", "12345.00"), ("closing_balance", "1.00"),
])
def test_header_fields_are_checked(field, value):
    statement, texts = _parsed()
    assert reconcile(statement.model_copy(update={field: value}), texts) == [f"header_{field}"]
