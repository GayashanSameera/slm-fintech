"""Invoice schema (requirement FR-3.1).

Consistency checks for this type live in checks.py:
  * line item amounts add up to the subtotal
  * subtotal + CGST + SGST + IGST = total
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .base import DocBase, IsoDate, Money, Text

SCHEMA_VERSION = "1.0"   # bump when fields change (FR-3.10); stored with every result


class Party(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: Text = None
    gstin: Text = None      # 15-character GST identification number
    pan: Text = None        # masked to the last 4 characters by default (FR-4.3)
    address: Text = None


class LineItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    description: Text = None
    hsn_sac: Text = None    # goods (HSN) or services (SAC) classification code
    quantity: Money = None  # quantities can be fractional (e.g. 2.5 kg), so stored like money
    rate: Money = None
    amount: Money = None


class Invoice(DocBase):
    invoice_number: Text = None
    invoice_date: IsoDate = None
    due_date: IsoDate = None
    seller: Party | None = None
    buyer: Party | None = None
    line_items: list[LineItem] = []
    subtotal: Money = None
    cgst: Money = None
    sgst: Money = None
    igst: Money = None
    total: Money = None
