"""The extraction pipeline: turn prepared pages into a validated, MASKED result.

This is the heart of the platform. It follows the walkthrough in architecture
§4.5, stages 8 to 11:

    ┌─ Rung 0 (bank statements from known banks) ─────────────────────────────┐
    │  parser -> checks -> coverage -> AI verification -> accepted?           │
    └───────────────┬─────────────────────────────────────────────────────────┘
                    │ not eligible / rejected
                    ▼
    document type: given by the tenant, or detected (classify.py)
                    ▼
    AI model ladder, cheapest first (profile: start rung per type)
      attempt 1 -> parse JSON -> schema -> consistency checks -> pass? done
      attempt 2 (stronger model or same model with thinking) -> ...
      attempt 3 -> ... -> keep the best result + warnings
                    ▼
    normalise (nulls for missing fields) -> MASK -> return

Nothing in here touches storage: the worker stores what this returns. That keeps
the pipeline easy to unit-test with the FakeGateway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from app.core.errors import JobError
from app.core.logging import get_logger
from app.gateway import GatewayResult, ModelGateway

from .checks import DOCUMENT_ONLY, CheckFailure, run_checks
from .classify import classify
from .masking import has_aadhaar_markers, mask
from .normalise import to_result
from .parsers import try_rung0
from .profiles import Profile
from .prompts import instruction, strip_code_fences, system_prompt
from .schemas import REGISTRY

log = get_logger(__name__)


@dataclass
class ExtractionOutcome:
    doc_type: str
    doc_type_source: str                 # "client" | "detected" | "parser"
    doc_type_confidence: float | None
    result: dict                         # validated, normalised and MASKED
    warnings: list[str]
    method: str                          # "parser" | "ai"
    model_id: str | None                 # AI model that produced the result
    parser_id: str | None                # Rung 0 parser that produced the result
    verified_by: str | None              # model that verified a parser result
    schema_version: str
    calls: list[GatewayResult] = field(default_factory=list)   # every model call, for usage metering


# --- the attempt plan ----------------------------------------------------------------

def attempt_plan(profile: Profile, start: int) -> list[tuple[int, bool]]:
    """List of (rung index, thinking) attempts for one document.

    Rules (architecture §3.2, §6):
      * start on `start`;
      * a Claude Haiku rung gets one extra try WITH thinking before climbing;
      * then climb one rung at a time;
      * at most 1 + max_climbs attempts in total.

    Examples with the default ladder [Nova, Haiku, Sonnet] and max_climbs = 2:
      start=0 -> [(0, no), (1, think), (2, no)]
      start=1 -> [(1, no), (1, think), (2, no)]
      start=2 -> [(2, no)]
    """
    ladder = profile.ladder
    plan = [(start, ladder[start].thinking)]
    rung = start
    while len(plan) < 1 + profile.max_climbs:
        is_haiku = "haiku" in ladder[rung].model
        if profile.think_on_retry and is_haiku and (rung, True) not in plan:
            plan.append((rung, True))          # same model, now thinking
            continue
        if rung + 1 >= len(ladder):
            break                              # already at the top rung
        rung += 1
        plan.append((rung, profile.think_on_retry and "haiku" in ladder[rung].model))
    return plan


# --- one AI attempt ---------------------------------------------------------------------

def parse_and_validate(res: GatewayResult, model: type[BaseModel]) -> tuple[BaseModel | None, list[str]]:
    """Gates 1 and 2: valid JSON, valid schema. Returns (parsed, problems for the next attempt)."""
    if res.stop_reason == "max_tokens":
        return None, ["The answer was cut off. Return a shorter but complete JSON object."]
    try:
        raw = json.loads(strip_code_fences(res.text))
        if not isinstance(raw, dict):
            return None, ["Return exactly one JSON object."]
        return model.model_validate(raw), []
    except json.JSONDecodeError:
        return None, ["The answer was not valid JSON. Return exactly one JSON object."]
    except ValidationError as e:
        # Field NAMES and error TYPES only. Never e.errors()[i]["input"]: it holds document values.
        return None, [f"Field '{'.'.join(map(str, err['loc']))}': {err['type']}" for err in e.errors()[:10]]


def run_ladder(doc_type: str, pages: list[dict], profile: Profile, gateway: ModelGateway,
               start_rung: int | None = None) -> tuple[BaseModel, list[CheckFailure], str, list[GatewayResult]]:
    """Gate 3 and climbing. Returns (best parsed result, its failures, model id, calls)."""
    spec = REGISTRY[doc_type]
    system = system_prompt(spec)
    start = profile.start_rung.get(doc_type, 1) if start_rung is None else start_rung
    calls: list[GatewayResult] = []
    best: tuple[BaseModel, list[CheckFailure], str] | None = None
    problems: list[str] = []

    for attempt, (rung, thinking) in enumerate(attempt_plan(profile, start), start=1):
        model_id = profile.ladder[rung].model
        res = gateway.generate(model_id=model_id, system=system, pages=pages,
                               instruction=instruction(problems), thinking=thinking)
        calls.append(res)
        parsed, problems = parse_and_validate(res, spec.model)
        if parsed is None:
            log.info("ladder_attempt", rung=rung, attempt=attempt, model_id=model_id, status="invalid")
            continue
        failures = run_checks(doc_type, parsed)
        blocking = [f for f in failures if f.code not in DOCUMENT_ONLY]
        if best is None or len(failures) < len(best[1]):
            best = (parsed, failures, model_id)
        log.info("ladder_attempt", rung=rung, attempt=attempt, model_id=model_id,
                 status="ok" if not blocking else "checks_failed", checks_failed=[f.code for f in blocking])
        if not blocking:
            break                               # passed: stop climbing
        problems = [f.message for f in blocking]   # tell the next attempt what was wrong

    if best is None:
        err = JobError("extraction_failed")       # no usable answer from any model
        err.calls = calls
        raise err
    return best[0], best[1], best[2], calls


# --- the whole document --------------------------------------------------------------------

def extract_document(*, data: bytes, prepared, requested_type: str | None, tenant, profile: Profile,
                     gateway: ModelGateway, job_id: str) -> ExtractionOutcome:
    calls: list[GatewayResult] = []
    page_texts = [p.get("text") or "" for p in prepared.pages]

    # ---- Rung 0: deterministic parser for known bank statements -------------------------
    rung0_tried = False
    if profile.rung0_enabled and requested_type in (None, "auto", "bank_statement"):
        outcome, r0_calls, rung0_tried = try_rung0(
            data=data, prepared=prepared, gateway=gateway, verifier_model=profile.rung0_verifier,
            verification_mode=tenant.verification_mode, job_id=job_id)
        calls += r0_calls
        if outcome is not None:
            result = mask(to_result(outcome.statement),
                          strict_aadhaar=has_aadhaar_markers(*page_texts),
                          unmasked_kinds=set(tenant.unmasked_kinds))
            return ExtractionOutcome(
                doc_type="bank_statement", doc_type_source="parser", doc_type_confidence=1.0,
                result=result, warnings=[], method="parser", model_id=None,
                parser_id=outcome.parser_id, verified_by=outcome.verified_by,
                schema_version=REGISTRY["bank_statement"].version, calls=calls,
            )
        # A parser matched the layout: this IS a bank statement, whatever the tenant sent.
        if rung0_tried and requested_type in (None, "auto"):
            requested_type = "bank_statement"

    # ---- Document type ------------------------------------------------------------------
    if requested_type and requested_type != "auto":
        doc_type, source, confidence = requested_type, "client", None
    else:
        try:
            c = classify(prepared.pages, gateway, profile.classifier_models, profile.classifier_min_confidence)
        except JobError as e:
            e.calls = calls + getattr(e, "calls", [])
            raise
        calls += c.calls
        doc_type, source, confidence = c.doc_type, "detected", c.confidence

    # ---- AI ladder ----------------------------------------------------------------------
    # A bank statement whose Rung 0 attempt was rejected starts at the fallback rung (Haiku):
    # the cheapest model has already looked at it as the verifier.
    start = profile.rung0_fallback_rung if (doc_type == "bank_statement" and rung0_tried) else None
    try:
        parsed, failures, model_id, ladder_calls = run_ladder(doc_type, prepared.pages, profile, gateway, start)
    except JobError as e:
        e.calls = calls + getattr(e, "calls", [])
        raise
    calls += ladder_calls

    result = to_result(parsed)
    warnings = [f.code for f in failures]
    # The tenant said "invoice" but the model found nothing: probably the wrong type (§6.0).
    if source == "client" and not any(v not in (None, [], {}) for v in result.values()):
        warnings.append("document_type_mismatch")

    strict = REGISTRY[doc_type].sensitive_identity or has_aadhaar_markers(*page_texts, json.dumps(result))
    result = mask(result, strict_aadhaar=strict, unmasked_kinds=set(tenant.unmasked_kinds))
    return ExtractionOutcome(
        doc_type=doc_type, doc_type_source=source, doc_type_confidence=confidence,
        result=result, warnings=sorted(set(warnings)), method="ai", model_id=model_id,
        parser_id=None, verified_by=None, schema_version=REGISTRY[doc_type].version, calls=calls,
    )
