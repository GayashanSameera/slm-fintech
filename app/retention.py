"""Retention clean-up (requirement NFR-C5).

DynamoDB TTL deletes a job item when its `expires_at` passes (the tenant's
retention period: 7, 30, 90 or 365 days). TTL only deletes the DATABASE item,
so this Lambda listens to the table's stream and deletes the job's S3 objects
(result.json and anything left under the job prefix).

The stream trigger in infra/cloudformation/app.yaml filters for TTL deletions
only (userIdentity.principalId = "dynamodb.amazonaws.com"), so normal deletes
by the API don't invoke this function.

S3 lifecycle rules are the backstop if this ever misses an object.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.storage import keys
from app.storage.blobs import Blobs

log = get_logger(__name__)


def handle_stream(event: dict, context=None) -> dict:
    """Lambda entry point (DynamoDB stream, TTL deletions only): delete each expired job's S3
    folder. Returns the number of objects deleted.
    """
    deleted = 0
    for record in event.get("Records", []):
        if record.get("eventName") != "REMOVE":
            continue
        old = (record.get("dynamodb") or {}).get("OldImage") or {}
        sk = (old.get("SK") or {}).get("S", "")
        tenant_id = (old.get("tenant_id") or {}).get("S")
        if not sk.startswith("JOB#") or not tenant_id:
            continue
        job_id = sk.removeprefix("JOB#")
        deleted += Blobs(tenant_id).delete_prefix(keys.job_prefix(tenant_id, job_id))
    log.info("retention_cleanup", count=deleted)
    return {"deleted_objects": deleted}
