"""Consistency checks (requirement FR-3).

These are the platform's strongest quality signal: arithmetic doesn't lie.
They run on EVERY result, whether it came from a Rung 0 parser or from AI.

* A failed check on AI output moves the document one rung up the model ladder.
* If checks still fail at the top, the result is kept with WARNINGS (FR-3.9).
* For Rung 0 parsers, any failed check rejects the parser result.

Always use Decimal, never float: in floating point 0.1 + 0.2 != 0.3, which would
create false warnings on perfectly good documents.

`CheckFailure.message` may contain numbers from the document. It is sent ONLY to
the model as feedback and is NEVER logged or stored. Only `code` is stored as a
warning on the job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .schemas.base import to_decimal

TOLERANCE = Decimal("0.01")   # one paisa: allow for rounding on the printed document

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")          # e.g. ABCDE1234F
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")          # e.g. HDFC0001234
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z0-9]{10}[0-9A-Z]Z[0-9A-Z]$")


@dataclass
class CheckFailure:
    code: str        # stable, stored as a warning, e.g. "line_items_sum_mismatch"
    message: str     # human description for the model's retry prompt (never logged)


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= TOLERANCE


def check_invoice(inv) -> list[CheckFailure]:
    out: list[CheckFailure] = []
    amounts = [to_decimal(li.amount) for li in inv.line_items if li.amount is not None]
    subtotal = to_decimal(inv.subtotal)
    total = to_decimal(inv.total)
    if amounts and subtotal is not None and not _close(sum(amounts), subtotal):
        out.append(CheckFailure("line_items_sum_mismatch",
                                f"Line item amounts add up to {sum(amounts)} but the subtotal is {subtotal}."))
    if subtotal is not None and total is not None:
        tax = sum((to_decimal(x) or Decimal(0)) for x in (inv.cgst, inv.sgst, inv.igst))
        if not _close(subtotal + tax, total):
            out.append(CheckFailure("total_mismatch",
                                    f"Subtotal {subtotal} plus taxes {tax} is {subtotal + tax}, but the total is {total}."))
    for party_name, party in (("seller", inv.seller), ("buyer", inv.buyer)):
        if party and party.gstin and not GSTIN_RE.match(party.gstin.replace(" ", "").upper()):
            out.append(CheckFailure("gstin_format", f"The {party_name} GSTIN does not have a valid format."))
        if party and party.pan and not PAN_RE.match(party.pan.replace(" ", "").upper()):
            out.append(CheckFailure("pan_format", f"The {party_name} PAN does not have a valid format."))
    return out


def check_bank_statement(st) -> list[CheckFailure]:
    out: list[CheckFailure] = []
    opening = to_decimal(st.opening_balance)
    closing = to_decimal(st.closing_balance)
    txns = st.transactions

    # 1. Running balance on every row: previous balance - debit + credit = this balance.
    previous = opening
    for index, t in enumerate(txns, start=1):
        bal = to_decimal(t.balance)
        if previous is not None and bal is not None:
            expected = previous - (to_decimal(t.debit) or 0) + (to_decimal(t.credit) or 0)
            if not _close(expected, bal):
                out.append(CheckFailure("running_balance_mismatch",
                                        f"Row {index}: expected balance {expected} but found {bal}."))
                break   # one report is enough; later rows would all cascade
        previous = bal if bal is not None else previous

    # 2. Opening + credits - debits = closing.
    if opening is not None and closing is not None and txns:
        credits = sum((to_decimal(t.credit) or Decimal(0)) for t in txns)
        debits = sum((to_decimal(t.debit) or Decimal(0)) for t in txns)
        if not _close(opening + credits - debits, closing):
            out.append(CheckFailure("closing_balance_mismatch",
                                    f"Opening {opening} + credits {credits} - debits {debits} "
                                    f"= {opening + credits - debits}, but closing is {closing}."))

    if st.ifsc and not IFSC_RE.match(st.ifsc.replace(" ", "").upper()):
        out.append(CheckFailure("ifsc_format", "The IFSC code does not have a valid format."))
    return out


def check_payslip(p) -> list[CheckFailure]:
    out: list[CheckFailure] = []
    gross = to_decimal(p.gross_pay)
    net = to_decimal(p.net_pay)
    earnings = [to_decimal(e.amount) for e in p.earnings if e.amount is not None]
    deductions = [to_decimal(d.amount) for d in p.deductions if d.amount is not None]
    if earnings and gross is not None and not _close(sum(earnings), gross):
        out.append(CheckFailure("earnings_sum_mismatch", f"Earnings add up to {sum(earnings)} but gross is {gross}."))
    total_ded = to_decimal(p.total_deductions) if p.total_deductions else (sum(deductions) if deductions else None)
    if gross is not None and net is not None and total_ded is not None and not _close(gross - total_ded, net):
        out.append(CheckFailure("net_pay_mismatch", f"Gross {gross} - deductions {total_ded} != net {net}."))
    if p.pan and not PAN_RE.match(p.pan.replace(" ", "").upper()):
        out.append(CheckFailure("pan_format", "The PAN does not have a valid format."))
    return out


def check_identity(doc) -> list[CheckFailure]:
    out: list[CheckFailure] = []
    if doc.id_type == "pan" and doc.id_number and not PAN_RE.match(doc.id_number.replace(" ", "").upper()):
        out.append(CheckFailure("pan_format", "The PAN number does not have a valid format."))
    if doc.expiry_date and doc.expiry_date < date.today():
        # Not an extraction error: the document itself is expired. Reported as a warning.
        out.append(CheckFailure("document_expired", "The document has expired."))
    return out


def check_kyc(form) -> list[CheckFailure]:
    out: list[CheckFailure] = []
    if form.pan and not PAN_RE.match(form.pan.replace(" ", "").upper()):
        out.append(CheckFailure("pan_format", "The PAN does not have a valid format."))
    missing = [f for f in ("name", "date_of_birth", "pan") if getattr(form, f) in (None, "")]
    if missing:
        out.append(CheckFailure("required_fields_missing", "Required fields missing: " + ", ".join(missing)))
    return out


_CHECKS = {
    "invoice": check_invoice,
    "bank_statement": check_bank_statement,
    "payslip": check_payslip,
    "identity_document": check_identity,
    "kyc_form": check_kyc,
}

# Failures that say something about the DOCUMENT, not about the extraction.
# Re-extracting with a stronger model would not change them, so they never
# trigger a climb; they are only reported as warnings.
DOCUMENT_ONLY = {"document_expired", "required_fields_missing"}


def run_checks(doc_type: str, parsed) -> list[CheckFailure]:
    return _CHECKS[doc_type](parsed)
