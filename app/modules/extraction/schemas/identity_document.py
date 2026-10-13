"""Indian identity document: Aadhaar, PAN card, passport, driving licence, voter ID (FR-3.4).

Aadhaar handling (UIDAI rules): when id_type is "aadhaar", document_number is masked to
"XXXX XXXX 1234" before the result is stored or returned, with no way to turn that off
(see masking.py). Identity documents are also excluded from model training by default.
"""

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field

from app.modules.extraction.schemas.base import DocumentSchema
from app.modules.extraction.schemas.invoice import PAN_PATTERN
from app.modules.extraction.schemas.types import DocDate, Text, text

IdType = Literal["aadhaar", "pan", "passport", "driving_licence", "voter_id", "other"]


def _id_type(value):
    """Accept common spellings ("Driving License", "PAN Card") and map them to our codes."""
    if value is None or value == "":
        return None
    v = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "pan_card": "pan",
        "driving_license": "driving_licence",
        "dl": "driving_licence",
        "voter_id_card": "voter_id",
        "epic": "voter_id",
        "aadhar": "aadhaar",
        "aadhaar_card": "aadhaar",
    }
    v = aliases.get(v, v)
    return v if v in IdType.__args__ else "other"


class IdentityDocument(DocumentSchema):
    id_type: Annotated[IdType | None, BeforeValidator(_id_type)] = Field(
        default=None, description="Which kind of identity document this is"
    )
    document_number: Text = text("Number printed on the document")
    full_name: Text = text()
    father_or_spouse_name: Text = text()
    date_of_birth: DocDate = None
    gender: Text = text()
    address: Text = text()
    issue_date: DocDate = None
    expiry_date: DocDate = None
    issuing_authority: Text = text()
    nationality: Text = text()

    def consistency_warnings(self) -> list[str]:
        warnings = []
        if self.id_type == "pan" and self.document_number and not re.fullmatch(PAN_PATTERN, self.document_number):
            warnings.append("document_number does not look like a valid PAN.")
        if self.expiry_date and self.expiry_date < date.today():
            warnings.append(f"The document expired on {self.expiry_date.isoformat()}.")
        return warnings
