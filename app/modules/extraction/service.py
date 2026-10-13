"""Extraction use cases: what each API route actually does.

One upload, end to end (no queue, one document at a time):

    check the request ──► create job ──► save input ──► run pipeline ──► save result ──► return
    (type known? size?     (status        (input.pdf)    (one document     (result.json,
     real file type?)       processing)                   at a time)        job.json)

Checks that fail BEFORE a job exists (unknown type, empty, too big, not a supported file)
return an HTTP error straight away (400/413/415). Once a job exists, every outcome is
recorded on it: the response is 200 with status "succeeded" or "failed" + an error code.
"""

import hashlib
import logging
from datetime import UTC, datetime

from app.core.errors import AppError
from app.core.ids import new_id
from app.deps import Deps
from app.modules.extraction.errors import ExtractionError
from app.modules.extraction.models import DocumentTypeOut, Job, JobError, JobOut
from app.modules.extraction.pipeline import run_extraction
from app.modules.extraction.preprocess import detect_kind
from app.modules.extraction.schemas import DOCUMENT_TYPES, get_document_type

log = logging.getLogger("slm.extraction")

_NOT_FOUND = AppError(404, "not_found", "No extraction with this id.")


def list_document_types() -> list[DocumentTypeOut]:
    return [
        DocumentTypeOut(name=t.name, version=t.version, description=t.description, json_schema=t.json_schema)
        for t in DOCUMENT_TYPES.values()
    ]


def extract_document(deps: Deps, document_type: str, data: bytes) -> JobOut:
    settings = deps.settings

    # ---- Checks before creating a job ----
    doc_type = get_document_type(document_type)
    if doc_type is None:
        names = ", ".join(DOCUMENT_TYPES)
        raise AppError(400, "unknown_document_type", f"Unknown document_type. Use one of: {names}.")
    if not data:
        raise AppError(400, "empty_file", "The uploaded file is empty.")
    if len(data) > settings.max_upload_bytes:
        raise AppError(413, "file_too_large", f"Files can be at most {settings.max_upload_bytes // (1024 * 1024)} MB.")
    try:
        kind = detect_kind(data)  # from the bytes, never the file name
    except ExtractionError as exc:
        raise AppError(exc.http_status, exc.code, exc.message) from exc

    # ---- Create the job and keep the uploaded file ----
    store = deps.job_store
    job = Job(
        id=new_id("job"),
        document_type=doc_type.name,
        schema_version=doc_type.version,
        status="processing",
        file_kind=kind.value,
        file_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    store.put_input(job.id, data, kind.extension)
    store.put_job(job)

    # ---- Run the pipeline, one document at a time ----
    # The local CPU model can only do one document efficiently. The lock makes a second
    # upload wait until the first has finished, instead of both running at half speed.
    result: dict | None = None
    with deps.extraction_lock:
        started = datetime.now(UTC)
        try:
            outcome = run_extraction(data, doc_type, deps.provider, settings)
        except ExtractionError as exc:
            job = job.model_copy(update={"status": "failed", "error": JobError(code=exc.code, message=exc.message)})
        except Exception:
            # A bug, not a document problem. Record it on the job (so it does not stay
            # "processing" forever) and log the stack trace (code locations only, no content).
            log.exception("extraction crashed", extra={"job_id": job.id})
            error = JobError(code="internal_error", message="Something went wrong on our side.")
            job = job.model_copy(update={"status": "failed", "error": error})
        else:
            result = outcome.result
            store.put_result(job.id, result)
            job = job.model_copy(
                update={
                    "status": "succeeded",
                    "pages": outcome.pages,
                    "route": outcome.route,
                    "warnings": outcome.warnings,
                    "model": outcome.model,
                    "input_tokens": outcome.input_tokens,
                    "output_tokens": outcome.output_tokens,
                    "prepare_ms": outcome.prepare_ms,
                    "model_ms": outcome.model_ms,
                }
            )
        finished = datetime.now(UTC)

    job = job.model_copy(
        update={"finished_at": finished, "total_ms": round((finished - started).total_seconds() * 1000)}
    )
    store.put_job(job)
    # Ids, codes, counts and timings only: never content (NFR-S3).
    log.info(
        "extraction finished",
        extra={
            "job_id": job.id,
            "document_type": job.document_type,
            "status": job.status,
            "error_code": job.error.code if job.error else None,
            "pages": job.pages,
            "total_ms": job.total_ms,
        },
    )
    return JobOut(**job.model_dump(), result=result)


def get_job(deps: Deps, job_id: str) -> JobOut:
    job = deps.job_store.get_job(job_id)
    if job is None:
        raise _NOT_FOUND
    result = deps.job_store.get_result(job_id) if job.status == "succeeded" else None
    return JobOut(**job.model_dump(), result=result)


def list_jobs(deps: Deps, limit: int) -> list[JobOut]:
    return [JobOut(**job.model_dump()) for job in deps.job_store.list_jobs(limit)]


def delete_job(deps: Deps, job_id: str) -> None:
    if not deps.job_store.delete_job(job_id):
        raise _NOT_FOUND
    log.info("extraction deleted", extra={"job_id": job_id})
