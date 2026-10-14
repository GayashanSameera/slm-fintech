"""Registry of document types.

Adding a new document type (requirement FR-3.11):
  1. create a new module in this folder with a Pydantic model + SCHEMA_VERSION,
  2. add ONE line to REGISTRY below,
  3. (optional) add consistency checks in checks.py and masking rules in masking.py.
Nothing else in the platform needs to change.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from . import bank_statement, identity_document, invoice, kyc_form, payslip


@dataclass(frozen=True)
class DocType:
    name: str
    model: type[BaseModel]
    version: str
    # Identity data (strict Aadhaar masking, never kept for training by default).
    sensitive_identity: bool = False


REGISTRY: dict[str, DocType] = {
    "invoice": DocType("invoice", invoice.Invoice, invoice.SCHEMA_VERSION),
    "bank_statement": DocType("bank_statement", bank_statement.BankStatement, bank_statement.SCHEMA_VERSION),
    "payslip": DocType("payslip", payslip.Payslip, payslip.SCHEMA_VERSION),
    "identity_document": DocType("identity_document", identity_document.IdentityDocument,
                                 identity_document.SCHEMA_VERSION, sensitive_identity=True),
    "kyc_form": DocType("kyc_form", kyc_form.KycForm, kyc_form.SCHEMA_VERSION, sensitive_identity=True),
}


def get_doc_type(name: str) -> DocType:
    """The DocType for a type name (KeyError for an unknown name)."""
    return REGISTRY[name]
