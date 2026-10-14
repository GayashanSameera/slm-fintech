"""Onboard a tenant: create its settings, its first API key and (optionally) its webhook secret.

This is an OPERATOR script, run by a person with admin AWS credentials. It is
never part of the public API: tenants cannot create tenants.

    # first tenant in prod, read+write key, no webhook
    uv run python scripts/create_tenant.py --env prod --name LYNEAR --profile lyndev

    # store the key in AWS Secrets Manager instead of printing it
    uv run python scripts/create_tenant.py --env prod --name LYNEAR --profile lyndev --secret-name slm/prod/api-keys/lynear

    # write the key to a private file instead of printing it (automation, smoke tests)
    uv run python scripts/create_tenant.py --env prod --name SMOKE-TEST --profile lyndev --key-file ~/.slm/smoke.key

    # with a webhook and a longer retention
    uv run python scripts/create_tenant.py --env prod --name LYNEAR \
        --webhook-url https://api.lynear.example/slm-events --retention-days 90

What it writes (architecture §7.1):
    TENANTNAME#<name>  / TENANTNAME     -> tenant_id      (makes names unique, see below)
    TENANT#<t>         / PROFILE        -> settings
    APIKEY#<hmac>      / APIKEY         -> tenant_id, scopes   (lookup item for the authorizer)
    TENANT#<t>         / KEY#<key_id>   -> key metadata, no secret
and, with --webhook-url, a Secrets Manager secret  slm/<env>/webhook/<tenant_id>.

Secrets handling:
  * The API key and the webhook secret are printed ONCE, here, and stored nowhere
    in plain form: we keep only HMAC(pepper, key) and the secret in Secrets Manager.
    Hand them to the tenant over a secure channel; if lost, create a new key.
  * Nothing secret is written to a file or a log.

Running it twice with the same name does NOT create a second tenant: the
TENANTNAME# item is written with a condition, and DynamoDB refuses a duplicate.
Only admin credentials can read that item (the authorizer may read only
APIKEY#/TENANT# keys, and tenant sessions only their own TENANT# keys).
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

# Make `import app...` work when run as `python scripts/create_tenant.py`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

AWS_REGION = "us-east-1"
SCOPES_READ_WRITE = ["extractions:read", "extractions:write"]
SCOPES_READ_ONLY = ["extractions:read"]
UNMASKABLE_KINDS = ("pan", "account", "id_number", "phone")   # app/modules/extraction/masking.py FIELD_KINDS


def configure_environment(env: str) -> None:
    """Point the app's settings at the right environment BEFORE importing anything from `app`.

    app/config.py reads SLM_* variables once at import time. For dev/prod we set
    the names that the CloudFormation stacks create (data.yaml, security.yaml).
    For "local" we leave the environment alone (docker compose / .env.local).
    """
    if env == "local":
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _aws import prefer_ipv4

    prefer_ipv4()                    # broken IPv6 on some networks makes AWS calls hang
    os.environ.update({
        "SLM_ENV": env,
        "SLM_REGION": AWS_REGION,
        "SLM_TABLE": f"slm-{env}-main",
        "SLM_PEPPER_SECRET_ID": f"slm/{env}/api-key-pepper",
    })
    # Deliberately NOT setting SLM_TENANT_DATA_ROLE_ARN: this script runs with the
    # operator's own admin credentials, not through the per-tenant role.


def store_key_in_secrets_manager(env: str, secret_name: str, api_key: str, tenant_name: str) -> str:
    """Save an API key as a Secrets Manager secret (created, or updated if it exists). Returns the ARN.

    For client applications in this AWS account that read their key at run time. The key
    is never printed. Encrypted with the platform's `logs` KMS key; any IAM principal in
    the account that is allowed secretsmanager:GetSecretValue on this secret can read it.
    """
    import boto3

    sm = boto3.client("secretsmanager", region_name=AWS_REGION)
    description = f"SLM extraction API key for tenant {tenant_name} ({env}). Use as 'Authorization: Bearer <value>'."
    try:
        arn = sm.create_secret(Name=secret_name, Description=description, KmsKeyId=f"alias/slm-{env}-logs",
                               SecretString=api_key,
                               Tags=[{"Key": "app", "Value": "slm"}, {"Key": "tenant", "Value": tenant_name}])["ARN"]
    except sm.exceptions.ResourceExistsException:
        arn = sm.put_secret_value(SecretId=secret_name, SecretString=api_key)["ARN"]
    return arn


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Command-line options, with the cross-checks argparse can't express (https webhooks,
    consent needs a name).
    """
    p = argparse.ArgumentParser(description="Create a tenant and its first API key.")
    p.add_argument("--env", required=True, choices=["local", "dev", "prod"])
    p.add_argument("--name", required=True, help="Tenant display name, e.g. LYNEAR. Must be unique.")
    p.add_argument("--profile", help="AWS CLI profile of the target account, e.g. lyndev")
    p.add_argument("--key-file", help="Write the API key to this file (mode 600) instead of printing it.")
    p.add_argument("--secret-name", help="Store the API key in AWS Secrets Manager under this name instead of "
                                         "printing it, e.g. slm/prod/api-keys/lynear")
    p.add_argument("--read-only", action="store_true", help="Key may only read results (no uploads).")
    p.add_argument("--retention-days", type=int, default=30, choices=[7, 30, 90, 365])
    p.add_argument("--verification-mode", default="ai", choices=["ai", "checks_only"],
                   help="checks_only = bank statements read by Rung 0 never go to any AI service (FR-9.4).")
    p.add_argument("--unmasked", nargs="*", default=[], choices=UNMASKABLE_KINDS,
                   help="Identifier kinds this tenant may see unmasked (FR-4.4). Aadhaar is ALWAYS masked.")
    p.add_argument("--webhook-url", help="https:// URL that receives job-finished events (FR-2.10).")
    p.add_argument("--training-consent", action="store_true",
                   help="Tenant has agreed IN WRITING to documents being kept to improve accuracy (NFR-C2).")
    p.add_argument("--consent-by", help="Who gave the consent (name / reference). Required with --training-consent.")
    args = p.parse_args(argv)
    if args.webhook_url and not args.webhook_url.startswith("https://"):
        p.error("--webhook-url must start with https://")
    if args.training_consent and not args.consent_by:
        p.error("--training-consent needs --consent-by (who agreed, for the audit trail)")
    return args


def claim_name(table, name: str, tenant_id: str) -> None:
    """Reserve the tenant name. Raises SystemExit if another tenant already has it."""
    from botocore.exceptions import ClientError

    try:
        table.put_item(
            Item={"PK": f"TENANTNAME#{name.strip().lower()}", "SK": "TENANTNAME", "tenant_id": tenant_id},
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            existing = table.get_item(Key={"PK": f"TENANTNAME#{name.strip().lower()}", "SK": "TENANTNAME"})
            raise SystemExit(f"A tenant named {name!r} already exists: {existing['Item']['tenant_id']}. "
                             "Nothing was created.") from None
        raise


def create_webhook_secret(env: str, tenant_id: str) -> tuple[str, str]:
    """Return (secret_id to store in the tenant settings, secret value to give the tenant)."""
    value = secrets.token_urlsafe(32)
    if env == "local":
        # Locally there is no Secrets Manager: the "id" IS the secret (app/core/secrets.py).
        return value, value
    import boto3

    sm = boto3.client("secretsmanager", region_name=AWS_REGION)
    resp = sm.create_secret(
        Name=f"slm/{env}/webhook/{tenant_id}",      # the webhook Lambda may read only slm/<env>/webhook/*
        Description="Webhook signing secret (HMAC-SHA256) for one tenant",
        KmsKeyId=f"alias/slm-{env}-logs",
        SecretString=value,
    )
    return resp["ARN"], value


def main(argv: list[str] | None = None) -> dict:
    """Onboard one tenant: claim the name, optional webhook secret, settings, first API key.
    Returns the ids and secrets (used by tests); prints a summary for the operator.
    """
    args = parse_args(argv)
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile
    configure_environment(args.env)

    # Imported only now, so the settings above are in effect.
    from app.core.clock import now_iso
    from app.core.ids import new_tenant_id
    from app.storage.aws import dynamodb_table
    from app.storage.tenants import create_api_key, create_tenant

    tenant_id = new_tenant_id()
    table = dynamodb_table()
    claim_name(table, args.name, tenant_id)       # 1. fail early on a duplicate name

    overrides: dict = {
        "retention_days": args.retention_days,
        "verification_mode": args.verification_mode,
        "unmasked_kinds": sorted(set(args.unmasked)),
        "training_consent": ({"given": True, "at": now_iso(), "by": args.consent_by}
                             if args.training_consent else {"given": False, "at": None, "by": None}),
    }
    webhook_secret_value = None
    if args.webhook_url:                          # 2. webhook secret (optional)
        secret_id, webhook_secret_value = create_webhook_secret(args.env, tenant_id)
        overrides.update(webhook_url=args.webhook_url, webhook_secret_id=secret_id)

    create_tenant(tenant_id, args.name, **overrides)          # 3. settings
    scopes = SCOPES_READ_ONLY if args.read_only else SCOPES_READ_WRITE
    api_key = create_api_key(tenant_id, scopes, live=args.env == "prod")   # 4. first key

    print(f"Tenant created: {args.name}")
    print(f"  tenant_id         {tenant_id}")
    print(f"  environment       {args.env}")
    print(f"  scopes            {' '.join(scopes)}")
    print(f"  retention         {args.retention_days} days")
    print(f"  verification      {args.verification_mode}")
    print(f"  unmasked kinds    {', '.join(overrides['unmasked_kinds']) or '(none: everything masked)'}")
    print(f"  training consent  {'yes, by ' + args.consent_by if args.training_consent else 'no'}")
    print()
    if args.secret_name:
        if args.env == "local":
            raise SystemExit("--secret-name needs an AWS environment (dev or prod).")
        arn = store_key_in_secrets_manager(args.env, args.secret_name, api_key, args.name)
        print(f"API key stored in Secrets Manager (not printed): {arn}")
    elif args.key_file:
        key_path = Path(args.key_file).expanduser()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(api_key + "\n")
        key_path.chmod(0o600)                       # readable by you only
        print(f"API key written to {key_path} (not printed).")
    else:
        print("API key (shown ONCE, store it in the tenant's secret manager now):")
        print(f"  {api_key}")
    if webhook_secret_value:
        print()
        print("Webhook signing secret (shown ONCE, the tenant uses it to verify X-Signature):")
        print(f"  {webhook_secret_value}")
    return {"tenant_id": tenant_id, "api_key": api_key, "webhook_secret": webhook_secret_value}


if __name__ == "__main__":
    main()
