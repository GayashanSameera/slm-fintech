"""Generic bank-statement parser: reads most digital statement layouts without bank-specific code.

Bank-specific parsers (parsers/bank/*.py) describe one exact layout. This parser instead
RECOGNISES the transaction table from the column titles banks commonly use, so a statement
from a bank we have never seen can still be read by code (exact, cheap) instead of AI. It is
tried LAST, after every bank-specific parser (parsers/fingerprint.py).

Nothing it produces is trusted on its own: like every Rung 0 result it must pass the
arithmetic checks, the coverage checks, the row-by-row text reconciliation and the AI
verification (parsers/__init__.py). And whenever the layout is AMBIGUOUS (a wrapped
description line that could belong to two rows, two tables in one file, ...) the parser
DECLINES: it returns no rows, and the statement goes to AI. It never guesses.

How it reads a statement:
  1. Words with their positions (pdfplumber), without watermarks, legacy-script fonts and
     symbol glyphs (base.page_words).
  2. The COLUMN-TITLE line: the first line whose words name a date, a balance and an amount
     column (COLUMN_TITLES). Its geometry is reused on every later page, because many banks
     print the titles only once.
  3. The HEADER above the table: account number, holder, branch, period, opening and closing
     balance, wherever their values sit relative to their labels (right, below or above).
  4. The ROWS: a line with a date in the date column and at least one amount starts a row;
     lines in the description/reference columns that are vertically chained to it are its
     wrapped description (above and/or below it, since cells can be vertically centred).

What real statements throw at a parser, and how this one copes:
  * Different titles ("Txn Date", "Narration", "Money out", "Libellé", "Solde"): COLUMN_TITLES.
  * Bilingual titles ("Date / Fecha", or a Sinhala/Hindi word beside the English one): words
    that aren't titles join the nearest title.
  * Titles only on page 1; every page with its own header and footer: geometry reused; header
    and footer lines are never inside a row's block.
  * Wrapped descriptions above/below the date line: vertical chaining (see _assign_extras).
  * One "Amount" column with a Type / Dr-Cr column, "(1,234.00)" or a sign: _finish_row.
  * "—" or "0.00" in an empty amount cell: empty.
  * Dates "01 Aug 2026", "01 Aug" (no year), "01.08.2026", "02/08": lexicon.leading_date, with
    the year taken from the statement period.
  * European amounts "10.481,44": lexicon.parse_amount.
  * Day headings ("MONDAY, 10 AUGUST 2026"), B/F lines, "Opening Balance" rows, "Total" lines.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from statistics import median

import pdfplumber

from app.core.logging import get_logger

from .base import StatementParser, group_into_lines, line_text, page_words
from .lexicon import (
    CLOSING_LINE,
    EMPTY_CELL,
    MONTHS,
    OPENING_LINE,
    TABLE_END,
    as_text,
    find_period,
    fold,
    is_money,
    leading_date,
    parse_amount,
)

log = get_logger(__name__)

# --- column titles -----------------------------------------------------------------------------
# Accent-free, lower case (see _key). Multi-word titles are matched before single words, so
# "value date" wins over "date" and "closing balance" over "balance".
COLUMN_TITLES: dict[str, list[str]] = {
    "date": ["transaction date", "txn date", "tran date", "trans date", "trn date", "posting date", "post date",
             "booking date", "entry date", "txn dt", "tran dt", "date", "fecha", "datum"],
    "value_date": ["value date", "value dt", "val date", "fecha valor", "date de valeur", "date valeur"],
    "description": ["transaction details", "transaction description", "transaction remarks", "transaction narration",
                    "narration", "narrative", "particulars", "description", "details", "remarks", "activity",
                    "libelle", "operation", "operacion", "concepto", "detalle"],
    "reference": ["reference no", "reference number", "reference id", "ref no", "ref number", "chq/ref no",
                  "chq/ref", "chq no", "cheque no", "cheque number", "instrument no", "instrument id",
                  "reference", "referencia", "ref"],
    "debit": ["withdrawal amt", "withdrawal amount", "withdrawals", "withdrawal", "debit amount", "debits", "debit",
              "dr amount", "money out", "paid out", "payments", "debe", "cargo", "cargos", "debito", "dr"],
    "credit": ["deposit amt", "deposit amount", "deposits", "deposit", "credit amount", "credits", "credit",
               "cr amount", "money in", "paid in", "receipts", "haber", "abono", "abonos", "credito", "cr"],
    "amount": ["transaction amount", "amount", "amt", "montant", "importe"],
    "drcr": ["dr / cr", "dr/cr", "cr/dr", "d/c", "c/d", "txn type", "transaction type", "type"],
    "balance": ["closing balance", "running balance", "available balance", "ledger balance", "daily balance",
                "balance amount", "running bal", "balance", "bal", "solde", "saldo"],
}
AMOUNT_FIELDS = ("debit", "credit", "amount", "balance")
DATE_FIELDS = ("date", "value_date")
TEXT_FIELDS = ("description", "reference")

# Currency words printed next to amounts ("LKR 1,234.00"): not part of any description.
CURRENCY_WORDS = {"lkr", "inr", "rs", "usd", "eur", "gbp", "aud", "sgd", "aed", "€", "$", "£", "₹"}

# What a Type / Dr-Cr cell says.
DEBIT_WORDS = {"dr", "d", "debit", "withdrawal", "wd", "db", "debito", "debe", "cargo", "out"}
CREDIT_WORDS = {"cr", "c", "credit", "deposit", "dep", "credito", "haber", "abono", "in"}

# Lines inside the table area that are never part of a row.
NOT_ROW = re.compile(r"(?i)^(page\s+\d+|continued|\(continued\)|(monday|tuesday|wednesday|thursday|friday|"
                     r"saturday|sunday)\b)")


def _key(text: str) -> str:
    """A word as compared with the title lists: accent-free, lower case, letters/digits/'/'."""
    return re.sub(r"[^a-z0-9/]", "", fold(text))


_TITLE_PHRASES = sorted(((f, [_key(w) for w in p.split()]) for f, ps in COLUMN_TITLES.items() for p in ps),
                        key=lambda fp: -len(fp[1]))


class Decline(Exception):
    """The layout is ambiguous: give the statement to AI instead of guessing. The message is a
    short reason code (never document content), logged for whoever tunes the parser."""


# --- the column-title line -----------------------------------------------------------------------

@dataclass
class Column:
    field: str          # one of COLUMN_TITLES' keys, or "other" (a column we don't read)
    x0: float
    x1: float

    @property
    def centre(self) -> float:
        return (self.x0 + self.x1) / 2


def _height(words: list[dict]) -> float:
    """Typical text height (≈ font size) of some words."""
    return median(w["bottom"] - w["top"] for w in words) if words else 8.0


def _phrases(keys: list[str], phrases, words: list[dict] | None = None) -> list[tuple[int, int, str]]:
    """(start, end, field) of every phrase found in a list of word keys, longest phrase first.
    With `words` (positions), the words of one phrase must be close together: "ACCOUNT" and
    "STATEMENT" printed 200 points apart are two separate things, not "account statement"."""
    close = 1.5 * _height(words) if words else None
    found, i = [], 0
    while i < len(keys):
        for fld, phrase in phrases:
            n = len(phrase)
            if keys[i:i + n] == phrase and (close is None or all(
                    words[k + 1]["x0"] - words[k]["x1"] < close for k in range(i, i + n - 1))):
                found.append((i, i + n, fld))
                i += n
                break
        else:
            i += 1
    return found


def _title_matches(keys: list[str], words: list[dict] | None = None) -> list[tuple[int, int, str]]:
    """Column titles in a list of word keys (see _phrases)."""
    return _phrases(keys, _TITLE_PHRASES, words)


def _complete(fields: set[str]) -> bool:
    """A transaction table needs a date, a balance and at least one amount column."""
    return "date" in fields and "balance" in fields and bool({"debit", "credit", "amount"} & fields)


def find_columns(line: list[dict]) -> list[Column] | None:
    """If `line` is the column-title line of a transaction table, its columns (left to right).

    Words that aren't titles ("(LKR)", "/", "Fecha", a word in another script) join the
    neighbouring title on the side of the SMALLER gap (titles of one column are printed close
    together; columns are separated by wider gaps). A group of unknown words with a wide gap
    on both sides is a column we don't read ("Channel", "Sr No"): kind "other".
    """
    if len(line) < 3:
        return None
    keys = [_key(w["text"]) for w in line]
    matches = _title_matches(keys, line)
    if not _complete({m[2] for m in matches}):
        return None
    wide = 0.9 * _height(line)                      # a gap wider than this separates columns
    gap = [line[j]["x0"] - line[j - 1]["x1"] for j in range(1, len(line))]   # gap[j-1]: before word j

    owner: list[int | None] = [None] * len(line)    # word index -> group index
    groups: list[str] = []
    prev_end = None
    for s, e, fld in matches:
        # "Opération / Operación", "Débit / Debe": the same field twice, close together, is ONE
        # bilingual title (the words between them join it too).
        if groups and groups[-1] == fld and all(gap[k - 1] <= wide for k in range(prev_end, s + 1)):
            for j in range(prev_end, e):
                owner[j] = len(groups) - 1
        else:
            groups.append(fld)
            for j in range(s, e):
                owner[j] = len(groups) - 1
        prev_end = e
    j = 0
    while j < len(line):                             # each run of unknown words
        if owner[j] is not None:
            j += 1
            continue
        a = j
        while j < len(line) and owner[j] is None:
            j += 1
        b = j                                       # run = words a..b-1
        left = owner[a - 1] if a > 0 else None
        right = owner[b] if b < len(line) else None
        g_left = gap[a - 1] if left is not None else float("inf")
        g_right = gap[b - 1] if right is not None else float("inf")
        if g_left > wide and g_right > wide:
            groups.append("other")                  # a column of its own
            for k in range(a, b):
                owner[k] = len(groups) - 1
            continue
        # Split the run at its widest gap: words before it join the left title, after it the right.
        if left is None:
            cut = a
        elif right is None:
            cut = b
        else:
            cuts = [(g_left, a)] + [(gap[k - 1], k) for k in range(a + 1, b)] + [(g_right, b)]
            cut = max(cuts)[1]
        for k in range(a, b):
            owner[k] = left if k < cut else right

    columns: list[Column] = []
    for g, fld in enumerate(groups):
        members = [line[k] for k in range(len(line)) if owner[k] == g]
        if members:
            columns.append(Column(fld, min(w["x0"] for w in members), max(w["x1"] for w in members)))
    columns.sort(key=lambda c: c.x0)
    # The same field twice: a second date column is the value date; anything else is ignored.
    seen: set[str] = set()
    for c in columns:
        if c.field in seen:
            c.field = "value_date" if c.field == "date" and "value_date" not in seen else "other"
        seen.add(c.field)
    return columns if _complete({c.field for c in columns}) else None


# --- header fields ----------------------------------------------------------------------------------
HEADER_LABELS: dict[str, list[str]] = {
    "account_number": ["account number", "account no", "account num", "a/c no", "a/c number", "acct no",
                       "acct number", "account #", "acc no", "numero de compte", "n de compte", "numero de cuenta",
                       "account", "number", "compte", "cuenta"],
    "account_holder": ["account holder name", "name of account holder", "account holder", "account name",
                       "customer name", "titulaire du compte", "titulaire du", "titulaire", "titular", "holder",
                       "name", "nom"],
    "branch": ["branch name", "home branch", "branch", "agence", "sucursal"],
    "period": ["statement period", "transaction period", "period of statement", "statement dates", "statement from",
               "period", "periode", "periodo"],
    "opening_balance": ["opening balance", "opening bal", "balance b/f", "solde initial", "saldo inicial", "opening"],
    "closing_balance": ["closing balance", "closing bal", "solde final", "saldo final", "closing"],
    # Labels of fields we don't read: they END the value of the label before them.
    "_other": ["account type", "account statement", "account summary", "account services", "account currency",
               "total credits", "total debits", "total deposits", "total withdrawals", "credits", "debits",
               "deposits", "withdrawals", "currency", "devise", "moneda", "statement date", "ifsc code", "ifsc",
               "customer id", "address", "phone", "email", "type", "balance", "page"],
}
_LABEL_PHRASES = sorted(((f, [_key(w) for w in p.split()]) for f, ps in HEADER_LABELS.items() for p in ps),
                        key=lambda fp: -len(fp[1]))
IFSC_RE = re.compile(r"\b([A-Z]{4}0[A-Z0-9]{6})\b")
_ACCOUNT_TOKEN = re.compile(r"^[A-Za-z0-9*•xX][A-Za-z0-9*•xX\-/]{5,33}$")   # 6+ chars, 4+ digits


@dataclass
class Label:
    field: str
    line: int            # index into the header lines
    start: int           # word indexes on that line
    end: int
    x0: float
    x1: float
    top: float
    bottom: float

    @property
    def words(self) -> int:
        return self.end - self.start


def _labels(lines: list[list[dict]]) -> list[Label]:
    """Every header label on the given lines, in reading order."""
    out = []
    for li, line in enumerate(lines):
        for s, e, fld in _phrases([_key(w["text"]) for w in line], _LABEL_PHRASES, line):
            out.append(Label(fld, li, s, e, line[s]["x0"], line[e - 1]["x1"],
                             min(w["top"] for w in line[s:e]), max(w["bottom"] for w in line[s:e])))
    return out


def _accept(fld: str, words: list[dict]):
    """The value of header field `fld` in these words, or None if they don't hold one."""
    texts = [w["text"].strip(":|") for w in words]
    while texts and texts[0] in ("", ":", "-", "–", "—", "|", "/", "•"):    # "ACCOUNT HOLDER / Name"
        texts = texts[1:]
    texts = [t for t in texts if t]
    if not texts:
        return None
    if fld == "account_number":
        for t in texts:
            if (_ACCOUNT_TOKEN.match(t) and sum(ch.isdigit() for ch in t) >= 4
                    and not is_money(t) and not leading_date([t])):
                return t
        return None
    if fld in ("opening_balance", "closing_balance"):
        for t in texts:
            if is_money(t):
                return as_text(parse_amount(t))
        return None
    if fld == "period":
        return find_period(" ".join(texts), allow_month_only=True)
    # Holder / branch: a short piece of text; not a date or an amount, and no word with 4+
    # digits (that is an account or phone number, not a name).
    text = " ".join(texts)
    if (not re.search(r"[A-Za-z]", text) or len(texts) > 10 or any(is_money(t) for t in texts)
            or any(sum(ch.isdigit() for ch in t) >= 4 for t in texts)
            or leading_date(texts) or find_period(text)):
        return None
    return text


def _cell_near(line: list[dict], x0: float, x1: float, skip: set[int], h: float) -> list[dict]:
    """The words of `line` that form the "cell" under/over the x-range [x0, x1]: the word
    nearest the range's centre, extended left and right while words are close together."""
    pad = 0.5 * (x1 - x0)
    hits = [i for i, w in enumerate(line) if i not in skip and w["x1"] >= x0 - pad and w["x0"] <= x1 + pad]
    if not hits:
        return []
    centre = (x0 + x1) / 2
    i = min(hits, key=lambda k: abs((line[k]["x0"] + line[k]["x1"]) / 2 - centre))
    a = b = i
    while a > 0 and a - 1 not in skip and line[a]["x0"] - line[a - 1]["x1"] < 1.5 * h:
        a -= 1
    while b + 1 < len(line) and b + 1 not in skip and line[b + 1]["x0"] - line[b]["x1"] < 1.5 * h:
        b += 1
    return line[a:b + 1]


def read_header(lines: list[list[dict]]) -> dict:
    """Header fields from the lines above the transaction table.

    A value can be printed:
      * to the RIGHT of its label: "ACCOUNT NUMBER  0012...". The label may be a two-line
        caption that straddles the value's line ("OPENING" / value / "BALANCE"), so the value
        is looked for on every line that vertically overlaps the label;
      * BELOW it: a summary table, labels on one line, values on the next;
      * ABOVE it: a value line followed by a small caption line ("HOLDER   PERIOD").
    For each label those places are tried in that order, and the first value with the right
    form for the field wins. Multi-word labels ("ACCOUNT NUMBER") are tried before single
    words ("Account"), which are vaguer. Dates come back as `date`, amounts as "1234.50".
    """
    labels = _labels(lines)
    label_at = {(lab.line, k): lab for lab in labels for k in range(lab.start, lab.end)}
    label_words = {}
    for lab in labels:
        label_words.setdefault(lab.line, set()).update(range(lab.start, lab.end))
    h = _height([w for line in lines for w in line])

    out: dict = {}
    for lab in sorted(labels, key=lambda lab: (-lab.words, lab.line, lab.start)):
        if lab.field == "_other" or out.get(lab.field) is not None:
            continue
        value = _accept(lab.field, _right_of(lab, lines, label_at, h))
        if value is None:
            top = lines[lab.line][0]["top"]
            neighbours = [li for li in (lab.line + 1, lab.line + 2)
                          if li < len(lines) and 0 < lines[li][0]["top"] - top <= 6 * h]
            if lab.line > 0 and 0 < top - lines[lab.line - 1][0]["top"] <= 3 * h:
                neighbours.append(lab.line - 1)
            for li in neighbours:
                cell = _cell_near(lines[li], lab.x0, lab.x1, label_words.get(li, set()), h)
                value = _accept(lab.field, cell)
                if value is not None:
                    break
        if value is not None:
            out[lab.field] = value

    period = out.pop("period", None)
    if period is None:                              # no labelled period: any date range up top
        for line in lines:
            period = find_period(line_text(line))
            if period:
                break
    if period:
        out["period_from"], out["period_to"] = period
    m = IFSC_RE.search("\n".join(line_text(line) for line in lines))
    if m:
        out["ifsc"] = m.group(1)
    return out


def _right_of(lab: Label, lines: list[list[dict]], label_at: dict, h: float) -> list[dict]:
    """The words to the right of a label, on its own line or a line that vertically overlaps it,
    up to the next label of ANOTHER field ("BRANCH Digital Branch CURRENCY": the second "Branch"
    belongs to the value)."""
    found = []
    for li in range(max(0, lab.line - 2), min(len(lines), lab.line + 3)):
        for wi, w in enumerate(lines[li]):
            overlap = min(w["bottom"], lab.bottom) - max(w["top"], lab.top)
            if w["x0"] >= lab.x1 - 1 and overlap >= 0.3 * h:
                found.append((w["x0"], li, wi, w))
    found.sort(key=lambda f: f[0])
    out = []
    for _, li, wi, w in found:
        other = label_at.get((li, wi))
        if other is not None and other.field != lab.field:
            break
        out.append(w)
    return out


# --- rows -----------------------------------------------------------------------------------------------

@dataclass
class Row:
    top: float
    date: date
    cells: dict[str, list[dict]]
    above: list[list[dict]] = field(default_factory=list)
    below: list[list[dict]] = field(default_factory=list)


class Table:
    """The column geometry of one transaction table, and how to route a word to a column."""

    def __init__(self, columns: list[Column], table_lines: list[list[dict]]):
        self.columns = columns
        tol = 2.0
        self.lefts = [c.x0 - tol for c in columns]
        self.amounts = [c for c in columns if c.field in AMOUNT_FIELDS]
        self.dates = [c for c in columns if c.field in DATE_FIELDS]
        text_cols = [c for c in columns if c.field in TEXT_FIELDS]
        date_col = next(c for c in columns if c.field == "date")
        after_date = [c for c in columns if c.x0 > date_col.x0]
        self.text_left = (min(c.x0 for c in text_cols) if text_cols else after_date[0].x0) - tol
        self.anchors = self._calibrate(table_lines)

    def _calibrate(self, table_lines: list[list[dict]]) -> tuple[str, list[float]] | None:
        """Where the amounts of each amount column really are.

        Titles don't tell us: a title can be left-aligned over right-aligned figures, so the
        figures of "Debit" can end closer to the "Credit" title than to their own. The figures
        of one column, however, line up: right-aligned columns share their right edge,
        left-aligned ones their left edge, centred ones their centre. So we look at every
        amount in the table and find, for one of those three edges, exactly as many tight
        clusters as there are amount columns. Returns (edge, cluster positions left to right),
        or None (then amounts are routed by the column areas, see route()).
        """
        money = [w for line in table_lines for w in line if is_money(w["text"])]
        if len(money) < 2 * len(self.amounts):
            return None
        for edge in ("x1", "x0", "centre"):
            values = sorted((w["x0"] + w["x1"]) / 2 if edge == "centre" else w[edge] for w in money)
            clusters: list[list[float]] = [[values[0]]]
            for v in values[1:]:
                if v - clusters[-1][-1] <= 3.0:
                    clusters[-1].append(v)
                else:
                    clusters.append([v])
            big = [c for c in clusters if len(c) >= max(3, 0.05 * len(values))]
            # Every amount must belong to one of the big clusters, and there must be one per column.
            if len(big) == len(self.amounts) and sum(len(c) for c in big) >= 0.95 * len(values):
                return edge, [median(c) for c in big]
        return None

    def amount_column(self, w: dict) -> str:
        """The amount column of a money token."""
        if self.anchors:
            edge, positions = self.anchors
            v = (w["x0"] + w["x1"]) / 2 if edge == "centre" else w[edge]
            i = min(range(len(positions)), key=lambda k: abs(positions[k] - v))
            if abs(positions[i] - v) <= 6:
                return self.amounts[i].field
        # No calibration (or an odd one out): the amount column whose area holds the token's
        # centre; left of every amount column, the first one.
        centre = (w["x0"] + w["x1"]) / 2
        chosen = self.amounts[0]
        for c in self.amounts:
            if centre >= c.x0 - 2.0:
                chosen = c
        return chosen.field

    def zone(self, w: dict) -> str:
        """The column whose area contains the word's left edge."""
        idx = 0
        for i, left in enumerate(self.lefts):
            if w["x0"] >= left:
                idx = i
        return self.columns[idx].field

    def route(self, line: list[dict]) -> dict[str, list[dict]]:
        """Assign every word of a table line to a column: amounts to their amount column
        (amount_column), a one-word date to the nearest date column, other words to the column
        area they start in. A "Cr"/"Dr" right after an amount marks that amount (key
        "_mark_<column>")."""
        cells: dict[str, list[dict]] = {}
        last_amount: tuple[str, dict] | None = None
        for w in line:
            t = w["text"]
            if is_money(t):
                f = self.amount_column(w)
                cells.setdefault(f, []).append(w)
                last_amount = (f, w)
                continue
            if last_amount and fold(t) in ("cr", "dr") and w["x0"] - last_amount[1]["x1"] < 6:
                cells.setdefault(f"_mark_{last_amount[0]}", []).append(w)
                continue
            last_amount = None
            if self.dates and len(t) >= 5 and leading_date([t]):
                c = min(self.dates, key=lambda c: abs((w["x0"] + w["x1"]) / 2 - c.centre))
                cells.setdefault(c.field, []).append(w)
                continue
            cells.setdefault(self._text_column(w), []).append(w)
        return cells

    def _text_column(self, w: dict) -> str:
        """The column of a word that is neither an amount nor a one-word date. Usually the column
        area it starts in; but a long description can run past the next title's left edge, so a
        word that can't belong to the column it lands in (a non-date word in the value-date
        area, a word in an amount area) belongs to the text column on its left."""
        f = self.zone(w)
        t = fold(w["text"]).strip(".,")
        date_part = t.isdigit() and len(t) <= 4 or t in MONTHS
        misplaced = (f == "value_date" and not date_part) or (
            f in AMOUNT_FIELDS and w["text"] not in EMPTY_CELL and t not in CURRENCY_WORDS)
        if misplaced:
            left = [c for c in self.columns if c.field in TEXT_FIELDS and c.x0 <= w["x0"]]
            if left:
                return left[-1].field
        return f


def _texts(words: list[dict]) -> list[str]:
    return [w["text"] for w in words]


class GenericStatementParser(StatementParser):
    """Layout-independent statement reader (see the module docstring)."""

    parser_id = "generic_v1"
    bank_name = ""

    # --- fingerprint ---------------------------------------------------------------------------
    def matches(self, first_pages_text: str) -> bool:
        """True if some line of the first pages' text names a date, a balance and an amount column.
        Cheap and lenient on purpose: parse() does the real, position-based check."""
        lines = (first_pages_text or "").splitlines()
        for i in range(len(lines)):
            for span in (1, 2, 3):                  # a title row can be broken over 2-3 text lines
                keys = [_key(t) for t in " ".join(lines[i:i + span]).split()]
                if _complete({m[2] for m in _title_matches(keys)}):
                    return True
        return False

    # --- parsing ---------------------------------------------------------------------------------
    def parse(self, data: bytes) -> dict:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            # Words in other scripts are kept only to locate the column titles (see page_words).
            with_scripts = [group_into_lines(page_words(p, keep_other_scripts=True)) for p in pdf.pages]
        pages = [[kept for line in page if (kept := [w for w in line if not w.get("script")])]
                 for page in with_scripts]
        try:
            return self._parse(pages, with_scripts)
        except Decline as reason:
            log.info("generic_parser_declined", reason=str(reason))
            # "_declined" is read by try_rung0 (and dropped by the schema): without a transaction
            # table ("no_column_titles") the document may not be a bank statement at all.
            return {"bank_name": None, "transactions": [], "_declined": str(reason)}

    def _parse(self, pages: list[list[list[dict]]], with_scripts: list[list[list[dict]]]) -> dict:
        """`pages`: lines of words per page; `with_scripts`: the same with other-script words."""
        found = None
        for pno, lines in enumerate(with_scripts):
            for line in lines:
                cols = find_columns(line)
                if cols:
                    found = (pno, line[0]["top"], cols)
                    break
            if found:
                break
        if not found:
            raise Decline("no_column_titles")
        pno, title_top, columns = found
        # The same line in `pages` (other-script words removed): the table starts below it.
        lno = max(i for i, ln in enumerate(pages[pno]) if ln[0]["top"] <= title_top + 1.0)
        start = (pno, lno, columns)

        # Header: everything above the table (pages before it, and the title page above the titles).
        header = read_header([ln for p in pages[:pno] for ln in p] + pages[pno][:lno])
        hint: date | None = header.get("period_to")

        table_lines = pages[pno][lno + 1:] + [ln for p in pages[pno + 1:] for ln in p]
        rows, opening_row = self._rows(pages, Table(columns, table_lines), start, hint)
        if not rows:
            raise Decline("no_rows")

        # The opening balance from the header; else the B/F / "Opening Balance" line. Never
        # derived from the first row: that would make the first running-balance check circular.
        opening = header.get("opening_balance") or opening_row
        closing = header.get("closing_balance") or rows[-1].get("balance")
        return {
            "bank_name": None,
            "account_holder": header.get("account_holder"),
            "account_number": header.get("account_number"),
            "ifsc": header.get("ifsc"),
            "branch": header.get("branch"),
            "period_from": header["period_from"].isoformat() if header.get("period_from") else None,
            "period_to": header["period_to"].isoformat() if header.get("period_to") else None,
            "opening_balance": opening,
            "closing_balance": closing,
            "transactions": rows,
        }

    def _rows(self, pages, table: Table, start, hint: date | None) -> tuple[list[dict], str | None]:
        """All transaction rows, in order, and the opening balance printed as a B/F line or an
        "Opening Balance" row (None if there is none)."""
        pno0, lno0, _ = start
        rows: list[dict] = []
        opening: str | None = None
        ended = False
        for pno in range(pno0, len(pages)):
            lines = pages[pno][lno0 + 1:] if pno == pno0 else pages[pno]
            if not lines:
                continue
            h = _height([w for ln in lines for w in ln])
            items: list[tuple[str, float, object]] = []   # ("row" | "extra" | "break", top, payload)
            for line in lines:
                folded = fold(line_text(line).strip())
                first = line[0]
                if find_columns(line):                    # titles repeated on this page
                    items.append(("break", first["top"], None))
                    continue
                cells = table.route(line)
                date_words = cells.get("date", [])
                found = leading_date(_texts(date_words), hint) if date_words else None
                has_amount = any(cells.get(f) for f in AMOUNT_FIELDS)
                description = fold(" ".join(_texts(cells.get("description", []))))
                any_rows = bool(rows) or any(k == "row" for k, _, _ in items)

                if found and has_amount:                  # a transaction line (or a balance line)
                    if OPENING_LINE.match(description):
                        if any_rows:
                            raise Decline("second_opening_balance")    # a second account's table
                        opening = self._only_amount(cells)
                        items.append(("break", first["top"], None))
                        continue
                    if CLOSING_LINE.match(description):
                        ended = True
                        items.append(("break", first["top"], None))
                        continue
                    if ended:
                        raise Decline("rows_after_table_end")
                    items.append(("row", first["top"], Row(first["top"], found[0], cells)))
                    continue
                if OPENING_LINE.match(folded):            # "B/F 1,234.00" without a date
                    if any_rows:
                        raise Decline("second_opening_balance")
                    opening = self._only_amount(cells)
                    items.append(("break", first["top"], None))
                    continue
                if TABLE_END.match(folded) and (first["x0"] < table.text_left or has_amount):
                    ended = True
                    items.append(("break", first["top"], None))
                    continue
                if NOT_ROW.match(folded):
                    items.append(("break", first["top"], None))
                    continue
                # A wrapped description line: only words in the description/reference columns,
                # no amounts, starting in those columns.
                zones = {table.zone(w) for w in line}
                if (first["x0"] >= table.text_left and not has_amount
                        and zones <= {"description", "reference", "other"}):
                    items.append(("extra", first["top"], line))
                else:
                    items.append(("break", first["top"], None))
            # After the table's end the rest of the document is still read: rows there would mean
            # a second table (another account), which this parser does not handle (Decline above).
            rows.extend(self._finish_row(r, table) for r in self._assign_extras(items, h))
        self._fill_sides_from_balance(rows, opening)
        return rows, opening

    @staticmethod
    def _assign_extras(items: list[tuple[str, float, object]], h: float) -> list[Row]:
        """Attach wrapped description lines to their rows.

        Lines closer together than a line pitch form a BLOCK (a "break" line ends a block).
          * a block with one row: all its extra lines belong to that row;
          * a block with several rows: each extra line goes to the clearly nearest row; if it is
            about as near to two rows, we can't know which one it belongs to -> Decline;
          * a block without a row: page header/footer text; it is ignored, unless it sits
            BETWEEN two rows of the page (unexplained text inside the table -> Decline).
        """
        pitch = 1.7 * h                      # consecutive lines of one cell are closer than this
        blocks: list[list[tuple[str, float, object]]] = []
        prev_top = None
        for kind, top, payload in items:
            if kind == "break":
                prev_top = None
                blocks.append([])
                continue
            if prev_top is None or top - prev_top > pitch or not blocks:
                blocks.append([])
            blocks[-1].append((kind, top, payload))
            prev_top = top
        blocks = [b for b in blocks if b]
        row_tops = [top for b in blocks for kind, top, _ in b if kind == "row"]
        out: list[Row] = []
        for block in blocks:
            rows = [p for kind, _, p in block if kind == "row"]
            extras = [(top, p) for kind, top, p in block if kind == "extra"]
            if not rows:
                tops = [t for t, _ in extras]
                if row_tops and min(row_tops) < min(tops) and max(tops) < max(row_tops):
                    raise Decline("text_between_rows")
                continue
            for top, line in extras:
                dists = sorted(((abs(r.top - top), i) for i, r in enumerate(rows)))
                if len(dists) > 1 and dists[0][0] > 0.6 * dists[1][0]:
                    raise Decline("ambiguous_wrapped_line")
                row = rows[dists[0][1]]
                (row.above if top < row.top else row.below).append(line)
            out.extend(rows)
        out.sort(key=lambda r: r.top)
        return out

    @staticmethod
    def _only_amount(cells: dict[str, list[dict]]) -> str | None:
        """The amount on a B/F or "Opening Balance" line (the balance column if it is there)."""
        for f in ("balance", "credit", "debit", "amount"):
            money = [w["text"] for w in cells.get(f, []) if is_money(w["text"])]
            if money:
                return as_text(parse_amount(money[-1]))
        return None

    def _finish_row(self, row: Row, table: Table) -> dict:
        """One transaction from its cells and wrapped lines: description and reference in reading
        order (lines above the date line, the date line, lines below), and the amounts."""
        cells = row.cells
        parts: dict[str, list[str]] = {"description": [], "reference": []}
        for line in sorted(row.above, key=lambda ln: ln[0]["top"]):
            for w in line:
                if table.zone(w) in parts:
                    parts[table.zone(w)].append(w["text"])
        for f in parts:
            parts[f].extend(_texts(cells.get(f, [])))
        for line in sorted(row.below, key=lambda ln: ln[0]["top"]):
            for w in line:
                if table.zone(w) in parts:
                    parts[table.zone(w)].append(w["text"])
        out: dict = {"date": row.date.isoformat()}
        for f, words in parts.items():
            # A cell holding only "—" is empty; a "-" INSIDE a description ("Fee - CityMart") is kept.
            out[f] = None if all(t in EMPTY_CELL for t in words) else " ".join(words)

        values: dict[str, Decimal | None] = {}
        for f in AMOUNT_FIELDS:
            money = [w for w in cells.get(f, []) if is_money(w["text"])]   # not the "—" of an empty cell
            if len(money) > 1:
                raise Decline("two_amounts_in_one_cell")
            values[f] = parse_amount(money[0]["text"]) if money else None

        debit = abs(values["debit"]) if values["debit"] is not None else None
        credit = abs(values["credit"]) if values["credit"] is not None else None
        if values["amount"] is not None:
            # One amount column: the side comes from the Type / Dr-Cr cell, a Cr/Dr suffix, or the
            # sign ("(1,234.00)" or "-1,234.00" is money out). Unsigned and unmarked: decided later
            # from the balance movement (_fill_sides_from_balance).
            kind = fold(" ".join(_texts(cells.get("drcr", [])))).strip(". ")
            kind = kind.split()[0] if kind else ""
            mark = fold(" ".join(_texts(cells.get("_mark_amount", []))))
            amount = abs(values["amount"])
            if kind in DEBIT_WORDS or mark == "dr":
                debit = amount
            elif kind in CREDIT_WORDS or mark == "cr":
                credit = amount
            elif values["amount"] < 0:
                debit = amount
            else:
                out["_unsigned"] = as_text(amount)
        out["debit"] = as_text(debit) if debit else None       # 0.00 means "nothing in this column"
        out["credit"] = as_text(credit) if credit else None
        out["balance"] = as_text(values["balance"])
        return out

    @staticmethod
    def _fill_sides_from_balance(rows: list[dict], opening: str | None) -> None:
        """An unsigned amount in a single Amount column: money in if the balance went up by
        exactly that amount, money out if it went down by it (otherwise left empty, and the
        running-balance check rejects the statement)."""
        previous = Decimal(opening) if opening else None
        for row in rows:
            amount = row.pop("_unsigned", None)
            if amount is not None and previous is not None and row.get("balance") is not None:
                delta = Decimal(row["balance"]) - previous
                if abs(abs(delta) - Decimal(amount)) <= Decimal("0.01"):
                    row["credit" if delta > 0 else "debit"] = amount
            if row.get("balance") is not None:
                previous = Decimal(row["balance"])
