"""Shared helpers for the LOCAL operator scripts (deploy, tenant onboarding, smoke test).

Not part of the application: the Lambda functions never import this.
"""

from __future__ import annotations

import os
import socket

REGION = "us-east-1"          # architecture §3.4


def prefer_ipv4() -> None:
    """Connect to AWS over IPv4 from this machine.

    Some AWS endpoints (e.g. Secrets Manager) also publish IPv6 addresses. On a network
    where IPv6 is broken, Python's HTTP library tries IPv6 first and hangs until a long
    timeout instead of falling back quickly (seen on the operator's machine, 2026-10-14).
    """
    import urllib3.util.connection as urllib3_connection

    urllib3_connection.allowed_gai_family = lambda: socket.AF_INET


def session(profile: str | None):
    """A boto3 session for `profile` in us-east-1; prints which account it acts on."""
    import boto3

    prefer_ipv4()
    if profile:
        os.environ["AWS_PROFILE"] = profile          # also used by the app code the scripts import
    os.environ["AWS_REGION"] = os.environ["AWS_DEFAULT_REGION"] = REGION
    sess = boto3.Session(profile_name=profile, region_name=REGION)
    ident = sess.client("sts").get_caller_identity()
    print(f"AWS account {ident['Account']} as {ident['Arn'].split('/')[-1]}, region {REGION}")
    return sess
