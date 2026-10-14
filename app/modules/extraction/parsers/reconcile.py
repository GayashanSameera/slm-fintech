"""Deterministic reconciliation of an extracted bank statement with the PDF's own text.

WHY: statement rows are reconciled against real transactions downstream, so nothing may
pass wrongly. Arithmetic (checks.py) proves the amounts are CONSISTENT; this module proves
every row is what the DOCUMENT SAYS, field by field, with no AI involved.

HOW: the PDF text layer is read by pypdfium2 (preprocess/pdf.py), a DIFFERENT library
from the pdfplumber word positions the Rung 0 parsers use, so it is an independent
reading of the same document. For every extracted transaction, in order, the matching
date-led line of that text must show:
    * the same date;
    * exactly the same set of amounts (debit or credit, and balance): nothing missing,
      nothing extra;
    * the reference, if there is one;
    * the description (a wrapped description may continue on the following lines).
And the number of date-led lines must equal the number of transactions.
Header fields (account number, IFSC, holder, period, opening and closing balance) must
appear in the text too.

What the text layer can NOT show is which COLUMN an amount was printed in (debit or
credit): a swap is caught by the running-balance check instead.

Used for:
  * Rung 0 parser results (parsers/__init__.py): any issue rejects the parser result;
  * AI results for DIGITAL PDFs (pipeline.py): issues count as failed checks, so the
    ladder climbs, and if they remain they are returned as warnings.
Scanned documents have no text layer, so this check can't run for them.

Issue codes name the field and row ("row_7_amounts"), never values (safe to log).
"""

from __future__ import annotations

import re
from decimal import Decimal

from ..schemas.base import parse_date, parse_money

# A line that starts with a date: "01/09/2026 ...", "1-Sep-26 ...", "01.09.2026 ...".
_DATE_LED = re.compile(r"^\s*\d{1,2}[/.-](\d{1,2}|[A-Za-z]{3})[/.-]\d{2,4}\b")
# A money token: 1,18,000.50 / -250.00 / (250.00) / 9,750.00Cr
_MONEY = re.compile(r"^\(?-?[\d,]+\.\d{2}\)?(Cr|Dr|CR|DR)?$")
# Lines inside the transaction area that are NOT part of a row's description.
_NOT_ROW_TEXT = re.compile(r"(opening|closing)\s+balance|statement\s+of\s+account|^page\s+\d+|"
                           r"^date\b.*\bbalance\b", re.IGNORECASE)


def _squash(text) -> str:
    """Comparable text: no whitespace, case-folded."""
    return re.sub(r"\s+", "", str(text or "")).casefold()


def _amounts_on(line: str) -> list[Decimal]:
    """Every money token on a line, as positive Decimals, sorted (the sign/column isn't in the text)."""
    out = []
    for token in line.split():
        if _MONEY.match(token):
            try:
                out.append(abs(Decimal(parse_money(token))))
            except ValueError:
                pass
    return sorted(out)


def _lines(page_texts: list[str]) -> list[str]:
    """All non-empty text lines of all pages, in reading order."""
    return [ln.strip() for text in page_texts for ln in (text or "").splitlines() if ln.strip()]


def reconcile_header(statement, page_texts: list[str]) -> list[str]:
    """Header fields must be printed in the document."""
    issues = []
    text = _squash(" ".join(page_texts))
    for field in ("account_number", "ifsc", "account_holder"):
        value = getattr(statement, field)
        if value and _squash(value) not in text:
            issues.append(f"header_{field}")
    all_amounts = set(_amounts_on(" ".join(_lines(page_texts))))
    for field in ("opening_balance", "closing_balance"):
        value = getattr(statement, field)
        if value is not None and abs(Decimal(value)) not in all_amounts:
            issues.append(f"header_{field}")
    dates = set()
    for token in " ".join(page_texts).split():
        try:
            d = parse_date(token)
            if d:
                dates.add(d)
        except ValueError:
            pass
    for field in ("period_from", "period_to"):
        value = getattr(statement, field)
        if value is not None and value not in dates:
            issues.append(f"header_{field}")
    return issues


def reconcile_rows(transactions, page_texts: list[str]) -> list[str]:
    """Every transaction must match its own date-led line of the text, in order."""
    lines = _lines(page_texts)
    starts = [i for i, ln in enumerate(lines) if _DATE_LED.match(ln)]
    if len(starts) != len(transactions):
        return ["row_count"]
    issues = []
    for k, (row, start) in enumerate(zip(transactions, starts, strict=True)):
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        block = [lines[start]]
        for ln in lines[start + 1:end]:            # continuation lines of a wrapped description
            if _NOT_ROW_TEXT.search(ln):
                break
            block.append(ln)
        first = lines[start]

        try:
            printed_date = parse_date(first.split()[0])
        except ValueError:
            printed_date = None
        if printed_date != row.date:
            issues.append(f"row_{k}_date")

        expected = sorted(abs(Decimal(v)) for v in (row.debit, row.credit, row.balance) if v is not None)
        if _amounts_on(first) != expected:
            issues.append(f"row_{k}_amounts")

        block_text = _squash(" ".join(block))
        if row.reference and _squash(row.reference) not in block_text:
            issues.append(f"row_{k}_reference")
        if row.description and _squash(row.description) not in block_text:
            issues.append(f"row_{k}_description")
    return issues


def reconcile(statement, page_texts: list[str]) -> list[str]:
    """All issues for a statement against its text layer. Empty list = fully reconciled."""
    return reconcile_header(statement, page_texts) + reconcile_rows(statement.transactions, page_texts)
