"""The accuracy layers inside the pipeline: grounding for digital documents, the second reading
for scans, and how both show up in the stored warnings."""

from __future__ import annotations

import json

from app.gateway.fake import FakeGateway
from app.modules.extraction.pipeline import extract_document, warning_codes
from app.modules.extraction.checks import CheckFailure
from app.modules.extraction.preprocess import Prepared
from app.modules.extraction.profiles import load_profile
from app.storage.tenants import TenantSettings

PROFILE = load_profile("default")
NOVA, HAIKU, SONNET = (r.model for r in PROFILE.ladder)

PAN_CARD = {"id_type": "pan", "id_number": "ABCPV1234F", "name": "ASHA VERMA", "date_of_birth": "1990-05-17"}
SCAN = [{"text": None, "image": b"img", "scanned": True}]
DIGITAL = [{"text": "INCOME TAX DEPARTMENT Permanent Account Number ABCPV1234F Name ASHA VERMA Date of Birth 17/05/1990",
            "image": None, "scanned": False}]


def _extract(pages, gw, **tenant):
    return extract_document(data=b"", prepared=Prepared(kind="png" if pages is SCAN else "docx", pages=pages),
                            requested_type="identity_document", tenant=TenantSettings(tenant_id="t", **tenant),
                            profile=PROFILE, gateway=gw, job_id="j")


def test_warning_codes_name_the_field_never_a_value():
    failures = [CheckFailure("text_grounding_failed", "msg", fields=("seller.gstin", "total")),
                CheckFailure("total_mismatch", "msg")]
    assert warning_codes(failures, ["name"]) == ["text_grounding_failed:seller.gstin", "text_grounding_failed:total",
                                                 "total_mismatch", "unverified_field:name"]


def test_scan_is_read_twice_by_different_models_and_agreement_is_silent():
    gw = FakeGateway().queue(json.dumps(PAN_CARD), json.dumps(PAN_CARD))
    out = _extract(SCAN, gw)
    assert [c["model_id"] for c in gw.calls] == [HAIKU, NOVA] and out.warnings == []


def test_scan_misread_that_the_arbiter_corrects():
    wrong = {**PAN_CARD, "name": "ASHA VARMA"}
    gw = FakeGateway().queue(json.dumps(wrong), json.dumps(PAN_CARD), json.dumps(PAN_CARD))
    out = _extract(SCAN, gw)
    assert out.result["name"] == "ASHA VERMA" and out.warnings == []
    assert [c["model_id"] for c in gw.calls] == [HAIKU, NOVA, SONNET]


def test_scan_with_no_majority_is_reported_as_unverified():
    gw = FakeGateway().queue(json.dumps(PAN_CARD), json.dumps({**PAN_CARD, "name": "ASHA VARMA"}),
                             json.dumps({**PAN_CARD, "name": "ASHNA VERMA"}))
    out = _extract(SCAN, gw)
    assert out.warnings == ["unverified_field:name"]


def test_digital_document_is_not_read_twice_but_is_grounded():
    gw = FakeGateway().queue(json.dumps(PAN_CARD))
    out = _extract(DIGITAL, gw)
    assert [c["model_id"] for c in gw.calls] == [HAIKU] and out.warnings == []


def test_digital_document_with_a_value_not_in_the_text_climbs_then_warns():
    invented = {**PAN_CARD, "name": "ASHA VARMA"}                # the text prints VERMA
    gw = FakeGateway().queue(json.dumps(invented), json.dumps(invented), json.dumps(invented))
    out = _extract(DIGITAL, gw)
    assert len(gw.calls) == 3 and out.warnings == ["text_grounding_failed:name"]


def test_checks_only_tenant_skips_the_second_reading():
    gw = FakeGateway().queue(json.dumps(PAN_CARD))
    out = _extract(SCAN, gw, verification_mode="checks_only")
    assert [c["model_id"] for c in gw.calls] == [HAIKU] and out.warnings == []


def test_no_second_opinion_possible_is_reported_never_silent():
    gw = FakeGateway().queue(json.dumps(PAN_CARD), "garbage", "garbage", "garbage", "garbage")
    out = _extract(SCAN, gw)
    assert out.warnings == ["unverified_field:crossread"]
