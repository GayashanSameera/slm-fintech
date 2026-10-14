"""Upload intake: turns a clean presigned upload into a queued job (architecture §5.1).

In AWS this is the `intake` Lambda, triggered by EventBridge with two kinds of events:

  1. "Object Created" (from S3), when the tenant's upload arrives:
        job: awaiting_upload -> scanning      (records uploaded_at)

  2. "GuardDuty Malware Protection Object Scan Result", seconds later:
        NO_THREATS_FOUND -> job: scanning -> queued, message sent to the worker queue
        THREATS_FOUND    -> job: failed (malware_detected), object deleted

Both handlers are idempotent: conditional status changes mean a repeated or
late event changes nothing.

`sweep()` is the `scan-sweeper` Lambda (every 5 minutes): it expires jobs whose
upload never arrived, and fails jobs whose scan result never came.

Locally there is no GuardDuty and no EventBridge, so `run_local_loop()` polls
for uploaded files and simulates a clean scan.
"""

from __future__ import annotations

import argparse
import time

from boto3.dynamodb.conditions import Key

from app.config import settings
from app.core.clock import epoch_in, epoch_now, now_iso
from app.core.logging import get_logger
from app.storage import keys, queues
from app.storage.aws import dynamodb_table
from app.storage.blobs import Blobs
from app.storage.jobs import PENDING_GSI, PENDING_PK_VALUE, JobRepo
from app.webhooks import enqueue as enqueue_webhook

log = get_logger(__name__)

SCAN_TIMEOUT_SECONDS = 300   # if no scan result arrives in 5 minutes, fail the job


def on_object_created(key: str) -> None:
    """S3 "Object Created": the upload arrived -> awaiting_upload to scanning, with a
    5-minute deadline for the scan result. Keys that aren't job sources are ignored.
    """
    parsed = keys.parse_source_key(key)
    if not parsed:
        return                       # not an upload key: ignore (e.g. result.json, training copies)
    tenant_id, job_id = parsed
    moved = JobRepo(tenant_id).transition(
        job_id, ["awaiting_upload"], "scanning", uploaded_at=now_iso(),
        pending_deadline=epoch_in(SCAN_TIMEOUT_SECONDS),
    )
    log.info("upload_received", tenant_id=tenant_id, job_id=job_id, status="scanning" if moved else "ignored")


def on_scan_result(key: str, scan_status: str) -> None:
    """GuardDuty's verdict for an upload: clean -> queued (and sent to the interactive queue);
    malware -> failed + file deleted; anything else -> failed ("scan_failed"): we can't prove it's safe.
    """
    parsed = keys.parse_source_key(key)
    if not parsed:
        return
    tenant_id, job_id = parsed
    repo = JobRepo(tenant_id)
    if scan_status == "NO_THREATS_FOUND":
        # "awaiting_upload" is also accepted in case the scan result beats the S3 event.
        if repo.transition(job_id, ["scanning", "awaiting_upload"], "queued",
                           remove=["pending_pk", "pending_deadline"]):
            queues.send_job(tenant_id, job_id, interactive=True)
            log.info("upload_clean", tenant_id=tenant_id, job_id=job_id)
    elif scan_status == "THREATS_FOUND":
        if repo.fail(job_id, "malware_detected", from_statuses=["scanning", "awaiting_upload"]):
            Blobs(tenant_id).delete(key)
            enqueue_webhook(tenant_id, job_id, "failed")
            log.warning("upload_malware", tenant_id=tenant_id, job_id=job_id)
    else:
        # UNSUPPORTED / ACCESS_DENIED / FAILED: we cannot prove the file is safe.
        if repo.fail(job_id, "scan_failed", from_statuses=["scanning", "awaiting_upload"]):
            enqueue_webhook(tenant_id, job_id, "failed")
            log.warning("upload_scan_failed", tenant_id=tenant_id, job_id=job_id, status=scan_status)


def handle_event(event: dict, context=None) -> None:
    """Lambda entry point (EventBridge)."""
    detail_type = event.get("detail-type", "")
    detail = event.get("detail") or {}
    if detail_type == "Object Created":
        on_object_created(detail.get("object", {}).get("key", ""))
    elif detail_type == "GuardDuty Malware Protection Object Scan Result":
        # Field names to re-check against a real sample event in phase 0
        # (save one as a test fixture).
        key = (detail.get("s3ObjectDetails") or {}).get("objectKey", "")
        status = (detail.get("scanResultDetails") or {}).get("scanResultStatus", "")
        on_scan_result(key, status)


def sweep(event: dict | None = None, context=None) -> dict:
    """`scan-sweeper` Lambda: handle jobs stuck waiting for an upload or a scan.

    Uses the sparse "pending" index, so it reads only waiting jobs, never the whole
    table. This function runs with its own narrow IAM role (query the index, update
    job status) because it works across tenants.
    """
    table = dynamodb_table()
    resp = table.query(
        IndexName=PENDING_GSI,
        KeyConditionExpression=Key("pending_pk").eq(PENDING_PK_VALUE) & Key("pending_deadline").lt(epoch_now()),
    )
    expired = failed = 0
    for item in resp.get("Items", []):
        tenant_id, job_id = item["tenant_id"], item["id"]
        repo = JobRepo(tenant_id)
        if item["status"] == "awaiting_upload":
            if repo.transition(job_id, ["awaiting_upload"], "expired", remove=["pending_pk", "pending_deadline"]):
                expired += 1
        elif item["status"] == "scanning":
            if repo.fail(job_id, "scan_timeout", from_statuses=["scanning"]):
                failed += 1
                enqueue_webhook(tenant_id, job_id, "failed")
    log.info("sweep_done", count=expired + failed)
    return {"expired": expired, "scan_timeout": failed}


def run_local_loop() -> None:
    """LOCAL ONLY: find awaiting_upload jobs whose file is now in MinIO, treat the scan as clean."""
    log.info("intake_started", source="local")
    while True:
        table = dynamodb_table()
        resp = table.query(IndexName=PENDING_GSI, KeyConditionExpression=Key("pending_pk").eq(PENDING_PK_VALUE))
        for item in resp.get("Items", []):
            key = keys.source_key(item["tenant_id"], item["id"])
            if item["status"] == "awaiting_upload" and Blobs(item["tenant_id"]).exists(key):
                on_object_created(key)
                on_scan_result(key, "NO_THREATS_FOUND")      # no GuardDuty locally
        sweep()
        time.sleep(2)


if __name__ == "__main__":
    argparse.ArgumentParser(description="Run the intake poller locally.").add_argument("--local", action="store_true")
    if not settings.is_local:
        raise SystemExit("The local intake loop is for SLM_ENV=local only.")
    run_local_loop()
