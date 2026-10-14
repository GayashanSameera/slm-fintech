"""End-to-end smoke test of a DEPLOYED environment with synthetic documents.

    uv run python scripts/smoke_test.py prod --profile lyndev --key-file ~/.slm/smoke.key

Run it after every deployment. It uses a dedicated SMOKE-TEST tenant, never a client's:
    uv run python scripts/create_tenant.py --env prod --name SMOKE-TEST --profile lyndev \
        --key-file ~/.slm/smoke.key

What it proves, against the real API, S3, GuardDuty, queues, workers and Bedrock:
  1. health check, and that requests without a valid key are refused (401);
  2. DIRECT flow: a digital bank statement -> Rung 0 parser, reconciled with the PDF
     text and checked by Nova Lite; correct rows, masked account number, token usage;
  3. PRESIGNED flow: an invoice image -> S3 upload -> malware scan -> Claude Haiku;
     correct totals, masked PAN, token usage.
Every document is synthetic (tests/helpers/sample_docs.py), and unique per run so
duplicate detection doesn't answer with an earlier result.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _aws import REGION, session  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    import httpx

    from tests.helpers.sample_docs import make_invoice_png, make_statement_pdf

    p = argparse.ArgumentParser(description="Smoke-test a deployed environment.")
    p.add_argument("env", choices=["dev", "prod"])
    p.add_argument("--profile")
    p.add_argument("--key-file", required=True, help="File holding the SMOKE-TEST tenant's API key")
    args = p.parse_args(argv)

    sess = session(args.profile)
    stack = sess.client("cloudformation").describe_stacks(StackName=f"slm-{args.env}-app")["Stacks"][0]
    api = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}["ApiUrl"].rstrip("/")
    auth = {"Authorization": f"Bearer {Path(args.key_file).expanduser().read_text().strip()}"}
    ok = True

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= bool(cond)
        print(f"  {'✓' if cond else '✗'} {label}{(': ' + detail) if detail else ''}")

    def wait_final(job_id: str, limit_s: int = 180) -> dict:
        deadline = time.monotonic() + limit_s
        while True:
            body = httpx.get(f"{api}/v1/extractions/{job_id}?wait=20", headers=auth, timeout=40).json()
            if body.get("status") in ("succeeded", "failed", "expired") or time.monotonic() > deadline:
                return body

    print(f"API {api}")
    print("1. Health and authentication")
    check("GET /healthz", httpx.get(f"{api}/healthz", timeout=30).json() == {"ok": True})
    check("no key -> 401", httpx.get(f"{api}/v1/extractions", timeout=30).status_code == 401)
    check("smoke key -> 200", httpx.get(f"{api}/v1/document-types", headers=auth, timeout=30).status_code == 200)

    print("2. Direct flow: digital bank statement -> parser + text reconciliation + Nova Lite check")
    t0 = time.monotonic()
    pdf = make_statement_pdf(rows=12, account=f"0012{int(time.time()) % 10**8:08d}")
    r = httpx.post(f"{api}/v1/extractions?wait=25", headers=auth, timeout=40,
                   files={"file": ("statement.pdf", pdf, "application/pdf")})
    body = r.json()
    if body.get("status") not in ("succeeded", "failed") and "id" in body:
        body = wait_final(body["id"])
    res = body.get("result") or {}
    check("extracted by the parser", body.get("status") == "succeeded" and body.get("method") == "parser",
          f"{body.get('status')} method={body.get('method')} verified_by={body.get('verified_by')} "
          f"warnings={body.get('warnings')} error={body.get('error')} in {time.monotonic() - t0:.1f}s")
    check("12 rows, account masked", len(res.get("transactions", [])) == 12
          and str(res.get("account_number", "")).startswith("XXXX"))
    check("token usage returned", (body.get("usage") or {}).get("model_calls", 0) >= 1, str(body.get("usage")))

    print("3. Presigned flow: invoice image -> S3 -> malware scan -> Claude Haiku 5.5")
    t0 = time.monotonic()
    created = httpx.post(f"{api}/v1/extractions", headers=auth, json={"document_type": "invoice"}, timeout=30).json()
    up = created["upload"]
    s3 = httpx.post(up["url"], data=up["fields"], timeout=60,
                    files={"file": ("invoice.png", make_invoice_png(), "image/png")})
    check("upload to S3", s3.status_code in (200, 204), f"HTTP {s3.status_code}")
    body = wait_final(created["id"])
    res = body.get("result") or {}
    check("invoice extracted", body.get("status") == "succeeded",
          f"model={body.get('model_id')} warnings={body.get('warnings')} error={body.get('error')} "
          f"in {time.monotonic() - t0:.1f}s")
    check("total and masked PAN", res.get("total") == "17700.00"
          and str((res.get("seller") or {}).get("pan", "")).startswith("XXXX"))

    print("\nALL CHECKS PASSED" if ok else
          f"\nSOME CHECKS FAILED: aws logs tail /aws/lambda/slm-{args.env}-worker --since 15m --region {REGION}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
