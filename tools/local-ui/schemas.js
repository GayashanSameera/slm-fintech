/**
 * Document types, consistency checks and masking rules for the local test UI.
 *
 * Mirrors requirements.md:
 *   FR-3  the five document types and their fields
 *   FR-3.9 consistency checks produce WARNINGS, never failures
 *   FR-4  masking of sensitive numbers (Aadhaar always)
 *
 * Once the platform exists, its Pydantic schemas (app/modules/extraction/schemas/) are the
 * source of truth and this file should be kept in line with them.
 *
 * What is a JSON schema here?
 *   A description of the exact JSON shape we want back: field names, types, and short
 *   descriptions. It is used twice:
 *     1. It is shown to the model in the prompt, so it knows what each field means.
 *     2. It is passed as `format` to Ollama (vLLM in AWS), which FORCES the output to
 *        match it. The model cannot invent extra fields or return broken JSON.
 */

// ---------- Schema building helpers ----------
// Every scalar field allows null ("type": ["string", "null"]): if a value is not in the
// document, the model must answer null instead of guessing (FR-3.6).

const str =(description) => ({ type: ["string", "null"], ...(description ? { description } : {}) });
const num = (description) => ({ type: ["number", "null"], ...(description ? { description } : {}) });
const bool = (description) => ({ type: ["boolean", "null"], ...(description ? { description } : {}) });
const date = (description) => str(description ? `${description}. Format YYYY-MM-DD` : "Format YYYY-MM-DD");
// Every field is listed as "required" so the model always writes every key (with null when
// empty). That keeps the output shape identical for every document of a type, which makes
// results easy to compare and to load into other systems.
const obj = (properties) => ({ type: "object", properties, required: Object.keys(properties) });
// A list of objects with the same fields, e.g. invoice line items or bank transactions.
const list = (properties) => ({ type: "array", items: obj(properties) });

// Seller and buyer on an invoice share the same shape.
const party = obj({
  name: str("Legal name as printed"),
  gstin: str("15-character GSTIN"),
  pan: str("10-character PAN"),
  address: str(),
});

// One salary component on a payslip, e.g. { name: "Basic Salary", amount: 40000 }.
const amountLine = { name: str("Component name as printed"), amount: num() };

/**
 * The five document types. Each entry has:
 *   label        name shown in the UI
 *   description  one sentence given to the model as context
 *   schema       the JSON schema the output must match
 *   sensitive    which fields to mask, and how ("path.to.field": kind). Kinds:
 *                  aadhaar    → XXXX XXXX 1234, ALWAYS (UIDAI rules)
 *                  pan, account, id_number, phone → keep the last 4 characters
 *   aadhaarScan  for ID and KYC documents, also search EVERY text field for 12-digit
 *                Aadhaar-like numbers and mask them, in case one appears in an unexpected
 *                field (e.g. inside an address)
 */
export const DOC_TYPES = {
  invoice: {
    label: "Invoice",
    description: "GST tax invoice issued by a seller to a buyer",
    schema: obj({
      invoice_number: str(),
      invoice_date: date(),
      due_date: date(),
      place_of_supply: str("State name or code"),
      currency: str("ISO code, e.g. INR"),
      seller: party,
      buyer: party,
      line_items: list({
        description: str(),
        hsn_sac: str("HSN or SAC code"),
        quantity: num(),
        unit_price: num(),
        tax_rate: num("Tax rate in percent"),
        amount: num("Taxable amount for the line"),
      }),
      subtotal: num("Total taxable value before tax"),
      cgst: num(),
      sgst: num(),
      igst: num(),
      total_tax: num(),
      total_amount: num("Invoice total including tax"),
    }),
    sensitive: { "seller.pan": "pan", "buyer.pan": "pan" },
  },

  bank_statement: {
    label: "Bank statement",
    description: "Bank account statement listing transactions for a period",
    schema: obj({
      bank_name: str(),
      branch: str(),
      account_holder_name: str(),
      account_number: str(),
      ifsc: str("11-character IFSC"),
      account_type: str("e.g. Savings, Current"),
      currency: str("ISO code, e.g. INR"),
      period_start: date(),
      period_end: date(),
      opening_balance: num(),
      closing_balance: num(),
      total_credits: num(),
      total_debits: num(),
      transactions: list({
        date: date("Transaction date"),
        description: str("Narration as printed"),
        reference: str("Cheque or reference number"),
        debit: num("Amount withdrawn, positive, null if none"),
        credit: num("Amount deposited, positive, null if none"),
        balance: num("Balance after the transaction"),
      }),
    }),
    sensitive: { account_number: "account" },
  },

  payslip: {
    label: "Payslip",
    description: "Monthly salary slip issued by an employer",
    schema: obj({
      employer_name: str(),
      employer_address: str(),
      employee_name: str(),
      employee_id: str(),
      designation: str(),
      pan: str("10-character PAN"),
      uan: str("12-digit UAN"),
      bank_name: str(),
      bank_account_number: str(),
      pay_period_start: date(),
      pay_period_end: date(),
      pay_date: date(),
      days_paid: num(),
      earnings: list(amountLine),
      deductions: list(amountLine),
      gross_earnings: num(),
      total_deductions: num(),
      net_pay: num(),
    }),
    sensitive: { pan: "pan", uan: "id_number", bank_account_number: "account" },
  },

  identity_document: {
    label: "Identity document",
    description: "Indian identity document: Aadhaar, PAN card, passport, driving licence or voter ID",
    schema: obj({
      id_type: {
        type: ["string", "null"],
        enum: ["aadhaar", "pan", "passport", "driving_licence", "voter_id", "other", null],
      },
      document_number: str("Number printed on the document"),
      full_name: str(),
      father_or_spouse_name: str(),
      date_of_birth: date(),
      gender: str(),
      address: str(),
      issue_date: date(),
      expiry_date: date(),
      issuing_authority: str(),
      nationality: str(),
    }),
    sensitive: { document_number: "id_number" },
    aadhaarScan: true,
  },

  kyc_form: {
    label: "KYC form",
    description: "Customer KYC application form",
    schema: obj({
      customer_name: str(),
      date_of_birth: date(),
      gender: str(),
      father_or_spouse_name: str(),
      pan: str("10-character PAN"),
      aadhaar_number: str(),
      ckyc_number: str(),
      mobile: str(),
      email: str(),
      current_address: str(),
      permanent_address: str(),
      occupation: str(),
      annual_income: str("As printed, e.g. a range"),
      politically_exposed: bool("Politically exposed person"),
      nominee_name: str(),
      nominee_relationship: str(),
      signature_present: bool(),
      form_date: date(),
    }),
    sensitive: { pan: "pan", aadhaar_number: "aadhaar", mobile: "phone" },
    aadhaarScan: true,
  },
};

// ---------- Consistency checks (warnings, never failures) ----------
// The model can misread a digit. Financial documents contain built-in arithmetic
// (totals, balances), so we re-do the sums. A mismatch does not prove the model is wrong
// (the document itself may be wrong), so it becomes a WARNING that a human or the tenant's
// system can review. The job still succeeds (FR-3.9).

// Allow up to ₹1 difference for rounding (e.g. tax rounded per line vs on the total).
const TOLERANCE = 1.0; // rupees
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const sum = (items, key) => items.reduce((acc, it) => acc + (isNum(it?.[key]) ? it[key] : 0), 0);
const fmt = (v) => v.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const differs = (a, b) => Math.abs(a - b) > TOLERANCE;
// PAN format: 5 letters, 4 digits, 1 letter (e.g. ABCDE1234F).
const PAN_RE = /^[A-Z]{5}[0-9]{4}[A-Z]$/;

// One check function per document type. Each receives the model's output `d` and returns
// a list of human-readable warnings (empty list = everything adds up).
const CHECKS = {
  // Invoice: line items should add up to the subtotal, and subtotal + tax to the total.
  // If total_tax is missing, CGST + SGST + IGST are added instead.
  invoice(d) {
    const w = [];
    const items = Array.isArray(d.line_items) ? d.line_items : [];
    if (items.length && isNum(d.subtotal) && differs(sum(items, "amount"), d.subtotal)) {
      w.push(`Line items add up to ${fmt(sum(items, "amount"))}, but subtotal is ${fmt(d.subtotal)}.`);
    }
    const tax = isNum(d.total_tax) ? d.total_tax : [d.cgst, d.sgst, d.igst].filter(isNum).reduce((a, b) => a + b, 0);
    if (isNum(d.subtotal) && isNum(d.total_amount) && differs(d.subtotal + tax, d.total_amount)) {
      w.push(`Subtotal + tax is ${fmt(d.subtotal + tax)}, but total is ${fmt(d.total_amount)}.`);
    }
    for (const p of ["seller", "buyer"]) {
      if (d[p]?.pan && !PAN_RE.test(d[p].pan)) w.push(`${p}.pan does not look like a valid PAN.`);
    }
    return w;
  },

  // Bank statement, two checks:
  //  1. opening balance + all credits − all debits should equal the closing balance;
  //  2. row by row, previous balance + credit − debit should equal the row's balance.
  //     The count of rows that fail points at misread amounts.
  bank_statement(d) {
    const w = [];
    const tx = Array.isArray(d.transactions) ? d.transactions : [];
    if (isNum(d.opening_balance) && isNum(d.closing_balance) && tx.length) {
      const expected = d.opening_balance + sum(tx, "credit") - sum(tx, "debit");
      if (differs(expected, d.closing_balance)) {
        w.push(`Opening + credits − debits is ${fmt(expected)}, but closing balance is ${fmt(d.closing_balance)}.`);
      }
    }
    let prev = isNum(d.opening_balance) ? d.opening_balance : null;
    let bad = 0;
    for (const t of tx) {
      if (prev !== null && isNum(t.balance)) {
        const expected = prev + (isNum(t.credit) ? t.credit : 0) - (isNum(t.debit) ? t.debit : 0);
        if (differs(expected, t.balance)) bad += 1;
      }
      prev = isNum(t.balance) ? t.balance : null;
    }
    if (bad) w.push(`Running balance does not match on ${bad} of ${tx.length} transactions.`);
    return w;
  },

  // Payslip: earnings add up to gross, deductions to total deductions,
  // and gross − deductions = net pay.
  payslip(d) {
    const w = [];
    const earn = Array.isArray(d.earnings) ? d.earnings : [];
    const ded = Array.isArray(d.deductions) ? d.deductions : [];
    if (earn.length && isNum(d.gross_earnings) && differs(sum(earn, "amount"), d.gross_earnings)) {
      w.push(`Earnings add up to ${fmt(sum(earn, "amount"))}, but gross is ${fmt(d.gross_earnings)}.`);
    }
    if (ded.length && isNum(d.total_deductions) && differs(sum(ded, "amount"), d.total_deductions)) {
      w.push(`Deductions add up to ${fmt(sum(ded, "amount"))}, but total deductions is ${fmt(d.total_deductions)}.`);
    }
    if (isNum(d.gross_earnings) && isNum(d.total_deductions) && isNum(d.net_pay)
        && differs(d.gross_earnings - d.total_deductions, d.net_pay)) {
      w.push(`Gross − deductions is ${fmt(d.gross_earnings - d.total_deductions)}, but net pay is ${fmt(d.net_pay)}.`);
    }
    if (d.pan && !PAN_RE.test(d.pan)) w.push("pan does not look like a valid PAN.");
    return w;
  },

  // Identity document: PAN number format, and whether the document has expired.
  identity_document(d) {
    const w = [];
    if (d.id_type === "pan" && d.document_number && !PAN_RE.test(d.document_number)) {
      w.push("document_number does not look like a valid PAN.");
    }
    if (d.expiry_date && /^\d{4}-\d{2}-\d{2}$/.test(d.expiry_date) && new Date(d.expiry_date) < new Date()) {
      w.push(`Document expired on ${d.expiry_date}.`);
    }
    return w;
  },

  // KYC form: PAN format, and the fields a KYC record cannot do without.
  kyc_form(d) {
    const w = [];
    if (d.pan && !PAN_RE.test(d.pan)) w.push("pan does not look like a valid PAN.");
    for (const f of ["customer_name", "date_of_birth", "pan"]) {
      if (d[f] === null || d[f] === undefined || d[f] === "") w.push(`Required field ${f} is missing.`);
    }
    return w;
  },
};

/**
 * Runs the checks for one document type. Never throws: if the output has an unexpected
 * shape, it returns one warning saying the checks could not run.
 */
export function consistencyWarnings(type, data) {
  try {
    return CHECKS[type] ? CHECKS[type](data) : [];
  } catch {
    return ["Consistency checks could not run on this output."];
  }
}

// ---------- Masking (FR-4) ----------
// Sensitive numbers are hidden before results are shown or stored. On the platform this
// happens BEFORE the result is written to S3, so full Aadhaar numbers are never stored.

// Aadhaar numbers are 12 digits and never start with 0 or 1, often written "2345 6789 0123".
const AADHAAR_RE = /\b[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}\b/g;
// UIDAI rules: a full Aadhaar number may not be stored without an Aadhaar Data Vault, so
// no setting can ever reveal it.
const NEVER_UNMASK = new Set(["aadhaar"]);

/** "ABCDE1234F" → "XXXXXX234F": keep the last 4 letters/digits so people can still match records. */
function keepLast4(value) {
  const chars = String(value).replace(/[^0-9A-Za-z]/g, "");
  if (chars.length <= 4) return "X".repeat(chars.length);
  return "X".repeat(chars.length - 4) + chars.slice(-4);
}

/** Any Aadhaar format → "XXXX XXXX 1234" (the standard masked form). */
function maskAadhaar(value) {
  const digits = String(value).replace(/\D/g, "");
  return digits.length >= 4 ? `XXXX XXXX ${digits.slice(-4)}` : "XXXX XXXX XXXX";
}

const MASKERS = { aadhaar: maskAadhaar, pan: keepLast4, account: keepLast4, id_number: keepLast4, phone: keepLast4 };

// getPath / setPath read and write a nested field by its dotted name, e.g. "seller.pan".
function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), obj);
}

function setPath(obj, path, value) {
  const keys = path.split(".");
  const last = keys.pop();
  const parent = keys.reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), obj);
  if (parent && typeof parent === "object" && last in parent) parent[last] = value;
}

/** Walks every string in the result (including nested objects and lists) and masks Aadhaar-like numbers. */
function scanStrings(value) {
  if (typeof value === "string") return value.replace(AADHAAR_RE, (m) => maskAadhaar(m));
  if (Array.isArray(value)) return value.map(scanStrings);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, scanStrings(v)]));
  }
  return value;
}

/**
 * Returns a masked COPY of the result (the original is left untouched, so the UI can
 * re-mask when the "Show unmasked" switches change).
 *
 * `unmask` is the set of kinds the viewer chose to reveal. On the platform this comes from
 * the tenant's settings (FR-4.4). Aadhaar is masked regardless.
 *
 * Special case: an identity document's `document_number` is normally kind "id_number",
 * but when `id_type` is "aadhaar" it is an Aadhaar number and gets the Aadhaar rule.
 */
export function maskResult(type, data, unmask = new Set()) {
  const def = DOC_TYPES[type];
  let out = structuredClone(data);
  for (const [path, kind] of Object.entries(def?.sensitive ?? {})) {
    const v = getPath(out, path);
    if (typeof v !== "string" || !v) continue;
    const effectiveKind = type === "identity_document" && path === "document_number" && out.id_type === "aadhaar"
      ? "aadhaar" : kind;
    if (NEVER_UNMASK.has(effectiveKind) || !unmask.has(effectiveKind)) {
      setPath(out, path, MASKERS[effectiveKind](v));
    }
  }
  if (def?.aadhaarScan) out = scanStrings(out);
  return out;
}
