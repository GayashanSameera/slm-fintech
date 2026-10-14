"""Who is calling? The `Principal` is the authenticated caller of one API request.

It is created ONLY from the API key (requirement FR-1.2):
  * in AWS, by the Lambda authorizer (app/auth/authorizer.py), which API Gateway
    runs before our API Lambda and whose result arrives in the request context;
  * locally, by `resolve_api_key()` below, called from the FastAPI dependency.

Both paths use the same `resolve_api_key()` function, so local and AWS behave
the same.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.errors import ApiError, unauthorized
from app.storage import keys
from app.storage.aws import dynamodb_table
from app.storage.tenants import hash_api_key


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    key_id: str
    scopes: frozenset[str] = field(default_factory=frozenset)

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise ApiError(403, "insufficient_scope", "This API key does not have the required scope.")


def parse_bearer(header: str | None) -> str | None:
    if not header or not header.startswith("Bearer "):
        return None
    token = header[len("Bearer "):].strip()
    # Cheap shape check before any database lookup.
    return token if token.startswith(("slm_live_", "slm_test_")) and len(token) < 200 else None


def resolve_api_key(api_key: str | None) -> Principal:
    """API key -> Principal, or raise 401/403. Uses the PLATFORM session: key lookup
    items are not tenant data (they are how we find the tenant in the first place)."""
    if not api_key:
        raise unauthorized()
    table = dynamodb_table()
    item = table.get_item(Key={"PK": keys.api_key_pk(hash_api_key(api_key)), "SK": "APIKEY"}).get("Item")
    if not item or item.get("status") != "active":
        raise unauthorized()
    profile = table.get_item(Key={"PK": keys.tenant_pk(item["tenant_id"]), "SK": "PROFILE"}).get("Item")
    if not profile or profile.get("status") != "active":
        raise ApiError(403, "tenant_suspended", "This account is not active.")    # FR-1.6
    return Principal(tenant_id=item["tenant_id"], key_id=item["key_id"], scopes=frozenset(item.get("scopes", [])))
