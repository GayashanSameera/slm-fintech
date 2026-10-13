"""One error format for the whole API.

Every failure, whatever caused it, returns the same JSON shape:

    { "error": { "code": "too_many_pages", "message": "Documents can have at most 20 pages." } }

`code` is stable and meant for programs (callers branch on it); `message` is for people.
The codes are listed in docs/processing-flow.md section 6.

Security rule (NFR-S8): error responses never echo the submitted input back. FastAPI's
default validation error includes the rejected value, which could be part of a document,
so we replace that handler.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("slm.errors")


class AppError(Exception):
    """An error we raise on purpose, with an HTTP status, a stable code and a message.

    Example: raise AppError(404, "not_found", "No extraction with this id.")
    """

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_body(code: str, message: str, **extra) -> dict:
    """Builds the standard error JSON. `extra` adds fields such as `details`."""
    return {"error": {"code": code, "message": message, **extra}}


async def _app_error(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=error_body(exc.code, exc.message))


async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Keep only WHERE the problem is (loc) and WHAT is wrong (msg). Drop "input", which
    # would repeat the submitted value.
    details = [{"loc": list(err.get("loc", ())), "msg": err.get("msg", "")} for err in exc.errors()]
    return JSONResponse(
        status_code=422,
        content=error_body("invalid_request", "The request is not valid.", details=details),
    )


# Standard codes for HTTP errors raised by the framework itself (unknown URL, wrong method).
_HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}


async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _HTTP_CODES.get(exc.status_code, f"http_{exc.status_code}")
    return JSONResponse(status_code=exc.status_code, content=error_body(code, str(exc.detail)))


async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # A bug, not a user mistake. Log the exception type and where it happened (no request
    # body, no document data) and return a generic message: internal details can help an
    # attacker and must not leak.
    log.error(
        "unhandled error",
        extra={"path": request.url.path, "error_type": type(exc).__name__},
        exc_info=exc,
    )
    return JSONResponse(status_code=500, content=error_body("internal_error", "Something went wrong on our side."))


def install_error_handlers(app: FastAPI) -> None:
    """Registers all handlers on the app. Called once from app.main.create_app()."""
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected_error)
