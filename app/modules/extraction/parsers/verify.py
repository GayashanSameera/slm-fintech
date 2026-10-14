"""AI verification of a Rung 0 parser result (requirement FR-9.3).

The cheapest model (Nova Lite) receives the statement pages AND the parser's
JSON, and is asked to list ONLY the fields that do not match the document.
Its answer is tiny ({"agree": true} or a short issue list), so verification
costs ~USD 0.00025 per page.

Safety properties:
  * The verifier can only REJECT a parser result. It never changes a value.
  * An unreadable or odd answer counts as DISAGREE: when in doubt, the statement
    goes to full AI extraction.
  * Issue details contain document values, so only the COUNT and the field PATHS
    are logged, never `document_value`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.gateway import GatewayResult, ModelGateway

from ..prompts import VERIFY_SYSTEM, strip_code_fences, verify_instruction

log = get_logger(__name__)


@dataclass
class VerifyResult:
    agree: bool
    issue_paths: list[str] = field(default_factory=list)
    gateway_result: GatewayResult | None = None


def verify(pages: list[dict], parsed_json: str, gateway: ModelGateway, model_id: str) -> VerifyResult:
    res = gateway.generate(
        model_id=model_id,
        system=VERIFY_SYSTEM,
        pages=pages,
        instruction=verify_instruction(parsed_json),
        thinking=False,
        max_tokens=800,
    )
    try:
        answer = json.loads(strip_code_fences(res.text))
        agree = answer.get("agree") is True
        issues = [] if agree else list(answer.get("issues") or [])
        paths = [str(i.get("path", "?")) for i in issues if isinstance(i, dict)]
        if not agree and not paths:
            paths = ["_unspecified"]
    except (json.JSONDecodeError, AttributeError, TypeError):
        agree, paths = False, ["_verifier_unreadable"]
    log.info("rung0_verified", agree=agree, issue_count=len(paths), model_id=model_id)
    return VerifyResult(agree=agree, issue_paths=paths, gateway_result=res)
