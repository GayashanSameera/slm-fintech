"""Small helpers for DynamoDB data conversion.

DynamoDB has no float type: numbers come back as `decimal.Decimal`, and floats
cannot be written at all. These helpers convert in both directions so the rest
of the code can work with normal Python ints and strings.

Money values are always stored as STRINGS like "11800.00" (requirement FR-3.8),
so no rounding ever happens in storage.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def from_ddb(value: Any) -> Any:
    """Recursively turn DynamoDB values into plain JSON-friendly Python values."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: from_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_ddb(v) for v in value]
    if isinstance(value, set):
        return sorted(from_ddb(v) for v in value)
    return value


def to_ddb(value: Any) -> Any:
    """Recursively make a value storable: floats -> Decimal, drop empty strings in sets."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_ddb(v) for v in value]
    return value
