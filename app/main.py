"""Builds the FastAPI application.

create_app() is a "factory": it returns a new, fully configured app. The real entry point
(app/asgi.py) calls it once; tests call it with their own Deps (temporary data folder,
fake model).

What gets set up, in order:
    1. logging (JSON lines)
    2. the app itself; the interactive docs page (/docs) only when running locally
    3. app.state.deps: settings, the job store, the model gateway
    4. error handlers: one JSON error format for every failure
    5. middleware: request id + one log line per request; CORS if configured
    6. routes: /healthz, /v1/document-types, /v1/extractions…
    7. locally: the test UI at /ui (open http://localhost:8000 in a browser)
"""

import logging
import mimetypes
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.deps import Deps
from app.modules.extraction.api import router as extraction_router

log = logging.getLogger("slm.http")

# Browsers only run JavaScript modules served as JavaScript. Python's MIME table comes from
# the operating system and does not always know ".mjs" (used by pdf.js), so register it.
mimetypes.add_type("text/javascript", ".mjs")

# The test UI lives in tools/local-ui at the repository root.
UI_DIR = Path(__file__).resolve().parent.parent / "tools" / "local-ui"


def create_app(deps: Deps | None = None) -> FastAPI:
    deps = deps or Deps()
    settings = deps.settings
    configure_logging(settings.log_level)

    app = FastAPI(
        title="SLM Fintech API",
        version="0.1.0",
        # The /docs page describes and lets you try every route. Local only.
        docs_url="/docs" if settings.is_local else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.is_local else None,
    )
    app.state.deps = deps
    install_error_handlers(app)

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        """Gives every request an id and writes ONE log line when it finishes.

        The id comes back in the X-Request-ID header, so a problem report can quote it and
        we can find the matching log line. Only method, path, status and time are logged:
        never headers or bodies (documents).
        """
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        if not request.url.path.startswith("/ui"):  # static UI files would only add noise
            log.info(
                "request",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "ms": round((time.perf_counter() - started) * 1000),
                },
            )
        return response

    # CORS lets a web page from ANOTHER origin call this API from the browser (e.g. the test
    # UI served separately on :8090). Not needed for the UI at /ui, which is the same origin.
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID"],
        )

    @app.get("/healthz", tags=["health"])
    def healthz() -> dict:
        """Liveness check: the process is up. No model call, so it is instant."""
        return {"status": "ok"}

    app.include_router(extraction_router)

    # Local only: serve the test UI from this same server, so one command starts everything.
    if settings.is_local and UI_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def root() -> RedirectResponse:
            return RedirectResponse("/ui/")

    return app
