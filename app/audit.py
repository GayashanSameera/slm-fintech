"""Audit log (requirement NFR-S4).

One record per API action: who (tenant, key), what (action, resource), when,
and the result. Records go to stdout with a distinct logger name; in AWS a
CloudWatch Logs subscription/metric filter routes them to the dedicated `audit`
log group, which is KMS-encrypted and kept for 180 days (NFR-C4).

Audit records contain IDs only, never document content or extracted values.
"""

from __future__ import annotations

import json
import logging

from app.core.clock import now_iso

_audit = logging.getLogger("audit")


def audit(*, tenant_id: str | None, key_id: str | None, action: str, resource_id: str | None,
          result: str, source_ip: str | None = None) -> None:
    """Write one audit record (JSON, logger "audit"): who, what, which resource, outcome, source IP.
    Ids and codes only, never request content.
    """
    _audit.info(json.dumps({
        "type": "audit",
        "ts": now_iso(),
        "tenant_id": tenant_id,
        "key_id": key_id,
        "action": action,              # e.g. "extraction.create", "extraction.get"
        "resource_id": resource_id,    # e.g. the job id
        "result": result,              # "ok" | "denied" | "error:<code>"
        "source_ip": source_ip,
    }))
