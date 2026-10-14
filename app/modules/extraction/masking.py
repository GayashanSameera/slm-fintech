"""Masking of sensitive identifiers (requirement FR-4).

Runs as the LAST step before a result is stored or returned, on every path
(Rung 0 parser and AI). Nothing unmasked is ever written anywhere.

Rules:
  FR-4.1  Aadhaar numbers -> "XXXX XXXX 1234". ALWAYS. No setting can disable it.
  FR-4.2  For identity/KYC documents, OR any document showing Aadhaar markers,
          ANY 12-digit Aadhaar-like number in ANY string field is masked.
  FR-4.3  PAN, bank account, UAN, ID numbers, phone numbers -> last 4 characters
          visible ("XXXXXX234F"), unless...
  FR-4.4  ...the tenant is allowed that KIND unmasked (pan, account, id_number, phone).

Why not mask every 12-digit number everywhere? Bank statements contain 12-digit
UPI reference numbers that tenants need. So the strict rule applies only to
identity/KYC documents, or when Aadhaar markers are present.
"""

from __future__ import annotations

import re
from typing import Any

# 12 digits, optionally grouped 4-4-4 with spaces or hyphens, not part of a longer number.
AADHAAR_RE = re.compile(r"(?<!\d)(\d{4})[ -]?(\d{4})[ -]?(\d{4})(?!\d)")
AADHAAR_MARKERS = re.compile(r"aadhaar|aadhar|आधार|uidai|\bVID\b", re.IGNORECASE)

# Field NAME -> kind of identifier. Matching is on the last part of the field path.
FIELD_KINDS: dict[str, str] = {
    "pan": "pan",
    "account_number": "account",
    "bank_account_number": "account",
    "uan": "id_number",
    "id_number": "id_number",
    "ckyc_number": "id_number",
    "mobile": "phone",
    "phone": "phone",
}
AADHAAR_FIELDS = {"aadhaar", "aadhaar_number"}


def mask_aadhaar(value: str) -> str:
    """'1234 5678 9012' -> 'XXXX XXXX 9012' (also inside longer text)."""
    return AADHAAR_RE.sub(lambda m: f"XXXX XXXX {m.group(3)}", value)


def mask_last4(value: str) -> str:
    """'ABCDE1234F' -> 'XXXXXX234F'. Spaces are removed first so length stays honest."""
    compact = re.sub(r"\s", "", value)
    if len(compact) <= 4:
        return compact
    return "X" * (len(compact) - 4) + compact[-4:]


def has_aadhaar_markers(*texts: str | None) -> bool:
    """True if any of the texts mentions Aadhaar (aadhaar, aadhar, आधार, UIDAI, VID)."""
    return any(t and AADHAAR_MARKERS.search(t) for t in texts)


def mask(result: dict, *, strict_aadhaar: bool, unmasked_kinds: set[str]) -> dict:
    """Return a masked COPY of `result` (a plain dict from model_dump)."""

    def walk(value: Any, field: str | None) -> Any:
        """Recursively mask one value; `field` is the name of the field it belongs to."""
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, field) for v in value]
        if not isinstance(value, str):
            return value

        # 1. Aadhaar fields: always masked (FR-4.1).
        if field in AADHAAR_FIELDS:
            masked = mask_aadhaar(value)
            return masked if masked != value else mask_last4(value)
        # 2. Identifier fields: last 4 unless the tenant allows that kind (FR-4.3/4.4).
        kind = FIELD_KINDS.get(field or "")
        if kind and kind not in unmasked_kinds:
            value = mask_last4(value)
        # 3. Strict mode: any Aadhaar-like number in any text (FR-4.2).
        if strict_aadhaar:
            value = mask_aadhaar(value)
        return value

    return walk(result, None)
