"""The generic statement parser (parsers/generic.py) on SYNTHETIC layouts (tests/helpers/
statement_layouts.py), each reproducing features of real statement layouts.

For every layout the parser's result must be EXACTLY right (rows, sides, balances, words,
header), and must pass every Rung 0 gate without AI: schema, arithmetic checks, coverage and
the row-by-row text reconciliation. Layouts the parser can't read with certainty must be
DECLINED (no rows), never guessed.
"""

from __future__ import annotations

import json

import pytest

from app.gateway.fake import FakeGateway
from app.modules.extraction.checks import run_checks
from app.modules.extraction.parsers import try_rung0
from app.modules.extraction.parsers.coverage import coverage_problems
from app.modules.extraction.parsers.fingerprint import pick_parser
from app.modules.extraction.parsers.generic import GenericStatementParser
from app.modules.extraction.parsers.reconcile import reconcile
from app.modules.extraction.preprocess import preprocess
from app.modules.extraction.schemas.bank_statement import BankStatement
from tests.helpers.statement_layouts import LAYOUTS, build


def _read(name: str):
    pdf, expected = build(name)
    prepared = preprocess(pdf)
    texts = [p["text"] for p in prepared.pages]
    statement = BankStatement.model_validate(GenericStatementParser().parse(pdf))
    return pdf, prepared, texts, statement, expected


@pytest.mark.parametrize("name", list(LAYOUTS))
def test_layout_is_read_exactly(name):
    _, _, _, st, expected = _read(name)
    got = [{"day": t.date.day, "description": t.description, "debit": t.debit, "credit": t.credit,
            "balance": t.balance} for t in st.transactions]
    want = [{k: v for k, v in t.items() if k != "reference"} for t in expected["transactions"]]
    assert got == want
    assert st.account_number == expected["account_number"]
    assert st.account_holder == expected["account_holder"]
    assert (st.period_from.isoformat(), st.period_to.isoformat()) == expected["period"]
    assert st.opening_balance == expected["opening_balance"]
    assert st.closing_balance == expected["closing_balance"]


@pytest.mark.parametrize("name", list(LAYOUTS))
def test_layout_passes_every_rung0_gate(name):
    _, prepared, texts, st, _ = _read(name)
    assert pick_parser("\n".join(texts[:3])).parser_id == "generic_v1"
    assert [f.code for f in run_checks("bank_statement", st)] == []
    assert coverage_problems(st, texts) == []
    assert reconcile(st, texts) == []


def test_references_are_read():
    *_, st, expected = _read("wrapped")
    assert [t.reference for t in st.transactions] == [t["reference"] for t in expected["transactions"]]


def test_watermark_does_not_leak_into_rows():
    # "SAMPLE COPY" is drawn diagonally across the table of the type_amount layout.
    *_, st, _ = _read("type_amount")
    assert not any("SAMPLE" in (t.description or "") or "COPY" in (t.description or "") for t in st.transactions)


@pytest.mark.parametrize("name", ["two_accounts", "ambiguous"])
def test_ambiguous_layouts_are_declined(name):
    pdf, _ = build(name)
    assert GenericStatementParser().parse(pdf)["transactions"] == []


@pytest.mark.parametrize("change", [
    {"debit": "1.00"},                       # a wrong amount
    {"description": "POS Somewhere Else"},   # a wrong description
])
def test_a_wrong_row_is_rejected(change):
    *_, texts, st, _ = _read("wrapped")
    rows = list(st.transactions)
    rows[5] = rows[5].model_copy(update=change)
    bad = st.model_copy(update={"transactions": rows})
    assert run_checks("bank_statement", bad) or reconcile(bad, texts)


def test_rung0_accepts_a_generic_layout_after_verification():
    pdf, prepared, _, st, expected = _read("wrapped")
    first, last = expected["transactions"][0], expected["transactions"][-1]
    reading = {"account_number": expected["account_number"], "period_from": "2026-08-01", "period_to": "2026-08-31",
               "account_holder": expected["account_holder"], "ifsc": None, "branch": None,
               "first_transaction": {"date": "2026-08-01", "description": first["description"]},
               "last_transaction": {"date": f"2026-08-{last['day']:02d}", "description": last["description"]}}
    gw = FakeGateway().queue(json.dumps(reading))
    outcome, calls, attempted = try_rung0(data=pdf, prepared=prepared, gateway=gw, verifier_model="nova",
                                          verification_mode="ai", job_id="t")
    assert attempted and outcome is not None
    assert outcome.parser_id == "generic_v1" and len(outcome.statement.transactions) == len(st.transactions)
    assert len(calls) == 1                   # one cheap verification call, no AI extraction


def test_a_document_without_a_transaction_table_is_not_claimed_as_a_statement():
    # The fingerprint matches the words, but there is no table: the pipeline must classify it
    # normally instead of treating it as a rejected bank statement.
    from tests.helpers.simple_pdf import make_pdf
    pdf = make_pdf([[(40, 800, "INVOICE"), (40, 780, "Date Amount Balance due"), (40, 760, "Thank you")]])
    prepared = preprocess(pdf)
    outcome, calls, attempted = try_rung0(data=pdf, prepared=prepared, gateway=FakeGateway(), verifier_model="nova",
                                          verification_mode="ai", job_id="t")
    assert outcome is None and attempted is False and calls == []
