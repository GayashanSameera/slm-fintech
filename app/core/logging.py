"""Safe, structured logging.

THE MOST IMPORTANT SAFETY NET AGAINST LEAKING CUSTOMER DATA (security.md §7.4).

Every log line is one JSON object. Only fields whose NAME is in ``ALLOWED_FIELDS``
are written; anything else is silently dropped. So even a careless call such as

    log.info("debug", text=page_text)          # 'text' is not allowlisted

cannot leak document content: the ``text`` field never reaches the log.

Exceptions are logged by TYPE only (e.g. "ValidationError"), never ``str(exc)``,
because exception messages can contain the values that caused them.

Usage:
    from app.core.logging import get_logger
    log = get_logger(__name__)
    log.info("job_started", tenant_id=t, job_id=j, pages=4)
"""

from __future__ import annotations

import json
import logging
import sys
import time

# The ONLY field names that may appear in logs. Add a name here only after
# checking it can never hold document content, extracted values or file names.
ALLOWED_FIELDS = frozenset(
    {
        "tenant_id", "job_id", "key_id", "doc_type", "status", "from_status", "to_status",
        "error_code", "exc_type", "pages", "rung", "model_id", "parser_id", "agree",
        "issue_count", "input_tokens", "output_tokens", "cache_read_tokens", "duration_ms",
        "attempt", "count", "queue", "flow", "method", "path", "http_status", "wait_s",
        "source", "warnings", "checks_failed", "event_type",
    }
)

_configured = False


def _configure_root(level: str = "INFO") -> None:
    """Send plain lines to stdout. Lambda forwards stdout to CloudWatch Logs."""
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))   # we format JSON ourselves
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Third-party libraries can be chatty (and boto3 debug logs may include payloads).
    for noisy in ("botocore", "boto3", "urllib3", "pdfminer", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


class SafeLogger:
    """A logger that only ever writes allowlisted fields."""

    def __init__(self, name: str):
        self._log = logging.getLogger(name)

    @staticmethod
    def _filter(fields: dict) -> dict:
        # Keep allowlisted keys only. Values are converted to str by json.dumps(default=str).
        return {k: v for k, v in fields.items() if k in ALLOWED_FIELDS}

    def _emit(self, level: int, event: str, fields: dict) -> None:
        record = {"ts": round(time.time(), 3), "level": logging.getLevelName(level), "event": event}
        record.update(self._filter(fields))
        self._log.log(level, json.dumps(record, default=str))

    def info(self, event: str, **fields) -> None:
        self._emit(logging.INFO, event, fields)

    def warning(self, event: str, **fields) -> None:
        self._emit(logging.WARNING, event, fields)

    def error(self, event: str, exc: BaseException | None = None, **fields) -> None:
        if exc is not None:
            # Type name only. NEVER str(exc) or the traceback's local variables.
            fields["exc_type"] = type(exc).__name__
        self._emit(logging.ERROR, event, fields)


def get_logger(name: str) -> SafeLogger:
    from app.config import settings  # imported here to avoid a circular import at startup

    _configure_root(settings.log_level)
    return SafeLogger(name)
