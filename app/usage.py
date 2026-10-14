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


def summarize(calls: list[GatewayResult]) -> dict:
    """Token usage of ONE job, as returned to the tenant in `usage` (requirement FR-7.1).

    Counts every model call made for the job: document-type detection, the Rung 0
    verifier and every attempt on the AI ladder. Per model too, because models are
    priced differently. Only counts: no prompts, answers or document content.

        {"model_calls": 2, "input_tokens": 5466, "output_tokens": 160, "cache_read_tokens": 0,
         "by_model": [{"model_id": "us.amazon.nova-lite-v1:0", "calls": 1, ...}, ...]}

    A Rung 0 statement with tenant setting `checks_only` makes no model call at all:
    every count is 0 and `by_model` is empty.
    """
    per_model: dict[str, dict] = {}
    for c in calls:
        m = per_model.setdefault(c.model_id, {"model_id": c.model_id, "calls": 0, "input_tokens": 0,
                                              "output_tokens": 0, "cache_read_tokens": 0})
        m["calls"] += 1
        m["input_tokens"] += c.input_tokens
        m["output_tokens"] += c.output_tokens
        m["cache_read_tokens"] += c.cache_read_tokens
    return {
        "model_calls": len(calls),
        "input_tokens": sum(c.input_tokens for c in calls),
        "output_tokens": sum(c.output_tokens for c in calls),
        "cache_read_tokens": sum(c.cache_read_tokens for c in calls),
        "by_model": list(per_model.values()),       # in the order the models were first called
    }


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
