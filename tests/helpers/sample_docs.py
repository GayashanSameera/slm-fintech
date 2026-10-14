"""SYNTHETIC sample documents for tests and the smoke test (never real data).

    make_invoice_png()      a scanned-looking GST invoice image (goes through the AI ladder)
    make_statement_pdf()    a digital DEMO BANK statement (goes through Rung 0)

All names, numbers, GSTINs and PANs are invented; the arithmetic is correct, so the
consistency checks pass when the extraction is right.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from tests.helpers.simple_pdf import demo_statement, sample_transactions


def make_invoice_png() -> bytes:
    """An A4-proportioned invoice: 2 line items, subtotal 15,000.00 + 18% GST = 17,700.00."""
    img = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(img)
    big, normal = ImageFont.load_default(size=40), ImageFont.load_default(size=26)
    lines = [
        (80, 80, "TAX INVOICE", big),
        (80, 160, "Demo Traders Pvt Ltd", normal),
        (80, 200, "GSTIN: 29ABCDE1234F1Z5    PAN: ABCDE1234F", normal),
        (80, 240, "12 MG Road, Bengaluru 560001", normal),
        (760, 160, "Invoice No: INV-2026-0042", normal),
        (760, 200, "Invoice Date: 07/09/2026", normal),
        (760, 240, "Due Date: 07/10/2026", normal),
        (80, 330, "Bill To: Sample Retail LLP", normal),
        (80, 370, "GSTIN: 27PQRSX5678K1Z2", normal),
        (80, 470, "Description            HSN     Qty    Rate        Amount", normal),
        (80, 520, "Steel brackets         7308    10     1,000.00    10,000.00", normal),
        (80, 560, "Installation service   9954    1      5,000.00    5,000.00", normal),
        (700, 660, "Subtotal:   15,000.00", normal),
        (700, 700, "CGST 9%:     1,350.00", normal),
        (700, 740, "SGST 9%:     1,350.00", normal),
        (700, 790, "Total:  Rs. 17,700.00", big),   # the built-in font has no ₹ glyph
    ]
    for x, y, text, font in lines:
        draw.text((x, y), text, fill="black", font=font)
    draw.line((80, 505, 1160, 505), fill="black", width=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_statement_pdf(rows: int = 12, account: str = "001234567890") -> bytes:
    """Pass a different `account` to get a different file: the API answers an IDENTICAL
    file from the same tenant with the earlier result (duplicate detection, FR-2.11)."""
    return demo_statement(sample_transactions(rows), account=account)
