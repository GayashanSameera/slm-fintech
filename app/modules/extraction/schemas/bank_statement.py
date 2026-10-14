"""Bank statement schema (requirement FR-3.2).

The most common document type on the platform. Digital statements from known
banks are produced by Rung 0 parsers (parsers/), everything else by AI.

Consistency checks (checks.py):
  * opening balance + total credits - total debits = closing balance
  * every row's running balance = previous balance - debit + credit
These checks are strong: if they pass on every row, the NUMBERS are almost
certainly right. Names, account number, dates and descriptions are not proven
by arithmetic; that is what the AI verifier checks for Rung 0 results.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .base import DocBase, IsoDate, Money, Text

SCHEMA_VERSION = "1.0"


class Transaction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    date: IsoDate = None
    description: Text = None
    reference: Text = None      # cheque number / UTR / reference id
    debit: Money = None         # money going out (withdrawal)
    credit: Money = None        # money coming in (deposit)
    balance: Money = None       # running balance after this row


class BankStatement(DocBase):
    bank_name: Text = None
    account_holder: Text = None
    account_number: Text = None   # masked to last 4 by default (FR-4.3)
    ifsc: Text = None             # 11 chars: 4 letters, '0', 6 alphanumerics
    branch: Text = None
    period_from: IsoDate = None
    period_to: IsoDate = None
    opening_balance: Money = None
    closing_balance: Money = None
    transactions: list[Transaction] = []
