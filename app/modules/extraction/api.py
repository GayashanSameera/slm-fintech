"""HTTP routes for extractions (/v1/extractions...).

Routes stay thin: they read the request, check the scope, call service.py and
shape the HTTP response. All rules live in service.py.

    POST   /v1/extractions                    create (direct or presigned flow)
    GET    /v1/extractions                    list, newest first
    GET    /v1/extractions/{id}?wait=0..40    one job (+ result when finished)
    DELETE /v1/extractions/{id}               delete job, file, result, corrections
    POST   /v1/extractions/{id}/corrections   field corrections
    GET    /v1/document-types                 supported types + JSON schemas
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from app.auth.dependencies import require_scope
from app.auth.principal import Principal
from app.config import settings
from app.core.errors import ApiError

from . import service
from .schemas import REGISTRY

router = APIRouter()


@router.post("/extractions")
async def create_extraction(
    request: Request,
    wait: int = Query(default=40, ge=0, le=40),
    principal: Principal = Depends(require_scope("extractions:write")),
):
    """ONE endpoint, TWO flows (architecture §5.2):
      * the request contains a file (multipart `file` part, or JSON `file.content_base64`)
        -> DIRECT flow: wait up to `wait` seconds and return the result (200) or 202;
      * no file -> PRESIGNED flow: return a one-time upload URL (201).
    """
    content_type = request.headers.get("content-type", "")
    doc_type: str | None = None
    data: bytes | None = None

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        doc_type = form.get("document_type") or None
        upload = form.get("file")
        if upload is not None and hasattr(upload, "read"):
            data = await upload.read(settings.max_inline_bytes + 1)   # read at most limit+1 bytes
            if len(data) > settings.max_inline_bytes:
                raise ApiError(413, "file_too_large", "The file is larger than 4 MB. Use the upload-link flow.")
    else:
        try:
            body = await request.json() if await request.body() else {}
        except ValueError:
            raise ApiError(400, "invalid_json", "The request body is not valid JSON.") from None
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_json", "The request body must be a JSON object.")
        doc_type = body.get("document_type")
        if isinstance(body.get("wait"), int):
            wait = max(0, min(body["wait"], settings.max_wait_seconds))
        file_obj = body.get("file")
        if isinstance(file_obj, dict) and "content_base64" in file_obj:
            data = service.decode_base64_strict(file_obj["content_base64"])

    doc_type = service.validate_doc_type(doc_type)
    if data is not None:
        status, payload, headers = await service.create_direct(principal, doc_type, data, wait)
    else:
        status, payload, headers = service.create_presigned(principal, doc_type)
    return JSONResponse(status_code=status, content=payload, headers=headers)


@router.get("/extractions")
def list_extractions(
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    principal: Principal = Depends(require_scope("extractions:read")),
):
    return service.list_jobs(principal, limit, cursor)


@router.get("/extractions/{job_id}")
async def get_extraction(
    job_id: str,
    wait: int = Query(default=0, ge=0, le=40),
    principal: Principal = Depends(require_scope("extractions:read")),
):
    """Always 200 with the job; `result` is present once status == "succeeded" (FR-2.16)."""
    body, headers = await service.get_job(principal, job_id, wait)
    return JSONResponse(content=body, headers=headers)


@router.delete("/extractions/{job_id}", status_code=204)
def delete_extraction(job_id: str, principal: Principal = Depends(require_scope("extractions:write"))):
    service.delete_job(principal, job_id)
    return Response(status_code=204)


@router.post("/extractions/{job_id}/corrections", status_code=201)
async def add_corrections(job_id: str, request: Request,
                          principal: Principal = Depends(require_scope("extractions:write"))):
    try:
        body = await request.json()
    except ValueError:
        raise ApiError(400, "invalid_json", "The request body is not valid JSON.") from None
    corrections = body.get("corrections") if isinstance(body, dict) else None
    if not isinstance(corrections, list) or not corrections:
        raise ApiError(400, "invalid_correction", "Send {\"corrections\": [{\"path\": ..., \"value\": ...}]}.")
    return service.add_corrections(principal, job_id, corrections)


@router.get("/document-types")
def document_types(principal: Principal = Depends(require_scope("extractions:read"))):
    """FR-2.13: every supported type with its JSON schema and schema version."""
    return {"data": [
        {"name": t.name, "schema_version": t.version, "json_schema": t.model.model_json_schema()}
        for t in REGISTRY.values()
    ]}
