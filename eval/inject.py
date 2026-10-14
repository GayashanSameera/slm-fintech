"""Fault injection: how often does the platform catch a misread value?

    AWS_PROFILE=lyndev uv run python -m eval.inject --per-group 8 --trials 3

Today's models read clean documents almost perfectly, so a normal evaluation (eval/run.py) cannot
show whether the safety nets work. This does: it takes the TRUTH of a generated document,
corrupts ONE value the way a misreading would (a wrong digit, a wrong letter, two characters
swapped), and pretends the model returned that. Then it asks every layer in order:

    1. consistency checks + identifier check digits + plausibility rules       (free)
    2. text grounding: is the value printed in the text layer?                  (free, digital docs)
    3. independent second reading + 2-of-3 vote                                 (scans; real Bedrock calls)

and records who caught it. The answer that matters is the last column: MISSED = the wrong value
would have been returned with no warning at all.
"""

from __future__ import annotations

import argparse
import copy
import random
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _aws import prefer_ipv4  # noqa: E402

prefer_ipv4()

from app.gateway.bedrock import BedrockGateway  # noqa: E402
from app.modules.extraction.checks import DOCUMENT_ONLY, run_checks  # noqa: E402
from app.modules.extraction.crossread import INTERPRETIVE_FIELDS, cross_read  # noqa: E402
from app.modules.extraction.pipeline import text_reconciliation_failures  # noqa: E402
from app.modules.extraction.preprocess import preprocess  # noqa: E402
from app.modules.extraction.profiles import load_profile  # noqa: E402
from app.modules.extraction.schemas import REGISTRY  # noqa: E402

from app.modules.extraction.pipeline import STATEMENT_PROVEN_FIELDS  # noqa: E402
from app.modules.extraction.preprocess.image import enhanced_pages  # noqa: E402

from eval.generate import build_cases  # noqa: E402
from eval.statements import ROWS_PER_PAGE, build_statement_cases  # noqa: E402
from eval.score import flatten  # noqa: E402

PROFILE = load_profile("default")
HAIKU = PROFILE.ladder[1].model

# Failure codes that existed BEFORE the identifier/plausibility/grounding work (arithmetic and
# plain formats). Everything else that fires is credited to the new layers.
OLD_CODES = {"line_items_sum_mismatch", "total_mismatch", "gstin_format", "pan_format", "earnings_sum_mismatch",
             "net_pay_mismatch", "ifsc_format", "required_fields_missing", "running_balance_mismatch",
             "closing_balance_mismatch", "transaction_missing_date", "transaction_missing_balance"}


# --- corrupting one value ----------------------------------------------------------------------

def _leaves(node, path=()):
    """(path, value) for every string leaf of a nested dict/list."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _leaves(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _leaves(v, path + (i,))
    elif isinstance(node, str) and node:
        yield path, node


def _set(node, path, value):
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value


def mutate(dump: dict, rng: random.Random, kind: str) -> tuple[dict, str] | None:
    """A copy of `dump` with one value corrupted, and the corrupted field's path (or None if no
    suitable value exists). kind: "digit" (one digit changed), "letter" (one letter changed),
    "swap" (two neighbouring characters exchanged)."""
    candidates = []
    for path, value in _leaves(dump):
        if path and path[-1] in INTERPRETIVE_FIELDS:
            continue
        if kind == "digit" and any(c.isdigit() for c in value):
            candidates.append((path, value))
        elif kind == "letter" and any(c.isalpha() for c in value) and not any(c.isdigit() for c in value):
            candidates.append((path, value))
        elif kind == "swap" and any(a != b and a.isalnum() and b.isalnum() for a, b in zip(value, value[1:])):
            candidates.append((path, value))
    if not candidates:
        return None
    path, value = rng.choice(candidates)
    chars = list(value)
    if kind == "digit":
        i = rng.choice([i for i, c in enumerate(chars) if c.isdigit()])
        chars[i] = rng.choice([d for d in "0123456789" if d != chars[i]])
    elif kind == "letter":
        i = rng.choice([i for i, c in enumerate(chars) if c.isalpha()])
        chars[i] = rng.choice([c for c in "aeioumnrstl" if c != chars[i].lower()])
        chars[i] = chars[i].upper() if value[i].isupper() else chars[i]
    else:
        i = rng.choice([i for i, (a, b) in enumerate(zip(chars, chars[1:])) if a != b and a.isalnum() and b.isalnum()])
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
    out = copy.deepcopy(dump)
    _set(out, path, "".join(chars))
    return out, ".".join(str(p) for p in path)


# --- one trial ----------------------------------------------------------------------------------

def trial(case, kind: str, seed: int, gateway) -> tuple[str, str, str] | None:
    """(condition, kind, verdict) for one corrupted value, or None if it could not be built.
    verdict: caught_old_checks | caught_new_checks | caught_grounding | corrected_by_vote |
             flagged_by_vote | MISSED"""
    spec = REGISTRY[case.doc_type]
    truth_dump = spec.model.model_validate(case.truth).model_dump(mode="json")
    rng = random.Random(f"{seed}-{case.case_id}-{kind}")
    for _ in range(12):                                   # some corruptions are not valid values: retry
        made = mutate(truth_dump, rng, kind)
        if made is None:
            return None
        mutated, _path = made
        try:
            parsed = spec.model.model_validate(mutated)
        except Exception:
            continue
        if parsed.model_dump(mode="json") == truth_dump:
            continue
        break
    else:
        return None

    pages = case.prepared.pages
    consistency = [f for f in run_checks(case.doc_type, parsed) if f.code not in DOCUMENT_ONLY]
    if consistency:
        codes = {f.code for f in consistency}
        return case.condition, kind, "caught_old_checks" if codes & OLD_CODES else "caught_new_checks"
    grounding = text_reconciliation_failures(case.doc_type, parsed, pages)
    if grounding:
        return case.condition, kind, "caught_grounding"
    if case.condition == "digital":
        return case.condition, kind, "MISSED"             # digital documents get no second reading
    result = cross_read(doc_type=case.doc_type, pages=pages, parsed=parsed, model_id=HAIKU, gateway=gateway,
                        readers=list(PROFILE.crossread_readers), arbiters=list(PROFILE.crossread_arbiters),
                        extra_check=lambda c: run_checks(case.doc_type, c))
    if flatten(result.parsed.model_dump(mode="json")) == flatten(truth_dump) and not result.unresolved:
        return case.condition, kind, "corrected_by_vote"
    if result.unresolved or [f for f in result.failures if f.code not in DOCUMENT_ONLY] or result.status == "unavailable":
        return case.condition, kind, "flagged_by_vote"
    return case.condition, kind, "MISSED"


def trial_statement(case, kind: str, seed: int, gateway) -> tuple[str, str, str] | None:
    """Fault injection for a BANK STATEMENT row, following the real pipeline:

      1. the whole-statement checks (balance chain, date order, statement period);
      2. digital: the row-by-row reconciliation with the PDF text;
      3. scanned: the page's second reading (same model, enhanced picture of that one page, amounts
         not compared) and, on disagreement, the arbiter.
    Only values inside transactions are corrupted: that is where a statement's risk is."""
    spec = REGISTRY["bank_statement"]
    truth_dump = spec.model.model_validate(case.truth).model_dump(mode="json")
    rng = random.Random(f"{seed}-{case.case_id}-{kind}")
    for _ in range(12):
        only_rows = {"transactions": truth_dump["transactions"]}
        made = mutate(only_rows, rng, kind)
        if made is None:
            return None
        mutated_rows, path = made
        mutated = {**truth_dump, "transactions": mutated_rows["transactions"]}
        try:
            parsed = spec.model.model_validate(mutated)
        except Exception:
            continue
        if parsed.model_dump(mode="json") != truth_dump:
            break
    else:
        return None
    row = int(path.split(".")[1])

    pages = case.prepared.pages
    consistency = [f for f in run_checks("bank_statement", parsed) if f.code not in DOCUMENT_ONLY]
    if consistency:
        codes = {f.code for f in consistency}
        return case.condition, kind, "caught_old_checks" if codes & OLD_CODES else "caught_new_checks"
    if case.condition == "digital":
        failed = text_reconciliation_failures("bank_statement", parsed, pages)
        return case.condition, kind, "caught_grounding" if failed else "MISSED"

    # Scanned: the second reading of the page the row is on (20 rows per page in these statements).
    page_no = row // ROWS_PER_PAGE
    page_rows = mutated["transactions"][page_no * ROWS_PER_PAGE:(page_no + 1) * ROWS_PER_PAGE]
    truth_rows = truth_dump["transactions"][page_no * ROWS_PER_PAGE:(page_no + 1) * ROWS_PER_PAGE]
    page_parsed = spec.model.model_validate({"transactions": page_rows})
    page = [pages[page_no]]
    result = cross_read(doc_type="bank_statement", pages=page, parsed=page_parsed, model_id=HAIKU, gateway=gateway,
                        readers=[], arbiters=list(PROFILE.crossread_arbiters), extra_check=lambda c: run_checks("bank_statement", c.model_copy(update={"opening_balance": None, "closing_balance": None})),
                        ignore=STATEMENT_PROVEN_FIELDS, reader_model=PROFILE.scan_reader, reader_pages=enhanced_pages(page))
    final = result.parsed.model_dump(mode="json")["transactions"]
    if flatten({"t": final}) == flatten({"t": truth_rows}) and not result.unresolved:
        return case.condition, kind, "corrected_by_vote"
    if result.unresolved or result.status == "unavailable" or [f for f in result.failures if f.code not in DOCUMENT_ONLY]:
        return case.condition, kind, "flagged_by_vote"
    return case.condition, kind, "MISSED"


def main() -> int:
    ap = argparse.ArgumentParser(description="Measure how often a misread value is caught.")
    ap.add_argument("--per-group", type=int, default=6)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", choices=["all", "documents", "statements"], default="all")
    args = ap.parse_args()

    cases = [] if args.only == "statements" else build_cases(args.per_group, args.seed, hard=True)
    for c in cases:
        c.prepared = preprocess(c.data)                   # sequential: pdfium is not thread-safe
    if args.only != "documents":
        cases += build_statement_cases(args.per_group, args.seed, hard=True)
    gateway = BedrockGateway()
    jobs = [(c, k) for c in cases for k in ("digit", "letter", "swap")]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = [r for r in pool.map(lambda j: (trial_statement if j[0].doc_type == 'bank_statement' else trial)(j[0], j[1], args.seed, gateway), jobs) if r]

    verdicts = ["caught_old_checks", "caught_new_checks", "caught_grounding", "corrected_by_vote", "flagged_by_vote", "MISSED"]
    lines = [f"# Fault injection {datetime.now():%Y-%m-%d %H:%M}\n",
             f"One corrupted value per trial ({len(results)} trials), seed {args.seed}. "
             "Share of trials by who caught the error.\n",
             "| Condition / corruption | Trials | " + " | ".join(verdicts) + " |",
             "|---|---|" + "---|" * len(verdicts)]
    groups: dict[str, Counter] = defaultdict(Counter)
    for cond, kind, verdict in results:
        for key in (f"{cond} / {kind}", f"{cond} / ALL", "ALL"):
            groups[key][verdict] += 1
    for key in sorted(groups):
        c = groups[key]
        n = sum(c.values())
        lines.append(f"| {'**'+key+'**' if key.endswith('ALL') else key} | {n} | "
                     + " | ".join(f"{c[v] / n:.0%}" if v != "MISSED" else f"**{c[v] / n:.1%}** ({c[v]})" for v in verdicts) + " |")
    report = "\n".join(lines)
    print(report)
    out = ROOT / "eval" / "results" / f"{datetime.now():%Y%m%d-%H%M}-inject.md"
    out.write_text(report + "\n")
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
