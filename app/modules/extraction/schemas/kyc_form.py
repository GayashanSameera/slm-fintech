"""Customer KYC application form (requirements FR-3.5).

Contains several sensitive numbers: PAN, Aadhaar (always masked), mobile. KYC forms are
excluded from model training by default.
"""

import re

from pydantic import Field

from app.modules.extraction.schemas.base import DocumentSchema
from app.modules.extraction.schemas.invoice import PAN_PATTERN
from app.modules.extraction.schemas.types import DocDate, Text, text


class KycForm(DocumentSchema):
    customer_name: Text = text()
    date_of_birth: DocDate = None
    gender: Text = text()
    father_or_spouse_name: Text = text()
    pan: Text = text("10-character PAN")
    aadhaar_number: Text = text()
    ckyc_number: Text = text("Central KYC identifier, if printed")
    mobile: Text = text()
    email: Text = text()
    current_address: Text = text()
    permanent_address: Text = text()
    occupation: Text = text()
    annual_income: Text = text("As printed, e.g. a range")
    politically_exposed: bool | None = Field(default=None, description="Politically exposed person (PEP)")
    nominee_name: Text = text()
    nominee_relationship: Text = text()
    signature_present: bool | None = Field(default=None, description="Whether the form is signed")
    form_date: DocDate = None

    def consistency_warnings(self) -> list[str]:
        warnings = []
        if self.pan and not re.fullmatch(PAN_PATTERN, self.pan):
            warnings.append("pan does not look like a valid PAN.")
        for name in ("customer_name", "date_of_birth", "pan"):
            if getattr(self, name) is None:
                warnings.append(f"Required field {name} is missing.")
        return warnings
