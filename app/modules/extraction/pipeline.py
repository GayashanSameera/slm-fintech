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
      (a LONG bank statement: the same, per chunk of 3 pages, chunks in parallel, then merged)
                    ▼
    normalise (nulls for missing fields) -> MASK -> return

Nothing in here touches storage: the worker stores what this returns. That keeps
the pipeline easy to unit-test with the FakeGateway.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
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
from .parsers.reconcile import reconcile, reconcile_rows
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
        # Never ask for a "shorter" answer: for a statement that means dropping rows. Long
        # statements are read in chunks (run_chunked_statement), so this should not happen.
        return None, ["The answer was cut off before it was complete. Return the COMPLETE JSON object: "
                      "every field and every row, nothing left out, no extra spaces."]
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


def _digital(pages: list[dict]) -> bool:
    """True if every page has a text layer (only then can rows be reconciled with the text)."""
    return bool(pages) and not any(p.get("scanned") or not p.get("text") for p in pages)


def text_reconciliation_failures(doc_type: str, parsed, pages: list[dict]) -> list[CheckFailure]:
    """Digital bank statements read by AI must ALSO match the PDF text, row by row (reconcile.py).

    Treated like a failed consistency check: the ladder climbs, and if it still fails at
    the top, the codes are returned as warnings. Messages name rows and fields only.
    Scanned pages have no text layer, so nothing can be reconciled for them.
    """
    if doc_type != "bank_statement" or not _digital(pages):
        return []
    issues = reconcile(parsed, [p["text"] for p in pages])
    return _reconciliation_failure(issues)


def _reconciliation_failure(issues: list[str]) -> list[CheckFailure]:
    if not issues:
        return []
    return [CheckFailure("text_reconciliation_failed",
                         "These fields do not match the document text exactly: " + ", ".join(issues[:15])
                         + ". Copy every row exactly as printed, in order.")]


def _climb(doc_type: str, pages: list[dict], profile: Profile, gateway: ModelGateway, start: int,
           check, chunk: tuple[int, int, int] | None = None):
    """The ladder for one set of pages: attempt after attempt (attempt_plan) until an answer is
    valid and `check(parsed)` finds nothing blocking. Returns (best parsed result, its
    failures, model id, calls); raises extraction_failed if no attempt gave a usable answer."""
    spec = REGISTRY[doc_type]
    system = system_prompt(spec)
    calls: list[GatewayResult] = []
    best: tuple[BaseModel, list[CheckFailure], str] | None = None
    problems: list[str] = []

    for attempt, (rung, thinking) in enumerate(attempt_plan(profile, start), start=1):
        model_id = profile.ladder[rung].model
        res = gateway.generate(model_id=model_id, system=system, pages=pages,
                               instruction=instruction(problems, chunk), thinking=thinking)
        calls.append(res)
        parsed, problems = parse_and_validate(res, spec.model)
        where = {"chunk_first_page": chunk[0]} if chunk else {}
        if parsed is None:
            log.info("ladder_attempt", rung=rung, attempt=attempt, model_id=model_id, status="invalid", **where)
            continue
        failures = check(parsed)
        blocking = [f for f in failures if f.code not in DOCUMENT_ONLY]
        if best is None or len(failures) < len(best[1]):
            best = (parsed, failures, model_id)
        log.info("ladder_attempt", rung=rung, attempt=attempt, model_id=model_id,
                 status="ok" if not blocking else "checks_failed", checks_failed=[f.code for f in blocking], **where)
        if not blocking:
            break                               # passed: stop climbing
        problems = [f.message for f in blocking]   # tell the next attempt what was wrong

    if best is None:
        err = JobError("extraction_failed")       # no usable answer from any model
        err.calls = calls
        raise err
    return best[0], best[1], best[2], calls


def run_ladder(doc_type: str, pages: list[dict], profile: Profile, gateway: ModelGateway,
               start_rung: int | None = None) -> tuple[BaseModel, list[CheckFailure], str, list[GatewayResult]]:
    """Gate 3 and climbing. Returns (best parsed result, its failures, model id, calls)."""
    start = profile.start_rung.get(doc_type, 1) if start_rung is None else start_rung
    if doc_type == "bank_statement" and len(pages) > profile.statement_chunk_pages:
        return run_chunked_statement(pages, profile, gateway, start)
    return _climb(doc_type, pages, profile, gateway, start,
                  check=lambda parsed: run_checks(doc_type, parsed)
                  + text_reconciliation_failures(doc_type, parsed, pages))


# --- long bank statements: chunks ------------------------------------------------------------

def run_chunked_statement(pages: list[dict], profile: Profile, gateway: ModelGateway, start: int):
    """A long bank statement, read a few pages at a time.

    WHY: one model answer has an output limit (8000 tokens, roughly 120 rows). A 15-page
    statement with ~190 rows does not fit: the answer is cut off, and a model asked to "be
    shorter" leaves rows out (seen in production on 2026-10-14). Chunks of 3 pages are far
    below the limit, they are read IN PARALLEL (so a long statement takes about as long as a
    short one), and a chunk that fails its checks climbs the ladder ALONE: one bad chunk
    doesn't cost a second reading of the whole statement.

    Each chunk is checked on its own pages: schema, the running balance from row to row, and
    (digital PDFs) its rows against the text of exactly those pages. Then the chunks are
    merged (header fields from the first chunk that prints them, transactions in page order)
    and the WHOLE statement is checked again, including the balances across chunk borders and
    opening + credits - debits = closing. Remaining problems become warnings.
    """
    size = profile.statement_chunk_pages
    total = len(pages)
    chunks = [(i, pages[i:i + size]) for i in range(0, total, size)]
    digital = _digital(pages)

    def check_chunk(parsed, chunk_pages):
        # Opening/closing belong to the whole statement: checked after the merge, not per chunk.
        rows_only = parsed.model_copy(update={"opening_balance": None, "closing_balance": None})
        failures = run_checks("bank_statement", rows_only)
        if digital:
            failures += _reconciliation_failure(reconcile_rows(parsed, [p["text"] for p in chunk_pages]))
        return failures

    def read(chunk):
        i, chunk_pages = chunk
        return _climb("bank_statement", chunk_pages, profile, gateway, start,
                      check=lambda parsed: check_chunk(parsed, chunk_pages),
                      chunk=(i + 1, i + len(chunk_pages), total))

    calls: list[GatewayResult] = []
    results = []
    if getattr(gateway, "parallel_safe", False):
        with ThreadPoolExecutor(max_workers=min(len(chunks), 5)) as pool:
            futures = [pool.submit(read, c) for c in chunks]
            for future in futures:                 # collect every chunk's calls, even after a failure
                try:
                    results.append(future.result())
                except JobError as e:
                    calls += getattr(e, "calls", [])
                    results.append(None)
    else:
        for c in chunks:
            try:
                results.append(read(c))
            except JobError as e:
                calls += getattr(e, "calls", [])
                results.append(None)
    for r in results:
        if r is not None:
            calls += r[3]
    if any(r is None for r in results):
        err = JobError("extraction_failed")       # a chunk got no usable answer from any model
        err.calls = calls
        raise err

    merged = merge_statement_chunks([r[0] for r in results])
    failures = run_checks("bank_statement", merged) + text_reconciliation_failures("bank_statement", merged, pages)
    # The strongest model any chunk needed is reported as the result's model.
    rank = {r.model: i for i, r in enumerate(profile.ladder)}
    model_id = max((r[2] for r in results), key=lambda m: rank.get(m, -1))
    return merged, failures, model_id, calls


def merge_statement_chunks(parts: list) -> BaseModel:
    """One statement from its chunks: each header field from the first chunk that has it (the
    header is usually on page 1; the closing balance may only be on the last page), and the
    transactions of all chunks in page order."""
    model = type(parts[0])
    data = parts[0].model_dump(mode="json")
    for part in parts[1:]:
        for key, value in part.model_dump(mode="json").items():
            if key != "transactions" and data.get(key) in (None, "") and value not in (None, ""):
                data[key] = value
    data["transactions"] = [t for part in parts for t in part.model_dump(mode="json")["transactions"]]
    return model.model_validate(data)


# --- the whole document --------------------------------------------------------------------

def extract_document(*, data: bytes, prepared, requested_type: str | None, tenant, profile: Profile,
                     gateway: ModelGateway, job_id: str) -> ExtractionOutcome:
    """Turn one prepared document into a validated, normalised, MASKED result.

    Order: Rung 0 (known bank layouts: parser + checks + reconciliation + AI check) ->
    document type (given, parser, or classify) -> AI ladder (cheapest model first, climbing on
    failed checks) -> nulls for missing fields -> masking. Returns an ExtractionOutcome with every
    model call made, so usage can be metered and returned. Never touches storage.
    """
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
