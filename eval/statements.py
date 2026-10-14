"""Labelled synthetic BANK STATEMENTS for the evaluation (digital PDFs and degraded scans).

Bank statements are the platform's main workload and the one document type where every row must
be right, so they get their own generator: multi-page, 40 to 70 rows, correct running balances,
dates in order inside the period. The same page layout is written as

    "digital"  a PDF with a text layer (Rung 0 / text reconciliation can prove every row), or
    "scan"     JPEG images of the pages, rotated, blurred and noisy (no text layer: only the
               running balance, the date checks and the second reading can catch a misread).

Everything is invented. See eval/generate.py for the helpers shared with the other types.
"""

from __future__ import annotations

import random
from datetime import date

from app.modules.extraction.preprocess import Prepared, preprocess
from app.modules.extraction.preprocess.image import prepare_image
from tests.helpers.simple_pdf import COLS, make_pdf

from eval.generate import Case, _scan

HOLDERS = ["Asha Verma", "Ravi Iyer", "Meera Nair", "Karan Mehta", "Divya Reddy", "Imran Khan"]
NARRATION = ["UPI/Grocery Mart", "UPI/Fuel Station", "NEFT/Salary Credit", "ATM WDL Main Road", "IMPS/Rent Payment",
             "POS/Pharmacy Plus", "ECS/Electricity Board", "CHQ DEP Clearing", "UPI/Mobile Recharge", "NEFT/Vendor Payout",
             "INT.PD Savings", "UPI/Restaurant Bill", "POS/Online Store", "BIL/Water Charges", "UPI/Taxi Ride"]
ROWS_PER_PAGE = 20
PT_TO_PX = 1240 / 595


def _rows(rng: random.Random, n: int, opening: float) -> tuple[list[dict], float]:
    """n rows with consistent running balances; dates ascend through September 2026."""
    rows, balance = [], opening
    days = sorted(rng.randint(1, 30) for _ in range(n))
    for i, day in enumerate(days):
        credit = rng.random() < 0.35
        amount = round(rng.randint(150, 90000) / 10, 2) * (3 if credit else 1)
        balance = round(balance + amount if credit else balance - amount, 2)
        rows.append({"date": date(2026, 9, day).isoformat(), "description": rng.choice(NARRATION),
                     "reference": f"{rng.choice(['UPI', 'REF', 'CHQ', 'NEFT'])}{rng.randint(100000, 999999)}",
                     "debit": None if credit else f"{amount:.2f}", "credit": f"{amount:.2f}" if credit else None,
                     "balance": f"{balance:.2f}"})
    return rows, balance


def _fmt(value: str | None) -> str:
    return f"{float(value):,.2f}" if value else ""


def _page_lines(truth: dict) -> list[list[tuple[float, float, str]]]:
    """The statement as drawing lines per page, in PDF points (origin bottom-left): the DEMO BANK layout."""
    header = [(40, 800, "DEMO BANK LTD"), (40, 785, "Statement of Account"),
              (40, 765, f"Account Holder: {truth['account_holder']}"), (40, 752, f"Account Number: {truth['account_number']}"),
              (40, 739, f"IFSC: {truth['ifsc']}"), (40, 726, f"Branch: {truth['branch']}"),
              (40, 713, "Statement Period: 01/09/2026 to 30/09/2026"),
              (40, 700, f"Opening Balance: {_fmt(truth['opening_balance'])}")]
    titles = [(x, 0, label) for label, x in zip(["Date", "Description", "Reference", "Debit", "Credit", "Balance"], COLS.values(), strict=True)]
    txns = truth["transactions"]
    pages = []
    chunks = [txns[i:i + ROWS_PER_PAGE] for i in range(0, len(txns), ROWS_PER_PAGE)]
    for number, chunk in enumerate(chunks):
        lines = list(header) if number == 0 else []
        top = 675 if number == 0 else 800
        lines += [(x, top, label) for x, _, label in titles]
        y = top - 22
        for t in chunk:
            cells = {"date": date.fromisoformat(t["date"]).strftime("%d/%m/%Y"), "description": t["description"],
                     "reference": t["reference"], "debit": _fmt(t["debit"]), "credit": _fmt(t["credit"]),
                     "balance": _fmt(t["balance"])}
            lines += [(x, y, cells[key]) for key, x in COLS.items() if cells[key]]
            y -= 22
        if number == len(chunks) - 1:
            lines.append((40, y - 10, f"Closing Balance: {_fmt(truth['closing_balance'])}"))
        pages.append(lines)
    return pages


def build_statement_cases(per_group: int, seed: int = 7, hard: bool = False) -> list[Case]:
    """`per_group` statements for each condition ("digital" and "scan"), already preprocessed."""
    cases: list[Case] = []
    for condition in ("digital", "scan"):
        for i in range(per_group):
            rng = random.Random(f"{seed}-statement-{condition}-{i}")
            opening = round(rng.randint(20000, 400000) / 10, 2)
            rows, closing = _rows(rng, rng.randint(40, 70), opening)
            truth = {"bank_name": "DEMO BANK LTD", "account_holder": rng.choice(HOLDERS), "account_number": f"00{rng.randint(10**9, 10**10 - 1)}",
                     "ifsc": "DEMO0001234", "branch": "MG Road", "period_from": "2026-09-01", "period_to": "2026-09-30",
                     "opening_balance": f"{opening:.2f}", "closing_balance": f"{closing:.2f}", "transactions": rows}
            pages = _page_lines(truth)
            if condition == "digital":
                data = make_pdf(pages)
                prepared = preprocess(data)
            else:
                images = []
                for lines in pages:
                    px = [(int(x * PT_TO_PX), int((842 - y) * PT_TO_PX) - 14, text, 19) for x, y, text in lines]
                    images.append(_scan(px, random.Random(f"{seed}-{i}-{len(images)}"), hard))
                data = images[0]
                prepared = Prepared(kind="png", pages=[{"text": None, "image": prepare_image(img), "scanned": True}
                                                       for img in images])
            case = Case(f"statement-{condition}-{i}", "bank_statement", condition, data, truth)
            case.prepared = prepared
            cases.append(case)
    return cases
