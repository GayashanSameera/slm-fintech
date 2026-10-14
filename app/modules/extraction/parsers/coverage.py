"""Coverage checks for Rung 0 parser results.

Arithmetic checks (checks.py) prove the numbers that WERE parsed are consistent.
Coverage checks ask the other question: did the parser MISS anything?

  * row_count_mismatch      the page text has more/fewer date-led lines than parsed rows
                            (a row was skipped, or a wrapped line was split wrongly)
  * closing_balance_mismatch the last row's balance is not the stated closing balance
  * no_transactions         nothing was parsed at all
  * missing_header_field    account number / period / opening or closing balance not found
                            (IFSC is Indian only; Sri Lankan and other statements have none)

Any coverage problem REJECTS the parser result and the statement goes to AI
extraction instead (requirement FR-9.2, FR-9.5).
"""

from __future__ import annotations

from ..schemas.base import to_decimal
from .lexicon import text_row_starts

REQUIRED_HEADER = ("account_number", "period_from", "period_to", "opening_balance", "closing_balance")


def coverage_problems(stmt, page_texts: list[str]) -> list[str]:
    """Codes for anything the parser may have MISSED (rows, closing balance, header fields).
    An empty list means the coverage is complete.
    """
    problems: list[str] = []
    if not stmt.transactions:
        return ["no_transactions"]
    # Count the lines of the text layer that start a transaction (a date, not a balance line or
    # a period); the same rule as the row-by-row reconciliation (lexicon.text_row_starts).
    lines = [ln.strip() for t in page_texts for ln in (t or "").splitlines() if ln.strip()]
    if len(text_row_starts(lines, stmt.period_to)) != len(stmt.transactions):
        problems.append("row_count_mismatch")
    last_balance = to_decimal(stmt.transactions[-1].balance)
    closing = to_decimal(stmt.closing_balance)
    if last_balance is not None and closing is not None and last_balance != closing:
        problems.append("closing_balance_mismatch")
    if any(getattr(stmt, f) in (None, "") for f in REQUIRED_HEADER):
        problems.append("missing_header_field")
    return problems
