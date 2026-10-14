"""Every DynamoDB key and S3 object key is built HERE, and only here.

Why one module? Tenant isolation depends on every key starting with the
tenant's prefix (security.md §4). If keys were built ad hoc all over the code,
one forgotten prefix would be a data leak. Building them in one place makes the
rule easy to review and impossible to forget.

DynamoDB single-table layout (architecture §7.1):

    PK                    SK                          item
    --------------------  --------------------------  ---------------------------
    APIKEY#<hmac>         APIKEY                      api key -> tenant lookup
    TENANT#<t>            PROFILE                     tenant name, status, settings
    TENANT#<t>            KEY#<key_id>                key metadata (no secret)
    TENANT#<t>            JOB#<job_id>                one extraction job
    TENANT#<t>            CORR#<job_id>#<ulid>        one correction for a job
    TENANT#<t>            DEDUP#<doc_type>#<sha256>   "this file was already processed"
    TENANT#<t>            USAGE#<yyyy-mm-dd>          daily usage counters

S3 layout (architecture §7.2):

    tenants/<t>/jobs/<job_id>/source         the uploaded file
    tenants/<t>/jobs/<job_id>/result.json    results > 100 KB
    tenants/<t>/training/<doc_type>/<job_id>/...   consented copies (FR-6)
"""

from __future__ import annotations

import re


def tenant_pk(tenant_id: str) -> str:
    return f"TENANT#{tenant_id}"


def api_key_pk(key_hash: str) -> str:
    return f"APIKEY#{key_hash}"


def job_sk(job_id: str) -> str:
    return f"JOB#{job_id}"


def correction_sk(job_id: str, corr_id: str) -> str:
    return f"CORR#{job_id}#{corr_id}"


def corrections_prefix(job_id: str) -> str:
    return f"CORR#{job_id}#"


def dedup_sk(doc_type: str | None, sha256: str) -> str:
    return f"DEDUP#{doc_type or 'auto'}#{sha256}"


def usage_sk(day: str) -> str:
    return f"USAGE#{day}"


def key_sk(key_id: str) -> str:
    return f"KEY#{key_id}"


# --- S3 ----------------------------------------------------------------------------

def tenant_prefix(tenant_id: str) -> str:
    return f"tenants/{tenant_id}/"


def job_prefix(tenant_id: str, job_id: str) -> str:
    return f"tenants/{tenant_id}/jobs/{job_id}/"


def source_key(tenant_id: str, job_id: str) -> str:
    return f"{job_prefix(tenant_id, job_id)}source"


def result_key(tenant_id: str, job_id: str) -> str:
    return f"{job_prefix(tenant_id, job_id)}result.json"


def training_prefix(tenant_id: str, doc_type: str, job_id: str) -> str:
    return f"tenants/{tenant_id}/training/{doc_type}/{job_id}/"


# Strict pattern for keys coming from S3 events. Anything else is ignored.
_SOURCE_KEY_RE = re.compile(r"^tenants/(t_[0-9a-z]{26})/jobs/(ext_[0-9A-HJKMNP-TV-Z]{26})/source$")


def parse_source_key(key: str) -> tuple[str, str] | None:
    """Return (tenant_id, job_id) for a valid upload key, else None.

    Used by the intake Lambda: S3/GuardDuty events carry the object key, and we
    must not trust its shape blindly."""
    m = _SOURCE_KEY_RE.match(key or "")
    return (m.group(1), m.group(2)) if m else None
