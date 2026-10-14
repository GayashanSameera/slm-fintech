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

import re

from pydantic import BaseModel, ConfigDict, model_validator

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

    @model_validator(mode="after")
    def _drop_balance_lines(self):
        """"Opening Balance" / "B/F" / "Closing Balance" lines are not transactions, but models
        often list them as rows anyway (seen with Claude Haiku on 2026-10-14, despite the prompt).
        Such a row moves no money (no debit, no credit): it is removed, and its balance fills
        the opening or closing balance if that is missing. Deterministic, so every reader
        (parser or model) gives the same rows."""
        kept = []
        for t in self.transactions:
            if t.debit is None and t.credit is None and _BALANCE_LINE.match((t.description or "").strip()):
                if _OPENING.match(t.description.strip()) and self.opening_balance is None and not kept:
                    self.opening_balance = t.balance
                elif not _OPENING.match(t.description.strip()) and self.closing_balance is None:
                    self.closing_balance = t.balance
                continue
            kept.append(t)
        self.transactions = kept
        return self


# Balance lines (same words as parsers/lexicon.py OPENING_LINE / CLOSING_LINE; not imported from
# there, because the parsers package itself imports this schema).
_OPENING = re.compile(r"(?i)^(b\s*/\s*f\b|balance\s+b/?f|brought\s+forward|opening\s+balance|balance\s+forward)")
_BALANCE_LINE = re.compile(r"(?i)^(b\s*/\s*f\b|balance\s+b/?f|brought\s+forward|opening\s+balance|balance\s+forward|"
                           r"c\s*/\s*f\b|carried\s+forward|closing\s+balance)\b")
