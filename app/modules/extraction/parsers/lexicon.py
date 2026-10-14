"""Shared reading rules for bank statements: dates, amounts, statement periods, special lines.

Three modules must agree EXACTLY on what a transaction line looks like:
  * generic.py    reads the rows from pdfplumber word positions;
  * coverage.py   counts the rows in the PDF text layer (did the parser miss any?);
  * reconcile.py  compares every extracted row with the PDF text layer.
If they used different rules, a correct statement could be rejected (or a wrong one
accepted), so the rules live here, once.

What real statements print (all seen in sample statements):
  dates     01/08/2026  01-08-2026  01.08.2026  01/08/26  01-Aug-2026  01 Aug 2026
            01 August 2026  1st Aug 2026  01 Aug (no year)  01/08 (no year)  01 août 2026
  amounts   1,245.55  1,18,000.50 (Indian grouping)  10.481,44 (European)  (11,452.30)
            -111,060.78  9,750.00Cr  1245.55
  empty     "—", "-", "–" or "0.00" in an amount column
  periods   "01/10/2026 to 31/10/2026"  "01 Aug – 31 Aug 2026"  "01–31 Aug 2026"
            "01 August 2026 – 31 August 2026"  "01–31 août 2026"

Dates are always read DAY first (dd/mm): the platform serves Indian and Sri Lankan banks,
which never print month first. A date without a year takes it from the statement period.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

# --- text normalisation -----------------------------------------------------------------

def fold(text: str) -> str:
    """Lower case without accents: "Débit" -> "debit", "août" -> "aout", "Libellé" -> "libelle".
    Used to compare words with the lists below, never to change what is extracted."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def squash(text) -> str:
    """Comparable text: no whitespace, case-folded (used by the text reconciliation)."""
    return re.sub(r"\s+", "", str(text or "")).casefold()


# --- months -------------------------------------------------------------------------------
# English, French and Spanish names and abbreviations, accent-folded (see fold()).
MONTHS: dict[str, int] = {}
for _number, _names in enumerate([
    ["jan", "january", "janv", "janvier", "ene", "enero"],
    ["feb", "february", "fev", "fevr", "fevrier", "febrero"],
    ["mar", "march", "mars", "marzo"],
    ["apr", "april", "avr", "avril", "abr", "abril"],
    ["may", "mai", "mayo"],
    ["jun", "june", "juin", "junio"],
    ["jul", "july", "juil", "juillet", "julio"],
    ["aug", "august", "aout", "ago", "agosto"],
    ["sep", "sept", "september", "septembre", "septiembre"],
    ["oct", "october", "octobre", "octubre"],
    ["nov", "november", "novembre", "noviembre"],
    ["dec", "december", "decembre", "dic", "diciembre"],
], start=1):
    for _name in _names:
        MONTHS[_name] = _number
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))

# --- amounts -------------------------------------------------------------------------------
# A money token. The LAST "." or "," followed by exactly two digits is the decimal mark;
# any other "." or "," separates thousands. So "1,234.50", "1.234,50" and "1,18,000.50" are all
# unambiguous without knowing the country. A bare "1234" (no decimals) is NOT money: it could
# be a cheque number.
# Thousands groups: Western "1,234,567" or Indian "12,34,567" (pairs, then a final three).
MONEY_RE = re.compile(r"^(\(?)([-+]?)(\d{1,3}(?:[,.]\d{2})*(?:[,.]\d{3})+|\d+)([.,])(\d{2})(\)?)(Cr|Dr|CR|DR|cr|dr)?$")
# Tokens printed for "nothing in this column".
EMPTY_CELL = {"—", "–", "-", "--", "−", "nil", "n/a"}


def is_money(token: str) -> bool:
    return bool(MONEY_RE.match(token or ""))


def parse_amount(token: str) -> Decimal | None:
    """'1,245.55' -> 1245.55, '10.481,44' -> 10481.44, '(250.00)' -> -250.00, '9.00Dr' -> -9.00.
    None if the token isn't a money token."""
    m = MONEY_RE.match(token or "")
    if not m:
        return None
    paren_open, sign, whole, _mark, cents, paren_close, suffix = m.groups()
    digits = re.sub(r"[,.\s]", "", whole)
    try:
        value = Decimal(f"{digits}.{cents}")
    except InvalidOperation:
        return None
    if (paren_open and paren_close) or sign == "-" or (suffix and suffix.lower() == "dr"):
        value = -value
    return value


def money_marker(token: str) -> str | None:
    """'Cr' / 'Dr' suffix of a money token, lower case, or None."""
    m = MONEY_RE.match(token or "")
    return m.group(7).lower() if m and m.group(7) else None


def as_text(value: Decimal | None) -> str | None:
    """Decimal -> the platform's amount format ("1245.55"), keeping the sign."""
    return None if value is None else f"{value.quantize(Decimal('0.01'))}"


# --- dates -----------------------------------------------------------------------------------
_NUM_DATE = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2}|\d{4}))?$")
_NAME_DATE_ONE_TOKEN = re.compile(rf"^(\d{{1,2}})(?:st|nd|rd|th)?[\-/.]?({_MONTH_ALT})\.?[\-/.,]?(\d{{2}}|\d{{4}})?$")
_DAY = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?[,.]?$")
_YEAR = re.compile(r"^(\d{4}|\d{2})[,.]?$")
_TIME = re.compile(r"^\d{1,2}:\d{2}")


def _year(text: str | None, hint: int | None) -> int | None:
    if not text:
        return hint
    y = int(text)
    return y + 2000 if y < 100 else y


def _make(day: int, month: int, year: int | None, hint_year: int | None, hint_month: int | None) -> date | None:
    """Build a date; a missing year comes from the statement period (`hint_year`). For a
    period that crosses a new year (Dec -> Jan), a month after the period's end month belongs
    to the previous year."""
    if year is None:
        if hint_year is None:
            return None
        year = hint_year - 1 if (hint_month is not None and month > hint_month) else hint_year
    try:
        return date(year, month, day)
    except ValueError:
        return None


def leading_date(tokens: list[str], hint: date | None = None) -> tuple[date, int] | None:
    """If the tokens START with a date, return (the date, how many tokens it used).

    `hint` is the statement's end date: it supplies the year for dates printed without one.
    Accepted: "01/08/2026", "01-08-26", "01.08.2026", "01/08", "01-Aug-2026", "01Aug2026",
    "01 Aug 2026", "01 August, 2026", "1st Aug 2026", "01 Aug", "01 août 2026".
    A date followed by a time ("14/10/2026 10:22") is a print timestamp, not a row.
    """
    if not tokens:
        return None
    hy, hm = (hint.year, hint.month) if hint else (None, None)
    first = fold(tokens[0]).strip(",")
    result: tuple[date, int] | None = None

    m = _NUM_DATE.match(first)
    if m:
        d = _make(int(m.group(1)), int(m.group(2)), _year(m.group(3), None), hy, hm)
        result = (d, 1) if d else None
    else:
        m = _NAME_DATE_ONE_TOKEN.match(first)
        if m:
            d = _make(int(m.group(1)), MONTHS[m.group(2)], _year(m.group(3), None), hy, hm)
            result = (d, 1) if d else None
        else:
            m = _DAY.match(first)
            if m and len(tokens) >= 2:
                month = MONTHS.get(fold(tokens[1]).strip(".,"))
                if month:
                    year_token = tokens[2] if len(tokens) >= 3 else ""
                    ym = _YEAR.match(year_token)
                    used = 3 if ym else 2
                    d = _make(int(m.group(1)), month, _year(ym.group(1), None) if ym else None, hy, hm)
                    result = (d, used) if d else None
    if result and len(tokens) > result[1] and _TIME.match(tokens[result[1]]):
        return None                                    # "14/10/2026 10:22": a timestamp
    return result


def parse_any_date(text: str, hint: date | None = None) -> date | None:
    """A whole string that is one date (e.g. a value-date cell), or None."""
    tokens = (text or "").split()
    found = leading_date(tokens, hint)
    return found[0] if found and found[1] == len(tokens) else None


# --- statement period -----------------------------------------------------------------------
_SEP = r"\s*(?:-|–|—|to|till|until|through|au|al|a|hasta)\s*"
_NUMERIC_RANGE = re.compile(rf"(\d{{1,2}}[/.\-]\d{{1,2}}[/.\-]\d{{2,4}}){_SEP}(\d{{1,2}}[/.\-]\d{{1,2}}[/.\-]\d{{2,4}})")
_NAMED_RANGE = re.compile(
    rf"(\d{{1,2}})(?:st|nd|rd|th)?[\s\-]*(?:({_MONTH_ALT})\.?[\s\-,]*(\d{{4}})?)?{_SEP}"
    rf"(\d{{1,2}})(?:st|nd|rd|th)?[\s\-]*({_MONTH_ALT})\.?[\s\-,]*(\d{{4}})")
_MONTH_ONLY = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{4}})\b")


def find_period(text: str, allow_month_only: bool = False) -> tuple[date, date] | None:
    """The first statement period printed in `text`, as (from, to), or None.

    "01/10/2026 to 31/10/2026", "01 Aug – 31 Aug 2026", "01–31 Aug 2026", "01 August 2026 –
    31 August 2026". With `allow_month_only` (only for text right next to a "Period" label),
    "August 2026" means the whole month.
    """
    folded = fold(text or "")
    m = _NUMERIC_RANGE.search(folded)
    if m:
        a, b = parse_any_date(m.group(1)), parse_any_date(m.group(2))
        if a and b and a <= b:
            return a, b
    m = _NAMED_RANGE.search(folded)
    if m:
        d1, mon1, y1, d2, mon2, y2 = m.groups()
        month2 = MONTHS[mon2]
        month1 = MONTHS[mon1] if mon1 else month2
        year2 = int(y2)
        year1 = int(y1) if y1 else (year2 - 1 if month1 > month2 else year2)
        a, b = _make(int(d1), month1, year1, None, None), _make(int(d2), month2, year2, None, None)
        if a and b and a <= b:
            return a, b
    if allow_month_only:
        m = _MONTH_ONLY.search(folded)
        if m:
            import calendar
            month, year = MONTHS[m.group(1)], int(m.group(2))
            return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    return None


# --- special lines ----------------------------------------------------------------------------
# A line (or the text after a row's date) that carries a balance, not a transaction.
OPENING_LINE = re.compile(r"(?i)^(b\s*/\s*f\b|balance\s+b/?f|brought\s+forward|opening\s+balance|balance\s+forward|"
                          r"solde\s+(initial|precedent|reporte)|saldo\s+(inicial|anterior))")
CLOSING_LINE = re.compile(r"(?i)^(c\s*/\s*f\b|carried\s+forward|closing\s+balance|solde\s+final|saldo\s+final)")
# The line that ends a transaction table.
TABLE_END = re.compile(r"(?i)^(total\b|totals\b|grand\s+total|closing\s+balance|transaction\s+summary|"
                       r"end\s+of\s+statement|\*+\s*end|statement\s+summary)")


def is_balance_line(text_after_date: str) -> bool:
    """True for "Opening Balance 9.99", "B/F 9.99", "Closing Balance ...": not transactions."""
    t = fold(text_after_date).strip()
    return bool(OPENING_LINE.match(t) or CLOSING_LINE.match(t))


def text_row_starts(lines: list[str], hint: date | None) -> list[int]:
    """Indexes of the lines of a PDF TEXT LAYER that start a transaction row.

    A row starts with a date. Not counted:
      * balance lines ("01/08/2026 Opening Balance ...");
      * date ranges ("01 Aug – 31 Aug 2026" at the start of a line is a period, not a row);
      * the second half of a range that wrapped inside a description ("Int. Pd:01-08-2026 to"
        then "31-08-2026" on the next line);
      * timestamps (handled by leading_date).
    Used by coverage.py and reconcile.py, so both count rows the same way.
    """
    starts = []
    for i, line in enumerate(lines):
        tokens = line.split()
        found = leading_date(tokens, hint)
        if not found:
            continue
        rest = " ".join(tokens[found[1]:])
        if is_balance_line(rest) or _starts_with_range(line):
            continue
        if i > 0 and _RANGE_TAIL.search(fold(lines[i - 1])):
            continue
        starts.append(i)
    return starts


# A line ending in a range word: the date on the next line completes the range.
_RANGE_TAIL = re.compile(r"(?:\bto|\btill|\buntil|\bau|\bal|-|–|—)\s*$")


def _starts_with_range(line: str) -> bool:
    """True if the line BEGINS with a date range such as "01 Aug – 31 Aug 2026"."""
    folded = fold(line)
    for rx in (_NUMERIC_RANGE, _NAMED_RANGE):
        if rx.match(folded.strip()):
            return True
    return False
