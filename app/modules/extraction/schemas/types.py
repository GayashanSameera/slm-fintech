"""Field types shared by all document schemas.

Money
    Amounts are Python Decimal, never float: 0.1 + 0.2 is 0.30000000000000004 in floating
    point, which is unacceptable for money. In the JSON we RETURN they are strings
    ("11800.00"), so no client can lose precision either (FR-3.8).
    The MODEL is asked for plain numbers (simpler for it), and the parser below also
    accepts what models and documents often produce anyway: "₹11,800.00", "Rs. 500",
    "(1,000.00)" for a negative amount.

DocDate
    Returned as "YYYY-MM-DD". The model is asked for that format, and the parser also
    accepts common Indian formats such as 30/09/2026 or 30-Sep-2026 (day first) (FR-3.7).

Each type has two faces:
    - BeforeValidator: how a raw value from the model is cleaned up and checked
    - WithJsonSchema:  what the model is told the field looks like
"""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, WithJsonSchema

# ---------- Amounts ----------

_CURRENCY = re.compile(r"(₹|rs\.?|inr|\$)", re.IGNORECASE)


def parse_amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):  # bool is a subclass of int in Python; reject it explicitly
        raise ValueError("expected an amount")
    if isinstance(value, int | float):
        # str() first: Decimal(0.1) would keep float's binary error, Decimal("0.1") does not.
        return Decimal(str(value))
    text = _CURRENCY.sub("", str(value)).replace(",", "").replace(" ", "").strip()
    negative = text.startswith("(") and text.endswith(")")  # accounting style: (1000.00)
    text = text.strip("()")
    if text in ("", "-"):
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("not a valid amount") from exc
    return -amount if negative else amount


Money = Annotated[
    Decimal | None,
    BeforeValidator(parse_amount),
    WithJsonSchema({"type": ["number", "null"]}),
]

# A plain number that is not money (quantity, tax rate, days paid). Same parsing.
Number = Money


# ---------- Dates ----------

# Tried in order. Day comes before month, as in India.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d %b %Y",
    "%d-%b-%Y",
    "%d %B %Y",
    "%d-%B-%Y",
    "%b %d, %Y",
    "%d/%m/%y",
    "%d-%m-%y",
    "%d-%b-%y",
)


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = " ".join(str(value).split())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007 - a calendar date, no time zone
        except ValueError:
            continue
    raise ValueError("not a recognised date")


DocDate = Annotated[
    date | None,
    BeforeValidator(parse_date),
    WithJsonSchema({"type": ["string", "null"], "description": "Date as YYYY-MM-DD"}),
]


# ---------- Text ----------


def _clean_text(value: Any) -> str | None:
    """Trims whitespace; turns empty strings into null; accepts numbers (e.g. an account
    number the model returned as 123456) by converting them to text."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("expected text")
    text = " ".join(str(value).split())
    return text or None


Text = Annotated[str | None, BeforeValidator(_clean_text)]


def text(description: str | None = None):
    """Shorthand for an optional text field with a description for the model."""
    return Field(default=None, description=description)
