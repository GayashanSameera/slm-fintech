"""Accuracy evaluation against labelled synthetic documents, through the REAL pipeline and Bedrock.

    AWS_PROFILE=lyndev uv run python -m eval.run --per-group 6 --mode both
    AWS_PROFILE=lyndev uv run python -m eval.run --per-group 4 --hard --mode on

What it does for every generated document (eval/generate.py):
    1. preprocess the file exactly like the worker does (PDF text layer or scan image);
    2. run extract_document with the real BedrockGateway (all checks, grounding, second reading);
    3. mask the TRUTH the same way the pipeline masks results, then compare field by field.
`--mode off` turns the independent second reading off, `on` leaves it on, `both` runs the two
so the effect of the second reading is visible. Cost is measured from the real token counts.

It writes a markdown report to eval/results/. It calls Bedrock and so costs a few US cents.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# Same workaround as the operator scripts: on a network with broken IPv6 the AWS client would hang
# trying IPv6 first (scripts/_aws.py). Must run before any AWS client is created.
from _aws import prefer_ipv4  # noqa: E402

prefer_ipv4()

from app.core.errors import JobError, TemporaryError  # noqa: E402
from app.gateway.bedrock import BedrockGateway  # noqa: E402
from app.modules.extraction.masking import has_aadhaar_markers, mask  # noqa: E402
from app.modules.extraction.normalise import to_result  # noqa: E402
from app.modules.extraction.pipeline import extract_document  # noqa: E402
from app.modules.extraction.preprocess import preprocess  # noqa: E402
from app.modules.extraction.profiles import load_profile  # noqa: E402
from app.modules.extraction.schemas import REGISTRY  # noqa: E402
from app.storage.tenants import TenantSettings  # noqa: E402
from app.usage import estimate_cost  # noqa: E402

from eval.generate import build_cases  # noqa: E402
from eval.score import DocScore, score  # noqa: E402
from eval.statements import build_statement_cases  # noqa: E402

# The tenant the evaluation pretends to be: sees PAN etc. unmasked so they can be compared
# (Aadhaar is ALWAYS masked; the truth is masked the same way below).
TENANT = TenantSettings(tenant_id="t_eval", verification_mode="ai",
                        unmasked_kinds=["pan", "account", "id_number", "phone"])


def run_case(case, profile, gateway) -> DocScore:
    """Extract one case and score it. A job that fails outright counts as all fields wrong.
    `case.prepared` was built beforehand, in the main thread: the PDF library (pdfium) is not
    thread-safe, so documents must never be preprocessed from several threads at once."""
    spec = REGISTRY[case.doc_type]
    # The right answer in the form the pipeline returns: normalised by the schema, then masked.
    truth = to_result(spec.model.model_validate(case.truth))
    strict = spec.sensitive_identity or has_aadhaar_markers(json.dumps(truth))
    case.truth_final = mask(truth, strict_aadhaar=strict, unmasked_kinds=set(TENANT.unmasked_kinds))
    try:
        prepared = case.prepared
        outcome = extract_document(data=case.data, prepared=prepared, requested_type=case.doc_type, tenant=TENANT,
                                   profile=profile, gateway=gateway, job_id=case.case_id)
    except (JobError, TemporaryError) as e:
        calls = getattr(e, "calls", [])
        return score(case, None, [], sum(estimate_cost(c) for c in calls), len(calls),
                     failed=getattr(e, "code", type(e).__name__))
    cost = sum(estimate_cost(c) for c in outcome.calls)
    s = score(case, outcome.result, outcome.warnings, cost, len(outcome.calls))
    s.pages = len(case.prepared.pages)
    for c in outcome.calls:
        entry = s.by_model.setdefault(c.model_id, [0, 0.0])
        entry[0] += 1
        entry[1] += estimate_cost(c)
    return s


def summarise(scores: list[DocScore]) -> dict:
    """Totals for a group of documents."""
    n = len(scores)
    fields = sum(s.fields for s in scores)
    return {
        "docs": n,
        "field_accuracy": sum(s.correct for s in scores) / fields if fields else 0.0,
        "docs_exact": sum(not s.has_error for s in scores),
        "docs_with_error": sum(s.has_error for s in scores),
        "docs_flagged": sum(s.has_error and bool(s.warnings) for s in scores),
        "wrong_pass": sum(s.wrongly_passed for s in scores),
        "failed": sum(bool(s.failed) for s in scores),
        "cost": sum(s.cost_usd for s in scores) / n if n else 0.0,
        "calls": sum(s.calls for s in scores) / n if n else 0.0,
    }


def cost_breakdown(scores: list[DocScore]) -> str:
    """Where the money goes: calls and USD per model, per page and per document."""
    models: dict[str, list] = {}
    for s in scores:
        for model, (calls, cost) in s.by_model.items():
            entry = models.setdefault(model, [0, 0.0])
            entry[0] += calls
            entry[1] += cost
    pages = sum(s.pages for s in scores) or 1
    total = sum(v[1] for v in models.values()) or 1e-9
    rows = ["| Model | Calls | USD | Share | USD per page |", "|---|---|---|---|---|"]
    for model, (calls, cost) in sorted(models.items(), key=lambda kv: -kv[1][1]):
        rows.append(f"| {model.split('.')[-1]} | {calls} | {cost:.4f} | {cost / total:.0%} | {cost / pages:.5f} |")
    rows.append(f"| **total** | {sum(v[0] for v in models.values())} | {total:.4f} | | **{total / pages:.5f}** ({pages} pages) |")
    return "\n".join(rows)


def table(scores: list[DocScore]) -> str:
    """A markdown table: one row per (document type, condition) plus a total."""
    groups: dict[tuple, list[DocScore]] = defaultdict(list)
    for s in scores:
        groups[(s.doc_type, s.condition)].append(s)
    rows = ["| Group | Docs | Field accuracy | Docs 100% right | Docs with an error | …of which flagged | **WRONG PASS** | Failed | Avg cost/doc | Avg calls |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for key in sorted(groups):
        m = summarise(groups[key])
        rows.append(f"| {key[0]} / {key[1]} | {m['docs']} | {m['field_accuracy']:.1%} | {m['docs_exact']} | {m['docs_with_error']} "
                    f"| {m['docs_flagged']} | **{m['wrong_pass']}** | {m['failed']} | ${m['cost']:.5f} | {m['calls']:.1f} |")
    m = summarise(scores)
    rows.append(f"| **TOTAL** | {m['docs']} | **{m['field_accuracy']:.1%}** | {m['docs_exact']} | {m['docs_with_error']} "
                f"| {m['docs_flagged']} | **{m['wrong_pass']}** | {m['failed']} | ${m['cost']:.5f} | {m['calls']:.1f} |")
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="Evaluate extraction accuracy on labelled synthetic documents.")
    ap.add_argument("--per-group", type=int, default=5, help="documents per (kind, condition)")
    ap.add_argument("--variants", default="chunk3,off,current",
                    help="comma list of: off (no second reading), current (profile as is), "
                         "nova-first (Nova reads first, Haiku second, Sonnet arbitrates), "
                         "haiku-second (Haiku first, Haiku-thinking cannot be reused: Nova second, Sonnet arbitrates)")
    ap.add_argument("--hard", action="store_true", help="rougher scans (more noise, blur, rotation)")
    ap.add_argument("--only", choices=["all", "documents", "statements"], default="all",
                    help="statements = bank statements only (the platform's main workload)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    base = load_profile("default")
    nova, haiku, sonnet = (r.model for r in base.ladder)
    variants = {
        # The behaviour before this work: scanned statements read 3 pages per call, no second reading.
        "chunk3": dataclasses.replace(base, crossread_enabled=False, scan_chunk_pages=3),
        "off": dataclasses.replace(base, crossread_enabled=False),
        "current": dataclasses.replace(base, crossread_enabled=True),
        "nova-first": dataclasses.replace(
            base, crossread_enabled=True, start_rung={k: 0 for k in base.start_rung},
            crossread_readers=(haiku, nova), crossread_arbiters=(sonnet, haiku)),
    }
    chosen = [v.strip() for v in args.variants.split(",")]
    gateway = BedrockGateway()
    report = [f"# Accuracy evaluation {datetime.now():%Y-%m-%d %H:%M}",
              f"\nSynthetic labelled documents, seed {args.seed}, {'hard' if args.hard else 'normal'} scans, "
              f"{args.per_group} per group, real Bedrock models.\n"]
    for name in chosen:
        profile = variants[name]
        cases = [] if args.only == "statements" else build_cases(args.per_group, args.seed, args.hard)
        for c in cases:
            c.prepared = preprocess(c.data)          # sequential on purpose (see run_case)
        if args.only != "documents":
            cases += build_statement_cases(args.per_group, args.seed, args.hard)
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            scores = list(pool.map(lambda c: run_case(c, profile, gateway), cases))
        title = f"Variant: {name}"
        print(f"\n## {title}  ({time.time() - t0:.0f}s)\n{table(scores)}")
        report += [f"\n## {title}\n", table(scores), "\n### Documents with errors\n"]
        for s in scores:
            if s.has_error:
                report.append(f"- `{s.case_id}`: wrong={s.wrong[:8]} invented={s.invented[:4]} warnings={s.warnings[:6]} failed={s.failed}")
    out = ROOT / "eval" / "results" / f"{datetime.now():%Y%m%d-%H%M}-{"hard" if args.hard else "normal"}.md"
    out.write_text("\n".join(report) + "\n")
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
