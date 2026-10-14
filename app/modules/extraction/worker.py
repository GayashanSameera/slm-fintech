"""The worker: processes one queued job from start to finish.

In AWS this is the `worker` Lambda, triggered by SQS (both job queues).
Locally, `python -m app.modules.extraction.worker --local` runs a loop that
long-polls the local queues and calls the SAME `process_job` function.

What happens for one job (architecture §4.5, stages 6-12):

    1. claim the job (queued -> processing, with a lease)          idempotency
    2. direct flow only: wait for the malware-scan tag             NFR-S6
    3. read the file from S3 (tenant-scoped credentials)
    4. preprocess: validate + build pages                          FR-2.3..2.5
    5. extract_document(): Rung 0 parser or AI ladder, checks, mask
    6. store the result (inline, or in S3 if > 100 KB)
    7. keep a training copy (only with consent) / delete the source FR-6.1, 6.2
    8. usage counters, dedup entry, webhook                        FR-7.1, 2.11, 2.10

Errors:
    JobError        bad document -> job "failed" with a code, message deleted (no retry)
    TemporaryError  e.g. Bedrock throttling -> lease released, SQS retries the message
    ScanPending     direct flow, GuardDuty tag not there yet -> retried; after the last
                    attempt the job fails with "scan_timeout"
    anything else   treated like temporary; after 3 attempts the job is marked failed
                    ("processing_error") and SQS moves the message to the DLQ (alarm).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time

from app import usage, webhooks
from app.config import settings
from app.core.errors import JobError, TemporaryError
from app.core.logging import get_logger
from app.gateway import get_gateway
from app.storage import keys, queues
from app.storage.blobs import Blobs
from app.storage.jobs import JobRepo
from app.storage.tenants import get_tenant_settings

from .masking import has_aadhaar_markers
from .pipeline import ExtractionOutcome, extract_document
from .preprocess import preprocess
from .profiles import load_profile
from .schemas import REGISTRY

log = get_logger(__name__)

LEASE_SECONDS = 150            # a bit longer than the 120 s worker Lambda timeout
INLINE_RESULT_MAX = 100_000    # results larger than this go to S3 (DynamoDB items max 400 KB)
SCAN_WAIT_SECONDS = 30
MAX_ATTEMPTS = 3               # must match maxReceiveCount on the SQS redrive policy


class ScanPending(TemporaryError):
    """The malware scan result is not available yet."""


def _wait_for_clean_scan(blobs: Blobs, key: str) -> None:
    """Direct flow only. Presigned uploads are queued by intake.py AFTER a clean scan."""
    if settings.is_local:
        return                   # no GuardDuty locally
    deadline = time.monotonic() + SCAN_WAIT_SECONDS
    while time.monotonic() < deadline:
        status = blobs.tags(key).get("GuardDutyMalwareScanStatus")
        if status == "NO_THREATS_FOUND":
            return
        if status == "THREATS_FOUND":
            blobs.delete(key)
            raise JobError("malware_detected")
        if status in ("UNSUPPORTED", "ACCESS_DENIED", "FAILED"):
            raise JobError("scan_failed")
        time.sleep(2)
    raise ScanPending()


def _keep_for_training(tenant, outcome: ExtractionOutcome, page_texts: list[str]) -> bool:
    """FR-6.1/6.2: keep the source only with consent, never identity/KYC documents, and
    for detected types only when we are very sure it is not an identity document."""
    if not tenant.consented_to_training:
        return False
    if REGISTRY[outcome.doc_type].sensitive_identity:
        return False
    if outcome.doc_type_source == "detected" and (outcome.doc_type_confidence or 0) < 0.95:
        return False
    return not has_aadhaar_markers(*page_texts)


def process_job(tenant_id: str, job_id: str, receive_count: int = 1) -> None:
    repo = JobRepo(tenant_id)
    if not repo.claim(job_id, LEASE_SECONDS):
        log.info("job_skipped", tenant_id=tenant_id, job_id=job_id)   # finished, or another worker has it
        return
    job = repo.get(job_id, consistent=True)
    if job is None:
        # Retention ended while the job waited in the queue: the data must not be processed.
        log.info("job_expired_before_processing", tenant_id=tenant_id, job_id=job_id)
        return
    blobs = Blobs(tenant_id)
    source = keys.source_key(tenant_id, job_id)
    started = time.monotonic()
    calls = []
    try:
        if job.get("flow") == "direct":
            _wait_for_clean_scan(blobs, source)
        data = blobs.get(source)
        prepared = preprocess(data)
        tenant = get_tenant_settings(tenant_id)
        profile = load_profile(tenant.profile or settings.profile_name)
        outcome = extract_document(data=data, prepared=prepared, requested_type=job.get("requested_doc_type"),
                                   tenant=tenant, profile=profile, gateway=get_gateway(), job_id=job_id)
        calls = outcome.calls

        # --- store the result -------------------------------------------------------------
        result_json = json.dumps(outcome.result, separators=(",", ":"))
        stored: dict = {}
        if len(result_json) <= INLINE_RESULT_MAX:
            stored["result"] = outcome.result
        else:
            rkey = keys.result_key(tenant_id, job_id)
            blobs.put(rkey, result_json.encode(), "application/json")
            stored["result_key"] = rkey
        repo.complete(
            job_id,
            remove=["lease_until"],
            document_type=outcome.doc_type,
            document_type_source=outcome.doc_type_source,
            document_type_confidence=outcome.doc_type_confidence,
            schema_version=outcome.schema_version,
            page_count=prepared.page_count,
            warnings=outcome.warnings,
            method=outcome.method,
            model_id=outcome.model_id,
            parser_id=outcome.parser_id,
            verified_by=outcome.verified_by,
            profile_id=profile.id,
            prompt_version=profile.prompt_version,
            **stored,
        )

        # --- the source file: keep a training copy only with consent, then delete --------
        page_texts = [p.get("text") or "" for p in prepared.pages]
        if _keep_for_training(tenant, outcome, page_texts):
            blobs.copy(source, keys.training_prefix(tenant_id, outcome.doc_type, job_id) + "source")
        blobs.delete(source)

        # --- dedup, usage, webhook -------------------------------------------------------
        # Remember this file so an identical upload by the same tenant is answered instantly.
        sha = job.get("sha256") or hashlib.sha256(data).hexdigest()
        repo.save_dedup(job.get("requested_doc_type"), sha, job_id, job["expires_at"])
        usage.record(tenant_id, pages=prepared.page_count, calls=calls, method=outcome.method)
        webhooks.enqueue(tenant_id, job_id, "succeeded")
        log.info("job_succeeded", tenant_id=tenant_id, job_id=job_id, doc_type=outcome.doc_type,
                 method=outcome.method, pages=prepared.page_count, warnings=len(outcome.warnings),
                 duration_ms=int((time.monotonic() - started) * 1000))

    except JobError as e:
        # A problem with the document itself: fail now, don't retry.
        repo.fail(job_id, e.code)
        blobs.delete(source)
        calls = getattr(e, "calls", calls)
        if calls:
            usage.record(tenant_id, pages=0, calls=calls, method="failed")
        webhooks.enqueue(tenant_id, job_id, "failed")
        log.info("job_failed", tenant_id=tenant_id, job_id=job_id, error_code=e.code)

    except ScanPending:
        # Direct flow: GuardDuty hasn't tagged the file yet. Not an error in OUR code, so
        # it gets its own outcome: retry later, and after the last attempt fail with the
        # real reason ("scan_timeout"), the same code the scan-sweeper uses for presigned uploads.
        if receive_count >= MAX_ATTEMPTS:
            repo.fail(job_id, "scan_timeout")
            blobs.delete(source)           # never processed, so never kept
            webhooks.enqueue(tenant_id, job_id, "failed")
            log.warning("job_scan_timeout", tenant_id=tenant_id, job_id=job_id, attempt=receive_count)
        else:
            repo.transition(job_id, ["processing"], "queued", remove=["lease_until"])
            log.info("job_scan_pending", tenant_id=tenant_id, job_id=job_id, attempt=receive_count)
        raise TemporaryError() from None

    except Exception as e:
        if receive_count >= MAX_ATTEMPTS:
            # Last attempt: mark the job failed so the tenant isn't left waiting.
            # SQS moves the message to the dead-letter queue, which raises an alarm.
            repo.fail(job_id, "processing_error")
            webhooks.enqueue(tenant_id, job_id, "failed")
            log.error("job_gave_up", exc=e, tenant_id=tenant_id, job_id=job_id, attempt=receive_count)
        else:
            # Give the job back so the retry can claim it immediately.
            repo.transition(job_id, ["processing"], "queued", remove=["lease_until"])
            log.error("job_retry", exc=e, tenant_id=tenant_id, job_id=job_id, attempt=receive_count)
        raise TemporaryError() from None


def handle_sqs_event(event: dict, context=None) -> dict:
    """Lambda entry point. Reports failed messages individually ("partial batch
    response") so SQS retries only those, not the whole batch."""
    failures = []
    for record in event.get("Records", []):
        msg = json.loads(record["body"])
        receive_count = int(record.get("attributes", {}).get("ApproximateReceiveCount", "1"))
        try:
            process_job(msg["tenant_id"], msg["job_id"], receive_count)
        except TemporaryError:
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def run_local_loop() -> None:
    """Local stand-in for the Lambda trigger: poll interactive first, then background."""
    log.info("worker_started", source="local")
    counts: dict[str, int] = {}
    while True:
        handled = False
        for queue in (settings.queue_interactive, settings.queue_background):
            for msg in queues.receive(queue, max_messages=1, wait_seconds=2):
                handled = True
                counts[msg["MessageId"]] = counts.get(msg["MessageId"], 0) + 1
                body = json.loads(msg["Body"])
                try:
                    process_job(body["tenant_id"], body["job_id"], counts[msg["MessageId"]])
                    queues.delete(queue, msg["ReceiptHandle"])
                except TemporaryError:
                    pass   # leave the message: it becomes visible again and is retried
        if not handled:
            time.sleep(0.5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the worker locally.")
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args()
    run_local_loop()
