"""Bank account statement (requirements FR-3.2)."""

from decimal import Decimal

from pydantic import BaseModel, Field

from app.modules.extraction.schemas.base import DocumentSchema, differs, fmt, total
from app.modules.extraction.schemas.types import DocDate, Money, Text, text


class Transaction(BaseModel):
    date: DocDate = Field(default=None, description="Transaction date")
    description: Text = text("Narration as printed")
    reference: Text = text("Cheque or reference number")
    debit: Money = Field(default=None, description="Amount withdrawn, positive, null if none")
    credit: Money = Field(default=None, description="Amount deposited, positive, null if none")
    balance: Money = Field(default=None, description="Balance after the transaction")


class BankStatement(DocumentSchema):
    bank_name: Text = text()
    branch: Text = text()
    account_holder_name: Text = text()
    account_number: Text = text()
    ifsc: Text = text("11-character IFSC")
    account_type: Text = text("e.g. Savings, Current")
    currency: Text = text("ISO code, e.g. INR")
    period_start: DocDate = None
    period_end: DocDate = None
    opening_balance: Money = None
    closing_balance: Money = None
    total_credits: Money = None
    total_debits: Money = None
    transactions: list[Transaction] = Field(default_factory=list)

    def consistency_warnings(self) -> list[str]:
        warnings = []
        tx = self.transactions
        # 1. Opening + all credits − all debits should equal the closing balance.
        if tx and self.opening_balance is not None and self.closing_balance is not None:
            expected = self.opening_balance + total(t.credit for t in tx) - total(t.debit for t in tx)
            if differs(expected, self.closing_balance):
                warnings.append(
                    f"Opening + credits − debits is {fmt(expected)}, "
                    f"but closing balance is {fmt(self.closing_balance)}."
                )
        # 2. Row by row: previous balance + credit − debit should equal this row's balance.
        #    The number of failing rows points at misread amounts.
        previous: Decimal | None = self.opening_balance
        bad_rows = 0
        for t in tx:
            if previous is not None and t.balance is not None:
                expected = previous + (t.credit or 0) - (t.debit or 0)
                if differs(expected, t.balance):
                    bad_rows += 1
            previous = t.balance
        if bad_rows:
            warnings.append(f"Running balance does not match on {bad_rows} of {len(tx)} transactions.")
        return warnings
