"""Registry of supported document types.

To add a type: write a schema module like invoice.py and add one entry below. Nothing
else in the pipeline changes (FR-3.11).

Each DocumentType says:
    name / version     the API name, and a version to bump whenever the output changes
    description        one sentence for the model
    model              the Pydantic schema class (fields + consistency checks)
    sensitive          fields to mask and how: {"dotted.path": kind}
                         aadhaar   → "XXXX XXXX 1234", always
                         pan, account, id_number, phone → last 4 characters kept
    aadhaar_scan       also mask any 12-digit Aadhaar-like number found in ANY text field
    training_allowed   may documents of this type be kept for training (with consent)?
                       Identity and KYC documents: no, by default (FR-6.2)
"""

from dataclasses import dataclass, field
from functools import cached_property

from app.modules.extraction.schemas.bank_statement import BankStatement
from app.modules.extraction.schemas.base import DocumentSchema, schema_for_model
from app.modules.extraction.schemas.identity_document import IdentityDocument
from app.modules.extraction.schemas.invoice import Invoice
from app.modules.extraction.schemas.kyc_form import KycForm
from app.modules.extraction.schemas.payslip import Payslip


@dataclass(frozen=True)
class DocumentType:
    name: str
    version: str
    description: str
    model: type[DocumentSchema]
    sensitive: dict[str, str] = field(default_factory=dict)
    aadhaar_scan: bool = False
    training_allowed: bool = True

    @cached_property
    def json_schema(self) -> dict:
        """The schema sent to the model (computed once, then reused)."""
        return schema_for_model(self.model)


DOCUMENT_TYPES: dict[str, DocumentType] = {
    t.name: t
    for t in (
        DocumentType(
            name="invoice",
            version="1",
            description="GST tax invoice issued by a seller to a buyer",
            model=Invoice,
            sensitive={"seller.pan": "pan", "buyer.pan": "pan"},
        ),
        DocumentType(
            name="bank_statement",
            version="1",
            description="Bank account statement listing transactions for a period",
            model=BankStatement,
            sensitive={"account_number": "account"},
        ),
        DocumentType(
            name="payslip",
            version="1",
            description="Monthly salary slip issued by an employer",
            model=Payslip,
            sensitive={"pan": "pan", "uan": "id_number", "bank_account_number": "account"},
        ),
        DocumentType(
            name="identity_document",
            version="1",
            description="Indian identity document: Aadhaar, PAN card, passport, driving licence or voter ID",
            model=IdentityDocument,
            sensitive={"document_number": "id_number"},
            aadhaar_scan=True,
            training_allowed=False,
        ),
        DocumentType(
            name="kyc_form",
            version="1",
            description="Customer KYC application form",
            model=KycForm,
            sensitive={"pan": "pan", "aadhaar_number": "aadhaar", "mobile": "phone"},
            aadhaar_scan=True,
            training_allowed=False,
        ),
    )
}


def get_document_type(name: str) -> DocumentType | None:
    return DOCUMENT_TYPES.get(name)
