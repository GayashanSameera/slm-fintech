"""Usage metering per tenant per day (requirement FR-7.1).

One DynamoDB item per tenant per day:  PK="TENANT#<t>"  SK="USAGE#2026-10-13"
Counters are updated with DynamoDB's atomic ADD, so concurrent workers never
overwrite each other's numbers.

Estimated cost uses the prices in docs/cost.md §2.1 (USD per million tokens).
It is an ESTIMATE for same-day dashboards and alarms; the AWS bill is the truth.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.clock import epoch_in, now
from app.gateway import GatewayResult
from app.storage import keys
from app.storage.aws import dynamodb_table, tenant_session

# (input, output, cache-read) USD per 1M tokens, US geographic profiles. Confirm in phase 0.
PRICES: dict[str, tuple[float, float, float]] = {
    "us.amazon.nova-lite": (0.06, 0.24, 0.015),
    "us.anthropic.claude-haiku-5-5": (0.11, 0.55, 0.011),
    "us.anthropic.claude-sonnet-5-5": (2.20, 11.00, 0.22),
}


def estimate_cost(call: GatewayResult) -> float:
    for prefix, (pin, pout, pcache) in PRICES.items():
        if call.model_id.startswith(prefix):
            return (call.input_tokens * pin + call.output_tokens * pout + call.cache_read_tokens * pcache) / 1e6
    return 0.0   # local/fake models cost nothing


def record(tenant_id: str, *, pages: int, calls: list[GatewayResult], method: str) -> None:
    """Add one finished job's usage to today's counters."""
    table = dynamodb_table(tenant_session(tenant_id))
    day = now().strftime("%Y-%m-%d")
    table.update_item(
        Key={"PK": keys.tenant_pk(tenant_id), "SK": keys.usage_sk(day)},
        UpdateExpression=(
            "ADD jobs :one, pages :pages, input_tokens :tin, output_tokens :tout, "
            "cache_read_tokens :tcache, model_calls :calls, est_cost_usd :cost, #m :one "
            "SET expires_at = if_not_exists(expires_at, :exp)"
        ),
        ExpressionAttributeNames={"#m": f"jobs_{method}"},     # jobs_parser / jobs_ai
        ExpressionAttributeValues={
            ":one": 1,
            ":pages": pages,
            ":tin": sum(c.input_tokens for c in calls),
            ":tout": sum(c.output_tokens for c in calls),
            ":tcache": sum(c.cache_read_tokens for c in calls),
            ":calls": len(calls),
            ":cost": Decimal(str(round(sum(estimate_cost(c) for c in calls), 6))),
            ":exp": epoch_in(400 * 86400),     # keep usage ~13 months for billing (FR-7.2)
        },
    )
