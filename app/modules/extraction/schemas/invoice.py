"""GST tax invoice (requirements FR-3.1).

Field descriptions are written FOR THE MODEL: they are sent with the schema and tell it
what to put in each field.
"""

import re

from pydantic import BaseModel, Field

from app.modules.extraction.schemas.base import DocumentSchema, differs, fmt, total
from app.modules.extraction.schemas.types import DocDate, Money, Number, Text, text

PAN_PATTERN = r"^[A-Z]{5}[0-9]{4}[A-Z]$"


class Party(BaseModel):
    name: Text = text("Legal name as printed")
    gstin: Text = text("15-character GSTIN")
    pan: Text = text("10-character PAN")
    address: Text = text()


class LineItem(BaseModel):
    description: Text = text()
    hsn_sac: Text = text("HSN or SAC code")
    quantity: Number = None
    unit_price: Money = None
    tax_rate: Number = Field(default=None, description="Tax rate in percent")
    amount: Money = Field(default=None, description="Taxable amount for the line")


class Invoice(DocumentSchema):
    invoice_number: Text = text()
    invoice_date: DocDate = None
    due_date: DocDate = None
    place_of_supply: Text = text("State name or code")
    currency: Text = text("ISO code, e.g. INR")
    seller: Party = Field(default_factory=Party)
    buyer: Party = Field(default_factory=Party)
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: Money = Field(default=None, description="Total taxable value before tax")
    cgst: Money = None
    sgst: Money = None
    igst: Money = None
    total_tax: Money = None
    total_amount: Money = Field(default=None, description="Invoice total including tax")

    def consistency_warnings(self) -> list[str]:
        warnings = []
        amounts = [item.amount for item in self.line_items]
        # 1. Line items should add up to the subtotal.
        if self.line_items and self.subtotal is not None and differs(total(amounts), self.subtotal):
            warnings.append(f"Line items add up to {fmt(total(amounts))}, but subtotal is {fmt(self.subtotal)}.")
        # 2. Subtotal + tax should equal the total. If total_tax is missing, add up the GST parts.
        tax = self.total_tax if self.total_tax is not None else total([self.cgst, self.sgst, self.igst])
        if (
            self.subtotal is not None
            and self.total_amount is not None
            and differs(self.subtotal + tax, self.total_amount)
        ):
            warnings.append(f"Subtotal + tax is {fmt(self.subtotal + tax)}, but total is {fmt(self.total_amount)}.")
        # 3. PAN format.
        for role, party in (("seller", self.seller), ("buyer", self.buyer)):
            if party.pan and not re.fullmatch(PAN_PATTERN, party.pan):
                warnings.append(f"{role}.pan does not look like a valid PAN.")
        return warnings
