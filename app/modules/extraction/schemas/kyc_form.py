"""KYC form schema (requirement FR-3.5). Strict Aadhaar masking applies (FR-4.2)."""

from __future__ import annotations

from .base import Address, DocBase, IsoDate, Money, Text

SCHEMA_VERSION = "1.0"


class KycForm(DocBase):
    name: Text = None
    date_of_birth: IsoDate = None
    gender: Text = None
    father_or_spouse_name: Text = None
    pan: Text = None
    aadhaar: Text = None          # always masked to XXXX XXXX 1234
    ckyc_number: Text = None
    mobile: Text = None
    email: Text = None
    current_address: Address | None = None
    permanent_address: Address | None = None
    occupation: Text = None
    annual_income: Money = None
    pep_status: Text = None       # Politically Exposed Person declaration
    nominee_name: Text = None
    signature_present: bool | None = None
    form_date: IsoDate = None
