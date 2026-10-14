"""Identifier helpers.

We use ULIDs (Universally Unique Lexicographically Sortable Identifiers):
  * 128 bits, of which 80 are random, so they cannot be guessed;
  * they sort by creation time, so "newest first" is a simple reverse sort
    in DynamoDB (requirement FR-2.8).

Prefixes make IDs self-describing in logs and support tickets:
  ext_...  an extraction job
  key_...  an API key id (NOT the secret part of the key)
  t_...    a tenant
"""

from __future__ import annotations

import re

from ulid import ULID

# A job id is "ext_" followed by a 26-character Crockford base32 ULID.
JOB_ID_RE = re.compile(r"^ext_[0-9A-HJKMNP-TV-Z]{26}$")


def new_job_id() -> str:
    """A new job id: "ext_" + a ULID (unique, and sorts by creation time)."""
    return f"ext_{ULID()}"


def new_key_id() -> str:
    # Short, readable id for an API key (the secret is generated separately).
    """A new API-key id (public: shown in key lists and audit records)."""
    return f"key_{str(ULID())[-10:].lower()}"


def new_tenant_id() -> str:
    """A new tenant id: "t_" + a lower-case ULID."""
    return f"t_{str(ULID()).lower()}"


def is_job_id(value: str) -> bool:
    """Validate the shape of a job id BEFORE using it in a database key.
    Rejecting junk early stops odd input from reaching storage code."""
    return bool(JOB_ID_RE.match(value or ""))
