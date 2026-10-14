"""Error types used across the platform.

There are three kinds of errors, and the difference matters:

``ApiError``
    Something wrong with the *request* (bad key, file too big, unknown job...).
    Returned to the client as JSON with an HTTP status and a stable ``code``.

``JobError``
    Something wrong with the *document* (corrupt, encrypted, too many pages...).
    The job is marked ``failed`` with this code. It is NOT retried, because
    retrying the same file would fail the same way.

``TemporaryError``
    Something that may go away by itself (Bedrock throttling, a network blip).
    The worker lets SQS redeliver the job; after 3 attempts it goes to the
    dead-letter queue and an alarm fires (requirement NFR-R1).

GOLDEN RULE (NFR-S8): error messages must never contain submitted values
(document text, extracted fields, file names). Use fixed, generic messages.
"""

from __future__ import annotations


class ApiError(Exception):
    """An error returned to the API client as {"error": {"code": ..., "message": ...}}."""

    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        """status: HTTP status; code: stable machine-readable code; message: fixed human text."""
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


class JobError(Exception):
    """A permanent problem with the document. The job fails with ``code``."""

    def __init__(self, code: str):
        """code: the stable reason the job failed, e.g. "too_many_pages"."""
        super().__init__(code)
        self.code = code


class TemporaryError(Exception):
    """A problem that may succeed on retry. Raised to make SQS redeliver the job."""


# Frequently used API errors, defined once so codes and wording stay consistent.
def not_found() -> ApiError:
    # Also used for OTHER tenants' job ids, so the caller cannot tell whether
    # a job exists in another tenant (security.md §4.1).
    """404 not_found: also for other tenants' ids, so nobody can tell whether they exist."""
    return ApiError(404, "not_found", "Extraction not found.")


def unauthorized() -> ApiError:
    """401 unauthorized: missing or invalid API key."""
    return ApiError(401, "unauthorized", "Missing or invalid API key.")
