"""Long bank statements are read by AI in chunks of pages (pipeline.run_chunked_statement).

WHY (seen in production, 2026-10-14): a 15-page statement with ~190 rows does not fit in one
model answer (8000 output tokens). The answer was cut off, and the retry ("return a shorter
answer") made the next model leave rows out. Chunks of 3 pages always fit; they are read in
parallel, and a chunk that fails its checks climbs the ladder alone.
"""

from __future__ import annotations

import json
from decimal import Decimal

from app.gateway.base import GatewayResult
from app.gateway.fake import FakeGateway
from app.modules.extraction.pipeline import parse_and_validate, run_ladder
from app.modules.extraction.profiles import load_profile
from app.modules.extraction.schemas.bank_statement import BankStatement

PROFILE = load_profile("default")
NOVA, HAIKU, SONNET = (r.model for r in PROFILE.ladder)

# Scanned pages: no text layer, so only the schema and the arithmetic checks apply.
PAGES = [{"text": None, "image": b"", "scanned": True} for _ in range(7)]


def _rows(start_day: int, n: int, balance: Decimal):
    rows = []
    for i in range(n):
        debit = Decimal("100.00") + i
        balance -= debit
        rows.append({"date": f"2026-08-{start_day + i:02d}", "description": f"POS Shop {start_day + i}",
                     "debit": f"{debit}", "balance": f"{balance}"})
    return rows, balance


def _chunks():
    """Answers for chunks of pages 1-3, 4-6 and 7, with consistent balances across them."""
    opening = Decimal("10000.00")
    r1, b1 = _rows(1, 4, opening)
    r2, b2 = _rows(5, 4, b1)
    r3, b3 = _rows(9, 2, b2)
    first = {"account_number": "001234567890", "period_from": "2026-08-01", "period_to": "2026-08-31",
             "opening_balance": f"{opening}", "transactions": r1}
    return [first, {"transactions": r2}, {"closing_balance": f"{b3}", "transactions": r3}]


def test_long_statement_is_read_in_chunks_and_merged():
    answers = _chunks()
    gw = FakeGateway().queue(*(json.dumps(a) for a in answers))
    parsed, failures, model_id, calls = run_ladder("bank_statement", PAGES, PROFILE, gw)
    assert [c["pages"] for c in gw.calls] == [3, 3, 1]
    assert "pages 1 to 3 of a 7-page bank statement" in gw.calls[0]["instruction"]
    assert "pages 7 to 7 of a 7-page" in gw.calls[2]["instruction"]
    assert len(parsed.transactions) == 10 and failures == []
    assert parsed.account_number == "001234567890"          # header from the first chunk
    assert parsed.closing_balance == answers[2]["closing_balance"]   # ... or the chunk that prints it
    assert model_id == HAIKU and len(calls) == 3


def test_a_failing_chunk_climbs_alone():
    answers = _chunks()
    broken = json.loads(json.dumps(answers[1]))
    broken["transactions"][2]["balance"] = "1.00"            # a misread balance inside chunk 2
    gw = FakeGateway().queue(json.dumps(answers[0]), json.dumps(broken), json.dumps(answers[1]),
                             json.dumps(answers[2]))
    parsed, failures, model_id, calls = run_ladder("bank_statement", PAGES, PROFILE, gw)
    # chunk 1 once, chunk 2 twice (the retry is Haiku WITH thinking), chunk 3 once.
    assert [(c["pages"], c["thinking"]) for c in gw.calls] == [(3, False), (3, False), (3, True), (1, False)]
    assert failures == [] and len(parsed.transactions) == 10 and len(calls) == 4


def test_a_mistake_across_chunks_is_still_caught_after_the_merge():
    answers = _chunks()
    answers[2]["transactions"][0]["balance"] = "1.00"        # breaks the balance chain at the chunk border
    answers[2]["transactions"][1]["balance"] = str(Decimal("1.00") - Decimal(answers[2]["transactions"][1]["debit"]))
    gw = FakeGateway().queue(*(json.dumps(a) for a in answers))
    _, failures, _, _ = run_ladder("bank_statement", PAGES, PROFILE, gw)
    assert "running_balance_mismatch" in [f.code for f in failures]


def test_short_statement_is_read_in_one_call():
    answers = _chunks()
    whole = {**answers[0], "transactions": answers[0]["transactions"]}
    gw = FakeGateway().queue(json.dumps(whole))
    run_ladder("bank_statement", PAGES[:3], PROFILE, gw)
    assert len(gw.calls) == 1 and "pages 1 to 3" not in gw.calls[0]["instruction"]


def test_a_cut_off_answer_never_asks_for_a_shorter_one():
    res = GatewayResult(text='{"transactions": [', model_id=HAIKU, input_tokens=1, output_tokens=8000,
                        stop_reason="max_tokens")
    parsed, problems = parse_and_validate(res, BankStatement)
    assert parsed is None
    assert "shorter" not in problems[0].lower() and "every row" in problems[0].lower()
