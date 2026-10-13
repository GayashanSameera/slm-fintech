"""Shared test fixtures.

Tests run without Docker and without Ollama:
    - storage is a temporary folder (pytest's tmp_path), deleted after;
    - the AI model is the FakeProvider, which returns canned answers instantly.

Fixtures (pytest creates them on demand when a test names them as an argument):
    settings   Settings for tests
    model      the FakeProvider; tests can change model.answers or set model.error
    deps       Deps wired to the temporary folder and the fake model
    client     an HTTP client for the app (FastAPI TestClient, no real server needed)
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.deps import Deps
from app.gateway.fake import FakeProvider
from app.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"

# What a model might answer for tests/fixtures/payslip.*. Values come in mixed formats on
# purpose (a dd/mm/yyyy date, an amount with a comma) to exercise normalisation. net_pay is
# deliberately wrong (48,250 instead of 48,200) so the consistency check has something to catch.
PAYSLIP_ANSWER = {
    "employer_name": "ACME FINANCE PRIVATE LIMITED",
    "employee_name": "Test Person",
    "employee_id": "E-1042",
    "pan": "ABCDE1234F",
    "uan": "100123456789",
    "bank_account_number": "50100123456789",
    "pay_period_start": "01/09/2026",
    "pay_period_end": "2026-09-30",
    "earnings": [{"name": "Basic Salary", "amount": 40000}, {"name": "House Rent", "amount": "10,000.00"}],
    "deductions": [{"name": "Provident Fund", "amount": 1800}],
    "gross_earnings": 50000,
    "total_deductions": 1800,
    "net_pay": 48250,
}


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch):
    # A developer's .env or shell variables must not change test behaviour.
    for key in list(os.environ):
        if key.startswith("SLM_"):
            monkeypatch.delenv(key)


@pytest.fixture
def settings(tmp_path) -> Settings:
    # _env_file=None: ignore any .env file on disk.
    return Settings(_env_file=None, env="test", data_dir=str(tmp_path / "data"))


@pytest.fixture
def model() -> FakeProvider:
    return FakeProvider(answers={"employer_name": PAYSLIP_ANSWER})


@pytest.fixture
def deps(settings, model) -> Deps:
    return Deps(settings, provider=model)


@pytest.fixture
def client(deps) -> TestClient:
    return TestClient(create_app(deps))


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()
