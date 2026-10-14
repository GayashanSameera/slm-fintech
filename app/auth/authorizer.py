"""Lambda authorizer for API Gateway (REQUEST type).

API Gateway calls this function BEFORE the API Lambda, with the request's
Authorization header. We return an IAM policy (allow/deny) plus a `context`
that API Gateway passes to the API Lambda:

    {"tenant_id": "t_...", "key_id": "key_...", "scopes": "extractions:read extractions:write"}

API Gateway caches the answer for 60 seconds per Authorization header value
(set in infra/cloudformation/app.yaml). So most requests don't run this at all,
and a revoked key stops working within a minute (security.md §6.1).

On failure we raise "Unauthorized": API Gateway turns that into HTTP 401
without calling our API.
"""

from __future__ import annotations

from app.core.errors import ApiError
from app.core.logging import get_logger

from .principal import parse_bearer, resolve_api_key

log = get_logger(__name__)


def _policy(principal_id: str, effect: str, method_arn: str, context: dict | None = None) -> dict:
    # Allow the whole API (all methods/paths of this stage) so the cached answer
    # works for every route; per-route scope checks happen in the API Lambda.
    """The authorizer's answer: an IAM policy (Allow/Deny) for the WHOLE stage, so API Gateway's
    60-second cache serves every route, plus the context passed on to the api function.
    """
    arn_parts = method_arn.split("/")
    api_wildcard = "/".join(arn_parts[:2]) + "/*"     # arn:...:apiId/stage/*
    doc = {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Action": "execute-api:Invoke", "Effect": effect, "Resource": api_wildcard}],
        },
    }
    if context:
        doc["context"] = context
    return doc


def handler(event: dict, context=None) -> dict:
    """Lambda entry point (API Gateway REQUEST authorizer): API key -> Allow with the tenant in the
    context; bad key -> 401; suspended tenant -> explicit Deny (403).
    """
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    try:
        principal = resolve_api_key(parse_bearer(headers.get("authorization")))
    except ApiError as e:
        log.info("auth_denied", error_code=e.code)
        if e.status == 403:
            # Suspended tenant: explicit Deny -> API Gateway answers 403 (FR-1.6).
            return _policy("denied", "Deny", event["methodArn"])
        raise Exception("Unauthorized") from None      # magic string: API Gateway returns 401
    return _policy(
        principal.key_id,
        "Allow",
        event["methodArn"],
        # Context values must be strings/numbers/booleans: scopes are space-separated.
        {"tenant_id": principal.tenant_id, "key_id": principal.key_id, "scopes": " ".join(sorted(principal.scopes))},
    )
