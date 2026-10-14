"""Shared test setup.

* Environment variables are set BEFORE the app is imported, because
  `app.config.settings` is read once at import time.
* `aws` fixture: an in-memory AWS (moto) with the same table, bucket and queues
  as a real environment, so storage code runs for real against fake AWS.
* `tenant` fixture: a tenant with an API key, ready to call the API.
"""

from __future__ import annotations

import os

# --- configure the app for tests (must happen before any `import app...`) ---------------
os.environ.update({
    "SLM_ENV": "local",                 # no IAM role switching, no KMS, no GuardDuty
    "SLM_MODEL_PROVIDER": "fake",
    "SLM_REGION": "us-east-1",
    "SLM_TABLE": "slm-test",
    "SLM_BUCKET": "slm-docs-test",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
})

import boto3  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

from app.storage import aws as aws_module  # noqa: E402


def create_resources() -> None:
    """Create the same resources as infra/cloudformation/data.yaml + app.yaml (minus security)."""
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="slm-test",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "pending_pk", "AttributeType": "S"},
            {"AttributeName": "pending_deadline", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "pending",
            "KeySchema": [{"AttributeName": "pending_pk", "KeyType": "HASH"},
                          {"AttributeName": "pending_deadline", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="slm-docs-test")
    sqs = boto3.client("sqs", region_name="us-east-1")
    for name in ("jobs-interactive", "jobs-background", "webhooks"):
        sqs.create_queue(QueueName=name)


@pytest.fixture
def aws():
    with mock_aws():
        aws_module.reset_caches()
        create_resources()
        from app.gateway import get_gateway
        get_gateway.cache_clear()
        yield
        aws_module.reset_caches()


@pytest.fixture
def tenant(aws):
    """A fresh tenant with a read+write API key. Returns (tenant_id, api_key)."""
    from app.core.ids import new_tenant_id
    from app.storage.tenants import create_api_key, create_tenant

    tenant_id = new_tenant_id()
    create_tenant(tenant_id, "Test Tenant")
    key = create_api_key(tenant_id, ["extractions:read", "extractions:write"], live=False)
    return tenant_id, key


@pytest.fixture
def fake_gateway(monkeypatch):
    """A FakeGateway installed as THE gateway, so tests control every model answer."""
    import app.modules.extraction.worker as worker_module
    from app.gateway.fake import FakeGateway

    gw = FakeGateway()
    monkeypatch.setattr(worker_module, "get_gateway", lambda: gw)
    return gw
