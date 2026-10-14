"""The job repository: every read and write of an extraction job.

One job = one DynamoDB item at PK="TENANT#<t>", SK="JOB#<job_id>".

Status lifecycle (architecture §4.4); status only ever moves FORWARD:

    awaiting_upload ──► scanning ──► queued ──► processing ──► succeeded
          │                │            ▲            │      └──► failed
          ▼                ▼            └────────────┘ (temporary error: retry)
       expired           failed

Every status change is a *conditional update*: "set status to X only if it is
currently Y". If two workers race for the same job, exactly one wins and the
other gets False back. That is what makes processing idempotent.

ALL methods use the tenant-scoped session (see app/storage/aws.py), so AWS IAM
enforces that this repository can only touch its own tenant's items.
"""

from __future__ import annotations

from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app.core.clock import epoch_in, epoch_now, now_iso
from app.storage import keys
from app.storage.aws import dynamodb_table, tenant_session
from app.storage.ddb import from_ddb, to_ddb

FINAL_STATUSES = frozenset({"succeeded", "failed", "expired"})

# Items for jobs still waiting for an upload or a scan result are put in a sparse
# GSI ("pending" index) so the scan-sweeper can find stuck jobs without scanning
# the whole table. The attribute is removed once the job moves on.
PENDING_GSI = "pending"
PENDING_PK_VALUE = "PENDING"


def _expired(item: dict) -> bool:
    """True once the tenant's retention period is over (NFR-C5).

    DynamoDB TTL deletes expired items only "usually within a few days", not at the
    exact second. Until then the item still exists, so EVERY read checks `expires_at`
    itself: an expired job, result or dedup entry is treated as if already deleted.
    """
    exp = item.get("expires_at")
    return exp is not None and int(exp) <= epoch_now()


class JobRepo:
    def __init__(self, tenant_id: str):
        """All job reads and writes for ONE tenant, through that tenant's scoped DynamoDB session."""
        self.tenant_id = tenant_id
        self.table = dynamodb_table(tenant_session(tenant_id))
        self.pk = keys.tenant_pk(tenant_id)

    # --- create / read -------------------------------------------------------------

    def create(self, job_id: str, *, status: str, flow: str, doc_type: str | None,
               retention_days: int, pending_deadline: int | None = None, **extra: Any) -> dict:
        """Create a new job item. Fails if the id already exists (it never should)."""
        item: dict[str, Any] = {
            "PK": self.pk,
            "SK": keys.job_sk(job_id),
            "id": job_id,
            "tenant_id": self.tenant_id,
            "status": status,
            "flow": flow,                         # "direct" | "presigned"
            "requested_doc_type": doc_type,       # what the tenant asked for (None = auto-detect)
            "created_at": now_iso(),
            # DynamoDB TTL deletes the item after the tenant's retention period (NFR-C5).
            "expires_at": epoch_in(retention_days * 86400),
            "warnings": [],
            **extra,
        }
        if pending_deadline is not None:
            item["pending_pk"] = PENDING_PK_VALUE
            item["pending_deadline"] = pending_deadline
        self.table.put_item(Item=to_ddb(item), ConditionExpression="attribute_not_exists(PK)")
        return from_ddb(item)

    def get(self, job_id: str, consistent: bool = False) -> dict | None:
        """Read one job. `consistent=True` guarantees we see the latest write (used by the wait loop)."""
        resp = self.table.get_item(Key={"PK": self.pk, "SK": keys.job_sk(job_id)}, ConsistentRead=consistent)
        item = resp.get("Item")
        return from_ddb(item) if item and not _expired(item) else None

    def list(self, limit: int = 20, cursor: str | None = None) -> tuple[list[dict], str | None]:
        """Newest jobs first (FR-2.8). Returns (jobs, next_cursor)."""
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("PK").eq(self.pk) & Key("SK").begins_with("JOB#"),
            "ScanIndexForward": False,      # ULIDs sort by time -> descending = newest first
            "Limit": limit,
        }
        if cursor:
            kwargs["ExclusiveStartKey"] = {"PK": self.pk, "SK": keys.job_sk(cursor)}
        resp = self.table.query(**kwargs)
        # Expired jobs are dropped (see _expired). A page can then hold fewer than
        # `limit` items; the cursor still continues correctly from the last key read.
        items = [from_ddb(i) for i in resp.get("Items", []) if not _expired(i)]
        last = resp.get("LastEvaluatedKey")
        next_cursor = last["SK"].removeprefix("JOB#") if last else None
        return items, next_cursor

    # --- status changes ------------------------------------------------------------

    def transition(self, job_id: str, from_statuses: list[str], to_status: str,
                   remove: list[str] | None = None, **set_fields: Any) -> bool:
        """Move a job to `to_status` ONLY if it is currently in `from_statuses`.

        Returns True if we changed it, False if someone else already did (not an error).
        `set_fields` are extra attributes to set in the same atomic write.
        `remove` lists attributes to delete (e.g. the pending-index markers).
        """
        names = {"#s": "status"}
        values: dict[str, Any] = {":to": to_status}
        sets = ["#s = :to"]
        for i, status in enumerate(from_statuses):
            values[f":f{i}"] = status
        for i, (field, value) in enumerate(set_fields.items()):
            names[f"#e{i}"] = field
            values[f":e{i}"] = to_ddb(value)
            sets.append(f"#e{i} = :e{i}")
        expr = "SET " + ", ".join(sets)
        if remove:
            for i, field in enumerate(remove):
                names[f"#r{i}"] = field
            expr += " REMOVE " + ", ".join(f"#r{i}" for i in range(len(remove)))
        condition = "#s IN (" + ", ".join(f":f{i}" for i in range(len(from_statuses))) + ")"
        try:
            self.table.update_item(
                Key={"PK": self.pk, "SK": keys.job_sk(job_id)},
                UpdateExpression=expr,
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False     # another process moved it first, or it doesn't exist
            raise

    def claim(self, job_id: str, lease_seconds: int) -> bool:
        """Worker takes the job: queued -> processing, with a LEASE.

        Why a lease? If a worker Lambda crashes or times out halfway, the job is left
        in "processing" and SQS delivers the message again. Without a lease, the
        retry could never claim it ("not queued") and the job would be stuck forever.
        With a lease, a retry may take over a "processing" job once its lease expired.
        """
        now = epoch_in(0)
        try:
            self.table.update_item(
                Key={"PK": self.pk, "SK": keys.job_sk(job_id)},
                UpdateExpression="SET #s = :processing, lease_until = :lease, started_at = :started "
                                 "ADD attempts :one",
                ConditionExpression="#s = :queued OR (#s = :processing AND lease_until < :now)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":processing": "processing", ":queued": "queued", ":now": now,
                                           ":lease": now + lease_seconds, ":started": now_iso(), ":one": 1},
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def complete(self, job_id: str, remove: list[str] | None = None, **fields: Any) -> bool:
        """processing -> succeeded, storing the result and how it was produced."""
        return self.transition(job_id, ["processing"], "succeeded", remove=remove,
                               completed_at=now_iso(), **fields)

    def fail(self, job_id: str, error_code: str, from_statuses: list[str] | None = None, **fields: Any) -> bool:
        """-> failed with `error_code`. `fields` are stored too (e.g. the tokens a failed job used)."""
        return self.transition(
            job_id,
            from_statuses or ["processing", "queued", "scanning", "awaiting_upload"],
            "failed",
            remove=["pending_pk", "pending_deadline"],
            completed_at=now_iso(),
            error_code=error_code,
            **fields,
        )

    # --- delete (FR-2.9) -----------------------------------------------------------

    def delete(self, job_id: str) -> None:
        """Delete the job item and all its corrections. (S3 data is deleted by the caller.)"""
        resp = self.table.query(
            KeyConditionExpression=Key("PK").eq(self.pk) & Key("SK").begins_with(keys.corrections_prefix(job_id))
        )
        with self.table.batch_writer() as batch:
            for item in resp.get("Items", []):
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            batch.delete_item(Key={"PK": self.pk, "SK": keys.job_sk(job_id)})

    # --- corrections (FR-2.12) ------------------------------------------------------

    def add_corrections(self, job_id: str, corr_id: str, corrections: list[dict], expires_at: int) -> None:
        """Store one batch of corrections for a job; they expire together with the job."""
        self.table.put_item(Item=to_ddb({
            "PK": self.pk,
            "SK": keys.correction_sk(job_id, corr_id),
            "job_id": job_id,
            "corrections": corrections,       # [{"path": "...", "value": "..."}]
            "created_at": now_iso(),
            "expires_at": expires_at,         # deleted together with the job
        }))

    # --- dedup (FR-2.11) -----------------------------------------------------------

    def find_dedup(self, doc_type: str | None, sha256: str) -> str | None:
        """Return the job id that already processed this exact file for this tenant, if any."""
        item = self.table.get_item(Key={"PK": self.pk, "SK": keys.dedup_sk(doc_type, sha256)}).get("Item")
        return item["job_id"] if item and not _expired(item) else None

    def save_dedup(self, doc_type: str | None, sha256: str, job_id: str, expires_at: int) -> None:
        """Remember which job processed this exact file (SHA-256) for this tenant and type, until the
        job expires, so an identical upload is answered with that job (FR-2.11).
        """
        self.table.put_item(Item={
            "PK": self.pk, "SK": keys.dedup_sk(doc_type, sha256),
            "job_id": job_id, "expires_at": expires_at,
        })
