# SLM Fintech Platform: Requirements

| | |
|---|---|
| Version | 0.1 (draft for review) |
| Date | 2026-10-13 |

## 1. Purpose

Fintech companies (our **tenants**) need to turn financial documents into structured data without sending that data to third-party AI services. This platform provides that as an API: a tenant sends a document and gets JSON back. The extraction runs on a small AI model that we host in our own AWS account in India.

A chatbot API is planned for a later phase. This release must leave room for it without building it.

## 2. Scope

### In scope (release 1)

- Document extraction API for five document types (see FR-3).
- Multi-tenant access by API key.
- Processing during business hours, with uploads accepted at any time.
- Storage of documents and results for model improvement, for tenants who opt in.
- A full local development environment that needs no AWS account and no GPU.
- Deployment to AWS ap-south-1 (Mumbai).

### Out of scope (release 1)

- Chatbot API. Routes are reserved and return `501 Not Implemented`.
- Custom document schemas defined by tenants.
- A web dashboard or admin UI. Tenants are managed with scripts.
- Human review screens.
- Model training pipeline. Data is collected now; training starts after the pilot.

## 3. Users

| User | Needs |
|---|---|
| Tenant developer | Clear API, predictable errors, webhooks, sandbox key for testing |
| Tenant compliance officer | Evidence of data isolation, retention control, masking, audit trail, India hosting |
| Platform operator (us) | Onboard tenants, rotate keys, watch queue and failures, control cost |
| ML engineer (us) | Labelled data, corrections, evaluation results per model version |

## 4. Functional requirements

Priority: **M** = must have for release 1, **S** = should have, **C** = could have.

### FR-1 Tenants and access

| ID | Requirement | Priority |
|---|---|---|
| FR-1.1 | Each request is authenticated with an API key sent as `Authorization: Bearer <key>`. | M |
| FR-1.2 | The tenant is determined only from the API key, never from request data. | M |
| FR-1.3 | API keys are shown once at creation and stored only as hashes. | M |
| FR-1.4 | A tenant can have several keys; each key can be revoked on its own. | M |
| FR-1.5 | Keys carry scopes: `extractions:read`, `extractions:write`. | M |
| FR-1.6 | A suspended tenant gets `403` on every request. | M |
| FR-1.7 | Each tenant has settings: retention days, training consent (with date), webhook URL and secret, fields allowed unmasked, model adapter. | M |
| FR-1.8 | Per-tenant request throttling. | S |
| FR-1.9 | Per-tenant IP allowlist. | C |

### FR-2 Extraction jobs

| ID | Requirement | Priority |
|---|---|---|
| FR-2.1 | A tenant can upload a file up to 5 MB in one multipart request and receive a job ID. | M |
| FR-2.2 | A tenant can request a presigned upload URL for files up to 25 MB, upload directly to storage, then submit the job. | M |
| FR-2.3 | Supported file types: PDF (digital or scanned), DOCX, JPG, PNG. Type is detected from file content, not the file name. | M |
| FR-2.4 | Unsupported, corrupt, encrypted or macro-enabled files are rejected with a specific error code. | M |
| FR-2.5 | Documents over 20 pages are rejected with `too_many_pages`. | M |
| FR-2.6 | Jobs are processed by a background worker. If a job is not finished when the response is sent, the response is `202 Accepted` with the job ID, status and, when outside business hours, the time processing will start. | M |
| FR-2.7 | A tenant can get one job with its status, page count, warnings and result. | M |
| FR-2.14 | **Wait for result.** Create and submit requests wait up to 40 seconds for the job to finish. If it finishes in time, the response is `200 OK` with the full result, so no second call is needed. Otherwise the response is `202 Accepted` (FR-2.6). The `wait` parameter (0–40 seconds, default 40) lets a client shorten or skip the wait. | M |
| FR-2.15 | Outside business hours, create and submit requests return `202` immediately, without waiting. | M |
| FR-2.16 | `GET /v1/extractions/{id}` accepts the same `wait` parameter (default 0), so a client that got `202` can call again and wait up to 40 seconds for the result. | M |
| FR-2.17 | Jobs whose client is waiting are processed before jobs nobody is waiting for, so a morning backlog does not hold up waiting requests. | S |
| FR-2.8 | A tenant can list its jobs, newest first, with paging. | M |
| FR-2.9 | A tenant can delete a job; this removes the file, result and corrections. | M |
| FR-2.10 | When a job finishes, the tenant's webhook receives an HMAC-signed event with the job ID and status only (no extracted data). | S |
| FR-2.11 | If the same tenant uploads an identical file for the same document type, the earlier result is reused without running the model. | S |
| FR-2.12 | A tenant can submit field corrections for a finished job. | M |
| FR-2.13 | A tenant can list supported document types and their JSON schemas. | M |

### FR-3 Document types

| ID | Type | Key fields | Consistency checks |
|---|---|---|---|
| FR-3.1 | `invoice` | number, dates, seller and buyer (name, GSTIN, PAN, address), line items (description, HSN/SAC, quantity, rate, amount), CGST/SGST/IGST, subtotal, total | line items sum to subtotal; subtotal + tax = total |
| FR-3.2 | `bank_statement` | bank, account holder, account number, IFSC, period, opening and closing balance, transactions (date, description, reference, debit, credit, balance) | opening + credits − debits = closing; running balance per row |
| FR-3.3 | `payslip` | employer, employee, employee ID, PAN, UAN, bank account, pay period, pay date, earnings, deductions, gross, net | earnings sum to gross; gross − deductions = net |
| FR-3.4 | `identity_document` | ID type (Aadhaar, PAN, passport, driving licence, voter ID, other), number, name, father or spouse name, date of birth, gender, address, issue and expiry dates, issuer | PAN format; document expired |
| FR-3.5 | `kyc_form` | name, date of birth, gender, father or spouse name, PAN, Aadhaar, CKYC number, mobile, email, current and permanent address, occupation, annual income, PEP status, nominee, signature present, form date | PAN format; required fields present |

General rules for all types:

| ID | Requirement | Priority |
|---|---|---|
| FR-3.6 | Fields not present in the document are returned as `null`. The model must not guess values. | M |
| FR-3.7 | Dates are returned as `YYYY-MM-DD`. Indian formats such as `30/09/2026` are normalised. | M |
| FR-3.8 | Amounts are returned as decimal strings (for example `"11800.00"`) to avoid rounding errors. | M |
| FR-3.9 | Failed consistency checks produce warnings on the job; they do not fail it. | M |
| FR-3.10 | Each type has a schema version. Results record the version used. | M |
| FR-3.11 | Adding a new document type requires only a new schema module. | S |

### FR-4 Masking

| ID | Requirement | Priority |
|---|---|---|
| FR-4.1 | Aadhaar numbers are always masked to `XXXX XXXX 1234` before being stored or returned. No tenant setting can turn this off. | M |
| FR-4.2 | For identity and KYC documents, any 12-digit Aadhaar-like number in any field is also masked. | M |
| FR-4.3 | PAN, bank account numbers, UAN, ID numbers and phone numbers are masked to the last 4 characters by default. | M |
| FR-4.4 | A tenant may be allowed to receive the fields in FR-4.3 unmasked, by kind (`pan`, `account`, `id_number`, `phone`). | S |

### FR-5 Business-hours processing

| ID | Requirement | Priority |
|---|---|---|
| FR-5.1 | The API accepts requests 24 hours a day, 7 days a week. | M |
| FR-5.2 | Documents are processed Monday to Friday, 09:00–19:00 IST. | M |
| FR-5.3 | Jobs submitted outside those hours wait in the queue and are processed from the next opening. | M |
| FR-5.4 | The processing window and time zone are configuration, not code. | M |
| FR-5.5 | Indian public holidays can be added as closed days. | C |

### FR-6 Training data

| ID | Requirement | Priority |
|---|---|---|
| FR-6.1 | Files are kept after processing only when the tenant has given training consent and the document type allows training. Otherwise the file is deleted once the result is stored. | M |
| FR-6.2 | `identity_document` and `kyc_form` files are excluded from training by default. | M |
| FR-6.3 | Corrections are stored with the job they belong to. | M |
| FR-6.4 | It must be possible to find and remove all data from one tenant or one job, including from training datasets. | M |

### FR-7 Usage

| ID | Requirement | Priority |
|---|---|---|
| FR-7.1 | Pages processed and model time are counted per tenant per day. | M |
| FR-7.2 | Usage can be exported for billing. | S |

### FR-8 Chat (reserved)

| ID | Requirement | Priority |
|---|---|---|
| FR-8.1 | `POST /v1/chat/sessions` and `POST /v1/chat/sessions/{id}/messages` exist, require authentication and return `501`. | M |
| FR-8.2 | The model gateway, tenancy and storage layers must be reusable by a chat module without changes. | M |

## 5. Non-functional requirements

### Security and privacy

| ID | Requirement |
|---|---|
| NFR-S1 | One tenant can never read, list, modify or infer another tenant's data. Isolation is enforced both in code and by AWS IAM. |
| NFR-S2 | All traffic uses TLS 1.2 or later. All stored data is encrypted with KMS. |
| NFR-S3 | Document content, extracted values and original file names never appear in logs, metrics or error messages. |
| NFR-S4 | Every API action is recorded in an audit log: tenant, key ID, action, resource ID, time, result. |
| NFR-S5 | Document text is treated as untrusted input. The model has no tools and its output is constrained to the schema. |
| NFR-S6 | Uploaded files are scanned for malware. |
| NFR-S7 | The model server has no internet access. |
| NFR-S8 | Validation errors never echo submitted values back. |
| NFR-S9 | Secrets are kept in AWS Secrets Manager, not in code or plain environment files in AWS. |

### Compliance (India)

| ID | Requirement |
|---|---|
| NFR-C1 | All data is stored and processed in AWS ap-south-1. An AWS policy denies use of other regions. |
| NFR-C2 | Training consent is recorded per tenant with a timestamp (DPDP Act 2023). |
| NFR-C3 | Aadhaar handling follows UIDAI rules (FR-4.1, FR-6.2). |
| NFR-C4 | Security and audit logs are kept for 180 days in India (CERT-In directions, 2022). |
| NFR-C5 | Retention per tenant is one of 7, 30, 90 or 365 days. Expired data is deleted automatically. |

Legal counsel must confirm NFR-C1 to NFR-C5 before the pilot.

### Performance and capacity

| ID | Requirement |
|---|---|
| NFR-P1 | Sized for 1,000 pages per day, with room to grow to 5,000 without redesign. |
| NFR-P2 | During business hours, with no backlog, digital PDFs, DOCX files and single photos of up to 4 pages return their result within the 40-second wait (`200`). Scanned documents of up to 4 pages complete within 2 minutes of submission. Targets to be confirmed in phase 3. |
| NFR-P3 | API responses that do not wait for a job return within 1 second at the 95th percentile. Waiting responses never exceed 40 seconds of waiting plus upload time. |
| NFR-P4 | All jobs waiting at 09:00 are cleared by 10:00 at 1,000 pages per day. |

### Reliability

| ID | Requirement |
|---|---|
| NFR-R1 | No submitted job is lost. Temporary failures are retried up to 3 times, then moved to a dead-letter queue and alerted. |
| NFR-R2 | Losing the GPU instance (for example a Spot interruption) delays jobs but does not fail them. |
| NFR-R3 | API availability target: 99.5% per month. |

### Cost

| ID | Requirement |
|---|---|
| NFR-K1 | Monthly AWS cost at 1,000 pages per day stays at or below about USD 150. |
| NFR-K2 | No component that charges by the hour runs outside business hours, except the VPC endpoints. |
| NFR-K3 | An AWS Budget alert fires at 80% and 100% of the monthly target. |

### Accuracy

| ID | Requirement |
|---|---|
| NFR-A1 | Field-level accuracy is measured per document type on a fixed labelled test set before release. |
| NFR-A2 | Targets per type are agreed in phase 3. Starting proposal: 95% for invoices and payslips, 90% for bank statement transactions, 95% for ID numbers and names. |
| NFR-A3 | A new model or adapter is released only if it matches or beats the current one on every type. |

### Operability

| ID | Requirement |
|---|---|
| NFR-O1 | The whole system runs locally with one command (`docker compose up`) and no AWS account. |
| NFR-O2 | The same code runs locally and in AWS; only configuration differs. |
| NFR-O3 | All AWS infrastructure is defined as code (Terraform). |
| NFR-O4 | Dashboards show queue depth, job failures, dead-letter count, GPU hours and cost. |

## 6. Constraints

- AWS only, in ap-south-1.
- Self-hosted open-weight model with a licence that allows commercial use. No third-party LLM APIs.
- One GPU (NVIDIA L4, 24 GB) for release 1.
- Development machines have no GPU (8 cores, 15 GB RAM).
- Libraries must have licences compatible with a closed-source SaaS (no AGPL).

## 7. Assumptions

- Tenants integrate server to server. No browser calls the API directly.
- Most documents are 1–4 pages. Bank statements may be longer, up to the 20-page limit.
- Most documents are in English. Hindi and regional-language content will appear and must be measured.
- Tenants accept results within minutes during business hours and next working morning outside them.
- Tenant clients can hold an HTTP request open for at least 45 seconds, and always handle both `200` (result included) and `202` (call again later).

## 8. Acceptance criteria for release 1

1. All **M** requirements are implemented and covered by automated tests.
2. The end-to-end test passes both locally and against AWS.
3. A test proves tenant A cannot access tenant B's jobs, files or results, with IAM enforcement turned on.
4. Accuracy on the labelled test set meets the targets agreed in phase 3.
5. Measured AWS cost for a week of pilot traffic projects to within NFR-K1.
6. Security review completed with no open high-severity findings.

## 9. Open questions

| Question | Owner |
|---|---|
| Labelled sample documents per type (30–50 each, anonymised) | Product |
| Process on Indian public holidays or not | Product |
| Result time promised to tenants in contracts | Product |
| Share of Hindi or regional-language documents | Product |
| Processor role, consent wording, Aadhaar Data Vault need | Legal |
