"""End-to-end flow tests: every stage of docs/implementation-flow.md, run for real.

The real FastAPI app (app.main) is driven with FastAPI's TestClient against
in-memory AWS (moto): DynamoDB, S3 and SQS behave like the real services. Only
two things are simulated, because they are AWS-managed:
  * the Lambda triggers: we read the SQS message ourselves and call the same
    handler function Lambda would call (worker.handle_sqs_event, ...);
  * GuardDuty / EventBridge: we call intake.handle_event with the event shape
    EventBridge delivers.
The AI model is the FakeGateway (tests decide its answers).

Stage numbers in comments refer to docs/implementation-flow.md.
"""

from __future__ import annotations

import base64
import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import settings
from app.storage import keys
from tests.helpers.simple_pdf import demo_reading, demo_statement, sample_transactions

GOOD_INVOICE = json.dumps({
    "invoice_number": "INV-1", "invoice_date": "07/09/2026",
    "seller": {"name": "Demo Traders", "pan": "ABCDE1234F"},
    "line_items": [{"description": "Widget", "amount": "1000.00"}],
    "subtotal": "1000.00", "cgst": "90.00", "sgst": "90.00", "total": "1180.00",
})


# --- helpers ---------------------------------------------------------------------------------

def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (200, 100), "white").save(buf, format="PNG")
    return buf.getvalue()


def _auth(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _drain(queue_name: str) -> list[dict]:
    """Take every message off a queue, shaped like the Lambda SQS event records."""
    from app.storage import queues

    records = []
    while True:
        msgs = queues.receive(queue_name, max_messages=10, wait_seconds=0)
        if not msgs:
            return records
        for m in msgs:
            records.append({"messageId": m["MessageId"], "body": m["Body"],
                            "attributes": {"ApproximateReceiveCount": "1"}})
            queues.delete(queue_name, m["ReceiptHandle"])


def _run_worker(queue_name: str) -> list[dict]:
    """What the worker Lambda's SQS trigger does: deliver the queued messages to the handler."""
    from app.modules.extraction.worker import handle_sqs_event

    records = _drain(queue_name)
    assert records, f"expected a job on {queue_name}"
    result = handle_sqs_event({"Records": records})
    assert result == {"batchItemFailures": []}
    return records


@pytest.fixture
def client(aws):
    from app.main import app
    return TestClient(app)


# --- stage 1-3: edge and authentication -------------------------------------------------------

def test_no_or_bad_key_is_401(client):
    assert client.get("/v1/extractions").status_code == 401
    assert client.get("/v1/extractions", headers=_auth("slm_test_nope_nope")).status_code == 401


def test_health_needs_no_key(client):
    assert client.get("/healthz").json() == {"ok": True}


# --- direct flow: stages 4a, 5-13 ------------------------------------------------------------

def test_direct_flow_end_to_end(client, tenant, fake_gateway):
    tenant_id, api_key = tenant
    png = _png()
    fake_gateway.queue(GOOD_INVOICE)

    # Stage 4a: file in the request (base64 JSON). wait=0 -> answer at once with 202.
    resp = client.post("/v1/extractions?wait=0", headers=_auth(api_key), json={
        "document_type": "invoice", "file": {"content_base64": base64.b64encode(png).decode()}})
    assert resp.status_code == 202, resp.text
    job = resp.json()
    assert job["status"] == "queued" and job["flow"] == "direct"
    assert resp.headers["Location"] == f"/v1/extractions/{job['id']}"

    # The file is in S3 under the tenant's prefix; the job is in DynamoDB.
    from app.storage.blobs import Blobs
    assert Blobs(tenant_id).exists(keys.source_key(tenant_id, job["id"]))

    # Stage 5-12: nobody is waiting (wait=0), so the job went to the BACKGROUND queue.
    _run_worker(settings.queue_background)

    # Stage 13: the client fetches the result.
    body = client.get(f"/v1/extractions/{job['id']}", headers=_auth(api_key)).json()
    assert body["status"] == "succeeded" and body["method"] == "ai"
    assert body["result"]["total"] == "1180.00"
    # Fields come back in the schema's documented order (DynamoDB itself doesn't keep order).
    from app.modules.extraction.schemas.invoice import Invoice
    assert list(body["result"]) == list(Invoice.model_fields)
    assert list(body["usage"]) == ["model_calls", "input_tokens", "output_tokens", "cache_read_tokens",
                                   "estimated_cost_usd", "by_model"]
    assert body["result"]["seller"]["pan"] == "XXXXXX234F"            # stage 11: masked
    assert body["page_count"] == 1 and body["model_id"].startswith("us.anthropic.claude-haiku")
    # Token usage of the job (FakeGateway reports 100 in / 50 out per call): one Haiku call.
    # Haiku list price: 100 * 0.11 + 50 * 0.55 per 1M tokens = USD 0.0000385.
    assert body["usage"] == {"model_calls": 1, "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0,
                             "estimated_cost_usd": 3.9e-05,
                             "by_model": [{"model_id": body["model_id"], "calls": 1, "input_tokens": 100,
                                           "output_tokens": 50, "cache_read_tokens": 0, "estimated_cost_usd": 3.9e-05}]}

    # Stage 12: the source file is deleted after processing (no training consent).
    assert not Blobs(tenant_id).exists(keys.source_key(tenant_id, job["id"]))

    # Stage 12: usage counters were added for today.
    from app.core.clock import now
    from app.storage.aws import dynamodb_table
    usage = dynamodb_table().get_item(Key={"PK": f"TENANT#{tenant_id}",
                                           "SK": f"USAGE#{now().strftime('%Y-%m-%d')}"})["Item"]
    assert usage["jobs"] == 1 and usage["pages"] == 1 and usage["jobs_ai"] == 1

    # Stage 13: a webhook message was queued (ids only) and is delivered without error.
    from app.webhooks import handle_sqs_event as webhook_handler
    records = _drain(settings.queue_webhooks)
    assert json.loads(records[0]["body"]) == {"tenant_id": tenant_id, "job_id": job["id"], "status": "succeeded"}
    assert webhook_handler({"Records": records}) == {"batchItemFailures": []}

    # Dedup (FR-2.11): the same file again is answered at once with the SAME job, no model call.
    calls_before = len(fake_gateway.calls)
    again = client.post("/v1/extractions?wait=0", headers=_auth(api_key), json={
        "document_type": "invoice", "file": {"content_base64": base64.b64encode(png).decode()}})
    assert again.status_code == 200 and again.json()["id"] == job["id"]
    assert again.json()["deduplicated"] is True              # usage shown is the ORIGINAL job's
    assert len(fake_gateway.calls) == calls_before

    # The job is listed (without the result), then deleted (FR-2.9).
    listed = client.get("/v1/extractions", headers=_auth(api_key)).json()["data"]
    assert [j["id"] for j in listed] == [job["id"]] and "result" not in listed[0]
    assert client.delete(f"/v1/extractions/{job['id']}", headers=_auth(api_key)).status_code == 204
    assert client.get(f"/v1/extractions/{job['id']}", headers=_auth(api_key)).status_code == 404


def test_direct_flow_bank_statement_uses_rung0(client, tenant, fake_gateway):
    _, api_key = tenant
    rows = sample_transactions(6)
    fake_gateway.queue(demo_reading(rows))               # only the verifier is asked
    pdf = demo_statement(rows)
    files = {"file": ("statement.pdf", pdf, "application/pdf")}
    resp = client.post("/v1/extractions?wait=0", headers=_auth(api_key), files=files)   # multipart
    assert resp.status_code == 202, resp.text
    _run_worker(settings.queue_background)

    body = client.get(f"/v1/extractions/{resp.json()['id']}", headers=_auth(api_key)).json()
    assert body["method"] == "parser" and body["parser_id"] == "demo_bank_v1"
    assert body["document_type"] == "bank_statement" and body["document_type_source"] == "parser"
    assert len(body["result"]["transactions"]) == 6
    assert [c["model_id"] for c in fake_gateway.calls] == ["us.amazon.nova-lite-v1:0"]
    assert body["usage"]["model_calls"] == 1
    assert [m["model_id"] for m in body["usage"]["by_model"]] == ["us.amazon.nova-lite-v1:0"]


def test_direct_flow_unsupported_file_is_415_before_anything_is_stored(client, tenant):
    _, api_key = tenant
    resp = client.post("/v1/extractions", headers=_auth(api_key),
                       json={"file": {"content_base64": base64.b64encode(b"GIF89a" + b"\0" * 50).decode()}})
    assert resp.status_code == 415
    assert client.get("/v1/extractions", headers=_auth(api_key)).json()["data"] == []


def test_bad_document_fails_job_with_code(client, tenant, fake_gateway):
    _, api_key = tenant
    fake_gateway.queue("not json", "still not json", "nope")    # every rung answers garbage
    resp = client.post("/v1/extractions?wait=0", headers=_auth(api_key), json={
        "document_type": "invoice", "file": {"content_base64": base64.b64encode(_png()).decode()}})
    _run_worker(settings.queue_background)
    body = client.get(f"/v1/extractions/{resp.json()['id']}", headers=_auth(api_key)).json()
    assert body["status"] == "failed" and body["error"] == {"code": "extraction_failed"}
    # Three unusable answers still cost tokens: reported per model (Haiku x2, then Sonnet).
    assert body["usage"]["model_calls"] == 3 and body["usage"]["input_tokens"] == 300
    assert [(m["model_id"].split(".")[-1], m["calls"]) for m in body["usage"]["by_model"]] == [
        ("claude-haiku-5-5", 2), ("claude-sonnet-5-5", 1)]


# --- presigned flow: stages 4b, 5-13 ---------------------------------------------------------

def _presigned_upload(client, tenant_id, api_key, data: bytes, doc_type: str | None = None) -> str:
    """Stage 4b: get an upload URL, then put the file where the URL points."""
    payload = {"document_type": doc_type} if doc_type else {}
    resp = client.post("/v1/extractions", headers=_auth(api_key), json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "awaiting_upload"
    assert body["upload"]["method"] == "POST" and body["upload"]["fields"]["key"].startswith(f"tenants/{tenant_id}/")
    # The tenant would HTTP-POST the form to S3; with moto we write the same object directly.
    from app.storage.blobs import Blobs
    Blobs(tenant_id).put(body["upload"]["fields"]["key"], data)
    return body["id"]


def _eventbridge_object_created(key: str) -> dict:
    return {"detail-type": "Object Created", "source": "aws.s3", "detail": {"object": {"key": key}}}


def _eventbridge_scan_result(key: str, status: str) -> dict:
    return {"detail-type": "GuardDuty Malware Protection Object Scan Result", "source": "aws.guardduty",
            "detail": {"s3ObjectDetails": {"objectKey": key}, "scanResultDetails": {"scanResultStatus": status}}}


def test_presigned_flow_end_to_end(client, tenant, fake_gateway):
    from app.intake import handle_event

    tenant_id, api_key = tenant
    fake_gateway.queue(GOOD_INVOICE)
    job_id = _presigned_upload(client, tenant_id, api_key, _png(), "invoice")
    key = keys.source_key(tenant_id, job_id)

    handle_event(_eventbridge_object_created(key))                       # S3 event -> scanning
    assert client.get(f"/v1/extractions/{job_id}", headers=_auth(api_key)).json()["status"] == "scanning"

    handle_event(_eventbridge_scan_result(key, "NO_THREATS_FOUND"))       # clean -> queued (interactive)
    assert client.get(f"/v1/extractions/{job_id}", headers=_auth(api_key)).json()["status"] == "queued"

    _run_worker(settings.queue_interactive)
    body = client.get(f"/v1/extractions/{job_id}?wait=5", headers=_auth(api_key)).json()   # final: no waiting
    assert body["status"] == "succeeded" and body["flow"] == "presigned"
    assert body["result"]["invoice_number"] == "INV-1"


def test_presigned_upload_with_malware_fails_and_is_deleted(client, tenant):
    from app.intake import handle_event
    from app.storage.blobs import Blobs

    tenant_id, api_key = tenant
    job_id = _presigned_upload(client, tenant_id, api_key, _png())
    key = keys.source_key(tenant_id, job_id)
    handle_event(_eventbridge_object_created(key))
    handle_event(_eventbridge_scan_result(key, "THREATS_FOUND"))

    body = client.get(f"/v1/extractions/{job_id}", headers=_auth(api_key)).json()
    assert body["status"] == "failed" and body["error"] == {"code": "malware_detected"}
    assert not Blobs(tenant_id).exists(key)
    assert _drain(settings.queue_interactive) == []                    # never reached the worker


def test_sweeper_expires_upload_that_never_arrived(client, tenant, monkeypatch):
    import app.intake as intake

    tenant_id, api_key = tenant
    resp = client.post("/v1/extractions", headers=_auth(api_key), json={})
    job_id = resp.json()["id"]
    # Pretend 30 minutes have passed: past the upload URL lifetime + grace period.
    real_now = intake.epoch_now
    monkeypatch.setattr(intake, "epoch_now", lambda: real_now() + 1800)
    assert intake.sweep() == {"expired": 1, "scan_timeout": 0}
    assert client.get(f"/v1/extractions/{job_id}", headers=_auth(api_key)).json()["status"] == "expired"


# --- stage 14: retention --------------------------------------------------------------------

def test_retention_cleaner_deletes_files_when_ttl_removes_the_job(tenant):
    from app.retention import handle_stream
    from app.storage.blobs import Blobs

    tenant_id, _ = tenant
    job_id = "ext_01jtestretention0000000000"
    blobs = Blobs(tenant_id)
    blobs.put(keys.result_key(tenant_id, job_id), b"{}")
    # The record DynamoDB Streams sends when TTL deletes the job item.
    record = {"eventName": "REMOVE", "dynamodb": {"OldImage": {
        "SK": {"S": f"JOB#{job_id}"}, "tenant_id": {"S": tenant_id}}}}
    assert handle_stream({"Records": [record]}) == {"deleted_objects": 1}
    assert not blobs.exists(keys.result_key(tenant_id, job_id))


# --- isolation (security.md §4.1) -----------------------------------------------------------

def test_other_tenant_gets_404_for_every_action(client, tenant, fake_gateway):
    from app.core.ids import new_tenant_id
    from app.storage.tenants import create_api_key, create_tenant

    tenant_id, api_key = tenant
    fake_gateway.queue(GOOD_INVOICE)
    resp = client.post("/v1/extractions?wait=0", headers=_auth(api_key), json={
        "document_type": "invoice", "file": {"content_base64": base64.b64encode(_png()).decode()}})
    job_id = resp.json()["id"]
    _run_worker(settings.queue_background)

    other_id = new_tenant_id()
    create_tenant(other_id, "Other Tenant")
    other_key = create_api_key(other_id, ["extractions:read", "extractions:write"], live=False)
    assert client.get(f"/v1/extractions/{job_id}", headers=_auth(other_key)).status_code == 404
    assert client.delete(f"/v1/extractions/{job_id}", headers=_auth(other_key)).status_code == 404
    assert client.post(f"/v1/extractions/{job_id}/corrections", headers=_auth(other_key),
                       json={"corrections": [{"path": "total", "value": "1"}]}).status_code == 404
    assert client.get("/v1/extractions", headers=_auth(other_key)).json()["data"] == []
    # ...and the owner still sees it.
    assert client.get(f"/v1/extractions/{job_id}", headers=_auth(api_key)).json()["status"] == "succeeded"


# --- stage 2-3 the AWS way: API Gateway -> authorizer Lambda -> Mangum -> FastAPI ----------

def test_aws_path_authorizer_then_api_lambda(tenant, monkeypatch):
    """In AWS the API key is checked by the authorizer Lambda, and the api Lambda trusts
    the context API Gateway passes on. Locally that path is skipped, so test it here
    with the event shapes API Gateway really sends (REST API, Lambda proxy)."""
    from app.auth.authorizer import handler as authorizer
    from app.main import handler as api_lambda

    tenant_id, api_key = tenant
    method_arn = "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/GET/v1/extractions"

    # 1. API Gateway calls the authorizer with the Authorization header.
    decision = authorizer({"type": "REQUEST", "methodArn": method_arn,
                           "headers": {"Authorization": f"Bearer {api_key}"}})
    statement = decision["policyDocument"]["Statement"][0]
    assert statement["Effect"] == "Allow"
    assert statement["Resource"] == "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/*"   # whole stage, cacheable
    assert decision["context"]["tenant_id"] == tenant_id

    # A bad key: the magic "Unauthorized" exception, which API Gateway turns into 401.
    with pytest.raises(Exception, match="Unauthorized"):
        authorizer({"type": "REQUEST", "methodArn": method_arn, "headers": {"Authorization": "Bearer junk"}})

    # 2. API Gateway calls the api Lambda with the authorizer's context; there is NO
    #    Authorization check in FastAPI any more (SLM_ENV != local).
    monkeypatch.setattr(settings, "env", "dev")
    event = {
        "resource": "/v1/{proxy+}", "path": "/v1/extractions", "httpMethod": "GET",
        "headers": {"Host": "abc123.execute-api.us-east-1.amazonaws.com"},
        "multiValueHeaders": {"Host": ["abc123.execute-api.us-east-1.amazonaws.com"]},
        "queryStringParameters": None, "multiValueQueryStringParameters": None,
        "pathParameters": {"proxy": "extractions"}, "stageVariables": None,
        "requestContext": {"resourcePath": "/v1/{proxy+}", "httpMethod": "GET", "stage": "dev",
                           "path": "/dev/v1/extractions", "requestId": "test-request",
                           "identity": {"sourceIp": "203.0.113.10"},
                           "authorizer": decision["context"]},
        "body": None, "isBase64Encoded": False,
    }
    resp = api_lambda(event, None)
    assert resp["statusCode"] == 200, resp
    assert json.loads(resp["body"]) == {"data": [], "next_cursor": None}

    # Without authorizer context (should be impossible behind API Gateway) -> 401, never a guess.
    event["requestContext"]["authorizer"] = {}
    assert api_lambda(event, None)["statusCode"] == 401


# --- audit trail (NFR-S4) ---------------------------------------------------------------------

def test_every_request_is_audited_with_ids_only(client, tenant, caplog):
    import logging

    tenant_id, api_key = tenant
    caplog.set_level(logging.INFO, logger="audit")
    client.get("/v1/extractions/ext_01jnotarealjob00000000000", headers=_auth(api_key))
    client.get("/v1/extractions", headers=_auth("slm_test_bad_key"))
    client.get("/healthz")

    records = [json.loads(r.getMessage()) for r in caplog.records if r.name == "audit"]
    assert [(r["action"], r["result"]) for r in records] == [
        ("GET /v1/extractions/{job_id}", "error:404"),    # route TEMPLATE, not the raw URL
        ("GET /v1/extractions", "denied"),
    ]                                                       # /healthz is not audited
    assert records[0]["tenant_id"] == tenant_id and records[0]["resource_id"] == "ext_01jnotarealjob00000000000"
    assert records[1]["tenant_id"] is None
    assert api_key not in json.dumps(records)              # never the key itself


# --- stage 6: the direct-flow malware-scan wait (AWS only) ------------------------------------

def test_direct_flow_scan_never_arrives_fails_with_scan_timeout(client, tenant, monkeypatch):
    """In AWS the worker waits for GuardDuty's tag before reading a direct-flow file.
    If the tag never comes, the job is retried, then fails with the REAL reason."""
    import app.modules.extraction.worker as worker
    from app.core.errors import TemporaryError
    from app.storage.blobs import Blobs

    tenant_id, api_key = tenant
    resp = client.post("/v1/extractions?wait=0", headers=_auth(api_key), json={
        "document_type": "invoice", "file": {"content_base64": base64.b64encode(_png()).decode()}})
    job_id = resp.json()["id"]
    monkeypatch.setattr(settings, "env", "dev")           # AWS behaviour: wait for the scan tag
    monkeypatch.setattr(worker, "SCAN_WAIT_SECONDS", 0)   # ...but don't actually sleep in a test

    with pytest.raises(TemporaryError):                   # attempt 1: give it back to the queue
        worker.process_job(tenant_id, job_id, receive_count=1)
    assert client.get(f"/v1/extractions/{job_id}", headers=_auth(api_key)).json()["status"] == "queued"

    with pytest.raises(TemporaryError):                   # last attempt: fail with the real reason
        worker.process_job(tenant_id, job_id, receive_count=worker.MAX_ATTEMPTS)
    monkeypatch.setattr(settings, "env", "local")
    body = client.get(f"/v1/extractions/{job_id}", headers=_auth(api_key)).json()
    assert body["status"] == "failed" and body["error"] == {"code": "scan_timeout"}
    assert not Blobs(tenant_id).exists(keys.source_key(tenant_id, job_id))


# --- stage 14: retention is exact, even before DynamoDB TTL runs -----------------------------

def test_expired_job_is_invisible_before_ttl_deletes_it(client, tenant, fake_gateway, monkeypatch):
    import app.storage.jobs as jobs

    _, api_key = tenant
    fake_gateway.queue(GOOD_INVOICE)
    png64 = base64.b64encode(_png()).decode()
    resp = client.post("/v1/extractions?wait=0", headers=_auth(api_key),
                       json={"document_type": "invoice", "file": {"content_base64": png64}})
    job_id = resp.json()["id"]
    _run_worker(settings.queue_background)
    assert client.get(f"/v1/extractions/{job_id}", headers=_auth(api_key)).status_code == 200

    # 31 days later (retention is 30): the item may still be in DynamoDB, but it is gone for the API.
    real_now = jobs.epoch_now
    monkeypatch.setattr(jobs, "epoch_now", lambda: real_now() + 31 * 86400)
    assert client.get(f"/v1/extractions/{job_id}", headers=_auth(api_key)).status_code == 404
    assert client.get("/v1/extractions", headers=_auth(api_key)).json()["data"] == []
    # ...and the same file is processed again instead of returning the expired result.
    fake_gateway.queue(GOOD_INVOICE)
    again = client.post("/v1/extractions?wait=0", headers=_auth(api_key),
                        json={"document_type": "invoice", "file": {"content_base64": png64}})
    assert again.status_code == 202 and again.json()["id"] != job_id
