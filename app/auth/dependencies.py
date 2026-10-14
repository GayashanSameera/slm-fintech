"""FastAPI dependency that gives every route its authenticated `Principal`.

Routes declare it like this:

    @router.get("/extractions/{job_id}")
    def get_job(job_id: str, principal: Principal = Depends(require_scope("extractions:read"))):
        ...

Where the identity comes from:
  * In AWS: API Gateway already ran the Lambda authorizer. Mangum (the Lambda
    adapter) puts the original API Gateway event in request.scope["aws.event"];
    the authorizer's context is at event["requestContext"]["authorizer"].
  * Locally: there is no API Gateway, so we check the Authorization header here
    with the SAME function the authorizer uses.

Routes never read the Authorization header themselves.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Request

from app.config import settings
from app.core.errors import unauthorized

from .principal import Principal, parse_bearer, resolve_api_key


def current_principal(request: Request) -> Principal:
    event = request.scope.get("aws.event")
    if event is not None and not settings.is_local:
        ctx = (event.get("requestContext") or {}).get("authorizer") or {}
        if not ctx.get("tenant_id"):
            raise unauthorized()
        return Principal(tenant_id=ctx["tenant_id"], key_id=ctx.get("key_id", ""),
                         scopes=frozenset((ctx.get("scopes") or "").split()))
    return resolve_api_key(parse_bearer(request.headers.get("authorization")))


def require_scope(scope: str) -> Callable[[Request], Principal]:
    def dependency(request: Request) -> Principal:
        principal = current_principal(request)
        principal.require(scope)
        request.state.principal = principal       # used by the audit middleware
        return principal
    return dependency
