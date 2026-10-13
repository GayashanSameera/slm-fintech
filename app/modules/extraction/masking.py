"""Masking of sensitive numbers (requirements FR-4).

Runs on the validated result BEFORE it is saved to result.json or returned, so the full
numbers are never stored.

Rules:
    aadhaar                         → "XXXX XXXX 1234"   ALWAYS. UIDAI rules forbid storing a
                                                         full Aadhaar number without an
                                                         Aadhaar Data Vault. No setting can
                                                         turn this off.
    pan, account, id_number, phone  → last 4 characters kept, e.g. "XXXXXX234F",
                                      unless that kind is listed in SLM_UNMASK_FIELDS
                                      (FR-4.4).

Which fields are sensitive is declared per document type in schemas/__init__.py.
For identity and KYC documents we also scan EVERY text value for Aadhaar-like numbers
(12 digits, first digit 2-9), in case one appears in an unexpected field such as an address.
"""

import copy
import re

from app.modules.extraction.schemas import DocumentType

AADHAAR_PATTERN = re.compile(r"\b[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}\b")
NEVER_UNMASK = frozenset({"aadhaar"})


def keep_last_4(value: str) -> str:
    chars = re.sub(r"[^0-9A-Za-z]", "", value)
    if len(chars) <= 4:
        return "X" * len(chars)
    return "X" * (len(chars) - 4) + chars[-4:]


def mask_aadhaar(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return f"XXXX XXXX {digits[-4:]}" if len(digits) >= 4 else "XXXX XXXX XXXX"


MASKERS = {
    "aadhaar": mask_aadhaar,
    "pan": keep_last_4,
    "account": keep_last_4,
    "id_number": keep_last_4,
    "phone": keep_last_4,
}


def _get(data: dict, path: str):
    node = data
    for key in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _set(data: dict, path: str, value) -> None:
    *parents, last = path.split(".")
    node = data
    for key in parents:
        node = node.get(key) if isinstance(node, dict) else None
    if isinstance(node, dict) and last in node:
        node[last] = value


def _scan(value):
    """Masks Aadhaar-like numbers in every string, at any depth."""
    if isinstance(value, str):
        return AADHAAR_PATTERN.sub(lambda m: mask_aadhaar(m.group(0)), value)
    if isinstance(value, list):
        return [_scan(v) for v in value]
    if isinstance(value, dict):
        return {k: _scan(v) for k, v in value.items()}
    return value


def mask_result(doc_type: DocumentType, data: dict, unmask: set[str] | frozenset[str] = frozenset()) -> dict:
    """Returns a masked COPY of `data` (the JSON form of a validated document)."""
    out = copy.deepcopy(data)
    for path, kind in doc_type.sensitive.items():
        value = _get(out, path)
        if not isinstance(value, str) or not value:
            continue
        # An identity document's number is an Aadhaar number when id_type says so.
        if doc_type.name == "identity_document" and path == "document_number" and out.get("id_type") == "aadhaar":
            kind = "aadhaar"
        if kind in NEVER_UNMASK or kind not in unmask:
            _set(out, path, MASKERS[kind](value))
    if doc_type.aadhaar_scan:
        out = _scan(out)
    return out
