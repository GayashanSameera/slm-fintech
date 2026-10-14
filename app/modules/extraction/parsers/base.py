"""Base class for Rung 0 bank-statement parsers (architecture §3.3).

A parser reads a DIGITAL bank statement (a PDF with a real text layer) without
any AI. It works from the position of every word on the page, which pdfplumber
gives us:

    {"text": "12,500.00", "x0": 431.2, "x1": 470.9, "top": 312.0, "bottom": 321.0}

Steps for each page:
  1. group words into LINES (words with nearly the same vertical position `top`);
  2. find the column HEADER line ("Date  Description  Reference  Debit ...");
  3. for every line below the header, put each word into the column whose header
     is horizontally closest (amount columns are right-aligned, so we compare
     centres);
  4. a line that STARTS with a date begins a new transaction; a line without a
     date is a continuation (a long description wrapped onto the next line).

A bank-specific parser (parsers/bank/*.py) only DESCRIBES its layout: marker
text, header labels, label names for header fields, date format. All the logic
lives here, so adding a bank is mostly configuration (requirement FR-9.6).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

import pdfplumber

# A date at the start of a transaction line: 30/09/2026, 30-09-26, 30.09.2026, 30-Sep-2026 ...
DATE_START_RE = re.compile(r"^\d{1,2}[/.-](\d{1,2}|[A-Za-z]{3})[/.-]\d{2,4}$")
AMOUNT_RE = re.compile(r"^\(?-?[\d,]+\.\d{2}\)?(Cr|Dr|CR|DR)?$")


@dataclass
class ColumnSpec:
    key: str      # our field name: "date", "description", "reference", "debit", "credit", "balance"
    label: str    # the header text printed by the bank, e.g. "Withdrawal Amt."


def group_into_lines(words: list[dict], tolerance: float = 3.0) -> list[list[dict]]:
    """Group words into lines by their vertical position, each line sorted left to right."""
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= tolerance:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def line_text(line: list[dict]) -> str:
    """The words of one line joined with spaces."""
    return " ".join(w["text"] for w in line)


class StatementParser:
    """Subclass this for each bank layout (one file per layout VERSION)."""

    parser_id: str = "base"
    bank_name: str = ""
    # Fingerprint: ALL of these must appear on page 1 (case-insensitive).
    markers: list[str] = []
    # Table columns, in printed order.
    columns: list[ColumnSpec] = []
    # Header fields: our field name -> the label printed before the value.
    header_labels: dict[str, str] = {}
    # Label of the line that ends the transaction table (e.g. "Closing Balance").
    table_end_label: str = "Closing Balance"

    # --- fingerprint ----------------------------------------------------------------

    def matches(self, first_page_text: str) -> bool:
        """True if page 1 looks like this layout. Be STRICT: a false match only costs
        a verification call, but a missed match costs a full AI extraction."""
        text = (first_page_text or "").upper()
        needed = self.markers + [c.label for c in self.columns]
        return all(n.upper() in text for n in needed)

    # --- parsing --------------------------------------------------------------------

    def parse(self, data: bytes) -> dict:
        """Return a dict shaped like the BankStatement schema (validated by the caller)."""
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages_lines = [group_into_lines(p.extract_words(x_tolerance=1.5, y_tolerance=2)) for p in pdf.pages]

        # Header fields are searched on ALL pages: most are on page 1, but the
        # closing balance is usually printed after the table on the last page.
        header = self.parse_header([line for lines in pages_lines for line in lines])
        rows: list[dict] = []
        for lines in pages_lines:
            rows.extend(self.parse_rows(lines))
        return {"bank_name": self.bank_name, **header, "transactions": rows}

    def parse_header(self, lines: list[list[dict]]) -> dict:
        """Find 'Label: value' pairs anywhere on page 1."""
        out: dict = {}
        texts = [line_text(line) for line in lines]
        for field, label in self.header_labels.items():
            for t in texts:
                idx = t.upper().find(label.upper())
                if idx != -1:
                    value = t[idx + len(label):].lstrip(" :").strip()
                    out[field] = value or None
                    break
        # "Statement Period: 01/09/2026 to 30/09/2026" -> two dates
        period = out.pop("period", None)
        if period and " to " in period:
            out["period_from"], out["period_to"] = (p.strip() for p in period.split(" to ", 1))
        return out

    def _header_line(self, lines: list[list[dict]]) -> tuple[int, dict[str, float]] | None:
        """Locate the column header line; return (line index, {column key: centre x})."""
        for i, line in enumerate(lines):
            text = line_text(line).upper()
            if all(c.label.upper() in text for c in self.columns):
                centres: dict[str, float] = {}
                for col in self.columns:
                    first_word = col.label.split()[0].upper()
                    for w in line:
                        if w["text"].upper() == first_word:
                            # Use the centre of the whole (possibly multi-word) label.
                            n = len(col.label.split())
                            idx = line.index(w)
                            span = line[idx: idx + n]
                            centres[col.key] = (span[0]["x0"] + span[-1]["x1"]) / 2
                            break
                if len(centres) == len(self.columns):
                    return i, centres
        return None

    def parse_rows(self, lines: list[list[dict]]) -> list[dict]:
        """Transaction rows of one page: lines below the column header, up to the table's end
        label. A line starting with a date opens a row; other lines continue its description.
        """
        found = self._header_line(lines)
        if not found:
            return []                      # a page without a table (e.g. a summary page)
        start, centres = found
        rows: list[dict] = []
        for line in lines[start + 1:]:
            if self.table_end_label.upper() in line_text(line).upper():
                break
            cells: dict[str, list[str]] = {k: [] for k in centres}
            for w in line:
                centre = (w["x0"] + w["x1"]) / 2
                # Amounts must land in an amount column; text goes to the nearest column.
                key = min(centres, key=lambda k: abs(centres[k] - centre))
                cells[key].append(w["text"])
            first = line[0]["text"]
            if DATE_START_RE.match(first):
                rows.append({k: " ".join(v) or None for k, v in cells.items()})
            elif rows:
                # Continuation line: a wrapped description.
                extra = " ".join(" ".join(v) for v in cells.values() if v).strip()
                if extra:
                    rows[-1]["description"] = f"{rows[-1].get('description') or ''} {extra}".strip()
        return rows
