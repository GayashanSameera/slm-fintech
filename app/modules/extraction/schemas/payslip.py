"""Monthly salary slip (requirements FR-3.3)."""

import re

from pydantic import BaseModel, Field

from app.modules.extraction.schemas.base import DocumentSchema, differs, fmt, total
from app.modules.extraction.schemas.invoice import PAN_PATTERN
from app.modules.extraction.schemas.types import DocDate, Money, Number, Text, text


class PayComponent(BaseModel):
    name: Text = text("Component name as printed, e.g. Basic Salary")
    amount: Money = None


class Payslip(DocumentSchema):
    employer_name: Text = text()
    employer_address: Text = text()
    employee_name: Text = text()
    employee_id: Text = text()
    designation: Text = text()
    pan: Text = text("10-character PAN")
    uan: Text = text("12-digit UAN")
    bank_name: Text = text()
    bank_account_number: Text = text()
    pay_period_start: DocDate = None
    pay_period_end: DocDate = None
    pay_date: DocDate = None
    days_paid: Number = None
    earnings: list[PayComponent] = Field(default_factory=list)
    deductions: list[PayComponent] = Field(default_factory=list)
    gross_earnings: Money = None
    total_deductions: Money = None
    net_pay: Money = None

    def consistency_warnings(self) -> list[str]:
        warnings = []
        earned = total(c.amount for c in self.earnings)
        deducted = total(c.amount for c in self.deductions)
        if self.earnings and self.gross_earnings is not None and differs(earned, self.gross_earnings):
            warnings.append(f"Earnings add up to {fmt(earned)}, but gross is {fmt(self.gross_earnings)}.")
        if self.deductions and self.total_deductions is not None and differs(deducted, self.total_deductions):
            warnings.append(
                f"Deductions add up to {fmt(deducted)}, but total deductions is {fmt(self.total_deductions)}."
            )
        if None not in (self.gross_earnings, self.total_deductions, self.net_pay):
            if differs(self.gross_earnings - self.total_deductions, self.net_pay):
                warnings.append(
                    f"Gross − deductions is {fmt(self.gross_earnings - self.total_deductions)}, "
                    f"but net pay is {fmt(self.net_pay)}."
                )
        if self.pan and not re.fullmatch(PAN_PATTERN, self.pan):
            warnings.append("pan does not look like a valid PAN.")
        return warnings
