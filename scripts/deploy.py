"""Deploy the platform's CloudFormation stacks, always through a reviewed change set.

    uv run python scripts/deploy.py prod all  --profile lyndev          # all four, in order
    uv run python scripts/deploy.py prod app  --profile lyndev          # just the code / app stack
    uv run python scripts/deploy.py prod app  --profile lyndev --yes    # no question (CI)

Stacks, in dependency order (each imports values exported by the ones before it):
    data           KMS keys, DynamoDB table, documents bucket, code bucket
    security       tenant-data role, API-key pepper, GuardDuty role, CloudTrail
    app            Lambda functions, queues, API Gateway, WAF, events, GuardDuty plan
    observability  alarms, budget, dashboard, security-change alerts
    web            HTTPS hosting of the React console (private S3 + CloudFront); independent

For each stack this:
  1. lints the template with cfn-lint (if available),
  2. reads its parameters from infra/cloudformation/params/<env>.json,
  3. for `app`: builds the Lambda zip (scripts/build_lambda.py) and uploads it to the
     code bucket, unless that exact package (named by content hash) is already there,
  4. creates a CHANGE SET, prints what would change, and asks before executing it
     ("Replacement: True" means a resource is DELETED and recreated: stop and think),
  5. executes it and waits, printing the first errors if it fails (CloudFormation then
     rolls the stack back to its last working state by itself).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from _aws import REGION, session  # noqa: E402

TEMPLATES = ROOT / "infra" / "cloudformation"
ORDER = ("data", "security", "app", "observability", "web")
INLINE_LIMIT = 51_200        # CloudFormation's limit for a template sent inline


def lint(template: Path) -> None:
    """Run cfn-lint on a template (via uvx if not installed); stop the deployment on any problem."""
    exe = shutil.which("cfn-lint") or (shutil.which("uvx") and "uvx")
    if not exe:
        print("  (cfn-lint not available: skipping lint)")
        return
    # The template goes BEFORE --regions: that option takes several values and would swallow it.
    cmd = (["uvx"] if exe == "uvx" else []) + ["cfn-lint", str(template), "--regions", REGION]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout or result.stderr)
        raise SystemExit(f"cfn-lint found problems in {template.name}")
    print("  lint ok")


def exports(cfn) -> dict:
    """Every CloudFormation export of the account and region, as {name: value}."""
    out, token = {}, None
    while True:
        resp = cfn.list_exports(**({"NextToken": token} if token else {}))
        out.update({e["Name"]: e["Value"] for e in resp["Exports"]})
        token = resp.get("NextToken")
        if not token:
            return out


def stack_status(cfn, name: str) -> str | None:
    """A stack's status, e.g. "UPDATE_COMPLETE", or None if it doesn't exist."""
    try:
        return cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
    except cfn.exceptions.ClientError:
        return None


def print_failures(cfn, name: str) -> None:
    """Print the last failed resources of a stack: the first one is usually the real cause."""
    events = cfn.describe_stack_events(StackName=name)["StackEvents"]
    for e in reversed([e for e in events if e["ResourceStatus"].endswith("FAILED")][-8:]):
        print(f"  ✗ {e['LogicalResourceId']} ({e['ResourceType']}): {e.get('ResourceStatusReason', '')}")


def deploy_stack(sess, env: str, stack: str, assume_yes: bool) -> bool:
    """Deploy one stack through a change set (see the module docstring). True on success."""
    cfn, s3 = sess.client("cloudformation"), sess.client("s3")
    name = f"slm-{env}-{stack}"
    template = TEMPLATES / f"{stack}.yaml"
    print(f"\n=== {name}")
    lint(template)

    params = dict(json.loads((TEMPLATES / "params" / f"{env}.json").read_text())[stack])
    code_bucket = exports(cfn).get(f"slm-{env}-CodeBucketName")
    if stack == "app":
        if not code_bucket:
            raise SystemExit("Deploy the data stack first: it creates the code bucket.")
        sys.path.insert(0, str(ROOT / "scripts"))
        import build_lambda
        zip_path = build_lambda.build()
        key = f"code/{zip_path.name}"
        try:
            s3.head_object(Bucket=code_bucket, Key=key)
            print(f"  {key} already uploaded")
        except s3.exceptions.ClientError:
            print(f"  uploading {zip_path.name} ({zip_path.stat().st_size / 1e6:.1f} MB)")
            s3.upload_file(str(zip_path), code_bucket, key)
        params["CodeKey"] = key

    body = template.read_text()
    source: dict = {"TemplateBody": body}
    if len(body.encode()) > INLINE_LIMIT:
        if not code_bucket:
            raise SystemExit(f"{template.name} is too large to send inline and there is no code bucket yet.")
        tkey = f"templates/{stack}-{int(time.time())}.yaml"
        s3.put_object(Bucket=code_bucket, Key=tkey, Body=body.encode())
        source = {"TemplateURL": f"https://{code_bucket}.s3.{REGION}.amazonaws.com/{tkey}"}

    status = stack_status(cfn, name)
    if status == "ROLLBACK_COMPLETE":
        print("  A failed first deployment left a dead stack: deleting it before retrying.")
        cfn.delete_stack(StackName=name)
        cfn.get_waiter("stack_delete_complete").wait(StackName=name)
        status = None
    exists = status not in (None, "REVIEW_IN_PROGRESS")

    cs_name = f"deploy-{int(time.time())}"
    cfn.create_change_set(
        StackName=name, ChangeSetName=cs_name, ChangeSetType="UPDATE" if exists else "CREATE",
        Parameters=[{"ParameterKey": k, "ParameterValue": str(v)} for k, v in params.items()],
        Capabilities=["CAPABILITY_NAMED_IAM"],
        Tags=[{"Key": "app", "Value": "slm"}, {"Key": "env", "Value": env}], **source)
    try:
        cfn.get_waiter("change_set_create_complete").wait(
            StackName=name, ChangeSetName=cs_name, WaiterConfig={"Delay": 5, "MaxAttempts": 120})
    except Exception:
        cs = cfn.describe_change_set(StackName=name, ChangeSetName=cs_name)
        reason = cs.get("StatusReason", "")
        if "didn't contain changes" in reason or "No updates" in reason:
            print("  no changes")
            cfn.delete_change_set(StackName=name, ChangeSetName=cs_name)
            return True
        print(f"  change set failed: {reason}")
        return False

    changes, token = [], None
    while True:
        cs = cfn.describe_change_set(StackName=name, ChangeSetName=cs_name, **({"NextToken": token} if token else {}))
        changes += [c["ResourceChange"] for c in cs["Changes"]]
        token = cs.get("NextToken")
        if not token:
            break
    for c in changes:
        flag = "  <-- REPLACED (deleted and recreated)" if c.get("Replacement") == "True" else ""
        print(f"  {c['Action']:7s} {c['LogicalResourceId']:34s} {c['ResourceType']}{flag}")
    if not assume_yes and input(f"Execute these {len(changes)} changes on {name}? [y/N] ").strip().lower() != "y":
        print("  Not executed; the change set is kept for review in the console.")
        return False

    cfn.execute_change_set(StackName=name, ChangeSetName=cs_name)
    print("  deploying ...")
    try:
        cfn.get_waiter("stack_update_complete" if exists else "stack_create_complete").wait(
            StackName=name, WaiterConfig={"Delay": 15, "MaxAttempts": 160})
    except Exception:
        print(f"  FAILED: {stack_status(cfn, name)}")
        print_failures(cfn, name)
        return False
    print(f"  {stack_status(cfn, name)}")
    return True


def main(argv: list[str] | None = None) -> int:
    """Deploy the requested stacks in dependency order; stop at the first failure."""
    p = argparse.ArgumentParser(description="Deploy SLM Fintech stacks through reviewed change sets.")
    p.add_argument("env", choices=["dev", "prod"])
    p.add_argument("stacks", nargs="+", choices=list(ORDER) + ["all"])
    p.add_argument("--profile", help="AWS CLI profile, e.g. lyndev")
    p.add_argument("--yes", action="store_true", help="Execute change sets without asking (CI)")
    args = p.parse_args(argv)
    stacks = ORDER if "all" in args.stacks else [s for s in ORDER if s in args.stacks]
    sess = session(args.profile)
    for stack in stacks:
        if not deploy_stack(sess, args.env, stack, args.yes):
            return 1
    app = sess.client("cloudformation").describe_stacks(StackName=f"slm-{args.env}-app")["Stacks"][0] \
        if stack_status(sess.client("cloudformation"), f"slm-{args.env}-app") else None
    if app:
        url = {o["OutputKey"]: o["OutputValue"] for o in app.get("Outputs", [])}.get("ApiUrl")
        print(f"\nAPI: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
