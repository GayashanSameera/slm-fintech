"""The extraction API end to end, with the fake model."""

import json

from app.gateway.base import ModelOutputInvalid, ModelUnavailable
from tests.conftest import fixture_bytes


def upload(client, name="payslip.pdf", document_type="payslip", content=None):
    data = content if content is not None else fixture_bytes(name)
    return client.post("/v1/extractions", data={"document_type": document_type}, files={"file": (name, data)})


def test_upload_returns_result_in_same_response(client, deps):
    res = upload(client)
    assert res.status_code == 200
    job = res.json()
    assert job["status"] == "succeeded"
    assert job["file_kind"] == "pdf" and job["pages"] == 1
    result = job["result"]
    # normalised
    assert result["pay_period_start"] == "2026-09-01"
    assert result["earnings"][1]["amount"] == "10000.00"
    # masked
    assert result["pan"] == "XXXXXX234F"
    # consistency check caught the wrong net pay
    assert any("net pay" in w for w in job["warnings"])


def test_files_written_to_data_folder(client, deps):
    job = upload(client).json()
    folder = deps.data_dir / "jobs" / job["id"]
    assert (folder / "input.pdf").read_bytes() == fixture_bytes("payslip.pdf")
    assert json.loads((folder / "result.json").read_text())["pan"] == "XXXXXX234F"
    assert json.loads((folder / "job.json").read_text())["status"] == "succeeded"
    # The original file name is not stored anywhere.
    assert "payslip.pdf" not in (folder / "job.json").read_text()


def test_model_receives_text_for_digital_pdf(client, model):
    upload(client)
    assert model.last_request.images == []
    assert "<document>" in model.last_request.user_prompt


def test_model_receives_image_for_photo(client, model):
    upload(client, name="payslip-photo.png")
    assert len(model.last_request.images) == 1


def test_get_list_and_delete(client, deps):
    job_id = upload(client).json()["id"]
    got = client.get(f"/v1/extractions/{job_id}").json()
    assert got["result"]["pan"] == "XXXXXX234F"
    listed = client.get("/v1/extractions").json()
    assert [j["id"] for j in listed] == [job_id] and listed[0]["result"] is None

    assert client.delete(f"/v1/extractions/{job_id}").status_code == 204
    assert not (deps.data_dir / "jobs" / job_id).exists()
    assert client.get(f"/v1/extractions/{job_id}").status_code == 404


def test_list_newest_first(client):
    first = upload(client).json()["id"]
    second = upload(client).json()["id"]
    assert [j["id"] for j in client.get("/v1/extractions").json()] == [second, first]


def test_path_traversal_id_is_not_found(client):
    assert client.get("/v1/extractions/..%2F..%2Fetc").status_code == 404
    assert client.get("/v1/extractions/job_../../x").status_code == 404


def test_unknown_document_type(client):
    res = upload(client, document_type="passport_photo")
    assert res.status_code == 400 and res.json()["error"]["code"] == "unknown_document_type"


def test_unsupported_file(client):
    res = upload(client, name="notes.txt", content=b"hello")
    assert res.status_code == 415 and res.json()["error"]["code"] == "unsupported_file_type"


def test_empty_file(client):
    assert upload(client, content=b"").json()["error"]["code"] == "empty_file"


def test_file_too_large(client, deps):
    deps.settings.max_upload_bytes = 100
    assert upload(client).status_code == 413


def test_model_down_records_failed_job(client, model):
    model.error = ModelUnavailable("The model server cannot be reached.")
    job = upload(client).json()
    assert job["status"] == "failed" and job["error"]["code"] == "model_unavailable"
    assert client.get(f"/v1/extractions/{job['id']}").json()["status"] == "failed"


def test_invalid_model_output_records_failed_job(client, model):
    model.error = ModelOutputInvalid("The model did not return valid JSON.")
    assert upload(client).json()["error"]["code"] == "model_output_invalid"


def test_unreadable_field_is_dropped_with_warning(client, model):
    model.answers["employer_name"] = {**model.answers["employer_name"], "pay_date": "31/02/2026"}
    job = upload(client).json()
    assert job["status"] == "succeeded"
    assert job["result"]["pay_date"] is None
    assert "Could not read pay_date; left empty." in job["warnings"]


def test_document_types(client):
    types = client.get("/v1/document-types").json()
    assert {t["name"] for t in types} == {"invoice", "bank_statement", "payslip", "identity_document", "kyc_form"}


def test_validation_error_does_not_echo_input(client):
    res = client.post("/v1/extractions", data={"document_type": "SECRET_VALUE"})  # missing file
    assert res.status_code == 422 and "SECRET_VALUE" not in res.text
