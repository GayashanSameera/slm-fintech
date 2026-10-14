"""Rung 0: rule-based extraction of digital bank statements, verified by AI.

`try_rung0()` is called by the pipeline BEFORE any AI extraction. It returns:
  * a `Rung0Outcome` with the accepted statement, or
  * None, meaning "Rung 0 doesn't apply or was rejected: use the AI ladder".

It NEVER raises for parser problems: a broken or confused parser must not fail
the job (requirement FR-9.5). It just steps aside.

Flow (architecture §3.3):
    fingerprint -> parse -> schema validation -> consistency checks -> coverage checks
    -> text reconciliation (every row, every field, against the PDF text; reconcile.py)
    -> AI verification (unless tenant chose checks_only)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError

from app.core.logging import get_logger
from app.gateway import GatewayResult, ModelGateway

from ..checks import run_checks
from ..schemas.bank_statement import BankStatement
from .coverage import coverage_problems
from .fingerprint import pick_parser
from .reconcile import reconcile
from .verify import verify

log = get_logger(__name__)


@dataclass
class Rung0Outcome:
    statement: BankStatement
    parser_id: str
    verified_by: str | None                      # model id, or None for checks_only
    usage: list[GatewayResult] = field(default_factory=list)


def try_rung0(*, data: bytes, prepared, gateway: ModelGateway, verifier_model: str,
              verification_mode: str, job_id: str) -> tuple[Rung0Outcome | None, list[GatewayResult], bool]:
    """Return (outcome or None, gateway calls made, attempted).

    `attempted` is True when a parser matched the layout (even if its result was
    then rejected): the pipeline uses it to start the AI ladder at the fallback
    rung, because the cheapest model has already seen the document as verifier.
    Calls are returned so their tokens are metered (FR-7.1)."""
    calls: list[GatewayResult] = []
    if not prepared.is_digital_pdf:
        return None, calls, False
    parser = pick_parser(prepared.pages[0]["text"])
    if parser is None:
        return None, calls, False

    try:
        statement = BankStatement.model_validate(parser.parse(data))
    except ValidationError:
        log.info("rung0_rejected", job_id=job_id, parser_id=parser.parser_id, error_code="schema_invalid")
        return None, calls, True
    except Exception as exc:   # a parser bug must never fail the job
        log.error("rung0_parser_error", exc=exc, job_id=job_id, parser_id=parser.parser_id)
        return None, calls, True

    failed = [f.code for f in run_checks("bank_statement", statement)]
    page_texts = [p["text"] for p in prepared.pages]
    failed += coverage_problems(statement, page_texts)
    # Statement rows are reconciled with real transactions downstream: EVERY row must
    # match the document's own text, field by field, before anything else is trusted.
    failed += reconcile(statement, page_texts)
    if failed:
        log.info("rung0_rejected", job_id=job_id, parser_id=parser.parser_id,
                 error_code="checks_failed", checks_failed=failed)
        return None, calls, True

    if verification_mode == "checks_only":
        # Tenant chose: these documents never go to any AI service (FR-9.4).
        return Rung0Outcome(statement, parser.parser_id, verified_by=None), calls, True

    v = verify(prepared.pages, statement, gateway, verifier_model)
    if v.gateway_result:
        calls.append(v.gateway_result)
    if not v.agree:
        log.info("rung0_rejected", job_id=job_id, parser_id=parser.parser_id,
                 error_code="verifier_disagreed", issue_count=len(v.issue_paths))
        return None, calls, True
    return Rung0Outcome(statement, parser.parser_id, verified_by=verifier_model), calls, True
