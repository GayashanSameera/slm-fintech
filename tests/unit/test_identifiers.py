"""Check digits and structure rules for Aadhaar, GSTIN and PAN (identifiers.py).

The key property: a SINGLE misread digit/character must always be caught. These are the
mistakes a model makes on a scanned page (3 for 8, 0 for O, 1 for 7).
"""

from __future__ import annotations

import pytest

from app.modules.extraction.identifiers import (
    aadhaar_valid, gstin_check_char, gstin_problem, pan_holder_valid, pan_matches_name,
    verhoeff_check_digit, verhoeff_valid,
)


def test_verhoeff_published_examples():
    # Examples from the algorithm's reference description: 236 -> 2363, 75872 -> 758722.
    assert verhoeff_check_digit("236") == "3" and verhoeff_valid("2363")
    assert verhoeff_check_digit("75872") == "2" and verhoeff_valid("758722")


def test_every_single_digit_error_in_an_aadhaar_is_caught():
    payload = "23456789012"
    number = payload + verhoeff_check_digit(payload)
    assert aadhaar_valid(number)
    for position in range(12):                       # change each digit to each other digit
        for wrong in "0123456789":
            if wrong == number[position]:
                continue
            mutated = number[:position] + wrong + number[position + 1:]
            if mutated[0] in "01":
                continue                              # rejected by the first-digit rule anyway
            assert not aadhaar_valid(mutated), (position, wrong)


def test_adjacent_swap_in_an_aadhaar_is_caught():
    payload = "23456789012"
    number = payload + verhoeff_check_digit(payload)
    swapped = number[:3] + number[4] + number[3] + number[5:]
    assert swapped != number and not aadhaar_valid(swapped)


def test_aadhaar_accepts_spaces_and_rejects_bad_shapes():
    payload = "23456789012"
    number = payload + verhoeff_check_digit(payload)
    assert aadhaar_valid(f"{number[:4]} {number[4:8]} {number[8:]}")
    assert not aadhaar_valid("1234 5678 9012")        # starts with 1
    assert not aadhaar_valid("12345")                  # too short
    assert not aadhaar_valid("abcd efgh ijkl")


def test_gstin_known_real_format_example():
    # 27AAPFU0939F1ZV is the sample GSTIN used in the official GST documentation.
    assert gstin_check_char("27AAPFU0939F1Z") == "V"
    assert gstin_problem("27AAPFU0939F1ZV") is None
    assert gstin_problem("27AAPFU0939F1ZV", pan="AAPFU0939F") is None


def test_every_single_character_error_in_a_gstin_is_caught():
    good = "27AAPFU0939F1ZV"
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for position in range(15):
        for wrong in alphabet:
            if wrong == good[position]:
                continue
            mutated = good[:position] + wrong + good[position + 1:]
            assert gstin_problem(mutated) is not None, (position, wrong)


def test_gstin_pan_must_match_the_pan_inside_it():
    assert gstin_problem("27AAPFU0939F1ZV", pan="AAPFU0939G") == "pan"


def test_gstin_unknown_state_code():
    assert gstin_problem("00AAPFU0939F1ZV") == "state"


def test_pan_holder_letter():
    assert pan_holder_valid("ABCPE1234F") and pan_holder_valid("ABCCE1234F")
    assert not pan_holder_valid("ABCDE1234F")          # D is not a holder type
    assert not pan_holder_valid("ABC")


def test_pan_fifth_letter_is_the_surname_initial_for_persons():
    assert pan_matches_name("ABCPV1234F", "Asha Verma") is True      # V = Verma
    assert pan_matches_name("ABCPA1234F", "Asha Verma") is True      # A = first word (name order varies)
    assert pan_matches_name("ABCPZ1234F", "Asha Verma") is False
    assert pan_matches_name("ABCCV1234F", "Asha Verma") is None      # company PAN: not judged
    assert pan_matches_name("ABCPV1234F", None) is None


@pytest.mark.parametrize("value", ["", "ABC", None])
def test_helpers_never_raise_on_garbage(value):
    assert gstin_problem(value or "") == "format"
    assert not aadhaar_valid(value or "")
