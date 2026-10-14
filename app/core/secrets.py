"""Reading secrets.

Secrets are NEVER stored in code or in plain Lambda environment variables
(requirement NFR-S9). In AWS they live in AWS Secrets Manager; the Lambda
reads them once per cold start and keeps them in memory for 5 minutes.

Locally (SLM_ENV=local) there is no Secrets Manager, so the values come from
`.env.local`. Those local values are NOT real secrets.
"""

from __future__ import annotations

import json
import time

import boto3

from app.config import settings

_CACHE: dict[str, tuple[float, str]] = {}
_TTL_SECONDS = 300   # re-read a secret at most every 5 minutes (allows rotation)


def get_secret_string(secret_id: str) -> str:
    """Return the SecretString of a Secrets Manager secret, cached for 5 minutes."""
    cached = _CACHE.get(secret_id)
    if cached and cached[0] > time.time():
        return cached[1]
    client = boto3.client("secretsmanager", region_name=settings.region)
    value = client.get_secret_value(SecretId=secret_id)["SecretString"]
    _CACHE[secret_id] = (time.time() + _TTL_SECONDS, value)
    return value


def api_key_pepper() -> bytes:
    """The secret 'pepper' mixed into API-key hashes (security.md §6.1).

    A stolen copy of the DynamoDB table is useless without the pepper, because
    the stored hashes cannot be checked against guessed keys."""
    if settings.is_local or not settings.pepper_secret_id:
        return settings.api_key_pepper.encode()
    raw = get_secret_string(settings.pepper_secret_id)
    # The secret may be stored as plain text or as {"pepper": "..."}.
    try:
        return json.loads(raw)["pepper"].encode()
    except (ValueError, KeyError, TypeError):
        return raw.encode()


def webhook_secret(secret_id: str | None) -> bytes | None:
    """The per-tenant webhook signing secret (FR-2.10). None if not configured."""
    if not secret_id:
        return None
    if settings.is_local:
        # Locally the "secret id" is the secret itself (set by scripts/create_tenant.py).
        return secret_id.encode()
    return get_secret_string(secret_id).encode()
