"""Deterministic reconciliation of an extracted bank statement with the PDF's own text.

WHY: statement rows are reconciled against real transactions downstream, so nothing may
pass wrongly. Arithmetic (checks.py) proves the amounts are CONSISTENT; this module proves
every row is what the DOCUMENT SAYS, field by field, with no AI involved.

HOW: the PDF text layer is read by pypdfium2 (preprocess/pdf.py), a DIFFERENT library
from the pdfplumber word positions the Rung 0 parsers use, so it is an independent
reading of the same document. The rows of that text are found with the same rule the
coverage check uses (lexicon.text_row_starts: a line that starts with a date, other than
a balance line or a period). Their number must equal the number of transactions, and for
every transaction, in order, its row of the text must show:
    * the same date;
    * exactly the same amounts (debit or credit, and balance; "0.00" counts as empty):
      nothing missing, nothing extra. Read in order from the date line, the row's amounts
      must all appear before any other amount (PDFium can put some of them on later lines;
      see _amounts_match);
    * every word of the description and of the reference, somewhere between the previous
      row and the next one (a description can wrap onto lines above and below the date).
Header fields (account number, IFSC, holder, branch, period, opening and closing balance)
must appear in the text too.

What the text layer can NOT show is which COLUMN an amount was printed in (debit or
credit): a swap is caught by the running-balance check instead. And which of two
neighbouring rows a wrapped description line belongs to: the generic parser declines
such layouts rather than guess (generic._assign_extras).

Used for:
  * Rung 0 parser results (parsers/__init__.py): any issue rejects the parser result;
  * AI results for DIGITAL PDFs (pipeline.py): issues count as failed checks, so the
    ladder climbs, and if they remain they are returned as warnings.
Scanned documents have no text layer, so this check can't run for them.

Issue codes name the field and row ("row_7_amounts"), never values (safe to log).
"""

from __future__ import annotations

from decimal import Decimal

from .lexicon import find_period, leading_date, parse_amount, squash, text_row_starts


def _amounts_on(tokens: list[str]) -> list[Decimal]:
    """The non-zero money amounts among some tokens, as positive Decimals (the text can't show
    the column, so the sign isn't compared). "0.00" is what many banks print in an empty column."""
    out = []
    for token in tokens:
        value = parse_amount(token)
        if value is not None and value != 0:
            out.append(abs(value))
    return out


def _lines(page_texts: list[str]) -> list[str]:
    """All non-empty text lines of all pages, in reading order."""
    return [ln.strip() for text in page_texts for ln in (text or "").splitlines() if ln.strip()]


def _hint(statement):
    """The statement's end date: it supplies the year of dates printed without one."""
    if statement.period_to:
        return statement.period_to
    dates = [t.date for t in statement.transactions if t.date]
    return max(dates) if dates else None


def _amounts_match(expected: list[Decimal], lines: list[str], start: int, end: int, skip: int) -> bool:
    """True if, reading the row's lines in order (from its date line, after the `skip` date
    tokens, up to the next row), its amounts appear EXACTLY: every expected amount, before any
    amount that isn't one of them.

    Banks lay rows out differently in the text layer: all amounts on the date line; the
    amounts on the next line; or (seen in a real statement) the balance on the date line,
    the wrapped description, then a line with the reference and the debit/credit. Stopping
    as soon as the row's amounts are complete keeps the next page's header, totals and
    footers out of the comparison; an amount that doesn't belong, a missing one or a misread
    one fails.
    """
    remaining = sorted(expected)
    if not remaining:
        return False
    for i in range(start, end):
        for amount in _amounts_on(lines[i].split()[skip:] if i == start else lines[i].split()):
            if amount not in remaining:
                return False                       # an amount this row doesn't have
            remaining.remove(amount)
            if not remaining:
                return True
    return False                                   # some of the row's amounts aren't printed


def reconcile_header(statement, page_texts: list[str]) -> list[str]:
    """Header fields must be printed in the document."""
    issues = []
    lines = _lines(page_texts)
    text = squash(" ".join(lines))
    for field in ("account_number", "ifsc", "account_holder", "branch"):
        value = getattr(statement, field)
        if value and squash(value) not in text:
            issues.append(f"header_{field}")
    all_amounts = {abs(v) for ln in lines for v in map(parse_amount, ln.split()) if v is not None}
    for field in ("opening_balance", "closing_balance"):
        value = getattr(statement, field)
        if value is not None and abs(Decimal(value)) not in all_amounts:
            issues.append(f"header_{field}")
    # The period: a printed date range, or (failing that) the two dates printed somewhere.
    if statement.period_from or statement.period_to:
        periods = {p for ln in lines if (p := find_period(ln))}
        hint = _hint(statement)
        dates = set()
        for ln in lines:
            tokens = ln.split()
            for i in range(len(tokens)):
                found = leading_date(tokens[i:i + 3], hint)
                if found:
                    dates.add(found[0])
        in_range = (statement.period_from, statement.period_to) in periods
        for field in ("period_from", "period_to"):
            value = getattr(statement, field)
            if value is not None and not in_range and value not in dates:
                issues.append(f"header_{field}")
    return issues


def reconcile_rows(statement, page_texts: list[str]) -> list[str]:
    """Every transaction must match its own row of the text, in order."""
    transactions = statement.transactions
    lines = _lines(page_texts)
    hint = _hint(statement)
    starts = text_row_starts(lines, hint)
    if len(starts) != len(transactions):
        return ["row_count"]
    issues = []
    for k, (row, start) in enumerate(zip(transactions, starts, strict=True)):
        previous = starts[k - 1] if k > 0 else -1
        following = starts[k + 1] if k + 1 < len(starts) else len(lines)

        tokens = lines[start].split()
        found = leading_date(tokens, hint)
        if found is None or found[0] != row.date:
            issues.append(f"row_{k}_date")

        expected = [abs(Decimal(v)) for v in (row.debit, row.credit, row.balance)
                    if v is not None and Decimal(v) != 0]
        if not _amounts_match(expected, lines, start, following, found[1] if found else 0):
            issues.append(f"row_{k}_amounts")

        # Words: each word of the description / reference between the previous and next rows.
        window = squash(" ".join(lines[previous + 1:following]))
        for field in ("description", "reference"):
            value = getattr(row, field)
            if value and any(squash(word) not in window for word in value.split()):
                issues.append(f"row_{k}_{field}")
    return issues


def reconcile(statement, page_texts: list[str]) -> list[str]:
    """All issues for a statement against its text layer. Empty list = fully reconciled."""
    return reconcile_header(statement, page_texts) + reconcile_rows(statement, page_texts)
