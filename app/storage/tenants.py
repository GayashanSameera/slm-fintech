"""Tenants and API keys.

A tenant is one of our client's customers. Its settings item looks like:

    PK="TENANT#t_..."  SK="PROFILE"
    name, status ("active" | "suspended"),
    retention_days (7 | 30 | 90 | 365),
    training_consent {given: bool, at: iso, by: str}         (NFR-C2)
    unmasked_kinds   ["pan", "account", "id_number", "phone"] (FR-4.4)
    verification_mode "ai" | "checks_only"                   (FR-9.4)
    webhook_url, webhook_secret_id                           (FR-2.10)
    profile "default"                                        (FR-1.7)

API keys:
  * The full key looks like  slm_live_<key_id>_<secret>  and is shown ONCE.
  * We store only HMAC-SHA256(pepper, key) as the lookup item
        PK="APIKEY#<hmac>"  SK="APIKEY"  -> tenant_id, key_id, scopes, status
    plus a metadata item under the tenant (no secret) for listing/revoking.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Any

from app.core.clock import now_iso
from app.core.ids import new_key_id
from app.core.secrets import api_key_pepper
from app.storage import keys
from app.storage.aws import dynamodb_table, tenant_session
from app.storage.ddb import from_ddb, to_ddb

DEFAULT_SETTINGS: dict[str, Any] = {
    "status": "active",
    "retention_days": 30,
    "training_consent": {"given": False, "at": None, "by": None},
    "unmasked_kinds": [],
    "verification_mode": "ai",
    "webhook_url": None,
    "webhook_secret_id": None,
    "profile": "default",
}


@dataclass
class TenantSettings:
    tenant_id: str
    name: str = ""
    status: str = "active"
    retention_days: int = 30
    training_consent: dict = field(default_factory=lambda: {"given": False})
    unmasked_kinds: list[str] = field(default_factory=list)
    verification_mode: str = "ai"
    webhook_url: str | None = None
    webhook_secret_id: str | None = None
    profile: str = "default"

    @property
    def consented_to_training(self) -> bool:
        return bool(self.training_consent and self.training_consent.get("given"))


def hash_api_key(api_key: str) -> str:
    """HMAC-SHA256 with the pepper. Deterministic, so we can look the key up by its hash."""
    return hmac.new(api_key_pepper(), api_key.encode(), hashlib.sha256).hexdigest()


def get_tenant_settings(tenant_id: str) -> TenantSettings:
    table = dynamodb_table(tenant_session(tenant_id))
    item = table.get_item(Key={"PK": keys.tenant_pk(tenant_id), "SK": "PROFILE"}).get("Item") or {}
    data = {k: v for k, v in from_ddb(item).items() if k in TenantSettings.__dataclass_fields__}
    data["tenant_id"] = tenant_id
    return TenantSettings(**data)


# --- Admin operations (used by scripts/create_tenant.py, not by the public API) --------

def create_tenant(tenant_id: str, name: str, **overrides: Any) -> TenantSettings:
    table = dynamodb_table()   # admin credentials of whoever runs the script
    item = {"PK": keys.tenant_pk(tenant_id), "SK": "PROFILE", "name": name,
            "created_at": now_iso(), **DEFAULT_SETTINGS, **overrides}
    table.put_item(Item=to_ddb(item), ConditionExpression="attribute_not_exists(PK)")
    return get_tenant_settings(tenant_id)


def create_api_key(tenant_id: str, scopes: list[str], live: bool = True) -> str:
    """Create a key and return it. This is the ONLY time the full key is ever visible."""
    key_id = new_key_id()
    secret = secrets.token_urlsafe(32)          # 256 bits of randomness
    api_key = f"slm_{'live' if live else 'test'}_{key_id}_{secret}"
    table = dynamodb_table()
    table.put_item(Item={
        "PK": keys.api_key_pk(hash_api_key(api_key)), "SK": "APIKEY",
        "tenant_id": tenant_id, "key_id": key_id, "scopes": scopes, "status": "active",
    })
    table.put_item(Item={
        "PK": keys.tenant_pk(tenant_id), "SK": keys.key_sk(key_id),
        "key_id": key_id, "scopes": scopes, "created_at": now_iso(),
        "hint": api_key[-4:],                    # last 4 chars, to recognise the key in a list
    })
    return api_key
