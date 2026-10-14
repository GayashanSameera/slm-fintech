"""Independent second reading and 2-of-3 voting (crossread.py), using the FakeGateway.

The fake plays three models. Each test queues what reader A (the ladder), reader B (Nova Lite)
and, if needed, reader C (Sonnet) "read", and checks the outcome: accepted, corrected by vote,
or reported as unverified.
"""

from __future__ import annotations

import json

from app.gateway.fake import FakeGateway
from app.modules.extraction.checks import run_checks
from app.modules.extraction.crossread import cross_read, disagreements, normalise, vote
from app.modules.extraction.profiles import load_profile
from app.modules.extraction.schemas import REGISTRY

PROFILE = load_profile("default")
NOVA, HAIKU, SONNET = (r.model for r in PROFILE.ladder)

PAGES = [{"text": None, "image": b"img", "scanned": True}]
BASE = {"name": "Asha Verma", "date_of_birth": "1990-05-17", "pan": "ABCPV1234F",
        "mobile": "9876543210", "email": "asha@example.com"}


def _kyc(**changes):
    return {**BASE, **changes}


def _run(a: dict, queued: list[dict | str], model_id: str = HAIKU):
    """Cross-read `a` (the ladder's answer, by `model_id`) with the queued readings."""
    gw = FakeGateway().queue(*[q if isinstance(q, str) else json.dumps(q) for q in queued])
    parsed = REGISTRY["kyc_form"].model.model_validate(a)
    result = cross_read(doc_type="kyc_form", pages=PAGES, parsed=parsed, model_id=model_id, gateway=gw,
                        readers=list(PROFILE.crossread_readers), arbiters=list(PROFILE.crossread_arbiters),
                        extra_check=lambda c: run_checks("kyc_form", c))
    return result, gw


def test_normalise_ignores_case_spacing_punctuation_and_empty_forms():
    assert normalise("Demo Traders Pvt. Ltd.") == normalise("DEMO TRADERS PVT LTD")
    assert normalise(None) == normalise("") == ""
    assert normalise(True) == "true"


def test_disagreements_names_paths_only():
    a = {"x": "1", "y": {"z": "same"}, "rows": [{"v": "1"}, {"v": "2"}]}
    b = {"x": "2", "y": {"z": "SAME"}, "rows": [{"v": "1"}, {"v": "3"}]}
    assert disagreements(a, b) == ["rows[1].v", "x"]      # keys are visited alphabetically
    assert disagreements({"rows": [1]}, {"rows": [1, 2]}) == ["rows[]"]


def test_agreement_costs_one_extra_call_and_changes_nothing():
    result, gw = _run(_kyc(), [_kyc()])
    assert result.status == "agreed" and result.unresolved == []
    assert [c["model_id"] for c in gw.calls] == [NOVA]            # a different model than the ladder's Haiku
    assert result.parsed.name == "Asha Verma"


def test_the_second_reader_is_never_the_model_that_made_the_first_answer():
    # The ladder's answer came from Nova itself: the independent reader must be Haiku.
    _, gw = _run(_kyc(), [_kyc()], model_id=NOVA)
    assert [c["model_id"] for c in gw.calls] == [HAIKU]


def test_a_misread_is_corrected_by_two_of_three():
    # A misread the mobile number; B and C (stronger) agree on the right one.
    right, wrong = _kyc(), _kyc(mobile="9876543219")
    result, gw = _run(wrong, [right, right])
    assert result.status == "voted" and result.unresolved == []
    assert result.parsed.mobile == "9876543210"                    # the majority value won
    assert [c["model_id"] for c in gw.calls] == [NOVA, SONNET]     # arbiter called only on disagreement


def test_the_original_wins_when_the_arbiter_sides_with_it():
    a, b = _kyc(), _kyc(name="Asha Varma")
    result, _ = _run(a, [b, a])
    assert result.parsed.name == "Asha Verma" and result.unresolved == []


def test_three_different_readings_are_reported_not_accepted_silently():
    a = _kyc(name="Asha Verma")
    result, _ = _run(a, [_kyc(name="Asha Varma"), _kyc(name="Ashna Verma")])
    assert result.unresolved == ["name"]                           # flagged: pipeline adds unverified_field:name
    assert result.parsed.name == "Asha Verma"                      # still A's value, but reported


def test_a_second_reader_with_unusable_output_falls_back_to_the_arbiter():
    # The second reader gets ONE retry (a formatting slip is cheap to ask again); then the arbiter steps in.
    result, gw = _run(_kyc(), ["not json at all", "still not json", _kyc()])
    assert result.status == "agreed"
    assert [c["model_id"] for c in gw.calls] == [NOVA, NOVA, SONNET]


def test_no_independent_opinion_is_reported_as_unavailable():
    result, _ = _run(_kyc(), ["garbage", "garbage", "garbage", "garbage"])
    assert result.status == "unavailable"


def test_a_vote_that_breaks_the_checks_is_not_used():
    # B and C agree on a PAN with an impossible holder letter (D): the merged answer would FAIL the
    # checks A passes, so A is kept and the dispute is reported.
    bad = _kyc(pan="ABCDV1234F")
    result, _ = _run(_kyc(), [bad, bad])
    assert result.parsed.pan == "ABCPV1234F"
    assert result.unresolved == ["pan"]


def test_vote_handles_lists_by_length_then_by_item():
    unresolved: list[str] = []
    a = {"rows": [{"v": "1"}, {"v": "2"}]}
    b = {"rows": [{"v": "1"}]}
    c = {"rows": [{"v": "1"}, {"v": "2"}]}
    assert vote([a, b, c], "", unresolved) == a and unresolved == []


# --- noise that must NOT trigger the expensive third reading ----------------------------------

def test_an_address_split_differently_is_the_same_address():
    a = {"current_address": {"line": "12 Park Street", "city": "Hyderabad", "state": "Telangana", "pincode": "500001"}}
    b = {"current_address": {"line": "12 Park Street, Hyderabad, Telangana 500001"}}
    assert disagreements(a, b) == []
    c = {"current_address": {"line": "12 Park Street", "city": "Hyderabad", "state": "Telangana", "pincode": "500002"}}
    assert disagreements(a, c) == ["current_address"]               # a different PIN code IS a disagreement


def test_an_address_is_voted_as_a_whole_never_mixed():
    unresolved: list[str] = []
    a = {"address": {"line": "1 A Road", "city": "Pune"}}
    b = {"address": {"line": "9 Z Lane", "city": "Pune"}}
    merged = vote([a, b, a], "", unresolved)
    assert merged["address"] == a["address"] and unresolved == []


def test_payslip_total_lines_in_the_second_reading_are_dropped_before_comparing():
    from app.modules.extraction.crossread import clean_reading
    reading = {"earnings": [{"name": "Basic", "amount": "100.00"}, {"name": "Gross pay", "amount": "100.00"}],
               "deductions": [{"name": "PF", "amount": "10.00"}, {"name": "Total deductions", "amount": "10.00"}]}
    cleaned = clean_reading("payslip", reading)
    assert [r["name"] for r in cleaned["earnings"]] == ["Basic"] and [r["name"] for r in cleaned["deductions"]] == ["PF"]


def test_interpretive_fields_are_not_compared():
    assert disagreements({"pay_period": "June 2026", "id_type": "pan"}, {"pay_period": "2026-06-01", "id_type": "other"}) == []
