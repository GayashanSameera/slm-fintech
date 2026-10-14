"""AWS clients and TENANT-SCOPED credentials.

Two kinds of AWS access exist in this platform:

1. **Platform access** (`platform_session()`): the Lambda's own IAM role.
   It is used for things that are not tenant data: reading API-key lookup items,
   sending queue messages, calling Bedrock.

2. **Tenant access** (`tenant_session(tenant_id)`): temporary credentials that
   ONLY work for one tenant's data. We get them by assuming the `tenant-data`
   IAM role with a session tag `tenant_id=<t>`. That role's policy allows:
     * DynamoDB items whose partition key is "TENANT#<t>"
     * S3 objects under "tenants/<t>/"
   So if code ever builds a key for the wrong tenant, AWS itself refuses
   (AccessDenied). This is layer 2 of tenant isolation (security.md §4.2).

Locally (SLM_ENV=local) there is no IAM, so both return a normal session and the
code path stays identical.
"""

from __future__ import annotations

import threading
import time
from functools import lru_cache

import boto3
from botocore.config import Config

from app.config import settings

# Retries with backoff for transient AWS errors; "adaptive" also slows down when throttled.
_BOTO_CONFIG = Config(retries={"max_attempts": 4, "mode": "adaptive"})

_lock = threading.Lock()
_tenant_sessions: dict[str, tuple[float, boto3.Session]] = {}
_SESSION_DURATION_S = 3600   # 1 hour: the longest STS allows when a role assumes a role
# A presigned upload URL stops working when the credentials that signed it expire.
# So a cached session is replaced while it still has MORE than one full upload-URL
# lifetime left (15 min + 1 min spare). Every URL we hand out is valid for its whole TTL.
_REFRESH_MARGIN_S = settings.upload_url_ttl_seconds + 60


@lru_cache(maxsize=1)
def platform_session() -> boto3.Session:
    return boto3.Session(region_name=settings.region)


def tenant_session(tenant_id: str) -> boto3.Session:
    """A boto3 session whose credentials only reach `tenant_id`'s data (cached up to ~44 min)."""
    if settings.is_local or not settings.tenant_data_role_arn:
        return platform_session()

    with _lock:
        cached = _tenant_sessions.get(tenant_id)
        if cached and cached[0] - _REFRESH_MARGIN_S > time.time():
            return cached[1]

    sts = platform_session().client("sts", config=_BOTO_CONFIG)
    creds = sts.assume_role(
        RoleArn=settings.tenant_data_role_arn,
        RoleSessionName=f"tenant-{tenant_id}"[:64],
        DurationSeconds=_SESSION_DURATION_S,
        # The session tag becomes aws:PrincipalTag/tenant_id inside IAM policies.
        Tags=[{"Key": "tenant_id", "Value": tenant_id}],
    )["Credentials"]
    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=settings.region,
    )
    expires = creds["Expiration"].timestamp()
    with _lock:
        _tenant_sessions[tenant_id] = (expires, session)
    return session


# --- Client factories --------------------------------------------------------------
# Each takes an optional session so the same helper works for platform and tenant access.

def dynamodb_table(session: boto3.Session | None = None):
    s = session or platform_session()
    return s.resource("dynamodb", endpoint_url=settings.dynamodb_endpoint, config=_BOTO_CONFIG).Table(
        settings.table
    )


def s3_client(session: boto3.Session | None = None, public: bool = False):
    """`public=True` is used to sign upload URLs that a client OUTSIDE docker will call
    (locally MinIO is 'minio:9000' inside docker but 'localhost:9000' outside)."""
    s = session or platform_session()
    endpoint = settings.s3_public_endpoint if (public and settings.s3_public_endpoint) else settings.s3_endpoint
    cfg = _BOTO_CONFIG.merge(Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    return s.client("s3", endpoint_url=endpoint, config=cfg)


@lru_cache(maxsize=1)
def sqs_client():
    return platform_session().client("sqs", endpoint_url=settings.sqs_endpoint, config=_BOTO_CONFIG)


@lru_cache(maxsize=8)
def queue_url(name: str) -> str:
    """Look up a queue URL by name once and cache it."""
    return sqs_client().get_queue_url(QueueName=name)["QueueUrl"]


def reset_caches() -> None:
    """Used by tests to start from a clean state."""
    platform_session.cache_clear()
    sqs_client.cache_clear()
    queue_url.cache_clear()
    with _lock:
        _tenant_sessions.clear()
