"""HTTP routes for extraction. Each route reads the request and calls service.py.

    GET    /v1/document-types              supported types and their JSON schemas
    POST   /v1/extractions                 upload one file + document_type → result (200)
    GET    /v1/extractions/{id}            a job and its result
    GET    /v1/extractions?limit=20        recent jobs, newest first (no results, for speed)
    DELETE /v1/extractions/{id}            erase the file, result and record (204)

This is a local proof of concept: there is no authentication. The server listens on
127.0.0.1 only, so only this machine can reach it.

The routes are plain `def` (not `async def`): FastAPI runs them in a thread pool, which is
right for slow blocking work like PDF rendering and waiting for the model.
"""

from fastapi import APIRouter, File, Form, Query, Request, Response, UploadFile

from app.modules.extraction import service
from app.modules.extraction.models import DocumentTypeOut, JobOut

router = APIRouter(prefix="/v1", tags=["extraction"])


@router.get("/document-types", response_model=list[DocumentTypeOut])
def list_document_types():
    return service.list_document_types()


@router.post("/extractions", response_model=JobOut)
def create_extraction(
    request: Request,
    file: UploadFile = File(..., description="PDF, DOCX, JPG or PNG"),
    document_type: str = Form(..., description="invoice, bank_statement, payslip, identity_document or kyc_form"),
):
    deps = request.app.state.deps
    # Read at most one byte more than the limit: enough to know the file is too big without
    # loading a huge upload into memory.
    data = file.file.read(deps.settings.max_upload_bytes + 1)
    return service.extract_document(deps, document_type, data)


@router.get("/extractions/{job_id}", response_model=JobOut)
def get_extraction(job_id: str, request: Request):
    return service.get_job(request.app.state.deps, job_id)


@router.get("/extractions", response_model=list[JobOut])
def list_extractions(request: Request, limit: int = Query(20, ge=1, le=100)):
    return service.list_jobs(request.app.state.deps, limit)


@router.delete("/extractions/{job_id}", status_code=204)
def delete_extraction(job_id: str, request: Request):
    service.delete_job(request.app.state.deps, job_id)
    return Response(status_code=204)
