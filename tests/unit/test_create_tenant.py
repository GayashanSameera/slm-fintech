"""Tests for the onboarding script scripts/create_tenant.py, using in-memory AWS (moto).

Covers what matters when onboarding the first tenant, LYNEAR:
  * the printed API key really authenticates (the same lookup the authorizer does),
  * settings are stored as requested,
  * running the script twice with the same name creates nothing new.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "create_tenant.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("create_tenant", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_onboard_lynear_key_authenticates(aws, capsys):
    from app.auth.principal import resolve_api_key
    from app.storage.tenants import get_tenant_settings

    out = _load_script().main(["--env", "local", "--name", "LYNEAR"])

    principal = resolve_api_key(out["api_key"])          # what the authorizer runs on every request
    assert principal.tenant_id == out["tenant_id"]
    assert principal.scopes == {"extractions:read", "extractions:write"}

    tenant = get_tenant_settings(out["tenant_id"])
    assert tenant.name == "LYNEAR" and tenant.status == "active"
    assert tenant.retention_days == 30 and tenant.unmasked_kinds == []
    assert not tenant.consented_to_training               # consent is never assumed (NFR-C2)

    printed = capsys.readouterr().out
    assert out["api_key"] in printed                      # shown once, to the operator
    assert out["api_key"].startswith("slm_test_")         # non-prod keys are visibly test keys


def test_same_name_twice_is_refused(aws):
    script = _load_script()
    first = script.main(["--env", "local", "--name", "LYNEAR"])
    with pytest.raises(SystemExit) as exc:
        script.main(["--env", "local", "--name", "lynear"])   # names are case-insensitive
    assert first["tenant_id"] in str(exc.value)


def test_settings_and_webhook(aws):
    from app.storage.tenants import get_tenant_settings

    out = _load_script().main([
        "--env", "local", "--name", "LYNEAR", "--retention-days", "90", "--verification-mode", "checks_only",
        "--unmasked", "pan", "--webhook-url", "https://hooks.example.com/slm",
    ])
    tenant = get_tenant_settings(out["tenant_id"])
    assert tenant.retention_days == 90 and tenant.verification_mode == "checks_only"
    assert tenant.unmasked_kinds == ["pan"]
    assert tenant.webhook_url == "https://hooks.example.com/slm"
    assert out["webhook_secret"]


@pytest.mark.parametrize("bad_args", [
    ["--webhook-url", "http://plain-http.example.com"],     # webhooks must be https
    ["--training-consent"],                                  # consent needs a named person
    ["--retention-days", "45"],                              # only 7/30/90/365 exist
])
def test_invalid_arguments_rejected(aws, bad_args):
    with pytest.raises(SystemExit):
        _load_script().main(["--env", "local", "--name", "LYNEAR", *bad_args])
