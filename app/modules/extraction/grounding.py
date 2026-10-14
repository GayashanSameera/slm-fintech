"""Text grounding: every value the AI returned must be PRINTED in the document's text layer.

WHY: bank statements are proven row by row against the PDF text (parsers/reconcile.py). The
other document types (invoice, payslip, identity document, KYC form) had no such proof, so a
model that "helpfully" corrected, completed or invented a value could pass the arithmetic
checks. This module closes that gap for every DIGITAL document (PDF with a text layer, DOCX):

    for each value in the result:   is it in the text the document itself prints?

HOW (all deterministic, no AI):
    * money   "17700.00" must appear as a number in the text: 17,700.00 / 17700 / 1,77,00.00 ...
              (digits compared as Decimals, so thousands separators and trailing zeros don't matter);
    * dates   "2026-09-07" must appear in ANY common printed form: 07/09/2026, 7-9-26, 07 Sep 2026,
              September 7, 2026, 7th September 2026 ...;
    * text    every WORD of the value must appear in the text (case, spacing and punctuation are
              ignored), so a wrapped or re-spaced name still matches but an invented or
              "corrected" one does not.

What this proves: the value exists in the document. What it does not prove: that it is in the
RIGHT field (a buyer's GSTIN under "seller"). That is what the independent second reading in
crossread.py is for. Scanned pages have no text layer, so this check cannot run for them
(pipeline._digital); they rely on the identifier check digits (identifiers.py) and crossread.py.

Field PATHS (e.g. "seller.gstin", "line_items[2].amount") are returned, never values, so the
result is safe to log and to show as a warning.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

# Fields whose value is a choice or a paraphrase rather than printed text: "M" for "Male", a
# schema enum, a yes/no declaration. They are checked by the schema, not against the text.
SKIP_FIELDS = {"id_type", "gender", "pep_status", "currency"}

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONEY = re.compile(r"^-?\d+\.\d{2}$")                    # parse_money always gives 2 decimals
_NUMBER_IN_TEXT = re.compile(r"\d[\d,]*(?:\.\d+)?")        # 1,18,000.50 | 17700 | 9.5
_ORDINAL = re.compile(r"(?<=\d)(?:st|nd|rd|th)\b", re.IGNORECASE)

_MONTHS_SHORT = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MONTHS_LONG = ["january", "february", "march", "april", "may", "june", "july", "august",
                "september", "october", "november", "december"]


def _squash(text: str) -> str:
    """Case-folded, whitespace-free, punctuation-free form used to compare words."""
    return re.sub(r"[\W_]+", "", str(text or "").casefold())


def _numbers_in(text: str) -> set[Decimal]:
    """Every number printed in the text, as a Decimal (commas removed). 0.00 and 17700 both work."""
    found: set[Decimal] = set()
    for token in _NUMBER_IN_TEXT.findall(text):
        try:
            found.add(Decimal(token.replace(",", "")).normalize())
        except InvalidOperation:
            continue
    return found


def _date_forms(d: date) -> set[str]:
    """All the ways a date is commonly printed, already squashed (no spaces, lower case).
    Day and month appear both zero-padded and plain; years in 4 and 2 digits."""
    days = {str(d.day), f"{d.day:02d}"}
    months = {str(d.month), f"{d.month:02d}"}
    years = {str(d.year), f"{d.year % 100:02d}"}
    forms: set[str] = set()
    for dd in days:
        for mm in months:
            for yy in years:
                for sep in ("/", "-", ".", ""):
                    if sep == "" and len(yy) == 2:
                        continue                    # "70926" would match unrelated numbers
                    forms.add(_squash_keep_digits(f"{dd}{sep}{mm}{sep}{yy}"))
    for mon in (_MONTHS_SHORT[d.month - 1], _MONTHS_LONG[d.month - 1]):
        for dd in days:
            for yy in years:
                forms.add(f"{dd}{mon}{yy}")         # 07 Sep 2026 | 7 September 26 (squashed)
                forms.add(f"{mon}{dd}{yy}")         # Sep 7, 2026
    forms.add(f"{d.year}{d.month:02d}{d.day:02d}")  # 20260907
    forms.add(f"{d.year}-{d.month:02d}-{d.day:02d}")
    return forms


def _squash_keep_digits(text: str) -> str:
    """Like _squash but keeps '/', '-' and '.' between digits, which separate day/month/year."""
    return re.sub(r"\s+", "", text.casefold())


def _text_forms(page_texts: list[str]) -> tuple[str, str, set[Decimal]]:
    """The document's text in the three comparable forms: (words squashed, dates squashed, numbers)."""
    joined = "\n".join(t or "" for t in page_texts)
    joined = _ORDINAL.sub("", joined)                   # "7th September" -> "7 September"
    dates_form = re.sub(r"[\s,]+", "", joined.casefold())   # keeps / - . for numeric dates
    return _squash(joined), dates_form, _numbers_in(joined)


def _walk(value, path: str):
    """Yield (path, field_name, leaf) for every non-empty leaf of a result dict."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")
    elif value not in (None, ""):
        yield path, path.rsplit(".", 1)[-1].split("[")[0], value


def ungrounded_fields(result: dict, page_texts: list[str]) -> list[str]:
    """Paths of values that do NOT appear in the document text. [] means everything is grounded.

    `result` is the validated model dumped to JSON (dates as ISO text, money as "123.45").
    """
    words, dates_form, numbers = _text_forms(page_texts)
    absolute_numbers = {n.copy_abs() for n in numbers}     # a printed "(250.00)" or "-250.00" grounds 250.00
    missing: list[str] = []
    for path, field, leaf in _walk(result, ""):
        if field in SKIP_FIELDS or isinstance(leaf, bool):
            continue
        text = str(leaf)
        if _MONEY.match(text):
            if Decimal(text).copy_abs().normalize() not in absolute_numbers:
                missing.append(path)
        elif _ISO_DATE.match(text):
            try:
                forms = _date_forms(date.fromisoformat(text))
            except ValueError:
                missing.append(path)
                continue
            if not any(form in dates_form for form in forms):
                missing.append(path)
        else:
            tokens = [_squash(w) for w in text.split()]
            if any(t and t not in words for t in tokens):
                missing.append(path)
    return missing
