"""The extraction pipeline: file bytes in, validated and masked JSON out.

No HTTP and no storage here, just the processing steps, so they can be tested alone and
reused unchanged by any caller.

    1. PREPARE    preprocess/  → text and/or page images             (processing-flow §3)
    2. ASK        gateway      → the model's JSON, forced to the schema (processing-flow §4)
    3. VALIDATE   schemas/     → check every field; normalise dates and amounts
                                 A field that cannot be read (e.g. "31/02/2026") is set to
                                 empty with a warning instead of failing the whole document.
    4. CHECK      schemas/     → arithmetic and format checks → warnings
    5. MASK       masking.py   → hide sensitive numbers (Aadhaar always)

Errors become ExtractionError with a stable code (see errors.py).
"""

import copy
import logging
import time
from dataclasses import dataclass, field

from pydantic import ValidationError

from app.config import Settings
from app.gateway.base import ExtractionRequest, ModelError, ModelOutputInvalid, ModelProvider, ModelUnavailable
from app.modules.extraction.errors import ExtractionError
from app.modules.extraction.masking import mask_result
from app.modules.extraction.preprocess import prepare
from app.modules.extraction.prompts import SYSTEM_PROMPT, build_user_prompt
from app.modules.extraction.schemas import DocumentSchema, DocumentType

log = logging.getLogger("slm.pipeline")


@dataclass
class ExtractionOutcome:
    result: dict  # validated, masked; this is what goes into result.json
    warnings: list[str]
    pages: int
    route: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    prepare_ms: int
    model_ms: int
    extra: dict = field(default_factory=dict)


def run_extraction(
    data: bytes,
    doc_type: DocumentType,
    provider: ModelProvider,
    settings: Settings,
) -> ExtractionOutcome:
    # ---- 1. PREPARE ----
    started = time.perf_counter()
    prepared = prepare(data, settings)
    prepare_ms = _ms_since(started)

    # ---- 2. ASK THE MODEL ----
    request = ExtractionRequest(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=build_user_prompt(doc_type, prepared),
        images=prepared.images,
        json_schema=doc_type.json_schema,
    )
    started = time.perf_counter()
    try:
        response = provider.extract(request)
    except ModelUnavailable as exc:
        raise ExtractionError("model_unavailable", str(exc), http_status=503) from exc
    except ModelError as exc:
        raise ExtractionError("model_error", str(exc), http_status=502) from exc
    except ModelOutputInvalid as exc:
        raise ExtractionError("model_output_invalid", str(exc)) from exc
    model_ms = _ms_since(started)

    # ---- 3. VALIDATE ----
    document, unreadable = _validate(doc_type, response.data)

    # ---- 4. CHECK ----
    warnings = [f"Could not read {path}; left empty." for path in unreadable]
    warnings += document.consistency_warnings()
    warnings += [f"Note: {note}" for note in prepared.notes]

    # ---- 5. MASK ----
    # mode="json": dates → "YYYY-MM-DD", Decimal amounts → strings like "11800.00".
    result = mask_result(doc_type, document.model_dump(mode="json"), settings.unmask_set)

    return ExtractionOutcome(
        result=result,
        warnings=warnings,
        pages=prepared.pages,
        route=prepared.route,
        model=response.model,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        prepare_ms=prepare_ms,
        model_ms=model_ms,
    )


def _ms_since(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _validate(doc_type: DocumentType, data: dict) -> tuple[DocumentSchema, list[str]]:
    """Validates the model's JSON. Returns (document, list of field paths that were dropped).

    First attempt: validate as-is. If some fields are invalid (e.g. an impossible date),
    remove just those fields so they fall back to empty, and validate once more. Losing one
    field is better than losing the whole document. If it still fails, give up.
    Error messages contain field PATHS only ("line_items.0.amount"), never values.
    """
    try:
        return doc_type.model.model_validate(data), []
    except ValidationError as first:
        bad_locations = [err["loc"] for err in first.errors()]

    repaired = copy.deepcopy(data)
    # Remove deepest/last locations first so list indexes stay valid while removing.
    for loc in sorted(bad_locations, key=lambda loc: [str(p) for p in loc], reverse=True):
        _remove(repaired, loc)
    paths = [".".join(str(p) for p in loc) for loc in bad_locations]
    try:
        return doc_type.model.model_validate(repaired), paths
    except ValidationError as second:
        failed = sorted({".".join(str(p) for p in err["loc"]) for err in second.errors()})
        raise ExtractionError(
            "validation_failed", "The model's answer does not fit the schema at: " + ", ".join(failed[:10])
        ) from second


def _remove(data, loc: tuple) -> None:
    """Deletes the value at `loc` (e.g. ("line_items", 0, "amount")) if it exists."""
    node = data
    for part in loc[:-1]:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node):
            node = node[part]
        else:
            return
    last = loc[-1] if loc else None
    if isinstance(node, dict):
        node.pop(last, None)  # missing key -> the schema's default (null / empty)
    elif isinstance(node, list) and isinstance(last, int) and 0 <= last < len(node):
        node.pop(last)  # an unusable list item (e.g. a string instead of an object)
