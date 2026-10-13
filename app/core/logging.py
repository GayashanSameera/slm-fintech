"""Structured (JSON) logging.

Every log line is one JSON object, for example:

    {"ts": "2026-10-13T10:15:02.120Z", "level": "INFO", "logger": "slm.http",
     "msg": "request", "method": "GET", "path": "/healthz", "status": 200, "ms": 3}

Why JSON? CloudWatch Logs can filter and count JSON fields (e.g. "all status=500 in the
last hour"), which plain text makes hard.

THE RULE (NFR-S3, coding rule 2 in the implementation guide):
    Never log document content, extracted values, prompts, model output, API keys or
    original file names. Log ids, codes, counts and timings only.
Pass extra fields with `extra={...}`; they are added to the JSON object.
"""

import json
import logging
import sys
from datetime import UTC, datetime

# Attributes every LogRecord has. Anything else on the record came from `extra={...}`.
_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Copy the fields passed with extra={...}.
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            # Stack traces show code locations, not data, so they are safe to log.
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Sends all logs to stdout (the terminal, or `docker compose logs`) as JSON."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Library loggers are noisy at INFO; keep them to warnings.
    for noisy in ("httpx", "httpcore", "PIL", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
