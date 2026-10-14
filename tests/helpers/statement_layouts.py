"""SYNTHETIC bank statements in different layouts, for the generic parser's tests.

Every bank, person and number here is invented. Each layout reproduces features seen in real
statement layouts (anonymised samples, 2026-10), so the tests prove the parser copes with
them without any real document in the repository:

  wrapped     titles only once (page 2, after a summary page), the same header and footer on
              every page, descriptions wrapped over 2-3 lines below the date line, Deposits
              BEFORE Withdrawals, "0.00" in empty cells, a B/F line, a summary table (labels
              on one line, values on the next), dates like "01-Aug-2026", a "Total" line
  type_amount one Amount column with a Type column (Debit/Credit) and "(1,234.00)" for money
              out, dates as three words ("03 Aug 2026"), captions straddling their value
              ("ACCOUNT" / value / "HOLDER"), titles repeated on every page, a diagonal
              watermark across the table
  yearless    dates without a year ("02/08"), a D/C column, "OPENING" / value / "BALANCE",
              titles only on page 1, day headings ("MONDAY, 10 AUGUST 2026")
  european    French titles, "10.481,44" amounts, "—" in empty cells
  two_accounts  two account tables in one file (the parser must decline)
  ambiguous     a wrapped line exactly between two rows (the parser must decline)

`build(name)` returns (pdf bytes, expected) where `expected` holds what a correct reading gives.
"""

from __future__ import annotations

from decimal import Decimal

from tests.helpers.simple_pdf import make_pdf

# Helvetica character widths (1/1000 of the font size), enough to right-align amounts.
_WIDTH = {**{d: 556 for d in "0123456789"}, ",": 278, ".": 278, "(": 333, ")": 333, "-": 333, " ": 278}
SIZE = 9


def _width(text: str) -> float:
    return sum(_WIDTH.get(ch, 556) for ch in text) * SIZE / 1000


def right(x_right: float, y: float, text: str) -> tuple[float, float, str]:
    """Text whose right edge is at x_right (amounts are right-aligned)."""
    return (x_right - _width(text), y, text)


def _money(value: Decimal, european: bool = False) -> str:
    text = f"{value:,.2f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".") if european else text


def _rows(n: int, opening: Decimal):
    """n consistent rows: (day, description lines, debit, credit, balance)."""
    shops = ["Grocery Mart", "City Fuel", "Salary ACME Ltd", "Water Board", "Phone top-up", "Book Shop"]
    out, balance = [], opening
    for i in range(n):
        if i % 3 == 2:
            debit, credit = None, Decimal("1500.00") + i
        else:
            debit, credit = Decimal("120.50") + 7 * i, None
        balance = balance - (debit or 0) + (credit or 0)
        words = [f"POS {shops[i % len(shops)]}"]
        if i % 2 == 0:
            words.append(f"Card ending {1000 + i}")       # a wrapped second line
        if i % 4 == 0:
            words.append("Colombo")                         # and sometimes a third
        out.append((1 + i, words, debit, credit, balance))
    return out


def _expected(rows, opening, account, holder, period, closing=None):
    return {
        "account_number": account, "account_holder": holder, "period": period,
        "opening_balance": f"{opening:.2f}", "closing_balance": f"{(closing or rows[-1][4]):.2f}",
        "transactions": [{"day": d, "description": " ".join(w), "debit": f"{dr:.2f}" if dr else None,
                          "credit": f"{cr:.2f}" if cr else None, "balance": f"{bal:.2f}"}
                         for d, w, dr, cr, bal in rows],
    }


def wrapped(n: int = 24, per_page: int = 9):
    opening = Decimal("5000.00")
    rows = _rows(n, opening)
    account, holder = "001234567890", "Sample Customer A"
    deposits = sum((r[3] or 0) for r in rows)
    withdrawals = sum((r[2] or 0) for r in rows)
    closing = rows[-1][4]

    def frame(lines):                                  # the header and footer of EVERY page
        lines += [(30, 810, "ACME BANK e-Statement"), (30, 797, "Statement Period : 01-08-2026 to 31-08-2026"),
                  (30, 784, "12 Sample Road Colombo 03"), (30, 40, "Hotline 011 555 0100 www.example.invalid")]
        return lines

    pages = [frame([(30, 740, "Relationship Summary"), (30, 720, "Savings products held: 1")])]
    chunks = [rows[i:i + per_page] for i in range(0, n, per_page)]
    for p, chunk in enumerate(chunks):
        lines = frame([])
        y = 760
        if p == 0:
            lines += [(30, 760, f"Name : {holder}"),
                      (40, 740, "Account Number"), (140, 740, "Opening Balance"), (240, 740, "Total Deposits"),
                      (330, 740, "Total Withdrawals"), (450, 740, "Closing Balance"),
                      (40, 726, account), right(205, 726, _money(opening)), right(300, 726, _money(deposits)),
                      right(400, 726, _money(withdrawals)), right(515, 726, _money(closing))]
            lines += [(30, 700, "Transaction Date"), (105, 700, "Value Date"), (165, 700, "Transaction Details"),
                      (300, 700, "Reference No"), (372, 700, "Deposits"), (425, 700, "Withdrawals"),
                      (520, 700, "Balance")]
            lines += [(165, 685, "B/F"), right(565, 685, _money(opening))]
            y = 662
        for day, words, debit, credit, balance in chunk:
            date = f"{day:02d}-Aug-2026"
            lines += [(30, y, date), (105, y, date), (165, y, words[0]), (300, y, f"R{day:05d}"),
                      right(410, y, _money(credit or Decimal(0))), right(480, y, _money(debit or Decimal(0))),
                      right(565, y, _money(balance))]
            for k, extra in enumerate(words[1:], start=1):
                lines.append((165, y - 10 * k, extra))
            y -= 10 * len(words) + 13
        if p == len(chunks) - 1:
            lines += [(30, y - 5, "Total"), right(410, y - 5, _money(deposits)), right(480, y - 5, _money(withdrawals))]
        pages.append(lines)
    expected = _expected(rows, opening, account, None, ("2026-08-01", "2026-08-31"))
    expected["account_holder"] = holder
    for t in expected["transactions"]:
        t["reference"] = f"R{t['day']:05d}"
    return make_pdf(pages), expected


def type_amount(n: int = 20, per_page: int = 12):
    opening = Decimal("10000.00")
    rows = [(d, [w[0]], dr, cr, bal) for d, w, dr, cr, bal in _rows(n, opening)]   # single-line rows
    account, holder = "SAMPLE-ACB-000123", "Sample Customer B"
    pages = []
    for p in range(0, n, per_page):
        lines = []
        top = 690
        if p == 0:
            lines += [(80, 770, "ACCOUNT"), (320, 770, "STATEMENT"),          # captions straddling the values
                      (160, 766, holder), (390, 766, "01 Aug – 31 Aug 2026"),
                      (80, 762, "HOLDER"), (320, 762, "PERIOD"),
                      (80, 740, "ACCOUNT NUMBER"), (200, 740, account),
                      (80, 720, "OPENING BALANCE"), (330, 720, "CLOSING BALANCE"),
                      (80, 708, "LKR " + _money(opening)), (330, 708, "LKR " + _money(rows[-1][4]))]
        else:
            top = 780
        lines += [(51, top, "Date"), (113, top, "Description"), (306, top, "Type"), (354, top, "Amount (LKR)"),
                  (442, top, "Balance (LKR)")]
        y = top - 18
        for day, words, debit, credit, balance in rows[p:p + per_page]:
            amount = f"({_money(debit)})" if debit else _money(credit)
            lines += [(51, y, f"{day:02d}"), (63, y, "Aug"), (82, y, "2026"), (113, y, words[0]),
                      (306, y, "Debit" if debit else "Credit"), (354, y, amount), right(500, y, _money(balance))]
            y -= 18
        lines += [(200, 450, "SAMPLE COPY", 33), (40, 30, f"Page {p // per_page + 1}")]   # watermark, footer
        pages.append(lines)
    return make_pdf(pages), _expected(rows, opening, account, holder, ("2026-08-01", "2026-08-31"))


def yearless(n: int = 18, per_page: int = 10):
    opening = Decimal("525000.00")
    rows = [(d, [w[0]], dr, cr, bal) for d, w, dr, cr, bal in _rows(n, opening)]
    account, holder = "SAMPLE-08-AUG26", "Sample Customer C"
    pages = []
    for p in range(0, n, per_page):
        if p == 0:
            lines = [(76, 790, "ACCOUNT HOLDER"), (167, 790, holder), (303, 790, "STATEMENT PERIOD"),
                     (400, 790, "01 Aug – 31 Aug 2026"),
                     (76, 770, "ACCOUNT NUMBER"), (167, 770, account), (303, 770, "ACCOUNT TYPE"), (400, 770, "Business"),
                     (51, 748, "OPENING"), (111, 744, "LKR " + _money(opening)), (230, 744, "CLOSING"),
                     (290, 744, "LKR " + _money(rows[-1][4])), (51, 740, "BALANCE"),
                     (47, 700, "Date"), (101, 700, "Narrative"), (257, 700, "D/C"), (356, 700, "Amount"),
                     (438, 700, "Balance")]
            y = 684
        else:
            lines = [(43, 800, "TRANSACTION ACTIVITY — PAGE 2")]
            y = 780
        for i, (day, words, debit, credit, balance) in enumerate(rows[p:p + per_page]):
            if i == 4:
                lines.append((47, y, f"MONDAY, {day} AUGUST 2026"))     # a day heading inside the table
                y -= 16
            lines += [(47, y, f"{day:02d}/08"), (101, y, words[0]), (257, y, "D" if debit else "C"),
                      right(400, y, _money(debit or credit)), right(480, y, _money(balance))]
            y -= 16
        pages.append(lines)
    return make_pdf(pages), _expected(rows, opening, account, holder, ("2026-08-01", "2026-08-31"))


def european(n: int = 12):
    opening = Decimal("525000.00")
    rows = [(d, [w[0]], dr, cr, bal) for d, w, dr, cr, bal in _rows(n, opening)]
    account, holder = "FR-SAMPLE-000612", "Client Exemple"
    lines = [(76, 790, "TITULAIRE DU COMPTE"), (190, 790, holder), (303, 790, "PÉRIODE"), (360, 790, "01–31 août 2026"),
             (76, 770, "COMPTE"), (190, 770, account), (303, 770, "DEVISE"), (360, 770, "EUR"),
             (76, 750, "SOLDE INITIAL"), (190, 750, _money(opening, True)),
             (303, 750, "SOLDE FINAL"), (390, 750, _money(rows[-1][4], True)),
             (47, 700, "Date"), (101, 700, "Libellé de l’opération"), (300, 700, "Débit (€)"),
             (380, 700, "Crédit (€)"), (460, 700, "Solde (€)")]
    y = 684
    for day, words, debit, credit, balance in rows:
        lines += [(47, y, f"{day:02d}/08/2026"), (101, y, words[0]),
                  right(345, y, _money(debit, True)) if debit else (330, y, "—"),
                  right(425, y, _money(credit, True)) if credit else (410, y, "—"),
                  right(510, y, _money(balance, True))]
        y -= 16
    return make_pdf([lines]), _expected(rows, opening, account, holder, ("2026-08-01", "2026-08-31"))


def two_accounts():
    """Two account tables in one file, each starting with an "Opening Balance" row."""
    pages = []
    for k in range(2):
        opening = Decimal("1000.00") * (k + 1)
        rows = _rows(4, opening)
        lines = [(30, 800, "Account Number: 00123456789" + str(k)), (30, 785, "Transaction Period: 01/08/2026 - 31/08/2026"),
                 (33, 760, "Txn Date"), (100, 760, "Narration"), (330, 760, "Withdrawal"), (420, 760, "Deposit"),
                 (500, 760, "Balance"),
                 (33, 745, "01/08/2026"), (100, 745, "Opening Balance"), right(545, 745, _money(opening))]
        y = 730
        for day, words, debit, credit, balance in rows:
            lines += [(33, y, f"{day:02d}/08/2026"), (100, y, words[0])]
            lines += [right(380, y, _money(debit))] if debit else [right(460, y, _money(credit))]
            lines.append(right(545, y, _money(balance)))
            y -= 14
        pages.append(lines)
    return make_pdf(pages), None


def ambiguous():
    """Vertically centred cells where one wrapped line sits exactly between two rows."""
    lines = [(30, 800, "Statement Period : 01/08/2026 to 31/08/2026"), (30, 785, "Account Number : 001234567890"),
             (30, 770, "Opening Balance : 1,000.00"),
             (30, 740, "Date"), (100, 740, "Description"), (400, 740, "Debit"), (450, 740, "Credit"),
             (510, 740, "Balance")]
    lines += [(30, 700, "01/08/2026"), right(430, 700, "100.00"), right(545, 700, "900.00"),
              (100, 710, "POS Grocery Mart"), (100, 692, "half way line"),           # 8 above the next row too
              (30, 684, "02/08/2026"), right(430, 684, "100.00"), right(545, 684, "800.00"),
              (100, 674, "POS City Fuel")]
    return make_pdf([lines]), None


LAYOUTS = {"wrapped": wrapped, "type_amount": type_amount, "yearless": yearless, "european": european}


def build(name: str):
    return {**LAYOUTS, "two_accounts": two_accounts, "ambiguous": ambiguous}[name]()
