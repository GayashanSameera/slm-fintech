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

import dataclasses

DEFAULT = load_profile("default")
# These tests exercise the CHUNK mechanics on scanned pages with 3-page chunks (what digital statements
# read by AI still use). The real default for scans is one page per call: see the last tests.
PROFILE = dataclasses.replace(DEFAULT, scan_chunk_pages=3, crossread_enabled=False)
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


def _cross_chunk_mistake():
    """Answers where chunk 3's first rows have wrong balances: invisible inside chunk 3 alone (its own
    rows are consistent with each other) but the balance does not continue from chunk 2."""
    answers = _chunks()
    bad = json.loads(json.dumps(answers[2]))
    bad["transactions"][0]["balance"] = "1.00"
    bad["transactions"][1]["balance"] = str(Decimal("1.00") - Decimal(bad["transactions"][1]["debit"]))
    return answers, bad


def test_a_mistake_across_chunks_that_stays_wrong_is_reported():
    answers, bad = _cross_chunk_mistake()
    # First reading, then the strongest model re-reads the pages the problem points at, and is wrong too.
    gw = FakeGateway().queue(json.dumps(answers[0]), json.dumps(answers[1]), json.dumps(bad),
                             json.dumps(answers[1]), json.dumps(bad))
    _, failures, _, _ = run_ladder("bank_statement", PAGES, PROFILE, gw)
    assert "running_balance_mismatch" in [f.code for f in failures]


def test_a_mistake_across_chunks_is_repaired_by_rereading_only_the_pages_it_points_at():
    answers, bad = _cross_chunk_mistake()
    gw = FakeGateway().queue(json.dumps(answers[0]), json.dumps(answers[1]), json.dumps(bad),
                             json.dumps(answers[1]), json.dumps(answers[2]))     # the re-reads are right
    parsed, failures, model_id, calls = run_ladder("bank_statement", PAGES, PROFILE, gw)
    assert failures == [] and len(parsed.transactions) == 10
    # Chunks 2 and 3 (the border) were re-read by Sonnet, chunk 1 was not.
    assert [c["model_id"] for c in gw.calls] == [HAIKU, HAIKU, HAIKU, SONNET, SONNET]
    assert [c["pages"] for c in gw.calls[3:]] == [3, 1] and model_id == SONNET


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


# --- scanned statements: one page per call (measured far more accurate than 3-page chunks) ---------

def test_scanned_statement_default_is_read_one_page_per_call():
    assert DEFAULT.scan_chunk_pages == 1
    opening = Decimal("5000.00")
    pages = [{"text": None, "image": b"", "scanned": True} for _ in range(3)]
    r1, b1 = _rows(1, 3, opening)
    r2, b2 = _rows(4, 3, b1)
    r3, b3 = _rows(7, 3, b2)
    answers = [{"opening_balance": f"{opening}", "period_from": "2026-08-01", "period_to": "2026-08-31", "transactions": r1},
               {"transactions": r2}, {"closing_balance": f"{b3}", "transactions": r3}]
    gw = FakeGateway().queue(*(json.dumps(a) for a in answers))
    profile = dataclasses.replace(DEFAULT, crossread_enabled=False)
    parsed, failures, _, calls = run_ladder("bank_statement", pages, profile, gw)
    assert [c["pages"] for c in gw.calls] == [1, 1, 1]
    assert "pages 2 to 2 of a 3-page bank statement" in gw.calls[1]["instruction"]
    assert len(parsed.transactions) == 9 and failures == []


def test_second_reading_of_a_scanned_page_uses_the_enhanced_picture_and_arbitrates_disagreements():
    from io import BytesIO

    from PIL import Image

    from app.modules.extraction.pipeline import ladder_core

    buf = BytesIO(); Image.new("RGB", (1000, 1400), "white").save(buf, "JPEG")
    pages = [{"text": None, "image": buf.getvalue(), "scanned": True} for _ in range(2)]
    opening = Decimal("5000.00")
    r1, b1 = _rows(1, 3, opening)
    r2, b2 = _rows(4, 3, b1)
    p1 = {"opening_balance": f"{opening}", "period_from": "2026-08-01", "period_to": "2026-08-31", "transactions": r1}
    p2 = {"closing_balance": f"{b2}", "transactions": r2}
    wrong_p2 = json.loads(json.dumps(p2)); wrong_p2["transactions"][1]["description"] = "POS Shop WRONG"
    # page 1: read, then second reading agrees. page 2: read, second reading DIFFERS, arbiter sides with the first.
    gw = FakeGateway().queue(json.dumps(p1), json.dumps(p1), json.dumps(p2), json.dumps(wrong_p2), json.dumps(p2))
    parsed, failures, model_id, calls, unresolved = ladder_core("bank_statement", pages, DEFAULT, gw, second_reading=True)   # explicitly on: the profile default is off
    assert [c["model_id"] for c in gw.calls] == [HAIKU, HAIKU, HAIKU, HAIKU, SONNET]    # 2nd reader: Haiku again
    assert parsed.transactions[4].description == "POS Shop 5" and unresolved == [] and failures == []
    # The second reading was made from a different picture than the first.
    assert gw.calls[1]["pages"] == 1


def test_second_reading_of_scanned_statements_is_off_by_default_to_save_cost():
    assert DEFAULT.scan_second_reading is False
