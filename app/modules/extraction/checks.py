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

from .identifiers import aadhaar_valid, gstin_problem, pan_holder_valid, pan_matches_name
from .schemas.base import to_decimal

TOLERANCE = Decimal("0.01")   # one paisa: allow for rounding on the printed document

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")          # e.g. ABCDE1234F
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")          # e.g. HDFC0001234
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z0-9]{10}[0-9A-Z]Z[0-9A-Z]$")
PINCODE_RE = re.compile(r"^[1-9][0-9]{5}$")                 # Indian PIN code: 6 digits, never starts with 0
MOBILE_RE = re.compile(r"^[6-9][0-9]{9}$")                  # Indian mobile: 10 digits starting 6-9
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class CheckFailure:
    code: str        # stable, stored as a warning, e.g. "line_items_sum_mismatch"
    message: str     # human description for the model's retry prompt (never logged)
    # Field PATHS the failure is about ("seller.gstin"). Names only, never values, so they are safe
    # to store: each one becomes its own warning "<code>:<path>" so a client can see WHICH field.
    fields: tuple[str, ...] = ()
    # Bank statements: the 1-based ROW numbers the failure is about, so that a long statement read in
    # chunks can re-read just the pages those rows are on (pipeline.run_chunked_statement).
    rows: tuple[int, ...] = ()


def _close(a: Decimal, b: Decimal) -> bool:
    """True when two amounts differ by at most one paisa (TOLERANCE), to allow for printed rounding."""
    return abs(a - b) <= TOLERANCE


# --- identifier and plausibility rules shared by several document types ----------------------
# These catch a misread character on SCANNED pages, where no text layer exists to compare with.
# Each failure is a FIXED code plus a message that names the field, never the value.

def _pan_problems(pan: str | None, label: str) -> list[CheckFailure]:
    """PAN shape (already checked by PAN_RE elsewhere) plus its 4th letter (holder type)."""
    if pan and PAN_RE.match(pan.replace(" ", "").upper()) and not pan_holder_valid(pan):
        return [CheckFailure("pan_holder", f"The {label} PAN has an impossible 4th letter. Re-read the PAN.")]
    return []


def _dob_problems(dob: date | None) -> list[CheckFailure]:
    """A date of birth in the future, or more than 120 years ago, was misread."""
    if dob is not None and (dob > date.today() or (date.today() - dob).days > 120 * 366):
        return [CheckFailure("dob_implausible", "The date of birth is not plausible. Re-read the year.")]
    return []


def _not_future(value: date | None, code: str, label: str) -> list[CheckFailure]:
    """A document date after today (one day of slack for time zones) is almost always a misread year."""
    if value is not None and (value - date.today()).days > 1:
        return [CheckFailure(code, f"The {label} is in the future. Re-read the date, especially the year.")]
    return []


def _address_problems(addr, label: str) -> list[CheckFailure]:
    """An Indian PIN code has 6 digits and never starts with 0."""
    pin = getattr(addr, "pincode", None) if addr else None
    if pin and not PINCODE_RE.match(re.sub(r"\s", "", pin)):
        return [CheckFailure("pincode_format", f"The {label} PIN code is not a valid 6-digit PIN code.")]
    return []


def _aadhaar_problems(value: str | None) -> list[CheckFailure]:
    """A printed 12-digit number must pass the Verhoeff check; otherwise a digit was misread.
    Values that are not 12 digits (already masked, or not an Aadhaar) are left alone."""
    digits = re.sub(r"[\s-]", "", value or "")
    if len(digits) == 12 and digits.isdigit() and not aadhaar_valid(digits):
        return [CheckFailure("aadhaar_checksum", "The Aadhaar number fails its check digit. Re-read every digit.")]
    return []


def check_invoice(inv) -> list[CheckFailure]:
    """Invoice: line items add up to the subtotal; subtotal + CGST + SGST + IGST = total;
    GSTIN and PAN formats of seller and buyer.
    """
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
        if party:
            out += _pan_problems(party.pan, party_name)
            # GSTIN: state code, check character, and the PAN inside it (characters 3-12).
            if party.gstin and GSTIN_RE.match(party.gstin.replace(" ", "").upper()):
                why = gstin_problem(party.gstin, party.pan)
                if why:
                    out.append(CheckFailure(f"gstin_{why}", f"The {party_name} GSTIN is not consistent ({why}). "
                                                            "Re-read every character of the GSTIN and the PAN."))
    # Dates: not in the future, and the due date is not before the invoice date.
    out += _not_future(inv.invoice_date, "date_in_future", "invoice date")
    if inv.invoice_date and inv.due_date and inv.due_date < inv.invoice_date:
        out.append(CheckFailure("due_before_invoice_date", "The due date is before the invoice date. Re-read both dates."))
    # quantity x rate = amount on each line. Discounts make this legitimately differ, so it is only
    # REPORTED (DOCUMENT_ONLY below), never a reason to re-extract.
    for index, li in enumerate(inv.line_items, start=1):
        qty, rate, amount = to_decimal(li.quantity), to_decimal(li.rate), to_decimal(li.amount)
        if None not in (qty, rate, amount) and abs(qty * rate - amount) > max(TOLERANCE, abs(amount) * Decimal("0.005")):
            out.append(CheckFailure("line_item_amount_mismatch",
                                    f"Line {index}: quantity x rate is not the amount (a discount column is possible)."))
            break
    return out


def check_bank_statement(st) -> list[CheckFailure]:
    """Bank statement: every row's running balance, opening + credits - debits = closing,
    and the IFSC format. A wrong row is reported once (later rows would only cascade).
    """
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
                                        f"Row {index}: expected balance {expected} but found {bal}.",
                                        rows=tuple(r for r in (index - 1, index) if r >= 1)))
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

    # 2b. The printed closing balance is the last row's balance, and the opening balance is what the first
    #     row started from. Cheap, and it catches a misread opening/closing line that the chain cannot see.
    if txns and closing is not None and to_decimal(txns[-1].balance) is not None and not _close(to_decimal(txns[-1].balance), closing):
        out.append(CheckFailure("closing_not_last_balance",
                                "The closing balance is not the balance of the last row. Re-read the closing balance line."))
    if txns and opening is not None and to_decimal(txns[0].balance) is not None:
        first = txns[0]
        if not _close(opening - (to_decimal(first.debit) or 0) + (to_decimal(first.credit) or 0), to_decimal(first.balance)):
            out.append(CheckFailure("opening_not_first_row",
                                    "The opening balance does not lead to the first row's balance. Re-read the opening balance line."))

    if st.ifsc and not IFSC_RE.match(st.ifsc.replace(" ", "").upper()):
        out.append(CheckFailure("ifsc_format", "The IFSC code does not have a valid format."))

    # 3. Dates. A statement lists its rows in date order (oldest first, or newest first: some banks
    #    do that), and every row falls inside the statement period. A misread digit in a date almost
    #    always breaks one of the two. The balance chain above proves the AMOUNTS; this proves the DATES.
    dated = [(i, t.date) for i, t in enumerate(txns, start=1) if t.date is not None]
    if len(dated) > 1:
        values = [d for _, d in dated]
        ascending = all(a <= b for a, b in zip(values, values[1:], strict=False))
        descending = all(a >= b for a, b in zip(values, values[1:], strict=False))
        if not (ascending or descending):
            # Name the first row that breaks the dominant direction so the retry can re-read it.
            rising = sum(a < b for a, b in zip(values, values[1:], strict=False))
            falling = sum(a > b for a, b in zip(values, values[1:], strict=False))
            wrong = [dated[k + 1][0] for k, (a, b) in enumerate(zip(values, values[1:], strict=False))
                     if (a > b if rising >= falling else a < b)]
            out.append(CheckFailure("dates_out_of_order",
                                    f"Rows {', '.join(map(str, wrong[:10]))} have a date out of order. Re-read those dates.",
                                    rows=tuple(wrong)))
    if st.period_from and st.period_to:
        outside = [i for i, d in dated if d < st.period_from or d > st.period_to]
        if outside:
            out.append(CheckFailure("date_outside_period",
                                    f"Rows {', '.join(map(str, outside[:10]))} are dated outside the statement period. "
                                    "Re-read those dates, especially the month and year.", rows=tuple(outside)))

    # 4. Complete rows. A row without a date, or without a balance when other rows have one, was
    #    misread (or invented: models guess when a page is unreadable). Without balances the
    #    arithmetic above proves nothing, so a statement that prints none is reported too.
    no_date = [i for i, t in enumerate(txns, start=1) if t.date is None]
    if no_date:
        out.append(CheckFailure("transaction_missing_date",
                                f"Rows {', '.join(map(str, no_date[:10]))} have no date. Every row has a date: read it.",
                                rows=tuple(no_date)))
    with_balance = [t for t in txns if t.balance is not None]
    if with_balance and len(with_balance) < len(txns):
        missing = [i for i, t in enumerate(txns, start=1) if t.balance is None]
        out.append(CheckFailure("transaction_missing_balance",
                                f"Rows {', '.join(map(str, missing[:10]))} have no balance, but other rows do.",
                                rows=tuple(missing)))
    if txns and not with_balance:
        out.append(CheckFailure("running_balance_not_printed",
                                "No row has a running balance, so the amounts could not be checked."))
    return out


def check_payslip(p) -> list[CheckFailure]:
    """Payslip: earnings add up to gross pay; gross - deductions = net pay; PAN format."""
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
    out += _pan_problems(p.pan, "employee")
    # A "Gross pay" / "Total deductions" / "Net pay" line listed as a component would be counted twice.
    for kind, rows in (("earnings", p.earnings), ("deductions", p.deductions)):
        if any(re.search(r"\b(gross|total|net pay|net salary)\b", (row.name or ""), re.IGNORECASE) for row in rows):
            out.append(CheckFailure("total_listed_as_component",
                                    f"A total line is listed inside {kind}. List only the individual components."))
    # The printed "total deductions" must equal the sum of the listed deductions.
    if p.total_deductions and deductions and not _close(sum(deductions), to_decimal(p.total_deductions)):
        out.append(CheckFailure("deductions_sum_mismatch",
                                f"Deductions add up to {sum(deductions)} but total deductions is {p.total_deductions}."))
    out += _not_future(p.pay_date, "date_in_future", "pay date")
    return out


def check_identity(doc) -> list[CheckFailure]:
    """Identity document: PAN format (for PAN cards) and expiry. An expired document is a
    DOCUMENT_ONLY finding: reported as a warning, never a reason to re-extract.
    """
    out: list[CheckFailure] = []
    if doc.id_type == "pan" and doc.id_number and not PAN_RE.match(doc.id_number.replace(" ", "").upper()):
        out.append(CheckFailure("pan_format", "The PAN number does not have a valid format."))
    if doc.id_type == "pan" and doc.id_number:
        out += _pan_problems(doc.id_number, "card")
        if pan_matches_name(doc.id_number, doc.name) is False:
            out.append(CheckFailure("pan_name_mismatch", "The PAN's 5th letter does not match the holder's name."))
    if doc.id_type == "aadhaar":
        out += _aadhaar_problems(doc.id_number)
    out += _dob_problems(doc.date_of_birth)
    out += _address_problems(doc.address, "address")
    if doc.issue_date and doc.expiry_date and doc.expiry_date < doc.issue_date:
        out.append(CheckFailure("expiry_before_issue", "The expiry date is before the issue date. Re-read both dates."))
    if doc.expiry_date and doc.expiry_date < date.today():
        # Not an extraction error: the document itself is expired. Reported as a warning.
        out.append(CheckFailure("document_expired", "The document has expired."))
    return out


def check_kyc(form) -> list[CheckFailure]:
    """KYC form: PAN format, and that name, date of birth and PAN are present."""
    out: list[CheckFailure] = []
    if form.pan and not PAN_RE.match(form.pan.replace(" ", "").upper()):
        out.append(CheckFailure("pan_format", "The PAN does not have a valid format."))
    out += _pan_problems(form.pan, "applicant")
    if form.pan and pan_matches_name(form.pan, form.name) is False:
        out.append(CheckFailure("pan_name_mismatch", "The PAN's 5th letter does not match the applicant's name."))
    out += _aadhaar_problems(form.aadhaar)
    out += _dob_problems(form.date_of_birth)
    out += _not_future(form.form_date, "date_in_future", "form date")
    out += _address_problems(form.current_address, "current address")
    out += _address_problems(form.permanent_address, "permanent address")
    if form.mobile:
        mobile = re.sub(r"[\s-]", "", form.mobile)
        mobile = mobile[3:] if mobile.startswith("+91") else mobile[1:] if mobile.startswith("0") else mobile
        if not MOBILE_RE.match(mobile):
            out.append(CheckFailure("mobile_format", "The mobile number is not a valid 10-digit Indian number."))
    if form.email and not EMAIL_RE.match(form.email.strip()):
        out.append(CheckFailure("email_format", "The e-mail address is not valid."))
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
DOCUMENT_ONLY = {"document_expired", "required_fields_missing", "running_balance_not_printed",
                 "line_item_amount_mismatch", "pan_name_mismatch"}


def run_checks(doc_type: str, parsed) -> list[CheckFailure]:
    """Run the consistency checks for `doc_type` on a validated result; [] means all passed."""
    return _CHECKS[doc_type](parsed)
