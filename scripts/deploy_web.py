"""Build the React console (web/) and publish it to the web stack (S3 + CloudFront).

    uv run python scripts/deploy_web.py prod --profile lyndev

Prerequisites: the `app` and `web` stacks are deployed (scripts/deploy.py prod app web),
and the API allows the console's origin (CorsAllowOrigin in params/<env>.json, data
and app blocks, = the web stack's WebOrigin output). The script warns if it doesn't.

What it does:
  1. builds web/ (npm ci + npm run build: type-check, then Vite);
  2. writes dist/config.json with the API URL (NEVER a key: clients type their own);
  3. adds the two SYNTHETIC sample documents (tests/helpers/sample_docs.py), so a client
     can try the console without a real file (--no-samples to leave them out);
  4. uploads the build to the private bucket, deleting files no longer in it:
       index.html, config.json   Cache-Control: no-cache  (a release shows up at once)
       assets/*                  cached for a year        (file names contain a hash)
  5. invalidates CloudFront's cached copies of the files that keep their names.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _aws import session  # noqa: E402

NO_CACHE = "no-cache"                                   # revalidate every time
IMMUTABLE = "public, max-age=31536000, immutable"       # hashed file names never change


def outputs(cfn, stack: str) -> dict:
    """A stack's outputs as {OutputKey: OutputValue}."""
    s = cfn.describe_stacks(StackName=stack)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in s.get("Outputs", [])}


def main(argv: list[str] | None = None) -> int:
    """Build and publish the console (see the module docstring). Returns the exit code."""
    p = argparse.ArgumentParser(description="Build and publish the web console.")
    p.add_argument("env", choices=["dev", "prod"])
    p.add_argument("--profile")
    p.add_argument("--no-samples", action="store_true", help="Don't include the synthetic sample documents")
    args = p.parse_args(argv)

    sess = session(args.profile)
    cfn, s3, cf = sess.client("cloudformation"), sess.client("s3"), sess.client("cloudfront")
    web = outputs(cfn, f"slm-{args.env}-web")
    api_url = outputs(cfn, f"slm-{args.env}-app")["ApiUrl"]

    # The API must allow this origin, or every browser call fails with a CORS error.
    params = json.loads((ROOT / "infra" / "cloudformation" / "params" / f"{args.env}.json").read_text())
    if params["app"].get("CorsAllowOrigin") != web["WebOrigin"]:
        print(f"WARNING: set \"CorsAllowOrigin\": \"{web['WebOrigin']}\" in params/{args.env}.json (data and app "
              f"blocks) and run scripts/deploy.py {args.env} data app, or the console can't call the API.")

    print("1/4 Building web/")
    if not (WEB / "node_modules").exists():
        subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=WEB, check=True)
    subprocess.run(["npm", "run", "build"], cwd=WEB, check=True)
    dist = WEB / "dist"

    print("2/4 Writing config.json" + ("" if args.no_samples else " and sample documents"))
    (dist / "config.json").write_text(json.dumps({"apiUrl": api_url, "environment": args.env}) + "\n")
    if not args.no_samples:
        from tests.helpers.sample_docs import make_invoice_png, make_statement_pdf
        (dist / "samples").mkdir(exist_ok=True)
        (dist / "samples" / "sample-invoice.png").write_bytes(make_invoice_png())
        (dist / "samples" / "sample-statement.pdf").write_bytes(make_statement_pdf(rows=12))

    print(f"3/4 Uploading to s3://{web['WebBucketName']}")
    bucket = web["WebBucketName"]
    local = {str(f.relative_to(dist)): f for f in dist.rglob("*") if f.is_file()}
    for key, path in sorted(local.items()):
        ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
        cache = IMMUTABLE if key.startswith("assets/") else NO_CACHE
        s3.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": ctype, "CacheControl": cache})
        print(f"  {key:45s} {ctype:28s} {cache}")
    remote = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket) for o in page.get("Contents", [])]
    stale = [k for k in remote if k not in local]
    if stale:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale]})
        print(f"  removed {len(stale)} old files")

    print("4/4 Refreshing CloudFront")
    cf.create_invalidation(DistributionId=web["DistributionId"], InvalidationBatch={
        "Paths": {"Quantity": 3, "Items": ["/index.html", "/config.json", "/samples/*"]},
        "CallerReference": str(time.time())})
    print(f"\nConsole: {web['WebUrl']}   (API: {api_url})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
