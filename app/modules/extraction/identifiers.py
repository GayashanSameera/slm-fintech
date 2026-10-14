"""Check-digit and structure rules for Indian identifiers (GSTIN, PAN, Aadhaar).

WHY THIS EXISTS (accuracy of scanned documents and non-statement types):
A scanned page has no text layer, so a misread character cannot be caught by comparing
with text. But three identifiers carry their OWN proof of correctness:

    * Aadhaar: the 12th digit is a Verhoeff check digit of the first 11.
    * GSTIN:   the 15th character is a base-36 check character of the first 14,
               characters 3-12 are the seller's PAN, and characters 1-2 are a state code.
    * PAN:     the 4th letter says what kind of holder it is (P person, C company, ...).

If the model reads one digit wrong (8 for 3, 0 for O ...) the check fails with a probability
of about 90% (Verhoeff catches ALL single-digit errors and all adjacent swaps), so the
ladder climbs to a stronger model instead of letting a wrong number pass. A GSTIN, PAN or
Aadhaar read in full and passing all these rules is wrong far less than once in a thousand.

Everything here is pure Python on strings: no AI, no network, no cost.
Functions return True/False and never raise, and nothing here ever logs a value.
"""

from __future__ import annotations

import re

# --- Aadhaar: Verhoeff checksum ----------------------------------------------------------

# The three Verhoeff tables (dihedral group D5). `_D` is the multiplication table, `_P` the
# position-dependent permutation, `_INV` the inverse. They are fixed by the algorithm.
_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6), (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4), (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2), (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
_INV = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def verhoeff_valid(number: str) -> bool:
    """True if `number` (digits only, check digit LAST) passes the Verhoeff check.

    Walk the digits from the right; at each step combine the running value `c` with the
    permuted digit using the multiplication table. A valid number ends with c == 0.
    """
    if not number.isdigit():
        return False
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def verhoeff_check_digit(payload: str) -> str:
    """The check digit to append to `payload` (digits). Used to build valid synthetic test data."""
    c = 0
    for i, ch in enumerate(reversed(payload)):
        c = _D[c][_P[(i + 1) % 8][int(ch)]]
    return str(_INV[c])


def aadhaar_valid(value: str) -> bool:
    """True for a 12-digit Aadhaar number (spaces/dashes allowed) with a valid Verhoeff digit.
    Aadhaar numbers never start with 0 or 1 (UIDAI rule), which is checked too."""
    digits = re.sub(r"[\s-]", "", value or "")
    return len(digits) == 12 and digits[0] not in "01" and verhoeff_valid(digits)


# --- GSTIN -----------------------------------------------------------------------------

_GST_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# State / union-territory codes in use (01-38) plus 97 (other territory) and 99 (centre).
_GST_STATES = {f"{n:02d}" for n in range(1, 39)} | {"97", "99"}


def gstin_check_char(first14: str) -> str:
    """The GSTIN check character for its first 14 characters (base-36 weighted sum).

    Each character's value (0-9, A=10 ... Z=35) is multiplied by 1 or 2 alternately (starting
    with 1); each product p contributes (p // 36) + (p % 36); the check value is what must be
    added to the total to reach a multiple of 36.
    """
    total = 0
    for i, ch in enumerate(first14):
        product = _GST_ALPHABET.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return _GST_ALPHABET[(36 - total % 36) % 36]


def gstin_problem(gstin: str, pan: str | None = None) -> str | None:
    """None if the GSTIN is internally consistent, else a short FIXED reason (never the value).

    Reasons: "state" (unknown state code), "checksum" (15th character wrong), "pan" (the PAN
    inside the GSTIN differs from the PAN printed next to it).
    Assumes the shape was already validated by checks.GSTIN_RE.
    """
    g = gstin.replace(" ", "").upper()
    if len(g) != 15 or any(ch not in _GST_ALPHABET for ch in g):
        return "format"
    if g[:2] not in _GST_STATES:
        return "state"
    if gstin_check_char(g[:14]) != g[14]:
        return "checksum"
    if pan and pan.replace(" ", "").upper() != g[2:12]:
        return "pan"
    return None


# --- PAN -------------------------------------------------------------------------------

# 4th letter = holder type: P person, C company, H HUF, F firm/LLP, A association of persons,
# T trust, B body of individuals, L local authority, J artificial juridical person, G government.
_PAN_HOLDER = set("PCHFATBLJG")


def pan_holder_valid(pan: str) -> bool:
    """True if the PAN's 4th letter is one of the ten holder types (shape checked elsewhere)."""
    p = pan.replace(" ", "").upper()
    return len(p) == 10 and p[3] in _PAN_HOLDER


def pan_matches_name(pan: str, surname_or_name: str | None) -> bool | None:
    """For a PERSON's PAN (4th letter P) the 5th letter is the initial of the surname.

    Returns True/False, or None when it can't be judged (not a personal PAN, no name). The caller
    treats only a definite False as a problem, because a name can be printed in either order
    (the initial of the first OR the last word is accepted).
    """
    p = pan.replace(" ", "").upper()
    if len(p) != 10 or p[3] != "P" or not surname_or_name:
        return None
    words = [w for w in re.split(r"[\s.]+", surname_or_name.upper()) if w]
    if not words:
        return None
    return p[4] in {words[0][0], words[-1][0]}
