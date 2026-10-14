"""Document-type detection when the tenant does not send `document_type`
(requirement FR-2.18, architecture §6.0).

One small model call looks at PAGE 1 ONLY (cheap), using the cheapest model
first and a stronger one if the answer is unsure:

    Nova Lite  --(invalid / confidence < 0.8 / "other")-->  Haiku 5.5

If the type is still unknown, the job fails with `unrecognised_document_type`
and the tenant can resend the document with `document_type` set.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.core.errors import JobError
from app.gateway import GatewayResult, ModelGateway

from .prompts import CLASSIFY_INSTRUCTION, CLASSIFY_SYSTEM, strip_code_fences
from .schemas import REGISTRY


@dataclass
class Classification:
    doc_type: str
    confidence: float
    calls: list[GatewayResult] = field(default_factory=list)


def classify(pages: list[dict], gateway: ModelGateway, models: list[str], min_confidence: float) -> Classification:
    first_page = [{"image": pages[0].get("image"), "text": (pages[0].get("text") or "")[:1500]}]
    calls: list[GatewayResult] = []
    best: tuple[str, float] | None = None
    for model_id in models:
        res = gateway.generate(model_id=model_id, system=CLASSIFY_SYSTEM, pages=first_page,
                               instruction=CLASSIFY_INSTRUCTION, max_tokens=100)
        calls.append(res)
        try:
            answer = json.loads(strip_code_fences(res.text))
            doc_type = str(answer.get("type", "other"))
            confidence = float(answer.get("confidence", 0))
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            continue                                   # unreadable answer: ask the next model
        if doc_type in REGISTRY and (best is None or confidence > best[1]):
            best = (doc_type, confidence)
        if doc_type in REGISTRY and confidence >= min_confidence:
            return Classification(doc_type, confidence, calls)
    if best is not None and best[1] >= 0.5:
        # Below the bar but still a plausible answer: accept, with the low confidence recorded.
        return Classification(best[0], best[1], calls)
    err = JobError("unrecognised_document_type")
    err.calls = calls            # keep the calls so their tokens are still metered
    raise err
