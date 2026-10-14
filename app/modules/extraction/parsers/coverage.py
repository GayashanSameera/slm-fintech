"""Coverage checks for Rung 0 parser results.

Arithmetic checks (checks.py) prove the numbers that WERE parsed are consistent.
Coverage checks ask the other question: did the parser MISS anything?

  * row_count_mismatch      the page text has more/fewer date-led lines than parsed rows
                            (a row was skipped, or a wrapped line was split wrongly)
  * closing_balance_mismatch the last row's balance is not the stated closing balance
  * no_transactions         nothing was parsed at all
  * missing_header_field    account number / IFSC / period not found

Any coverage problem REJECTS the parser result and the statement goes to AI
extraction instead (requirement FR-9.2, FR-9.5).
"""

from __future__ import annotations

import re

from ..schemas.base import to_decimal

# Lines in the page text that start with a date, e.g. "01/09/2026 UPI/..."
_DATE_LED_LINE = re.compile(r"^\s*\d{1,2}[/.-](\d{1,2}|[A-Za-z]{3})[/.-]\d{2,4}\s+\S", re.MULTILINE)
REQUIRED_HEADER = ("account_number", "ifsc", "period_from", "period_to", "opening_balance", "closing_balance")


def coverage_problems(stmt, page_texts: list[str]) -> list[str]:
    """Codes for anything the parser may have MISSED (rows, closing balance, header fields).
    An empty list means the coverage is complete.
    """
    problems: list[str] = []
    if not stmt.transactions:
        return ["no_transactions"]
    # Count date-led lines in the table part of every page. The statement period line
    # ("Statement Period: 01/09/2026 ...") does not START with a date, so it isn't counted.
    expected_rows = sum(len(_DATE_LED_LINE.findall(t or "")) for t in page_texts)
    if expected_rows != len(stmt.transactions):
        problems.append("row_count_mismatch")
    last_balance = to_decimal(stmt.transactions[-1].balance)
    closing = to_decimal(stmt.closing_balance)
    if last_balance is not None and closing is not None and last_balance != closing:
        problems.append("closing_balance_mismatch")
    if any(getattr(stmt, f) in (None, "") for f in REQUIRED_HEADER):
        problems.append("missing_header_field")
    return problems
