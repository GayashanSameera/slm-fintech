"""Unit tests for masking of sensitive identifiers (FR-4, guide §6.1).

Masking is the LAST step before anything is stored or returned. These tests
pin down the rules:
  * Aadhaar is ALWAYS masked, wherever it sits, and no setting can turn that off;
  * PAN / account / ID / phone fields keep only the last 4 characters,
    unless the tenant is allowed that kind unmasked;
  * "strict" mode (identity/KYC documents, or Aadhaar markers on the page)
    masks ANY 12-digit Aadhaar-like number in ANY text field.
All numbers below are synthetic.
"""

from __future__ import annotations

import pytest

from app.modules.extraction.masking import has_aadhaar_markers, mask, mask_aadhaar, mask_last4


def _mask(result: dict, strict: bool = False, unmasked: set[str] | None = None) -> dict:
    """mask() with the given strict mode and tenant-unmasked kinds."""
    return mask(result, strict_aadhaar=strict, unmasked_kinds=unmasked or set())


@pytest.mark.parametrize("raw", ["1234 5678 9012", "1234-5678-9012", "123456789012"])
def test_aadhaar_field_always_masked(raw):
    # Even a tenant that is allowed EVERYTHING unmasked still gets Aadhaar masked (FR-4.1).
    out = _mask({"aadhaar_number": raw}, unmasked={"pan", "account", "id_number", "phone"})
    assert out["aadhaar_number"] == "XXXX XXXX 9012"


def test_aadhaar_inside_longer_text_in_strict_mode():
    text = "Address proof: Aadhaar 2345 6789 0123 issued 2019"
    out = _mask({"notes": text}, strict=True)
    assert out["notes"] == "Address proof: Aadhaar XXXX XXXX 0123 issued 2019"


def test_aadhaar_in_nested_lists_in_strict_mode():
    # Odd place: a transaction description deep in a list of dicts.
    result = {"transactions": [{"description": "KYC ref 3456 7890 1234"}]}
    out = _mask(result, strict=True)
    assert out["transactions"][0]["description"] == "KYC ref XXXX XXXX 1234"


def test_upi_reference_kept_when_not_strict():
    # Bank statements have 12-digit UPI references that tenants need: not masked
    # unless the document is identity/KYC or shows Aadhaar markers.
    result = {"transactions": [{"reference": "426512345678"}]}
    assert _mask(result, strict=False) == result


def test_longer_numbers_are_not_aadhaar():
    # 16-digit card-like numbers must not be half-masked as if they were Aadhaar.
    assert mask_aadhaar("1234567890123456") == "1234567890123456"


def test_pan_last4_by_default():
    assert _mask({"pan": "ABCDE1234F"})["pan"] == "XXXXXX234F"


def test_account_number_last4_by_default():
    assert _mask({"account_number": "001234567890"})["account_number"] == "XXXXXXXX7890"


def test_tenant_unmasked_kinds():
    # FR-4.4: this tenant may see PAN unmasked, but NOT account numbers.
    out = _mask({"pan": "ABCDE1234F", "account_number": "001234567890"}, unmasked={"pan"})
    assert out["pan"] == "ABCDE1234F"
    assert out["account_number"] == "XXXXXXXX7890"


def test_identity_document_masks_every_12_digit_number():
    # Identity documents run in strict mode: an Aadhaar number in id_number is masked
    # even when the tenant may see id_number unmasked.
    out = _mask({"id_number": "9876 5432 1098", "name": "Asha Verma"}, strict=True, unmasked={"id_number"})
    assert out["id_number"] == "XXXX XXXX 1098"
    assert out["name"] == "Asha Verma"


def test_non_strings_untouched_and_input_not_modified():
    original = {"pan": "ABCDE1234F", "page_count": 3, "items": [None, 1.5]}
    out = _mask(original)
    assert out["page_count"] == 3 and out["items"] == [None, 1.5]
    assert original["pan"] == "ABCDE1234F"          # mask() returns a copy


def test_mask_last4_short_values():
    assert mask_last4("12 34") == "1234"            # 4 chars or fewer: nothing to hide


@pytest.mark.parametrize("text, expected", [
    ("Government of India - Aadhaar", True),
    ("UIDAI enrolment", True),
    ("आधार", True),
    ("Statement of Account", False),
    (None, False),
])
def test_aadhaar_markers(text, expected):
    assert bool(has_aadhaar_markers(text)) is expected
