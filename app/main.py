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


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


app.include_router(extraction_router, prefix="/v1")
app.include_router(chat_router, prefix="/v1")

# AWS Lambda entry point (api_gateway_base_path strips nothing; routes already include /v1).
handler = Mangum(app, lifespan="off")
