"""S3 access for one tenant.

All objects live under "tenants/<tenant_id>/" (see app/storage/keys.py).
The client uses the TENANT-SCOPED session, so S3 refuses any key outside that
prefix in AWS (IAM condition on the `tenant-data` role).

Encryption: in AWS every object is written with SSE-KMS using our customer-
managed "documents" key. We also attach an encryption context
{"tenant_id": "<t>"}; when the optional key-policy condition is switched on
(CloudFormation parameter EnforceTenantEncryptionContext), KMS will only
decrypt an object for a session tagged with the same tenant (security.md §4.3).
"""

from __future__ import annotations

import base64
import json

from botocore.exceptions import ClientError

from app.config import settings
from app.storage import keys
from app.storage.aws import s3_client, tenant_session


class Blobs:
    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id
        self.s3 = s3_client(tenant_session(tenant_id))

    def _sse_args(self) -> dict:
        """Extra put_object arguments that make S3 encrypt with our KMS key (AWS only)."""
        if settings.is_local or not settings.kms_documents_key_id:
            return {}
        context = base64.b64encode(json.dumps({"tenant_id": self.tenant_id}).encode()).decode()
        return {
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": settings.kms_documents_key_id,
            "SSEKMSEncryptionContext": context,
        }

    def _check(self, key: str) -> None:
        # Defence in depth: never touch a key outside this tenant's prefix, even locally.
        if not key.startswith(keys.tenant_prefix(self.tenant_id)):
            raise PermissionError("key outside tenant prefix")

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._check(key)
        self.s3.put_object(Bucket=settings.bucket, Key=key, Body=data, ContentType=content_type,
                           **self._sse_args())

    def get(self, key: str) -> bytes:
        self._check(key)
        return self.s3.get_object(Bucket=settings.bucket, Key=key)["Body"].read()

    def exists(self, key: str) -> bool:
        self._check(key)
        try:
            self.s3.head_object(Bucket=settings.bucket, Key=key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def tags(self, key: str) -> dict[str, str]:
        """Object tags. GuardDuty writes the scan result as tag 'GuardDutyMalwareScanStatus'."""
        self._check(key)
        resp = self.s3.get_object_tagging(Bucket=settings.bucket, Key=key)
        return {t["Key"]: t["Value"] for t in resp.get("TagSet", [])}

    def delete(self, key: str) -> None:
        self._check(key)
        self.s3.delete_object(Bucket=settings.bucket, Key=key)

    def delete_prefix(self, prefix: str) -> int:
        """Delete every object under `prefix` (used for job and tenant deletion)."""
        self._check(prefix)
        deleted = 0
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=settings.bucket, Prefix=prefix):
            objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objects:
                self.s3.delete_objects(Bucket=settings.bucket, Delete={"Objects": objects})
                deleted += len(objects)
        return deleted

    def copy(self, src_key: str, dst_key: str) -> None:
        self._check(src_key)
        self._check(dst_key)
        self.s3.copy_object(Bucket=settings.bucket, Key=dst_key,
                            CopySource={"Bucket": settings.bucket, "Key": src_key}, **self._sse_args())

    def presigned_upload(self, key: str, max_bytes: int, expires_in: int) -> dict:
        """A one-time, write-only upload URL for exactly `key` (presigned POST).

        S3 enforces everything in the signed policy itself:
          * the exact object key (so the upload belongs to exactly one job),
          * a size range of 1 byte .. max_bytes,
          * SSE-KMS with our key (AWS only),
          * the expiry time.
        """
        self._check(key)
        fields: dict[str, str] = {}
        conditions: list = [["content-length-range", 1, max_bytes]]
        if not settings.is_local and settings.kms_documents_key_id:
            fields = {
                "x-amz-server-side-encryption": "aws:kms",
                "x-amz-server-side-encryption-aws-kms-key-id": settings.kms_documents_key_id,
            }
            conditions += [{k: v} for k, v in fields.items()]
        # Sign with the public endpoint so the URL works from outside docker locally.
        signer = s3_client(tenant_session(self.tenant_id), public=True)
        return signer.generate_presigned_post(
            Bucket=settings.bucket, Key=key, Fields=fields, Conditions=conditions, ExpiresIn=expires_in
        )
