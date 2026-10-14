"""Synthetic labelled documents for the accuracy evaluation (eval/run.py).

Every document is generated FROM a known truth (a dict that matches the extraction schema),
so the evaluation can compare each extracted field with the right answer. Nothing here is real
data: names, numbers and identifiers are invented, but identifiers are VALID (GSTIN and Aadhaar
check digits are computed, PANs use a real holder letter) because real documents have valid ones.

Two conditions per document type:
    "digital"  a PDF with a text layer (the pipeline also checks every value against that text);
    "scan"     a JPEG of the page, rotated a little, noisy and blurred like a phone scan
               (no text layer: only check digits, arithmetic and the second reading can catch errors).

Deterministic: the same seed produces the same documents, so runs are comparable.
"""

from __future__ import annotations

import io
import random
from dataclasses import dataclass
from datetime import date, timedelta

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.modules.extraction.identifiers import gstin_check_char, verhoeff_check_digit
from tests.helpers.simple_pdf import make_pdf

FIRST = ["Asha", "Ravi", "Meera", "Karan", "Divya", "Imran", "Sunita", "Arjun", "Neha", "Vikram"]
LAST = ["Verma", "Sharma", "Iyer", "Khan", "Reddy", "Patel", "Nair", "Singh", "Das", "Mehta"]
COMPANY = ["Demo Traders", "Sample Retail", "Acme Steel", "Bright Foods", "Nova Textiles", "Kiran Logistics"]
CITY = [("Bengaluru", "Karnataka", "560001", "29"), ("Mumbai", "Maharashtra", "400001", "27"),
        ("Chennai", "Tamil Nadu", "600001", "33"), ("Pune", "Maharashtra", "411001", "27"),
        ("Hyderabad", "Telangana", "500001", "36")]
ITEMS = ["Steel brackets", "Installation service", "Copper wire 2mm", "Packing charges", "Freight",
         "Cotton fabric", "Consulting hours", "Machine oil 5L"]


@dataclass
class Case:
    case_id: str
    doc_type: str            # schema name: invoice | payslip | identity_document | kyc_form
    condition: str           # "digital" | "scan"
    data: bytes              # the file to upload
    truth: dict              # the right answer, in schema shape
    prepared: object = None           # preprocessed pages (set by run.py)
    truth_final: dict | None = None   # the truth in the form the pipeline returns (set by run.py: normalised + masked)


# --- identifiers (valid, like real ones) ------------------------------------------------------

def _letters(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ") for _ in range(n))   # no I/O: look like digits


def make_pan(rng: random.Random, holder: str, initial: str) -> str:
    """AAAH I 9999 L : 3 letters, holder type, surname initial (persons), 4 digits, 1 letter."""
    return f"{_letters(rng, 3)}{holder}{initial}{rng.randint(1000, 9999)}{_letters(rng, 1)}"


def make_gstin(state_code: str, pan: str) -> str:
    first14 = f"{state_code}{pan}1Z"
    return first14 + gstin_check_char(first14)


def make_aadhaar(rng: random.Random) -> str:
    payload = str(rng.randint(2, 9)) + "".join(str(rng.randint(0, 9)) for _ in range(10))
    return payload + verhoeff_check_digit(payload)


def _money(x: float) -> str:
    return f"{x:.2f}"


def _fmt(value: str) -> str:
    """'17700.00' -> '17,700.00' as printed on invoices."""
    return f"{float(value):,.2f}"


def _date(rng: random.Random, back_days: int = 400) -> date:
    return date.today() - timedelta(days=rng.randint(10, back_days))


# --- drawing -----------------------------------------------------------------------------------

def _scan(lines: list[tuple[int, int, str, int]], rng: random.Random, hard: bool) -> bytes:
    """Draw (x, y, text, size) lines on A4 at 150 dpi, then make it look like a phone scan."""
    img = Image.new("L", (1240, 1754), 255)
    draw = ImageDraw.Draw(img)
    for x, y, text, size in lines:
        draw.text((x, y), text, fill=20, font=ImageFont.load_default(size=size))
    angle = rng.uniform(-1.2, 1.2) * (2 if hard else 1)
    img = img.rotate(angle, resample=Image.BICUBIC, fillcolor=255)
    img = img.filter(ImageFilter.GaussianBlur(0.9 if hard else 0.5))
    noise = Image.effect_noise(img.size, 22 if hard else 12)                  # grey noise around 128
    img = Image.blend(img, noise, 0.10 if hard else 0.05)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=55 if hard else 72)
    return out.getvalue()


def _pdf(lines: list[tuple[int, int, str, int]]) -> bytes:
    """The same lines as a digital PDF (Helvetica text layer). Coordinates: image px -> PDF points."""
    scale = 595 / 1240
    return make_pdf([[(x * scale, 842 - y * scale - 10, text) for x, y, text, _ in lines]], font_size=9)


def _render(lines, condition: str, rng: random.Random, hard: bool) -> bytes:
    return _pdf(lines) if condition == "digital" else _scan(lines, rng, hard)


# --- the four document types -------------------------------------------------------------------

def invoice(rng: random.Random, condition: str, hard: bool) -> tuple[bytes, dict]:
    city, state, pin, code = rng.choice(CITY)
    seller_pan = make_pan(rng, "C", rng.choice("ABCDEFGHJKLM"))
    buyer_pan = make_pan(rng, "F", rng.choice("ABCDEFGHJKLM"))
    items = []
    for name in rng.sample(ITEMS, rng.randint(2, 4)):
        qty, rate = rng.randint(1, 40), rng.randint(2, 90) * 50
        items.append({"description": name, "hsn_sac": str(rng.randint(1000, 9999)), "quantity": _money(qty),
                      "rate": _money(rate), "amount": _money(qty * rate)})
    subtotal = sum(float(i["amount"]) for i in items)
    half = round(subtotal * 0.09, 2)
    issued = _date(rng)
    truth = {
        "invoice_number": f"INV-{issued.year}-{rng.randint(100, 9999):04d}",
        "invoice_date": issued.isoformat(), "due_date": (issued + timedelta(days=30)).isoformat(),
        "seller": {"name": f"{rng.choice(COMPANY)} Pvt Ltd", "gstin": make_gstin(code, seller_pan), "pan": seller_pan,
                   "address": f"{rng.randint(1, 99)} Main Road, {city} {pin}"},
        "buyer": {"name": f"{rng.choice(COMPANY)} LLP", "gstin": make_gstin(code, buyer_pan)},
        "line_items": items, "subtotal": _money(subtotal), "cgst": _money(half), "sgst": _money(half),
        "total": _money(subtotal + 2 * half),
    }
    lines = [(80, 80, "TAX INVOICE", 40), (80, 160, truth["seller"]["name"], 26),
             (80, 200, f"GSTIN: {truth['seller']['gstin']}   PAN: {seller_pan}", 26),
             (80, 240, truth["seller"]["address"], 26),
             (700, 160, f"Invoice No: {truth['invoice_number']}", 26),
             (700, 200, f"Invoice Date: {issued:%d/%m/%Y}", 26),
             (700, 240, f"Due Date: {issued + timedelta(days=30):%d/%m/%Y}", 26),
             (80, 330, f"Bill To: {truth['buyer']['name']}", 26), (80, 370, f"GSTIN: {truth['buyer']['gstin']}", 26),
             (80, 450, "Description          HSN     Qty      Rate         Amount", 26)]
    y = 500
    for i in items:
        lines.append((80, y, f"{i['description']:<22}{i['hsn_sac']}   {float(i['quantity']):>5.0f}   "
                             f"{_fmt(i['rate']):>10}   {_fmt(i['amount']):>12}", 26))
        y += 44
    y += 40
    lines += [(640, y, f"Subtotal:  {_fmt(truth['subtotal'])}", 26), (640, y + 44, f"CGST 9%:  {_fmt(truth['cgst'])}", 26),
              (640, y + 88, f"SGST 9%:  {_fmt(truth['sgst'])}", 26), (640, y + 140, f"Total:  Rs. {_fmt(truth['total'])}", 34)]
    return _render(lines, condition, rng, hard), truth


def payslip(rng: random.Random, condition: str, hard: bool) -> tuple[bytes, dict]:
    first, last = rng.choice(FIRST), rng.choice(LAST)
    earn = [("Basic", rng.randint(200, 600) * 100), ("HRA", rng.randint(80, 200) * 100),
            ("Special allowance", rng.randint(30, 120) * 100)]
    ded = [("Provident Fund", rng.randint(10, 40) * 100), ("Professional Tax", 200), ("Income Tax", rng.randint(5, 60) * 100)]
    gross, total_ded = sum(a for _, a in earn), sum(a for _, a in ded)
    month = date.today().replace(day=1) - timedelta(days=rng.randint(1, 300))
    truth = {
        "employer_name": f"{rng.choice(COMPANY)} Pvt Ltd", "employee_name": f"{first} {last}",
        "employee_id": f"EMP{rng.randint(1000, 99999)}", "pan": make_pan(rng, "P", last[0]),
        "pay_period": f"{month:%B %Y}",
        "earnings": [{"name": n, "amount": _money(a)} for n, a in earn],
        "deductions": [{"name": n, "amount": _money(a)} for n, a in ded],
        "gross_pay": _money(gross), "total_deductions": _money(total_ded), "net_pay": _money(gross - total_ded),
    }
    lines = [(80, 80, "PAYSLIP", 40), (80, 150, truth["employer_name"], 28),
             (80, 200, f"Pay period: {truth['pay_period']}", 26),
             (80, 250, f"Employee: {truth['employee_name']}     Employee ID: {truth['employee_id']}", 26),
             (80, 290, f"PAN: {truth['pan']}", 26), (80, 360, "EARNINGS", 28)]
    y = 400
    for e in truth["earnings"]:
        lines.append((80, y, f"{e['name']:<24}{_fmt(e['amount']):>14}", 26)); y += 40
    lines.append((80, y + 10, f"Gross pay                {_fmt(truth['gross_pay']):>14}", 26))
    y += 80
    lines.append((80, y, "DEDUCTIONS", 28)); y += 40
    for d in truth["deductions"]:
        lines.append((80, y, f"{d['name']:<24}{_fmt(d['amount']):>14}", 26)); y += 40
    lines.append((80, y + 10, f"Total deductions         {_fmt(truth['total_deductions']):>14}", 26))
    lines.append((80, y + 70, f"NET PAY                  {_fmt(truth['net_pay']):>14}", 32))
    return _render(lines, condition, rng, hard), truth


def identity_pan(rng: random.Random, condition: str, hard: bool) -> tuple[bytes, dict]:
    first, last = rng.choice(FIRST), rng.choice(LAST)
    born = date.today() - timedelta(days=rng.randint(22, 60) * 365 + rng.randint(0, 364))
    truth = {"id_type": "pan", "id_number": make_pan(rng, "P", last[0]), "name": f"{first} {last}".upper(),
             "father_or_spouse_name": f"{rng.choice(FIRST)} {last}".upper(), "date_of_birth": born.isoformat()}
    lines = [(80, 80, "INCOME TAX DEPARTMENT", 34), (80, 130, "GOVT. OF INDIA", 28),
             (80, 220, "Permanent Account Number", 24), (80, 260, truth["id_number"], 40),
             (80, 340, "Name", 22), (80, 372, truth["name"], 30),
             (80, 440, "Father's Name", 22), (80, 472, truth["father_or_spouse_name"], 30),
             (80, 540, "Date of Birth", 22), (80, 572, f"{born:%d/%m/%Y}", 30)]
    return _render(lines, condition, rng, hard), truth


def identity_aadhaar(rng: random.Random, condition: str, hard: bool) -> tuple[bytes, dict]:
    first, last = rng.choice(FIRST), rng.choice(LAST)
    city, state, pin, _ = rng.choice(CITY)
    born = date.today() - timedelta(days=rng.randint(20, 70) * 365 + rng.randint(0, 364))
    number = make_aadhaar(rng)
    truth = {"id_type": "aadhaar", "id_number": f"{number[:4]} {number[4:8]} {number[8:]}",
             "name": f"{first} {last}", "date_of_birth": born.isoformat(), "gender": rng.choice(["Male", "Female"]),
             "address": {"line": f"{rng.randint(1, 200)} Gandhi Road", "city": city, "state": state, "pincode": pin}}
    lines = [(80, 80, "Government of India", 32), (80, 130, "Unique Identification Authority of India", 26),
             (80, 220, f"Name: {truth['name']}", 30), (80, 270, f"DOB: {born:%d/%m/%Y}", 28),
             (80, 320, f"Gender: {truth['gender']}", 28), (80, 400, truth["id_number"], 44),
             (80, 480, f"Address: {truth['address']['line']}, {city}, {state} - {pin}", 24)]
    return _render(lines, condition, rng, hard), truth


def kyc_form(rng: random.Random, condition: str, hard: bool) -> tuple[bytes, dict]:
    first, last = rng.choice(FIRST), rng.choice(LAST)
    city, state, pin, _ = rng.choice(CITY)
    born = date.today() - timedelta(days=rng.randint(25, 60) * 365 + rng.randint(0, 364))
    filled = _date(rng, 200)
    truth = {"name": f"{first} {last}", "date_of_birth": born.isoformat(), "gender": rng.choice(["Male", "Female"]),
             "father_or_spouse_name": f"{rng.choice(FIRST)} {last}", "pan": make_pan(rng, "P", last[0]),
             "mobile": str(rng.randint(6, 9)) + "".join(str(rng.randint(0, 9)) for _ in range(9)),
             "email": f"{first.lower()}.{last.lower()}@example.com",
             "current_address": {"line": f"{rng.randint(1, 99)} Park Street", "city": city, "state": state, "pincode": pin},
             "occupation": rng.choice(["Salaried", "Self employed", "Business"]),
             "annual_income": _money(rng.randint(3, 40) * 50000), "form_date": filled.isoformat()}
    a = truth["current_address"]
    lines = [(80, 80, "KYC APPLICATION FORM", 38), (80, 160, f"Full name: {truth['name']}", 28),
             (80, 205, f"Date of birth: {born:%d/%m/%Y}     Gender: {truth['gender']}", 28),
             (80, 250, f"Father's name: {truth['father_or_spouse_name']}", 28), (80, 295, f"PAN: {truth['pan']}", 28),
             (80, 340, f"Mobile: {truth['mobile']}     Email: {truth['email']}", 28),
             (80, 385, f"Current address: {a['line']}, {a['city']}, {a['state']} {a['pincode']}", 28),
             (80, 430, f"Occupation: {truth['occupation']}", 28),
             (80, 475, f"Annual income: Rs. {_fmt(truth['annual_income'])}", 28),
             (80, 520, f"Date: {filled:%d/%m/%Y}", 28)]
    return _render(lines, condition, rng, hard), truth


GENERATORS = {"invoice": invoice, "payslip": payslip, "pan_card": identity_pan,
              "aadhaar_card": identity_aadhaar, "kyc_form": kyc_form}
SCHEMA_OF = {"invoice": "invoice", "payslip": "payslip", "pan_card": "identity_document",
             "aadhaar_card": "identity_document", "kyc_form": "kyc_form"}
# Identity cards are photographed, never "digital" PDFs.
CONDITIONS = {"invoice": ("digital", "scan"), "payslip": ("digital", "scan"), "pan_card": ("scan",),
              "aadhaar_card": ("scan",), "kyc_form": ("digital", "scan")}


def build_cases(per_group: int, seed: int = 7, hard: bool = False) -> list[Case]:
    """`per_group` documents for every (kind, condition) pair."""
    cases: list[Case] = []
    for kind, generate in GENERATORS.items():
        for condition in CONDITIONS[kind]:
            for i in range(per_group):
                rng = random.Random(f"{seed}-{kind}-{condition}-{i}")
                data, truth = generate(rng, condition, hard)
                cases.append(Case(f"{kind}-{condition}-{i}", SCHEMA_OF[kind], condition, data, truth))
    return cases
