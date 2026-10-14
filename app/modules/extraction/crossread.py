"""Independent second reading with 2-of-3 voting, for documents that have no other proof.

THE PROBLEM: a SCANNED page has no text layer, so nothing can show that a name, date or amount
was read correctly, and the arithmetic checks (checks.py) only prove that numbers are
CONSISTENT with each other. Invoices, payslips, identity documents and KYC forms have far fewer
such checks than bank statements. One model can misread a digit and still pass.

THE IDEA: two readers rarely make the SAME mistake. After the normal ladder produced an answer
that passed every check:

    reading A   = the answer the ladder produced                       (e.g. Claude Haiku)
    reading B   = a second, INDEPENDENT reading by a different model   (e.g. Nova Lite)
                  with the same prompt; it never sees A.
    A and B agree on every field  -> accept (cost: one cheap extra call).
    they disagree somewhere       -> reading C by a third, stronger model (e.g. Sonnet),
                                     then every disputed field takes the value at least TWO
                                     of the three readings agree on.
    all three differ on a field   -> the value stays as A read it but the field is reported
                                     in `warnings` as "unverified_field:<path>". It is never
                                     silently accepted.

Everything after the model calls is deterministic Python (compare, vote, re-run the checks).
The merged answer is validated and checked again; if the vote made things worse than A, A is
kept. Only field PATHS leave this module, never values (safe for logs and warnings).

Not used for bank statements: their rows are proven by the running balance and, for digital
PDFs, by the text itself (parsers/reconcile.py).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.core.errors import JobError
from app.core.logging import get_logger
from app.gateway import GatewayResult, ModelGateway

from .checks import DOCUMENT_ONLY, CheckFailure
from .prompts import instruction, system_prompt
from .schemas import REGISTRY

log = get_logger(__name__)


# --- comparing readings --------------------------------------------------------------------

# Fields whose value is the model's INTERPRETATION (which enum value fits, how an issuer is worded),
# not something read off the page. Two good readers legitimately differ on them, and arguing about
# them would trigger the expensive third reading for nothing. They are validated by the schema.
INTERPRETIVE_FIELDS = {"id_type", "issuer", "gender", "pep_status", "occupation", "signature_present",
                       "pay_period", "bank_name"}      # a bank's name is branding, worded differently on every page      # "June 2026" and "2026-06" are the same period

# An address is compared (and voted on) AS A WHOLE. Two readers split it differently ("12 Park Street"
# + "Hyderabad" vs one long line) while reading exactly the same words, so field-by-field comparison
# would raise false alarms. Its parts are joined in a fixed order and compared as one string.
ATOMIC_FIELDS = {"address", "current_address", "permanent_address"}
_ADDRESS_PARTS = ("line", "city", "state", "pincode")


def address_text(value: Any) -> str:
    """The normalised words of an address dict, whichever fields they were put in."""
    if not isinstance(value, dict):
        return normalise(value)
    return "".join(normalise(value.get(part)) for part in _ADDRESS_PARTS)


_TOTAL_LINE = re.compile(r"\b(gross|total|net pay|net salary)\b", re.IGNORECASE)


def clean_reading(doc_type: str, dump: dict) -> dict:
    """Remove known harmless quirks from a second reading BEFORE comparing: some models list the
    "Gross pay" / "Total deductions" lines of a payslip as components. The ladder's own reading is
    rejected by checks.py for that mistake; here it would only cause a pointless third reading."""
    if doc_type != "payslip":
        return dump
    cleaned = dict(dump)
    for key in ("earnings", "deductions"):
        cleaned[key] = [row for row in dump.get(key) or [] if not _TOTAL_LINE.search((row or {}).get("name") or "")]
    return cleaned

def normalise(value: Any) -> str:
    """The comparable form of one value, so harmless differences are not 'disagreements'.

    Money ("118000.50") and dates ("2026-09-07") are already canonical (schemas/base.py), so they
    compare as they are. Text is case-folded with spaces and punctuation removed: "Demo Traders
    Pvt. Ltd." equals "DEMO TRADERS PVT LTD". None and "" are the same: 'not found'.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return re.sub(r"[\W_]+", "", str(value).casefold())


def disagreements(a: Any, b: Any, path: str = "", ignore: frozenset = frozenset()) -> list[str]:
    """Field paths where two readings differ. Lists are compared item by item; a different
    number of items is reported once, as "<path>[]". Field names in `ignore` are skipped at every
    depth (used for bank statements: amounts and balances are proven by the running balance)."""
    if isinstance(a, dict) or isinstance(b, dict):
        a, b = a if isinstance(a, dict) else {}, b if isinstance(b, dict) else {}
        out: list[str] = []
        for key in sorted(set(a) | set(b)):
            if key in INTERPRETIVE_FIELDS or key in ignore:
                continue
            child = f"{path}.{key}" if path else key
            if key in ATOMIC_FIELDS:
                if address_text(a.get(key)) != address_text(b.get(key)):
                    out.append(child)
                continue
            out += disagreements(a.get(key), b.get(key), child, ignore)
        return out
    if isinstance(a, list) or isinstance(b, list):
        a, b = a if isinstance(a, list) else [], b if isinstance(b, list) else []
        if len(a) != len(b):
            return [f"{path}[]"]
        out = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out += disagreements(x, y, f"{path}[{i}]", ignore)
        return out
    return [] if normalise(a) == normalise(b) else [path]


# --- voting --------------------------------------------------------------------------------

def vote(readings: list[Any], path: str, unresolved: list[str], ignore: frozenset = frozenset()) -> Any:
    """Merge several readings of the same value by majority. `readings[0]` is the ladder's answer
    and wins ties between equal-sized groups only when it is part of the biggest group; fields on
    which no two readings agree are appended to `unresolved` and keep reading 0's value.

    Recursion mirrors the JSON: objects vote key by key; lists first vote on their LENGTH (the
    items of readings with the winning length are then voted one by one); scalars vote on their
    normalised value.
    """
    if any(isinstance(r, dict) for r in readings):
        dicts = [r if isinstance(r, dict) else {} for r in readings]
        keys = {k: None for d in dicts for k in d}          # insertion-ordered union of keys
        merged = {}
        for k in keys:
            child = f"{path}.{k}" if path else k
            if k in ignore:
                merged[k] = dicts[0].get(k)         # proven elsewhere (balance chain): keep the ladder's value
            elif k in ATOMIC_FIELDS:
                merged[k] = _vote_atomic([d.get(k) for d in dicts], child, unresolved)
            else:
                merged[k] = vote([d.get(k) for d in dicts], child, unresolved, ignore)
        return merged

    if any(isinstance(r, list) for r in readings):
        lists = [r if isinstance(r, list) else [] for r in readings]
        lengths = Counter(len(x) for x in lists)
        length, count = lengths.most_common(1)[0]
        if count < 2:                                       # three different lengths: no majority
            unresolved.append(f"{path}[]")
            length = len(lists[0])
        voters = [x for x in lists if len(x) == length] or [lists[0]]
        return [vote([x[i] for x in voters], f"{path}[{i}]", unresolved, ignore) for i in range(length)]

    counts = Counter(normalise(r) for r in readings)
    winner, count = counts.most_common(1)[0]
    if count < 2 and len(readings) > 1:
        unresolved.append(path)                             # nobody agrees: keep reading 0, flag it
        return readings[0]
    # Prefer reading 0's own raw value when it is in the winning group (keeps its formatting).
    for r in readings:
        if normalise(r) == winner:
            return None if winner == "" else r
    return readings[0]


def _vote_atomic(readings: list[Any], path: str, unresolved: list[str]) -> Any:
    """Vote on a whole address: the winning reading's address is taken as it is, never mixed."""
    counts = Counter(address_text(r) for r in readings)
    winner, count = counts.most_common(1)[0]
    if count < 2 and len(readings) > 1:
        unresolved.append(path)
        return readings[0]
    return next(r for r in readings if address_text(r) == winner)


# --- the procedure ---------------------------------------------------------------------------

@dataclass
class CrossReadResult:
    parsed: BaseModel                       # the answer to use (A, or the voted merge)
    failures: list[CheckFailure]            # its consistency-check failures
    model_id: str                           # model credited with the result
    calls: list[GatewayResult]              # every extra model call, for metering
    unresolved: list[str] = field(default_factory=list)   # field paths no two readings agreed on
    status: str = "agreed"                  # "agreed" | "voted" | "unavailable"


def _pick(preferences: list[str], exclude: set[str]) -> str | None:
    """First model in `preferences` that is not in `exclude` (so readers are independent)."""
    return next((m for m in preferences if m not in exclude), None)


def _read(doc_type: str, pages: list[dict], gateway: ModelGateway, model_id: str,
          calls: list[GatewayResult]) -> BaseModel | None:
    """One independent extraction by `model_id`; None if the answer is unusable (bad JSON/schema).

    A weaker model sometimes answers in the wrong shape (for example a date written year-day-month).
    It gets ONE retry with the problem named, because falling straight through to the expensive
    arbiter for a formatting slip would cost about 30 times more than asking again."""
    # Imported here: pipeline imports this module, and parse_and_validate lives in pipeline.
    from .pipeline import parse_and_validate

    spec = REGISTRY[doc_type]
    problems: list[str] = []
    for _attempt in range(2):
        res = gateway.generate(model_id=model_id, system=system_prompt(spec, copy_dates=True), pages=pages,
                               instruction=instruction(problems), thinking=False)
        calls.append(res)
        parsed, problems = parse_and_validate(res, spec.model)
        if parsed is not None:
            return parsed
    return None


def blocking(failures: list[CheckFailure]) -> list[CheckFailure]:
    """The failures that mean 'the extraction is wrong' (not just 'the document is odd')."""
    return [f for f in failures if f.code not in DOCUMENT_ONLY]


def cross_read(*, doc_type: str, pages: list[dict], parsed: BaseModel, model_id: str,
               gateway: ModelGateway, readers: list[str], arbiters: list[str],
               extra_check, ignore: frozenset = frozenset(), reader_model: str | None = None,
               reader_pages: list[dict] | None = None) -> CrossReadResult:
    """Run the second (and, on disagreement, third) reading and vote.

    `readers`/`arbiters` are model ids in order of preference; models already used are skipped so
    the readings stay independent. `reader_model` (with `reader_pages`) instead fixes the second
    reader even if it is the same model as the first: used for scanned statement pages, where the
    same model reads a DIFFERENTLY PROCESSED picture of the page (preprocess/image.enhanced_view). `extra_check(parsed)` returns the full list of check failures
    for a candidate answer (consistency checks + text grounding), used to re-validate the merge.
    Model errors are not fatal: if no second reading is possible the original answer is returned
    with status "unavailable" and the caller reports it.
    """
    spec = REGISTRY[doc_type]
    calls: list[GatewayResult] = []
    original = parsed.model_dump(mode="json")
    used = {model_id}

    reader = reader_model or _pick(readers, used)
    if reader is None:
        return CrossReadResult(parsed, extra_check(parsed), model_id, calls, status="unavailable")
    used.add(reader)
    try:
        second = _read(doc_type, reader_pages or pages, gateway, reader, calls)
    except JobError:
        second = None
    if second is None:
        # The second reader gave nothing usable: try the arbiter in its place so there is still
        # an independent opinion; if that fails too the answer is returned as unverified.
        arbiter = _pick(arbiters, used)
        if arbiter is not None:
            used.add(arbiter)
            try:
                second = _read(doc_type, pages, gateway, arbiter, calls)
            except JobError:
                second = None
        if second is None:
            log.info("crossread", doc_type=doc_type, status="unavailable")
            return CrossReadResult(parsed, extra_check(parsed), model_id, calls, status="unavailable")

    second_dump = clean_reading(doc_type, second.model_dump(mode="json"))
    disputed = disagreements(original, second_dump, ignore=ignore)
    if not disputed:
        log.info("crossread", doc_type=doc_type, status="agreed", count=len(calls))
        return CrossReadResult(parsed, extra_check(parsed), model_id, calls, status="agreed")

    # Disagreement: a third, stronger reading decides each disputed field by majority.
    arbiter = _pick(arbiters, used)
    third_dump = None
    if arbiter is not None:
        try:
            third = _read(doc_type, pages, gateway, arbiter, calls)
            third_dump = clean_reading(doc_type, third.model_dump(mode="json")) if third is not None else None
        except JobError:
            third_dump = None
    unresolved: list[str] = []
    readings = [original, second_dump] + ([third_dump] if third_dump is not None else [])
    merged_raw = vote(readings, "", unresolved, ignore) if len(readings) > 2 else original
    if len(readings) == 2:
        unresolved = list(disputed)             # only two opinions and they differ: nothing to vote with
    log.info("crossread", doc_type=doc_type, status="voted", count=len(calls),
             issue_count=len(disputed), issue_paths=disputed[:10])

    try:
        merged = spec.model.model_validate(merged_raw)
    except Exception:                           # the merge produced an invalid object: keep A, flag all
        return CrossReadResult(parsed, extra_check(parsed), model_id, calls, disputed, "voted")
    merged_failures, original_failures = extra_check(merged), extra_check(parsed)
    if len(blocking(merged_failures)) > len(blocking(original_failures)):
        # The vote made the checks worse (e.g. broke a total): do not use it; report the disputes.
        return CrossReadResult(parsed, original_failures, model_id, calls, disputed, "voted")
    credited = arbiter if (third_dump is not None and merged_raw != original and arbiter) else model_id
    return CrossReadResult(merged, merged_failures, credited, calls, unresolved, "voted")
