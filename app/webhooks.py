"""Webhooks (requirement FR-2.10).

When a job finishes, the worker queues a small message; the `webhook` Lambda
signs an event and POSTs it to the tenant's URL.

The event carries NO extracted data, only the job id and status:
    {"event": "extraction.succeeded", "job_id": "ext_...", "status": "succeeded",
     "occurred_at": "2026-10-13T10:15:14Z"}
A leaked or misrouted webhook therefore reveals nothing; the tenant fetches the
result with its API key.

Signature header (tenants verify it with their webhook secret):
    X-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>." + body)>
The timestamp is inside the signed message, so an old event can't be replayed.

SSRF protection: we only call https:// URLs whose host resolves to PUBLIC IP
addresses, and we never follow redirects. Otherwise a tenant could point the
webhook at internal addresses.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import time
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.core.clock import now_iso
from app.core.logging import get_logger
from app.core.secrets import webhook_secret
from app.storage import queues
from app.storage.tenants import get_tenant_settings

log = get_logger(__name__)


def build_event(job_id: str, status: str) -> bytes:
    return json.dumps({"event": f"extraction.{status}", "job_id": job_id, "status": status,
                       "occurred_at": now_iso()}, separators=(",", ":")).encode()


def sign(secret: bytes, body: bytes, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    mac = hmac.new(secret, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def is_safe_url(url: str) -> bool:
    """https only, and the host must resolve only to public addresses."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return False
    return True


def enqueue(tenant_id: str, job_id: str, status: str) -> None:
    """Called by the worker. Cheap: just a queue message with ids."""
    queues.send(settings.queue_webhooks, {"tenant_id": tenant_id, "job_id": job_id, "status": status})


def deliver(tenant_id: str, job_id: str, status: str) -> bool:
    """Send one webhook. Returns True on success (2xx). Raises nothing."""
    tenant = get_tenant_settings(tenant_id)
    secret = webhook_secret(tenant.webhook_secret_id)
    if not tenant.webhook_url or not secret:
        return True                                   # no webhook configured: nothing to do
    if not is_safe_url(tenant.webhook_url):
        log.warning("webhook_blocked_url", tenant_id=tenant_id, job_id=job_id)
        return True                                   # never retry an unsafe URL
    body = build_event(job_id, status)
    try:
        resp = httpx.post(tenant.webhook_url, content=body, timeout=10, follow_redirects=False,
                          headers={"Content-Type": "application/json", "X-Signature": sign(secret, body)})
        ok = 200 <= resp.status_code < 300
        log.info("webhook_sent", tenant_id=tenant_id, job_id=job_id, http_status=resp.status_code)
        return ok
    except httpx.HTTPError as exc:
        log.error("webhook_failed", exc=exc, tenant_id=tenant_id, job_id=job_id)
        return False


def handle_sqs_event(event: dict, context=None) -> dict:
    """Lambda entry point for the `webhooks` queue. Failed deliveries are retried by SQS
    (5 receives, then the dead-letter queue)."""
    failures = []
    for record in event.get("Records", []):
        msg = json.loads(record["body"])
        if not deliver(msg["tenant_id"], msg["job_id"], msg["status"]):
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
