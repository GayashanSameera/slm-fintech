"""Shared building blocks for document schemas.

Each document type (invoice, bank statement, ...) is a Pydantic model. Pydantic
does two jobs for us:
  1. VALIDATION: model output (or parser output) is checked field by field.
     Wrong shapes raise ValidationError, which moves the document one rung up
     the model ladder (architecture §3.2).
  2. NORMALISATION: the field types below convert common Indian formats into
     the platform's standard output formats:
       * dates   -> "YYYY-MM-DD"            (requirement FR-3.7)
       * amounts -> "11800.00" (a string)   (requirement FR-3.8)

`extra="ignore"` means fields the model invents are DROPPED, not stored: model
output can never add unexpected data to a result (security.md §7.2).

NOTE on error messages: validators raise ValueError with FIXED text and never
include the offending value (requirement NFR-S8).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer

# Date formats seen on Indian financial documents, tried in this order.
_DATE_FORMATS = [
    "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y",
    "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d %b %y", "%d %B %Y", "%b %d, %Y", "%d-%B-%Y",
]


def parse_date(value: Any) -> date | None:
    """Accept common Indian date formats; return a `date` or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError("unrecognised date format")   # no value in the message (NFR-S8)


# Characters/words to strip from amounts: currency symbols, thousands separators, spaces.
_MONEY_JUNK = re.compile(r"(₹|INR|Rs\.?|,|\s)", re.IGNORECASE)


def parse_money(value: Any) -> str | None:
    """Turn '₹1,18,000.50', 'Rs. 500', 11800 or '(250.00)' into a 2-decimal string."""
    if value is None or value == "":
        return None
    text = str(value).strip()
    negative = text.startswith("(") and text.endswith(")")   # accounting style negatives
    text = _MONEY_JUNK.sub("", text.strip("()"))
    if text.endswith(("Cr", "CR")):          # some statements suffix balances with Cr/Dr
        text = text[:-2]
    elif text.endswith(("Dr", "DR")):
        text, negative = text[:-2], True
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError("not an amount") from None
    if negative:
        amount = -amount
    # Decimal (never float) avoids rounding errors such as 0.1 + 0.2 != 0.3.
    return f"{amount.quantize(Decimal('0.01'))}"


def to_decimal(value: str | None) -> Decimal | None:
    """Helper for consistency checks: "11800.00" -> Decimal("11800.00")."""
    return Decimal(value) if value not in (None, "") else None


# Reusable annotated field types ---------------------------------------------------
# BeforeValidator runs our parser before Pydantic's own type check.
# PlainSerializer controls how the value is written back out to JSON.
IsoDate = Annotated[
    date | None,
    BeforeValidator(parse_date),
    PlainSerializer(lambda d: d.isoformat() if d else None, return_type=str | None),
]
Money = Annotated[str | None, BeforeValidator(parse_money)]
Text = Annotated[str | None, BeforeValidator(lambda v: (str(v).strip() or None) if v is not None else None)]


class DocBase(BaseModel):
    """Base class for every document schema."""
    model_config = ConfigDict(extra="ignore")


class Address(BaseModel):
    model_config = ConfigDict(extra="ignore")
    line: Text = None
    city: Text = None
    state: Text = None
    pincode: Text = None
