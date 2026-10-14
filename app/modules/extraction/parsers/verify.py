"""AI verification of a Rung 0 parser result (requirement FR-9.3).

HOW IT WORKS: "read independently, compare in code".
  1. The cheapest model (Nova Lite) reads a few KEY fields from the statement pages
     itself: account number and period (required), and, where printed, account holder,
     IFSC, branch, and the first and last transaction (date + description).
  2. Our code compares its reading with the parser's result, field by field, after
     normalising case, spaces and date formats.
  3. Any mismatch rejects the parser result (the statement then goes to the AI ladder).

Why these fields: amounts and balances are already PROVEN by the running-balance and
coverage checks (checks.py, coverage.py). What arithmetic can't prove is who the
statement belongs to, which account, which period, and whether the parser framed the
rows correctly (first and last row). That is what an independent reading checks.
The NUMBER of rows is not asked: models miscount (Nova Lite was one short every time),
and coverage.py already compares the row count exactly against the PDF's text layer.

Why not "show the model the parser's JSON and ask for differences" (the first design):
in the first real AWS test (2026-10-14) Nova Lite reported identical values as
differences, ignored the "at most 10 issues" rule and ran out of output tokens on a
two-page statement. Reading a dozen fields is a task it does reliably; the comparison
is done by code, which doesn't make mistakes. The answer is ~150 tokens whatever the
statement's length, so verification costs ~USD 0.0002 per statement, and the parser's
output is never sent to the model at all.

Safety properties:
  * The verifier can only REJECT a parser result. It never changes a value.
  * An unreadable answer, or a required field the model couldn't read, counts as a
    mismatch: when in doubt, the statement goes to full AI extraction.
  * Only field NAMES are logged (`issue_paths`), never values from either side.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.gateway import GatewayResult, ModelGateway

from ..prompts import VERIFY_INSTRUCTION, VERIFY_SYSTEM, strip_code_fences
from ..schemas.base import parse_date
from .lexicon import parse_any_date

log = get_logger(__name__)

# Fields the model must read; if it can't, we can't confirm the parser, so it's a mismatch.
# (Account holder and IFSC are not REQUIRED: many statements, e.g. Sri Lankan ones, print no
# IFSC, and some print the holder only in an address block the parser doesn't read.)
REQUIRED = ("account_number", "period_from", "period_to")
# Compared when BOTH sides have a value (a layout may not print a branch; the parser may not
# have found the holder). A value the parser has but the model reads differently rejects.
OPTIONAL = ("account_holder", "ifsc", "branch", "first_transaction.date", "first_transaction.description",
            "last_transaction.date", "last_transaction.description")
# Free text: models shorten long descriptions or leave out a wrapped line, so these match when one
# reading CONTAINS the other. Exact words are already proven by the text reconciliation
# (reconcile.py); here the question is only "is this the right row / the right person".
LENIENT = ("account_holder", "branch", "first_transaction.description", "last_transaction.description")


@dataclass
class VerifyResult:
    agree: bool
    issue_paths: list[str] = field(default_factory=list)
    gateway_result: GatewayResult | None = None


def parser_view(statement) -> dict:
    """The same key fields, taken from the parser's result, for comparison."""
    data = statement.model_dump(mode="json")
    rows = data.get("transactions") or []
    view = {k: data.get(k) for k in ("account_holder", "account_number", "ifsc", "branch", "period_from", "period_to")}
    if rows:
        view["first_transaction"] = {"date": rows[0].get("date"), "description": rows[0].get("description")}
        view["last_transaction"] = {"date": rows[-1].get("date"), "description": rows[-1].get("description")}
    return view


def _get(data: dict, path: str):
    """Value at a dotted path such as "first_transaction.date"; None if any part is missing."""
    node = data
    for part in path.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def _normalise(value, hint=None) -> str | None:
    """Comparable form: dates -> ISO (a date without a year takes it from `hint`, the
    statement's end date), everything else -> no spaces, case-folded."""
    if value is None or value == "":
        return None
    text = str(value).strip()
    try:
        parsed = parse_date(text)
        if parsed:
            return parsed.isoformat()
    except ValueError:
        pass
    as_date = parse_any_date(text, hint)
    if as_date:
        return as_date.isoformat()
    return re.sub(r"\s+", "", text).casefold()


def compare(model_reading: dict, parsed: dict) -> list[str]:
    """Paths where the model's independent reading doesn't confirm the parser."""
    try:
        hint = parse_date(parsed.get("period_to"))       # ISO "2026-08-31" from the parser
    except ValueError:
        hint = None
    issues = []
    for path in REQUIRED + OPTIONAL:
        theirs, ours = _normalise(_get(model_reading, path), hint), _normalise(_get(parsed, path), hint)
        if path in REQUIRED:
            if theirs is None or theirs != ours:
                issues.append(path)          # missing on either side, or different
            continue
        if theirs is None or ours is None:
            continue                         # not printed / not found: nothing to compare
        if path in LENIENT:
            if theirs not in ours and ours not in theirs:
                issues.append(path)
        elif theirs != ours:
            issues.append(path)
    return issues


def verify(pages: list[dict], statement, gateway: ModelGateway, model_id: str) -> VerifyResult:
    """Independent AI check of a parser result: Nova Lite reads the key facts from the page
    images without seeing the parser's output, and `compare` checks them field by field.
    One model call (~150 output tokens); the result can only reject, never change a value.
    """
    res = gateway.generate(
        model_id=model_id,
        system=VERIFY_SYSTEM,
        pages=pages,
        instruction=VERIFY_INSTRUCTION,
        thinking=False,
        max_tokens=400,
    )
    try:
        reading = json.loads(strip_code_fences(res.text))
        if not isinstance(reading, dict):
            raise TypeError("not an object")
        paths = compare(reading, parser_view(statement))
    except (json.JSONDecodeError, TypeError):
        paths = ["_verifier_unreadable"]
    agree = not paths
    log.info("rung0_verified", agree=agree, issue_count=len(paths), issue_paths=paths[:10], model_id=model_id)
    return VerifyResult(agree=agree, issue_paths=paths, gateway_result=res)
