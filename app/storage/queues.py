"""SQS helpers.

Queue messages contain ONLY identifiers ({"tenant_id": ..., "job_id": ...}),
never document content or extracted values. A message is a pointer to work,
and the worker reads the actual data from S3/DynamoDB with tenant-scoped
credentials.

Queues (architecture §5, §6.6):
  jobs-interactive   jobs a client is waiting for (processed first, FR-2.17)
  jobs-background    jobs nobody is waiting for
  webhooks           webhook deliveries
Each job queue has a dead-letter queue: after 3 failed receives the message
moves there and an alarm fires (NFR-R1).
"""

from __future__ import annotations

import json

from app.config import settings
from app.storage.aws import queue_url, sqs_client


def send(queue_name: str, body: dict) -> None:
    sqs_client().send_message(QueueUrl=queue_url(queue_name), MessageBody=json.dumps(body))


def send_job(tenant_id: str, job_id: str, interactive: bool = True) -> None:
    """Queue a job for the worker."""
    name = settings.queue_interactive if interactive else settings.queue_background
    send(name, {"tenant_id": tenant_id, "job_id": job_id})


def receive(queue_name: str, max_messages: int = 5, wait_seconds: int = 10) -> list[dict]:
    """Long-poll a queue. Used only by the LOCAL worker loop; in AWS, Lambda polls SQS for us."""
    resp = sqs_client().receive_message(
        QueueUrl=queue_url(queue_name), MaxNumberOfMessages=max_messages, WaitTimeSeconds=wait_seconds
    )
    return resp.get("Messages", [])


def delete(queue_name: str, receipt_handle: str) -> None:
    sqs_client().delete_message(QueueUrl=queue_url(queue_name), ReceiptHandle=receipt_handle)
