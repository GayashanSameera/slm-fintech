"""Pick the Rung 0 parser for a document, or None.

Every parser class is registered in PARSERS. For a digital PDF we look at the
text of page 1 and return the FIRST parser whose fingerprint matches (bank
marker text + all column header labels). Keep layout versions in order
newest-first, so e.g. hdfc_v2 is tried before hdfc_v1.
"""

from __future__ import annotations

from .bank.demo_bank_v1 import DemoBankV1
from .base import StatementParser

# Register parsers here (newest layout version first for each bank).
PARSERS: list[StatementParser] = [
    DemoBankV1(),
]


def pick_parser(first_page_text: str | None) -> StatementParser | None:
    """The first registered parser whose fingerprint (marker text + column labels) is on page 1, or None."""
    if not first_page_text:
        return None
    for parser in PARSERS:
        if parser.matches(first_page_text):
            return parser
    return None
