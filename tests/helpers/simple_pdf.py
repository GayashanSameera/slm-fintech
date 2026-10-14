"""A tiny PDF writer for TEST FIXTURES (no external libraries).

It places plain text lines at exact positions on A4 pages, using the standard
Helvetica font. That is enough to build SYNTHETIC bank statements that look,
to a parser, like a digital e-statement. Real customer documents are never
used in tests (golden rule #1).

    pdf = make_pdf([[(40, 800, "DEMO BANK LTD"), (40, 780, "Statement of Account")]])
"""

from __future__ import annotations


def _escape(text: str) -> str:
    """Escape the characters PDF string literals treat specially: backslash and parentheses."""
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[tuple]], font_size: int = 9) -> bytes:
    """pages: one list per page of (x, y, text) or (x, y, text, angle in degrees) — an angle
    draws rotated text, e.g. a diagonal "SAMPLE" watermark. (0, 0) is the bottom-left corner.
    Text is encoded as Windows-1252 (the PDF's WinAnsiEncoding), so "–", "—", "€", "é" work."""
    objects: list[bytes] = []

    def add(obj: bytes) -> int:
        """Append one PDF object and return its object number (numbering starts at 1)."""
        objects.append(obj)
        return len(objects)          # PDF object numbers start at 1

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    pages_obj_index = add(b"")       # placeholder, filled in after we know the page ids
    page_ids = []
    for lines in pages:
        stream = "BT\n" + "".join(_show(item, font_size) for item in lines) + "ET\n"
        data = stream.encode("cp1252")
        content = add(b"<< /Length %d >>\nstream\n" % len(data) + data + b"endstream")
        page = add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] /Contents %d 0 R "
            b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (pages_obj_index, content, font)
        )
        page_ids.append(page)
    kids = " ".join(f"{p} 0 R" for p in page_ids).encode()
    objects[pages_obj_index - 1] = b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(page_ids)
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj_index)

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + obj + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, xref_at)
    return bytes(out)


def _show(item: tuple, font_size: int) -> str:
    """One text-showing operation: position (and rotation) matrix, then the string."""
    import math

    x, y, text = item[:3]
    angle = math.radians(item[3]) if len(item) > 3 else 0.0
    a, b = math.cos(angle), math.sin(angle)
    return f"/F1 {font_size} Tf {a:.4f} {b:.4f} {-b:.4f} {a:.4f} {x:.1f} {y:.1f} Tm ({_escape(text)}) Tj\n"


# Column x positions for the synthetic DEMO BANK statement layout.
COLS = {"date": 40, "description": 110, "reference": 300, "debit": 380, "credit": 450, "balance": 515}


def demo_statement(transactions: list[dict], opening: str = "10,000.00", closing: str | None = None,
                   holder: str = "Asha Verma", account: str = "001234567890", rows_per_page: int = 25) -> bytes:
    """Build a synthetic DEMO BANK LTD statement (see parsers/bank/demo_bank_v1.py).

    transactions: [{"date": "01/09/2026", "description": "UPI/Grocery", "reference": "UPI001",
                    "debit": "500.00", "credit": "", "balance": "9,500.00"}, ...]
    """
    if closing is None:
        closing = transactions[-1]["balance"] if transactions else opening
    header = [
        (40, 800, "DEMO BANK LTD"),
        (40, 785, "Statement of Account"),
        (40, 765, f"Account Holder: {holder}"),
        (40, 752, f"Account Number: {account}"),
        (40, 739, "IFSC: DEMO0001234"),
        (40, 726, "Branch: MG Road"),
        (40, 713, "Statement Period: 01/09/2026 to 30/09/2026"),
        (40, 700, f"Opening Balance: {opening}"),
    ]
    table_header = [(x, 0, label) for label, x in
                    zip(["Date", "Description", "Reference", "Debit", "Credit", "Balance"], COLS.values(), strict=True)]

    pages: list[list[tuple[float, float, str]]] = []
    chunks = [transactions[i:i + rows_per_page] for i in range(0, max(len(transactions), 1), rows_per_page)]
    for page_no, chunk in enumerate(chunks):
        lines = list(header) if page_no == 0 else []
        top = 675 if page_no == 0 else 800
        lines += [(x, top, label) for x, _, label in table_header]
        y = top - 15
        for t in chunk:
            for key, x in COLS.items():
                if t.get(key):
                    lines.append((x, y, t[key]))
            y -= 13
        if page_no == len(chunks) - 1:
            lines.append((40, y - 10, f"Closing Balance: {closing}"))
        pages.append(lines)
    return make_pdf(pages)


def sample_transactions(n: int = 5, start: float = 10000.0) -> list[dict]:
    """n consistent rows: alternating debits and credits with correct running balances."""
    rows, balance = [], start
    for i in range(n):
        if i % 2 == 0:
            amount = 250.0 + i * 10
            balance -= amount
            debit, credit = f"{amount:,.2f}", ""
        else:
            amount = 1000.0 + i * 5
            balance += amount
            debit, credit = "", f"{amount:,.2f}"
        # Spread the rows over September (1st to 30th) so any number of rows gives real dates.
        day = 1 + (i * 30) // max(n, 1)
        rows.append({"date": f"{day:02d}/09/2026", "description": f"UPI/Shop{i}",
                     "reference": f"REF{i:04d}", "debit": debit, "credit": credit,
                     "balance": f"{balance:,.2f}"})
    return rows


def demo_reading(transactions: list[dict], account: str = "001234567890", holder: str = "Asha Verma") -> str:
    """What a CORRECT verifier model answers for demo_statement(transactions, ...): the key
    facts as printed (parsers/verify.py asks for exactly these). For FakeGateway tests."""
    import json

    return json.dumps({
        "account_holder": holder, "account_number": account, "ifsc": "DEMO0001234", "branch": "MG Road",
        "period_from": "01/09/2026", "period_to": "30/09/2026",
        "first_transaction": {"date": transactions[0]["date"], "description": transactions[0]["description"]},
        "last_transaction": {"date": transactions[-1]["date"], "description": transactions[-1]["description"]},
    })
