"""Payslip schema (requirement FR-3.3).

Checks: earnings add up to gross; gross - deductions = net.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .base import DocBase, IsoDate, Money, Text

SCHEMA_VERSION = "1.0"


class PayComponent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: Text = None       # e.g. "Basic", "HRA", "Provident Fund"
    amount: Money = None


class Payslip(DocBase):
    employer_name: Text = None
    employee_name: Text = None
    employee_id: Text = None
    pan: Text = None
    uan: Text = None                    # Universal Account Number (provident fund)
    bank_account_number: Text = None
    pay_period: Text = None             # e.g. "September 2026"
    pay_date: IsoDate = None
    earnings: list[PayComponent] = []
    deductions: list[PayComponent] = []
    gross_pay: Money = None
    total_deductions: Money = None
    net_pay: Money = None
