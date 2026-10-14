"""Reference parser for the synthetic "DEMO BANK LTD" statement layout.

This is a TEMPLATE, not a real bank. Real bank parsers are written after the
phase-0 samples show which banks are most common (architecture §3.3). To add a
real bank, copy this file to e.g. `hdfc_v1.py`, change the marker text, column
labels and header labels to match the bank's statement, add 3-5 SYNTHETIC
fixture PDFs that copy the layout (never real statements), and register the
class in parsers/fingerprint.py.

The layout this parser expects (page 1):

    DEMO BANK LTD
    Statement of Account
    Account Holder: Asha Verma
    Account Number: 001234567890
    IFSC: DEMO0001234
    Branch: MG Road
    Statement Period: 01/09/2026 to 30/09/2026
    Opening Balance: 10,000.00
    Date        Description            Reference   Debit     Credit     Balance
    01/09/2026  UPI/Grocery Store      UPI123      500.00              9,500.00
    ...
    Closing Balance: 12,345.00
"""

from __future__ import annotations

from ..base import ColumnSpec, StatementParser


class DemoBankV1(StatementParser):
    parser_id = "demo_bank_v1"
    bank_name = "Demo Bank Ltd"
    markers = ["DEMO BANK LTD", "Statement of Account"]
    columns = [
        ColumnSpec("date", "Date"),
        ColumnSpec("description", "Description"),
        ColumnSpec("reference", "Reference"),
        ColumnSpec("debit", "Debit"),
        ColumnSpec("credit", "Credit"),
        ColumnSpec("balance", "Balance"),
    ]
    header_labels = {
        "account_holder": "Account Holder",
        "account_number": "Account Number",
        "ifsc": "IFSC",
        "branch": "Branch",
        "period": "Statement Period",
        "opening_balance": "Opening Balance",
        "closing_balance": "Closing Balance",
    }
    table_end_label = "Closing Balance"
