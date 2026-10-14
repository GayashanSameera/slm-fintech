"""The API application.

Two ways it runs, same code (requirement NFR-O2):
  * Locally:  `uvicorn app.main:app` (docker compose service "api").
  * In AWS:   API Gateway -> Lambda. `handler` (Mangum) converts the API Gateway
              event into a normal ASGI request for FastAPI, and the response back.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mangum import Mangum

from app.audit import audit
from app.core.errors import ApiError
from app.core.logging import get_logger
from app.modules.chat.api import router as chat_router
from app.modules.extraction.api import router as extraction_router

log = get_logger(__name__)

app = FastAPI(title="SLM Fintech API", version="1.0.0", docs_url="/docs", redoc_url=None)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    # One error shape for every error: {"error": {"code": ..., "message": ...}}.
    return JSONResponse(status_code=exc.status, headers=exc.headers,
                        content={"error": {"code": exc.code, "message": exc.message}})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # FastAPI's default 422 body echoes the submitted input. NFR-S8 forbids that:
    # report field locations and error types only.
    details = [{"field": ".".join(str(p) for p in e["loc"]), "error": e["type"]} for e in exc.errors()]
    return JSONResponse(status_code=400, content={"error": {
        "code": "invalid_request", "message": "The request is not valid.", "details": details}})


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    # Log the exception TYPE only (never its message: it may contain data).
    log.error("unhandled_error", exc=exc, path=request.url.path)
    return JSONResponse(status_code=500, content={"error": {
        "code": "internal_error", "message": "Something went wrong. Please retry."}})


def _route_template(path: str, path_params: dict) -> str:
    """'/v1/extractions/ext_01J...' + {'job_id': 'ext_01J...'} -> '/v1/extractions/{job_id}'.

    Built from the real path because routers included with a prefix only know their own
    part of it ('/extractions/{job_id}'). Each path segment that equals a path-parameter
    value is replaced by the parameter's name.
    """
    by_value = {str(v): k for k, v in path_params.items()}
    return "/".join(f"{{{by_value[s]}}}" if s in by_value else s for s in path.split("/"))


@app.middleware("http")
async def audit_every_request(request: Request, call_next):
    """One audit record per API request (requirement NFR-S4): who, what, which job, outcome.

    `require_scope` (app/auth/dependencies.py) stores the caller in request.state.principal,
    and FastAPI's router writes the path parameters into the request scope, which this
    middleware shares. So after the route has run we know the tenant, the key, the route
    TEMPLATE ("/v1/extractions/{job_id}", never a raw URL with query strings) and the job
    id. Only ids and codes are written, never request content.

    Business events (e.g. "extraction.create" with result "ok:dedup") are additionally
    audited inside service.py; this record is the per-request trail around them.
    """
    response = await call_next(request)
    if request.url.path == "/healthz":
        return response                       # health checks are not tenant actions
    principal = getattr(request.state, "principal", None)
    path_params = request.scope.get("path_params") or {}
    status = response.status_code
    audit(
        tenant_id=principal.tenant_id if principal else None,
        key_id=principal.key_id if principal else None,
        action=f"{request.method} {_route_template(request.url.path, path_params)}",
        resource_id=path_params.get("job_id"),
        result="ok" if status < 400 else ("denied" if status in (401, 403) else f"error:{status}"),
        source_ip=request.client.host if request.client else None,
    )
    return response


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


app.include_router(extraction_router, prefix="/v1")
app.include_router(chat_router, prefix="/v1")

# AWS Lambda entry point (api_gateway_base_path strips nothing; routes already include /v1).
handler = Mangum(app, lifespan="off")
