"""Pick the Rung 0 parser for a document, or None.

Every parser class is registered in PARSERS. For a digital PDF we look at the
text of page 1 and return the FIRST parser whose fingerprint matches (bank
marker text + all column header labels). Keep layout versions in order
newest-first, so e.g. hdfc_v2 is tried before hdfc_v1.
"""

from __future__ import annotations

from .bank.demo_bank_v1 import DemoBankV1
from .base import StatementParser
from .generic import GenericStatementParser

# Register parsers here (newest layout version first for each bank). The generic parser
# reads any layout it can recognise from its column titles, so it must stay LAST: a
# bank-specific parser, when there is one, knows its layout better.
PARSERS: list[StatementParser] = [
    DemoBankV1(),
    GenericStatementParser(),
]

# How many pages' text the fingerprints look at. Some statements start with a summary or
# cover page and print the transaction table's column titles only on page 2 or 3.
FINGERPRINT_PAGES = 3


def pick_parser(first_pages_text: str | None) -> StatementParser | None:
    """The first registered parser whose fingerprint is in the text of the first pages, or None."""
    if not first_pages_text:
        return None
    for parser in PARSERS:
        if parser.matches(first_pages_text):
            return parser
    return None
