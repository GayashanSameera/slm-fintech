# Document Extraction API: Integration Guide

| | |
|---|---|
| Audience | Developers integrating the extraction API into another application (a client's backend) |
| API version | `v1` |
| Base URL (production) | `https://0vtusv00e4.execute-api.us-east-1.amazonaws.com/prod` |
| Test console (browser) | https://dcw2i73df06ry.cloudfront.net: sign in with your API key to upload documents and see results, tokens and AI cost |
| Data location | AWS us-east-1; AI processing in the US (us-east-1/-2, us-west-2) with zero data retention |
| Date | 2026-10-14 |

The API turns financial documents into structured JSON:

- **Inputs:** PDF (digital or scanned), JPG, PNG, DOCX, up to 25 MB and 20 pages.
- **Document types:** bank statements, invoices, payslips, identity documents, KYC forms.
- **What you get back:**
  - every field normalised: dates as `YYYY-MM-DD`, amounts as exact 2-decimal strings;
  - sensitive identifiers masked;
  - how the result was produced and verified;
  - the AI tokens it used.

---

## Contents

1. [Quick start](#1-quick-start)
2. [Access and API keys](#2-access-and-api-keys)
3. [Core concepts](#3-core-concepts)
4. [Choosing a flow](#4-choosing-a-flow)
5. [Endpoint reference](#5-endpoint-reference)
6. [The job object](#6-the-job-object)
7. [Result fields per document type](#7-result-fields-per-document-type)
8. [Trusting a result: verification, warnings and reconciliation](#8-trusting-a-result-verification-warnings-and-reconciliation)
9. [Webhooks](#9-webhooks)
10. [Errors and retries](#10-errors-and-retries)
11. [Limits](#11-limits)
12. [Client code: Python and Node.js](#12-client-code-python-and-nodejs)
13. [Integration checklist](#13-integration-checklist)
14. [For the platform operator: onboarding a client](#14-for-the-platform-operator-onboarding-a-client)

---

## 1. Quick start

```bash
export SLM_API=https://0vtusv00e4.execute-api.us-east-1.amazonaws.com/prod
export SLM_KEY=slm_live_...                                             # your API key

# Send a bank statement (≤ 4 MB) and wait up to 25 s for the result in the same response:
curl -s -X POST "$SLM_API/v1/extractions?wait=25" \
     -H "Authorization: Bearer $SLM_KEY" \
     -F "document_type=bank_statement" \
     -F "file=@statement.pdf"
```

- **`200`:** the body contains `"status": "succeeded"` and the `result`.
- **`202`:** processing is still running. Fetch the job with its `id`:

```bash
curl -s "$SLM_API/v1/extractions/ext_01JA2K8V7Q...?wait=20" -H "Authorization: Bearer $SLM_KEY"
```

---

## 2. Access and API keys

- **Getting a key:** the platform operator creates your organisation (a *tenant*) and an API key (section 14). The key looks like `slm_live_<id>_<secret>` in production, or `slm_test_...` in test environments.
- **It's shown only once.** Store it in your secret manager (AWS Secrets Manager, Vault, Azure Key Vault, ...), never in source code or config files under version control.
- **Server-side only.** Call the API from your backend. Never put the key in a browser, a mobile app or a desktop app: anyone could extract it.
- **Every request** sends it as `Authorization: Bearer <key>`.
- **Scopes:**
  - `extractions:read` lets a key fetch and list jobs;
  - `extractions:write` also lets it create jobs, delete them, and send corrections.
  - A read-only key is useful for reporting systems.
- **Rotation:** ask the operator for a second key, switch your application to it, then have the old one revoked. A revoked key stops working within 60 seconds.
- **Isolation:** your key can only ever see your organisation's jobs. Another organisation's job id answers `404`, exactly like an id that doesn't exist.

---

## 3. Core concepts

**Job.** Every document you send becomes a job with an id like `ext_01JA2K8V7Q4M...`. Ids sort by creation time.

**Status.** A job moves forward only:

```
awaiting_upload → scanning → queued → processing → succeeded
                                                 └→ failed
awaiting_upload → expired   (upload link never used)
```

| Status | Meaning | What you do |
|---|---|---|
| `awaiting_upload` | Job created, waiting for your file (upload-link flow) | Upload the file |
| `scanning` | File received; malware scan running | Wait |
| `queued` | Clean; waiting for a worker | Wait |
| `processing` | Being extracted | Wait |
| `succeeded` | Done | Read `result`, `warnings`, `usage` |
| `failed` | Not processed | Read `error.code` (section 10) |
| `expired` | No upload within 15 minutes | Create a new job |

**Final statuses** are `succeeded`, `failed` and `expired`. Once a job reaches one, it never changes again.

**Waiting.** Most calls accept `wait=N` (seconds). The server holds the request until the job is final or `N` seconds pass. This avoids polling in a tight loop: one call with `wait=20` replaces about 20 polls.

**Document type.**
- **Specify it:** send `document_type` (`bank_statement`, `invoice`, `payslip`, `identity_document`, `kyc_form`) when you know it. That's faster and cheaper.
- **Detect it:** omit it, or send `auto`, to have it detected. The job then reports `document_type_source: "detected"` and a confidence.

**Masking.** Identifiers are masked before anything is stored or returned:

| Field | Returned as |
|---|---|
| Aadhaar numbers | always `XXXX XXXX 1234`; this can never be turned off |
| PAN, account numbers, UAN, ID numbers, phone numbers | last 4 characters, e.g. `XXXXXX234F`. Your operator can allow some of these unmasked for your organisation |
| Identity and KYC documents | any Aadhaar-like 12-digit number in any field is masked |

**Retention.** Uploaded files are deleted as soon as they're processed. Jobs and results are kept for your organisation's retention period (7, 30, 90 or 365 days, default 30), then deleted automatically. From that moment they answer `404`. You can delete a job earlier yourself (`DELETE`).

**Duplicates.** Sending the **identical file** (same bytes, same `document_type`) again returns the earlier job immediately, with `"deduplicated": true`. No new processing happens and no tokens are used.

---

## 4. Choosing a flow

There is one endpoint, `POST /v1/extractions`; what you send decides the flow.

| | **Direct flow** | **Upload-link flow** |
|---|---|---|
| Request contains | the file (multipart, or base64 in JSON) | no file |
| File size | up to **4 MB** | up to **25 MB** |
| You get | the result in the same response if ready within `wait` (`200`), else `202` | a one-time S3 upload link (`201`) |
| Then | if `202`: `GET` the job with `wait` | upload the file to S3, then `GET` the job with `wait` (or wait for the webhook) |
| Best for | interactive use: a user waiting on a screen | large files, batch jobs, background processing |

**Rule of thumb:** use the direct flow for files up to 4 MB, and the upload link above that.

### 4.1 Direct flow

```http
POST /v1/extractions?wait=25
Authorization: Bearer slm_live_...
Content-Type: multipart/form-data; boundary=...

--...
Content-Disposition: form-data; name="document_type"

invoice
--...
Content-Disposition: form-data; name="file"; filename="invoice.pdf"
Content-Type: application/pdf

<bytes>
--...--
```

The same request as JSON, if multipart is awkward in your stack (base64 makes the body about 33% bigger; the 4 MB limit applies to the decoded file):

```json
POST /v1/extractions
{ "document_type": "invoice", "wait": 25, "file": { "content_base64": "JVBERi0xLjQK..." } }
```

A `data:application/pdf;base64,` prefix and line breaks inside the base64 are accepted.

### 4.2 Upload-link flow

**Step 1: create the job.**

```http
POST /v1/extractions
Content-Type: application/json

{ "document_type": "bank_statement" }
```

```http
HTTP/1.1 201 Created
Location: /v1/extractions/ext_01JA2K8V7Q...

{
  "id": "ext_01JA2K8V7Q...",
  "status": "awaiting_upload",
  "document_type": "bank_statement",
  "upload": {
    "method": "POST",
    "url": "https://slm-prod-docs-....s3.amazonaws.com/",
    "fields": { "key": "tenants/t_.../jobs/ext_.../source", "policy": "...", "x-amz-signature": "...", "...": "..." },
    "max_bytes": 26214400,
    "expires_at": "2026-10-14T10:30:00Z"
  }
}
```

**Step 2: upload the file to `upload.url`** as `multipart/form-data`. Send **every** entry of `upload.fields` as a form field, and the file **last**, in a field named `file`.
- S3 answers `204 No Content`.
- `403` means the link expired, the file is too big, or a field was changed.
- The link is valid for 15 minutes and works for exactly one file. It needs no API key and no AWS account.

**Step 3: get the result.** Call `GET /v1/extractions/{id}?wait=20` until the status is final, or wait for the webhook (section 9). The file is scanned for malware before anything reads it: an infected file ends as `failed` with `malware_detected` and is deleted.

---

## 5. Endpoint reference

All paths are relative to the base URL. Every request needs `Authorization: Bearer <key>`, except `GET /healthz`.

### `POST /v1/extractions`: create a job

| Parameter | Where | Notes |
|---|---|---|
| `file` | multipart field | Direct flow. PDF, JPG, PNG, DOCX; ≤ 4 MB. The real type is detected from the bytes, not the name |
| `file.content_base64` | JSON body | Direct flow, alternative to multipart |
| `document_type` | multipart field or JSON | Optional. One of the five types, or `auto` |
| `wait` | query (or JSON) | 0–25 seconds (direct flow only). Default 25. Use `0` to return at once |

| Response | When |
|---|---|
| `200` + job with `result` | Direct flow, finished within `wait` (also a duplicate file: `"deduplicated": true`) |
| `202` + job, `Location` header | Direct flow, still running |
| `201` + job with `upload` | No file sent: upload-link flow |
| `400`, `413`, `415` | Bad request, file too large, unsupported file type (section 10) |

### `GET /v1/extractions/{id}?wait=0..20`: get one job

- Always `200` with the job. `result` is present once the status is `succeeded`.
- While the job isn't final, the response has a `Retry-After: 2` header.

### `GET /v1/extractions?limit=20&cursor=...`: list jobs

Newest first, without results; fetch a job to see its result.

```json
{ "data": [ { "id": "ext_...", "status": "succeeded", "...": "..." } ], "next_cursor": "ext_01JA..." }
```

Pass `next_cursor` as `cursor` to get the next page; `null` means there are no more. `limit` is 1–100.

### `DELETE /v1/extractions/{id}`: delete a job

Returns `204`. Deletes the job, its result, any stored file and any corrections, permanently.

### `POST /v1/extractions/{id}/corrections`: report wrong fields

```json
{ "corrections": [ { "path": "transactions[3].description", "value": "NEFT/ACME PAYROLL SEP" },
                   { "path": "total", "value": "11800.00" } ] }
```

- Returns `201` with `{"job_id": "...", "accepted": 2}`. Only for `succeeded` jobs (`409` otherwise); at most 500 corrections.
- Corrections help improve accuracy over time. They don't change the stored result.

### `GET /v1/document-types`

Lists every supported type with its **JSON Schema** and `schema_version`. Use it to generate types or validators in your language.

### `GET /healthz`

`{"ok": true}`. No key needed. Use it for uptime monitoring.

---

## 6. The job object

```json
{
  "id": "ext_01JA2K8V7Q4M5N6P7R8S9T0V1W",
  "status": "succeeded",
  "flow": "direct",
  "document_type": "bank_statement",
  "document_type_source": "client",
  "schema_version": "1.0",
  "page_count": 2,
  "method": "parser",
  "parser_id": "hdfc_v1",
  "verified_by": "us.amazon.nova-lite-v1:0",
  "model_id": null,
  "warnings": [],
  "result": { "...": "see section 7" },
  "usage": {
    "model_calls": 1, "input_tokens": 2511, "output_tokens": 145, "cache_read_tokens": 0,
    "estimated_cost_usd": 0.000185,
    "by_model": [ { "model_id": "us.amazon.nova-lite-v1:0", "calls": 1, "input_tokens": 2511,
                    "output_tokens": 145, "cache_read_tokens": 0, "estimated_cost_usd": 0.000185 } ]
  },
  "created_at": "2026-10-14T10:15:02Z",
  "completed_at": "2026-10-14T10:15:09Z"
}
```

| Field | Meaning |
|---|---|
| `id`, `status`, `flow` | Section 3. `flow` is `direct` or `presigned` |
| `document_type` | The type the result follows |
| `document_type_source` | `client` (you sent it), `detected` (classified by AI, with `document_type_confidence` 0–1), or `parser` (recognised bank layout) |
| `schema_version` | Version of the result's field layout; it increases when fields change |
| `page_count` | Pages processed |
| `method` | `parser`: read by a deterministic Python parser for a known bank layout. `ai`: read by an AI model |
| `parser_id` / `verified_by` | Parser method only: which layout parser read it, and which AI model independently confirmed its key facts. `verified_by: null` means the operator configured your organisation for checks only, with no AI |
| `model_id` | AI method only: the model that produced the result |
| `warnings` | Codes for anything you should review (section 8). Empty means every check passed |
| `result` | The extracted data (section 7). Fields not found are `null`; lists are `[]` |
| `usage` | AI tokens used for this job, in total and per model, plus `estimated_cost_usd`: the AI cost at AWS Bedrock list prices (model tokens only; an estimate, your invoice is the reference). Includes failed jobs that called a model. `0` calls for checks-only parser results |
| `error` | `failed` only: `{"code": "..."}` (section 10) |
| `deduplicated` | `true` when this answer came from an earlier identical upload (no new tokens used) |
| `processing_starts_at` | Only if a processing window is configured and currently closed |
| `created_at`, `uploaded_at`, `completed_at` | UTC timestamps |

**Formats in `result`:** dates are `"YYYY-MM-DD"`. Amounts are **strings** with exactly two decimals (`"118000.50"`), never floats: parse them with a decimal type (`Decimal` in Python, `BigDecimal` in Java, a decimal library in JS), not `float`. Missing values are `null`.

---

## 7. Result fields per document type

The authoritative definitions are the JSON Schemas from `GET /v1/document-types`. In short:

**`bank_statement`**
```json
{ "bank_name": "...", "account_holder": "...", "account_number": "XXXXXXXX7890", "ifsc": "HDFC0001234",
  "branch": "...", "period_from": "2026-09-01", "period_to": "2026-09-30",
  "opening_balance": "10000.00", "closing_balance": "14380.00",
  "transactions": [ { "date": "2026-09-01", "description": "UPI/...", "reference": "UPI426512345678",
                      "debit": "250.00", "credit": null, "balance": "9750.00" } ] }
```

**`invoice`**
```json
{ "invoice_number": "INV-2026-0042", "invoice_date": "2026-09-07", "due_date": "2026-10-07",
  "seller": { "name": "...", "gstin": "...", "pan": "XXXXXX234F", "address": "..." },
  "buyer":  { "name": "...", "gstin": "...", "pan": null, "address": "..." },
  "line_items": [ { "description": "...", "hsn_sac": "7308", "quantity": "10.00", "rate": "1000.00", "amount": "10000.00" } ],
  "subtotal": "15000.00", "cgst": "1350.00", "sgst": "1350.00", "igst": null, "total": "17700.00" }
```

**`payslip`**: `employer_name`, `employee_name`, `employee_id`, `pan`, `uan`, `bank_account_number`, `pay_period`, `pay_date`, `earnings[]` and `deductions[]` (each `{name, amount}`), `gross_pay`, `total_deductions`, `net_pay`.

**`identity_document`**: `id_type` (`pan`, `aadhaar`, `passport`, `voter_id`, `driving_licence`), `id_number`, `name`, `father_or_spouse_name`, `date_of_birth`, `gender`, `address{line, city, state, pincode}`, `issue_date`, `expiry_date`, `issuer`.

**`kyc_form`**: `name`, `date_of_birth`, `gender`, `father_or_spouse_name`, `pan`, `aadhaar` (always masked), `ckyc_number`, `mobile`, `email`, `current_address`, `permanent_address`, `occupation`, `annual_income`, `pep_status`, `nominee_name`, `signature_present`, `form_date`.

---

## 8. Trusting a result: verification, warnings and reconciliation

Every result is checked before it's returned. What a clean result guarantees:

| Check | Applies to | Guarantees |
|---|---|---|
| **Arithmetic** | bank statements, invoices, payslips | Statements: every row's running balance, and opening + credits − debits = closing. Invoices: line items = subtotal, subtotal + taxes = total. Payslips: earnings = gross, gross − deductions = net. Tolerance 0.01 |
| **Text reconciliation** | digital (non-scanned) PDF bank statements | **Every row, every field** (date, description, reference, amounts) and the header fields match the document's own text, independently re-read, in order, with no row missing or extra. No AI involved |
| **Independent AI check** | `method: parser` results | A second reader (Nova Lite) reads holder, account, IFSC, branch, period, first and last row from the page images; any difference rejects the parser result |
| **Format checks** | all | PAN, GSTIN, IFSC formats |

How results are produced:
- **`method: parser`:** a parser result is accepted **only** if all of these pass. Otherwise the document is re-read by AI.
- **`method: ai`:** the AI result goes through the same checks. If a check fails, a stronger model retries, up to 3 attempts. If problems remain, the result is still returned, **with warnings**.

### Warning codes

| Code | Meaning | Recommended action |
|---|---|---|
| `running_balance_mismatch`, `closing_balance_mismatch` | Statement arithmetic doesn't hold | **Do not reconcile automatically**; route to manual review |
| `text_reconciliation_failed` | Extracted rows don't match the PDF's text exactly | **Do not reconcile automatically**; route to manual review |
| `line_items_sum_mismatch`, `total_mismatch` | Invoice arithmetic doesn't hold | Manual review |
| `earnings_sum_mismatch`, `net_pay_mismatch` | Payslip arithmetic doesn't hold | Manual review |
| `pan_format`, `gstin_format`, `ifsc_format` | An identifier has an invalid format | Check that field |
| `document_expired` | The identity document's expiry date has passed | Business decision (the extraction itself is fine) |
| `required_fields_missing` | A KYC form lacks name, date of birth or PAN | Ask for a complete form |
| `document_type_mismatch` | You said one type, but nothing of that type was found | Check you sent the right file and type |

**For automated reconciliation of bank statements**, accept a result automatically only when **all** of these hold, and send everything else to a person:
- `status` is `succeeded`;
- `warnings` is empty;
- the PDF was digital: `method` is `parser`, or `method` is `ai` on a non-scanned PDF.

Scanned statements (photos, scanner images) have no text layer, so text reconciliation can't run on them. Their arithmetic is still checked, but descriptions and references are not independently proven. Treat them as needing review if exactness matters.

---

## 9. Webhooks

Instead of polling, your operator can register an `https://` URL for your organisation. When a job finishes, the platform POSTs:

```json
{ "event": "extraction.succeeded", "job_id": "ext_01JA2K8V7Q...", "status": "succeeded", "occurred_at": "2026-10-14T10:15:09Z" }
```

`event` is `extraction.succeeded` or `extraction.failed`.

- **No document data is sent.** Fetch the result with `GET /v1/extractions/{job_id}` using your key.
- **Verify every webhook** with the secret you got from the operator. The header is `X-Signature: t=<unix-seconds>,v1=<hex>`, where `v1 = HMAC-SHA256(secret, "<t>." + raw_body)`. Reject it if the signature differs or `t` is more than 5 minutes old (replay protection).
- **Respond `2xx` quickly.** Do your processing asynchronously. Non-2xx answers are retried, up to 5 deliveries.
- **Delivery is at least once:** the same event can arrive twice. Make your handler idempotent, keyed on `job_id`.
- **The URL must be public `https`:** private, internal and redirecting URLs are refused.

**Python (Flask):**
```python
import hashlib, hmac, time
from flask import Flask, request, abort

app = Flask(__name__)
SECRET = b"..."   # webhook secret from the operator, from your secret manager

@app.post("/slm-events")
def slm_events():
    header = dict(part.split("=", 1) for part in request.headers.get("X-Signature", "").split(","))
    t, sig = header.get("t", "0"), header.get("v1", "")
    expected = hmac.new(SECRET, f"{t}.".encode() + request.get_data(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig) or abs(time.time() - int(t)) > 300:
        abort(401)
    event = request.get_json()
    enqueue_fetch(event["job_id"])          # your code: fetch the result asynchronously
    return "", 204
```

**Node.js (Express):**
```js
import crypto from "node:crypto";
import express from "express";

const app = express();
const SECRET = process.env.SLM_WEBHOOK_SECRET;

app.post("/slm-events", express.raw({ type: "application/json" }), (req, res) => {
  const parts = Object.fromEntries((req.get("X-Signature") || "").split(",").map(p => p.split("=")));
  const expected = crypto.createHmac("sha256", SECRET).update(`${parts.t}.`).update(req.body).digest("hex");
  const fresh = Math.abs(Date.now() / 1000 - Number(parts.t)) <= 300;
  if (!fresh || !parts.v1 || !crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(parts.v1))) return res.sendStatus(401);
  const event = JSON.parse(req.body);
  enqueueFetch(event.job_id);               // your code
  res.sendStatus(204);
});
```

---

## 10. Errors and retries

Every error has the same shape:
```json
{ "error": { "code": "file_too_large", "message": "The file is larger than 4 MB. Use the upload-link flow." } }
```
Messages are for humans and may change; **program against `code`**.

### Request errors (returned immediately)

| HTTP | `code` | Cause | Retry? |
|---|---|---|---|
| 400 | `invalid_request`, `invalid_json`, `invalid_base64`, `empty_file`, `invalid_document_type`, `invalid_cursor`, `invalid_correction` | Fix the request | No |
| 401 | `unauthorized` | Missing or invalid key | No, check the key |
| 403 | `tenant_suspended` | Your organisation is not active | No, contact the operator |
| 403 | `insufficient_scope` | Read-only key used to write | No |
| 403 | (no JSON body) | Blocked by the firewall: too many requests from your IP, or a malicious-looking request | After a pause |
| 404 | `not_found` | Unknown id (or not yours, or past retention) | No |
| 409 | `job_not_finished` | Corrections on an unfinished job | Later |
| 413 | `file_too_large` | Direct flow > 4 MB | Use the upload link |
| 415 | `unsupported_file_type` | Not PDF, JPG, PNG or DOCX | No |
| 429 | `rate_limited` | Too many requests | **Yes**, with backoff |
| 500, 502, 503, 504 | `internal_error` or none | Temporary problem | **Yes**, with backoff |

### Job errors (`status: failed`, `error.code`)

| `code` | Cause |
|---|---|
| `unsupported_file_type`, `corrupt_file` | File unreadable or not a supported type |
| `encrypted_document` | Password-protected PDF: send it without the password |
| `too_many_pages` | More than 20 pages: split it |
| `file_too_complex`, `macro_not_allowed` | Suspicious file structure, or a DOCX with macros |
| `malware_detected`, `scan_failed`, `scan_timeout` | Malware scan result (the file is deleted) |
| `unrecognised_document_type` | Couldn't tell the type: resend with `document_type` |
| `extraction_failed` | No usable result: check the file's quality and resend |
| `processing_error` | Internal failure after retries: resend, and contact the operator if it repeats |

**Retry policy:**
- **When to retry:** network errors, `429` and `5xx`.
- **How:** exponential backoff with jitter: 1 s, 2 s, 4 s, ... up to 30 s, at most 5 attempts.
- **Don't retry other `4xx` errors.**
- **Retrying a create is safe:** if the first attempt actually succeeded, resending the identical file returns that job (`deduplicated`) instead of processing it twice.

---

## 11. Limits

| Limit | Value |
|---|---|
| File size | 4 MB direct; 25 MB with an upload link |
| Pages | 20 per document |
| `wait` | up to 25 s on create, 20 s on get |
| Request rate | 50 requests/s steady (burst 100) for the whole API; firewall limit of 1,000 requests per 5 minutes per client IP |
| Upload link validity | 15 minutes, one file |
| Typical processing time | digital statement 3–8 s; scanned page or invoice 5–15 s |
| Result retention | your organisation's setting (default 30 days) |

Need more? Contact the operator. Limits are configuration, not code.

---

## 12. Client code: Python and Node.js

### Python (httpx)

```python
"""Minimal client for the Document Extraction API (pip install httpx)."""
import random
import time
from decimal import Decimal
from pathlib import Path

import httpx

FINAL = {"succeeded", "failed", "expired"}
DIRECT_LIMIT = 4 * 1024 * 1024


class ExtractionClient:
    def __init__(self, base_url: str, api_key: str):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=60,
                                 headers={"Authorization": f"Bearer {api_key}"})

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        """Retries network errors, 429 and 5xx with exponential backoff."""
        for attempt in range(5):
            try:
                r = self.http.request(method, path, **kw)
                if r.status_code != 429 and r.status_code < 500:
                    return r
            except httpx.TransportError:
                if attempt == 4:
                    raise
            time.sleep(min(30, 2 ** attempt) + random.random())
        return r

    def extract(self, path: str, document_type: str | None = None, timeout_s: int = 300) -> dict:
        """Send a file and return the FINAL job (succeeded or failed)."""
        data = Path(path).read_bytes()
        form = {"document_type": document_type} if document_type else {}
        if len(data) <= DIRECT_LIMIT:                                  # direct flow
            r = self._call("POST", "/v1/extractions", params={"wait": 25}, data=form,
                           files={"file": (Path(path).name, data)})
        else:                                                         # upload-link flow
            r = self._call("POST", "/v1/extractions", json=form)
            r.raise_for_status()
            job = r.json()
            s3 = httpx.post(job["upload"]["url"], data=job["upload"]["fields"],
                            files={"file": (Path(path).name, data)}, timeout=120)
            s3.raise_for_status()
        r.raise_for_status()
        job = r.json()
        deadline = time.monotonic() + timeout_s
        while job["status"] not in FINAL and time.monotonic() < deadline:
            job = self._call("GET", f"/v1/extractions/{job['id']}", params={"wait": 20}).json()
        return job


client = ExtractionClient("https://0vtusv00e4.execute-api.us-east-1.amazonaws.com/prod", api_key="slm_live_...")
job = client.extract("statement.pdf", document_type="bank_statement")
if job["status"] == "succeeded" and not job["warnings"]:
    for row in job["result"]["transactions"]:
        amount = Decimal(row["credit"] or "0") - Decimal(row["debit"] or "0")   # exact, never float
        ...
else:
    send_to_review(job)       # failed, or has warnings (section 8)
print("tokens used:", job.get("usage", {}).get("input_tokens"), "in /", job.get("usage", {}).get("output_tokens"), "out")
```

### Node.js (18+, built-in fetch)

```js
// Minimal client for the Document Extraction API.
import { readFile } from "node:fs/promises";
import { basename } from "node:path";

const FINAL = new Set(["succeeded", "failed", "expired"]);
const DIRECT_LIMIT = 4 * 1024 * 1024;

export class ExtractionClient {
  constructor(baseUrl, apiKey) { this.base = baseUrl.replace(/\/+$/, ""); this.key = apiKey; }

  async call(method, path, opts = {}) {            // retries network errors, 429 and 5xx
    for (let attempt = 0; ; attempt++) {
      try {
        const r = await fetch(this.base + path, { method, ...opts,
          headers: { Authorization: `Bearer ${this.key}`, ...(opts.headers || {}) } });
        if ((r.status !== 429 && r.status < 500) || attempt === 4) return r;
      } catch (e) { if (attempt === 4) throw e; }
      await new Promise(res => setTimeout(res, Math.min(30, 2 ** attempt) * 1000 + Math.random() * 1000));
    }
  }

  async extract(path, documentType, timeoutMs = 300_000) {
    const bytes = await readFile(path), name = basename(path);
    let r;
    if (bytes.length <= DIRECT_LIMIT) {                                   // direct flow
      const form = new FormData();
      if (documentType) form.append("document_type", documentType);
      form.append("file", new Blob([bytes]), name);
      r = await this.call("POST", "/v1/extractions?wait=25", { body: form });
    } else {                                                              // upload-link flow
      r = await this.call("POST", "/v1/extractions", { headers: { "Content-Type": "application/json" },
        body: JSON.stringify(documentType ? { document_type: documentType } : {}) });
      const created = await r.clone().json();
      const form = new FormData();
      for (const [k, v] of Object.entries(created.upload.fields)) form.append(k, v);
      form.append("file", new Blob([bytes]), name);                       // the file goes LAST
      const s3 = await fetch(created.upload.url, { method: "POST", body: form });
      if (!s3.ok) throw new Error(`S3 upload failed: HTTP ${s3.status}`);
    }
    if (!r.ok) throw new Error(`API error ${r.status}: ${await r.text()}`);
    let job = await r.json();
    const deadline = Date.now() + timeoutMs;
    while (!FINAL.has(job.status) && Date.now() < deadline) {
      job = await (await this.call("GET", `/v1/extractions/${job.id}?wait=20`)).json();
    }
    return job;
  }
}

// const client = new ExtractionClient(process.env.SLM_API, process.env.SLM_KEY);
// const job = await client.extract("statement.pdf", "bank_statement");
// Amounts are strings: use a decimal library (e.g. decimal.js), never Number, for money.
```

---

## 13. Integration checklist

- [ ] API key stored in a secret manager; used only server-side.
- [ ] Files ≤ 4 MB use the direct flow; larger files use the upload link.
- [ ] `document_type` sent whenever you know it.
- [ ] Polling uses `wait=20`, never a tight loop; or webhooks are used, with signatures verified.
- [ ] Retries only on network errors, `429` and `5xx`, with exponential backoff.
- [ ] Amounts parsed with a decimal type, never floating point.
- [ ] Results with `warnings`, or `failed` jobs, go to manual review. Scanned statements are treated as needing review when exactness matters (section 8).
- [ ] Masked values (`XXXX...`) are expected; don't treat them as errors.
- [ ] Your own copy of results follows your data-protection rules; jobs disappear from the API after the retention period.
- [ ] `usage` is recorded if you track AI consumption per document.
- [ ] Uptime monitored with `GET /healthz`.

---

## 14. For the platform operator: onboarding a client

These steps are run by the team operating the platform, not by the client.

1. **Create the organisation and its first key:**
   ```bash
   uv run python scripts/create_tenant.py --env prod --name "<CLIENT NAME>" --profile lyndev \
       [--webhook-url https://client.example.com/slm-events] [--retention-days 30] \
       [--verification-mode ai|checks_only] [--unmasked pan account] [--read-only]
   ```
   - The API key (and the webhook secret, if you passed a webhook URL) is printed **once**. Pass it to the client over a secure channel. `--key-file <path>` writes it to a private file instead of printing it.
   - Running the command again with the same name creates nothing.
2. **Send the client this guide and the console URL** (https://dcw2i73df06ry.cloudfront.net). The console is a browser app for testing: the client signs in with their own API key, which stays in their browser tab.
3. **Rotate or revoke keys** when a client asks, a key may have leaked, or someone with access leaves:
   ```bash
   uv run python scripts/manage_keys.py list   --env prod --tenant "<CLIENT NAME>" --profile lyndev
   uv run python scripts/manage_keys.py create --env prod --tenant "<CLIENT NAME>" --profile lyndev   # new key, shown once
   uv run python scripts/manage_keys.py revoke --env prod --tenant "<CLIENT NAME>" --key-id key_xxxxxxxx --profile lyndev
   ```
   A revoked key stops working within 60 seconds.
4. **Check the deployment** after every release: `uv run python scripts/smoke_test.py prod --profile lyndev --key-file ~/.slm/smoke.key`.
5. **Monitor:**
   - the CloudWatch dashboard `slm-prod`;
   - alarms (dead-letter queues, backlog, worker errors, API 5xx, security changes);
   - the monthly budget;
   - per-client usage, in the table items `TENANT#<id>` / `USAGE#<date>`: jobs, pages, tokens and estimated cost per day.

Deployment, operations and costs are described in [infra/cloudformation/README.md](../infra/cloudformation/README.md) and [cost.md](cost.md); internals in [implementation-flow.md](implementation-flow.md).
