"""Operator tool: list, create and revoke a tenant's API keys (key rotation).

    uv run python scripts/manage_keys.py list   --env prod --tenant LYNEAR --profile lyndev
    uv run python scripts/manage_keys.py create --env prod --tenant LYNEAR --profile lyndev [--read-only] [--key-file F]
    uv run python scripts/manage_keys.py revoke --env prod --tenant LYNEAR --key-id key_xxxxxxxx --profile lyndev

Rotation: `create` a second key, the client switches to it, then `revoke` the old one.
A revoked key stops working within 60 seconds (API Gateway caches the authorizer's answer).
A new key is printed ONCE (or written to --key-file, mode 600) and stored only as a hash.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="List, create and revoke a tenant's API keys.")
    p.add_argument("command", choices=["list", "create", "revoke"])
    p.add_argument("--env", required=True, choices=["local", "dev", "prod"])
    p.add_argument("--tenant", required=True, help="Tenant name (e.g. LYNEAR) or id (t_...)")
    p.add_argument("--profile")
    p.add_argument("--key-id", help="For revoke: the key id shown by `list`")
    p.add_argument("--read-only", action="store_true", help="For create: extractions:read only")
    p.add_argument("--key-file", help="For create: write the key to this file instead of printing it")
    args = p.parse_args(argv)
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile

    # Same environment setup as tenant onboarding (resource names, IPv4), BEFORE importing app.
    spec = importlib.util.spec_from_file_location("create_tenant", HERE / "create_tenant.py")
    onboarding = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(onboarding)
    onboarding.configure_environment(args.env)
    from app.storage.aws import dynamodb_table
    from app.storage.tenants import create_api_key, list_api_keys, revoke_api_key

    tenant_id = args.tenant
    if not tenant_id.startswith("t_"):
        item = dynamodb_table().get_item(Key={"PK": f"TENANTNAME#{args.tenant.strip().lower()}", "SK": "TENANTNAME"}).get("Item")
        if not item:
            raise SystemExit(f"No tenant named {args.tenant!r}.")
        tenant_id = item["tenant_id"]

    if args.command == "list":
        for k in sorted(list_api_keys(tenant_id), key=lambda k: k.get("created_at", "")):
            print(f"  {k['key_id']}  {k.get('status', 'active'):8s} ...{k.get('hint', '????')}  "
                  f"{' '.join(k.get('scopes', []))}  created {k.get('created_at')}"
                  + (f"  revoked {k['revoked_at']}" if k.get("revoked_at") else ""))
    elif args.command == "create":
        scopes = ["extractions:read"] if args.read_only else ["extractions:read", "extractions:write"]
        key = create_api_key(tenant_id, scopes, live=args.env == "prod")
        if args.key_file:
            path = Path(args.key_file).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(key + "\n")
            path.chmod(0o600)
            print(f"New key written to {path} (not printed).")
        else:
            print(f"New API key (shown ONCE):\n  {key}")
    else:
        if not args.key_id:
            raise SystemExit("revoke needs --key-id (see `list`).")
        print("Revoked." if revoke_api_key(tenant_id, args.key_id) else "Key not found (or created before revocation support).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
