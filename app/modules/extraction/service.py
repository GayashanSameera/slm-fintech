"""Extraction business logic used by the HTTP routes (api.py).

Two ways to create a job (architecture §5):

  DIRECT flow (create_direct):   file in the request (multipart or base64, <= 4 MB)
      detect type -> dedup? -> store in S3 -> create job (queued) -> send to SQS
      -> WAIT up to 40 s for the worker -> 200 with result, or 202

  PRESIGNED flow (create_presigned):   no file in the request
      create job (awaiting_upload) -> return a one-time S3 upload URL (201)
      ... client uploads to S3 -> S3 event + malware scan -> intake.py queues it
      ... client calls GET /v1/extractions/{id}?wait=40 for the result

Everything here is tenant-scoped: the tenant id comes from the Principal
(i.e. from the API key), never from the request.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import re
import time

from app.audit import audit
from app.config import settings
from app.core.clock import epoch_in, iso_in
from app.core.errors import ApiError, not_found
from app.core.ids import is_job_id, new_job_id
from app.storage import keys, queues
from app.storage.blobs import Blobs
from app.storage.jobs import FINAL_STATUSES, JobRepo
from app.storage.tenants import get_tenant_settings

from .preprocess.detect import content_type_for, detect_type
from .schemas import REGISTRY
from .window import next_opening_iso, processing_open

VALID_TYPES = set(REGISTRY) | {"auto"}


# ---------------------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------------------

def validate_doc_type(doc_type: str | None) -> str | None:
    """None or "auto" -> detect automatically (FR-2.18)."""
    if doc_type in (None, "", "auto"):
        return None
    if doc_type not in VALID_TYPES:
        raise ApiError(400, "invalid_document_type",
                       "document_type must be one of: " + ", ".join(sorted(VALID_TYPES)))
    return doc_type


_DATA_URL_PREFIX = re.compile(r"^data:[\w/+.-]+;base64,")
_WHITESPACE = re.compile(r"\s+")


def decode_base64_strict(value: str) -> bytes:
    """Decode `file.content_base64` safely (architecture §5.2).

    1. Accept an optional data-URL prefix ("data:application/pdf;base64,").
    2. Remove whitespace/newlines (some clients wrap base64 every 76 characters).
    3. Check the size BEFORE decoding (4 base64 chars = 3 bytes), so a huge
       string is rejected without allocating memory for it.
    4. Decode strictly: any character outside the base64 alphabet is an error.
    Error messages never echo the submitted value (NFR-S8).
    """
    if not isinstance(value, str):
        raise ApiError(400, "invalid_base64", "file.content_base64 must be a base64 string.")
    s = _WHITESPACE.sub("", _DATA_URL_PREFIX.sub("", value))
    if len(s) * 3 // 4 > settings.max_inline_bytes:
        raise ApiError(413, "file_too_large", "The file is larger than 4 MB. Use the upload-link flow.")
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(400, "invalid_base64", "file.content_base64 is not valid base64.") from None


# ---------------------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------------------

PUBLIC_FIELDS = (
    "id", "status", "flow", "document_type", "document_type_source", "document_type_confidence",
    "schema_version", "page_count", "warnings", "method", "parser_id", "verified_by", "model_id",
    "created_at", "uploaded_at", "completed_at", "processing_starts_at",
    "usage",           # tokens used by the job's model calls (usage.summarize), once it is final
)


_USAGE_ORDER = ("model_calls", "input_tokens", "output_tokens", "cache_read_tokens", "estimated_cost_usd", "by_model")
_MODEL_USAGE_ORDER = ("model_id", "calls", "input_tokens", "output_tokens", "cache_read_tokens", "estimated_cost_usd")


def _in_order(data: dict, order: tuple) -> dict:
    return {k: data[k] for k in order if k in data} | {k: v for k, v in data.items() if k not in order}


def _schema_order(doc_type: str | None, result: dict) -> dict:
    """Return the result with its fields in the schema's order (invoice_number, invoice_date, ...).

    DynamoDB does not keep the order of a map's keys, so a stored result comes back
    shuffled. Re-reading it through its own schema restores the documented order for
    every client. The values are the ones the schema produced in the first place, so
    nothing changes but the order; anything unexpected is returned as stored.
    """
    spec = REGISTRY.get(doc_type or "")
    if spec is None:
        return result
    try:
        return spec.model.model_validate(result).model_dump(mode="json")
    except ValueError:
        return result


def public_job(job: dict, blobs: Blobs | None = None) -> dict:
    """The JSON a tenant sees for a job. Internal fields (PK/SK, TTL...) are not exposed."""
    out = {k: job.get(k) for k in PUBLIC_FIELDS if job.get(k) is not None}
    out.setdefault("document_type", job.get("requested_doc_type") or "auto")
    out.setdefault("warnings", [])
    if isinstance(out.get("usage"), dict):
        out["usage"] = _in_order(out["usage"], _USAGE_ORDER)
        out["usage"]["by_model"] = [_in_order(m, _MODEL_USAGE_ORDER) for m in out["usage"].get("by_model", [])]
    if job.get("status") == "succeeded":
        if "result" in job:
            out["result"] = _schema_order(job.get("document_type"), job["result"])
        elif job.get("result_key") and blobs is not None:
            import json
            out["result"] = _schema_order(job.get("document_type"), json.loads(blobs.get(job["result_key"])))
    if job.get("status") == "failed":
        out["error"] = {"code": job.get("error_code", "processing_error")}
    return out


# ---------------------------------------------------------------------------------------
# Waiting for a result (FR-2.14, FR-2.16)
# ---------------------------------------------------------------------------------------

async def wait_for_job(repo: JobRepo, job_id: str, wait_s: int) -> dict | None:
    """Poll the job until it is final or `wait_s` seconds have passed.

    Strongly consistent reads, starting every 250 ms and slowing to every second.
    Each poll is one small DynamoDB read, so a 40-second wait costs fractions of a cent.
    `asyncio.sleep` lets the server handle other requests while we wait.
    """
    deadline = time.monotonic() + max(0, min(wait_s, settings.max_wait_seconds))
    delay = 0.25
    while True:
        job = repo.get(job_id, consistent=True)
        if job is None or job["status"] in FINAL_STATUSES or time.monotonic() >= deadline:
            return job
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 1.0)


# ---------------------------------------------------------------------------------------
# Create: direct flow
# ---------------------------------------------------------------------------------------

async def create_direct(principal, doc_type: str | None, data: bytes, wait: int) -> tuple[int, dict, dict]:
    """Returns (http status, body, headers)."""
    if not data:
        raise ApiError(400, "empty_file", "The file is empty.")
    if len(data) > settings.max_inline_bytes:
        raise ApiError(413, "file_too_large", "The file is larger than 4 MB. Use the upload-link flow.")
    kind = detect_type(data)                         # 415 for unsupported types (FR-2.3)

    tenant = get_tenant_settings(principal.tenant_id)
    repo = JobRepo(principal.tenant_id)
    blobs = Blobs(principal.tenant_id)

    # Dedup (FR-2.11): same tenant, same file, same requested type -> reuse the earlier result.
    sha = hashlib.sha256(data).hexdigest()
    previous_id = repo.find_dedup(doc_type, sha)
    if previous_id:
        previous = repo.get(previous_id)
        if previous and previous["status"] == "succeeded":
            audit(tenant_id=principal.tenant_id, key_id=principal.key_id, action="extraction.create",
                  resource_id=previous_id, result="ok:dedup")
            body = public_job(previous, blobs)
            # The earlier job's result: no model was called for THIS request. `usage` still
            # shows what the original job used, so this flag stops it being counted twice.
            body["deduplicated"] = True
            return 200, body, {}

    job_id = new_job_id()
    blobs.put(keys.source_key(principal.tenant_id, job_id), data, content_type_for(kind))
    repo.create(job_id, status="queued", flow="direct", doc_type=doc_type,
                retention_days=tenant.retention_days, sha256=sha)
    window_open = processing_open()
    # A waiting client goes on the interactive queue so it is served first (FR-2.17).
    queues.send_job(principal.tenant_id, job_id, interactive=wait > 0 and window_open)
    audit(tenant_id=principal.tenant_id, key_id=principal.key_id, action="extraction.create",
          resource_id=job_id, result="ok")

    location = {"Location": f"/v1/extractions/{job_id}"}
    if not window_open:
        # Outside a configured processing window: answer at once (FR-2.15).
        body = public_job(repo.get(job_id))
        body["processing_starts_at"] = next_opening_iso()
        return 202, body, location
    job = await wait_for_job(repo, job_id, wait) if wait > 0 else repo.get(job_id)
    if job and job["status"] in FINAL_STATUSES:
        return 200, public_job(job, blobs), location
    return 202, public_job(job or {"id": job_id, "status": "queued"}), location


# ---------------------------------------------------------------------------------------
# Create: presigned flow
# ---------------------------------------------------------------------------------------

def create_presigned(principal, doc_type: str | None) -> tuple[int, dict, dict]:
    tenant = get_tenant_settings(principal.tenant_id)
    job_id = new_job_id()
    key = keys.source_key(principal.tenant_id, job_id)
    upload = Blobs(principal.tenant_id).presigned_upload(key, settings.max_upload_bytes,
                                                        settings.upload_url_ttl_seconds)
    JobRepo(principal.tenant_id).create(
        job_id, status="awaiting_upload", flow="presigned", doc_type=doc_type,
        retention_days=tenant.retention_days,
        # The scan-sweeper expires the job if nothing arrives in time (FR-2.19).
        pending_deadline=epoch_in(settings.upload_url_ttl_seconds + 300),
    )
    audit(tenant_id=principal.tenant_id, key_id=principal.key_id, action="extraction.create_upload",
          resource_id=job_id, result="ok")
    body = {
        "id": job_id,
        "status": "awaiting_upload",
        "document_type": doc_type or "auto",
        "upload": {
            "method": "POST",
            "url": upload["url"],
            "fields": upload["fields"],     # send these form fields first, then the file LAST
            "max_bytes": settings.max_upload_bytes,
            "expires_at": iso_in(settings.upload_url_ttl_seconds),
        },
    }
    return 201, body, {"Location": f"/v1/extractions/{job_id}"}


# ---------------------------------------------------------------------------------------
# Read, list, delete, corrections
# ---------------------------------------------------------------------------------------

async def get_job(principal, job_id: str, wait: int) -> tuple[dict, dict]:
    if not is_job_id(job_id):
        raise not_found()
    repo = JobRepo(principal.tenant_id)
    job = await wait_for_job(repo, job_id, wait) if wait > 0 else repo.get(job_id)
    if job is None:
        raise not_found()          # also the answer for other tenants' ids (security.md §4.1)
    headers = {} if job["status"] in FINAL_STATUSES else {"Retry-After": "2"}
    return public_job(job, Blobs(principal.tenant_id)), headers


def list_jobs(principal, limit: int, cursor: str | None) -> dict:
    if cursor and not is_job_id(cursor):
        raise ApiError(400, "invalid_cursor", "The cursor is not valid.")
    items, next_cursor = JobRepo(principal.tenant_id).list(limit=max(1, min(limit, 100)), cursor=cursor)
    # The list view never includes results: fetch a job to see its result.
    return {"data": [{k: v for k, v in public_job(i).items() if k != "result"} for i in items],
            "next_cursor": next_cursor}


def delete_job(principal, job_id: str) -> None:
    """FR-2.9: removes the file, the result, corrections and any training copy."""
    if not is_job_id(job_id):
        raise not_found()
    repo = JobRepo(principal.tenant_id)
    job = repo.get(job_id)
    if job is None:
        raise not_found()
    blobs = Blobs(principal.tenant_id)
    blobs.delete_prefix(keys.job_prefix(principal.tenant_id, job_id))
    if job.get("document_type"):
        blobs.delete_prefix(keys.training_prefix(principal.tenant_id, job["document_type"], job_id))
    repo.delete(job_id)
    audit(tenant_id=principal.tenant_id, key_id=principal.key_id, action="extraction.delete",
          resource_id=job_id, result="ok")


def add_corrections(principal, job_id: str, corrections: list[dict]) -> dict:
    """FR-2.12: store field corrections for a finished job."""
    from app.core.ids import new_key_id  # short unique id is enough here
    if not is_job_id(job_id):
        raise not_found()
    repo = JobRepo(principal.tenant_id)
    job = repo.get(job_id)
    if job is None:
        raise not_found()
    if job["status"] != "succeeded":
        raise ApiError(409, "job_not_finished", "Corrections can only be added to a finished job.")
    clean = []
    for c in corrections[:500]:
        path = str(c.get("path", ""))[:200]
        if not path:
            raise ApiError(400, "invalid_correction", "Every correction needs a 'path'.")
        clean.append({"path": path, "value": c.get("value")})
    repo.add_corrections(job_id, new_key_id(), clean, expires_at=job["expires_at"])
    return {"job_id": job_id, "accepted": len(clean)}
