# SLM Fintech Platform: Requirements

| | |
|---|---|
| Version | 0.2 (draft for review) |
| Date | 2026-10-13 |
| Changes since 0.1 | Amazon Bedrock replaces the self-hosted model. Hosting in one AWS region (us-east-1 proposed). Rung 0: Python parsers for bank statements, verified by AI (FR-9). Presigned upload is the main flow, plus a direct flow for small files. `document_type` optional. 4 MB direct limit. Serverless on AWS Lambda. The reasons for each change are in [architecture.md](architecture.md) §2. |

## 1. Purpose

Fintech companies (our **tenants**) need to turn financial documents into structured data. This platform provides that as an API: a tenant sends a document and gets JSON back.

Most of the volume is **bank statements**. Digital statements from supported banks are read by deterministic Python code, and each result is verified by an AI model. Other documents are read by AI models on **Amazon Bedrock** in our own AWS account. Bedrock does not store prompts or outputs, does not use them for training and does not share them with the model provider.

A chatbot API is planned for a later phase. This release must leave room for it without building it.

## 2. Scope

### In scope (release 1)

- Document extraction API for five document types (see FR-3).
- Rule-based extraction of digital bank statements from supported banks, with AI verification (FR-9).
- Multi-tenant access by API key.
- Two upload flows: presigned upload (main) and direct upload for small files.
- Storage of documents and results for accuracy improvement, for tenants who opt in.
- A full local development environment that needs no AWS account and no GPU.
- Deployment to one AWS region (us-east-1 proposed), serverless on AWS Lambda.

### Out of scope (release 1)

- Chatbot API. Routes are reserved and return `501 Not Implemented`.
- Custom document schemas defined by tenants.
- A web dashboard or admin UI. Tenants are managed with scripts.
- Human review screens.
- Model fine-tuning. Collected data feeds evaluation and prompt improvement instead.
- Splitting files that contain several different documents.

## 3. Users

| User | Needs |
|---|---|
| Tenant developer | Clear API, predictable errors, webhooks, sandbox key for testing |
| Tenant compliance officer | Evidence of data isolation, retention control, masking, audit trail, AI data handling |
| Platform operator (us) | Onboard tenants, rotate keys, watch queues, failures and parser health, control cost |
| ML engineer (us) | Labelled data, corrections, evaluation results per model and parser version |

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
| FR-1.7 | Each tenant has these settings: retention days, training consent (with date), webhook URL and secret, fields allowed unmasked, **model profile**, and **Rung 0 verification mode** (`ai` or `checks_only`, FR-9.4). | M |
| FR-1.8 | Per-tenant request throttling. | S |
| FR-1.9 | Per-tenant IP allowlist. | C |

### FR-2 Extraction jobs

| ID | Requirement | Priority |
|---|---|---|
| FR-2.1 | **Direct flow.** A tenant can send a file of up to **4 MB** in one request, as multipart form data or as base64 in a JSON body. | M |
| FR-2.2 | **Presigned flow (main).** A tenant can create a job and receive a one-time upload URL for a file of up to 25 MB. The URL is valid for 15 minutes and works only for that job. Uploading the file starts processing; there is no separate submit call. | M |
| FR-2.3 | Supported file types: PDF (digital or scanned), DOCX, JPG, PNG. The type is detected from file content, not the file name. | M |
| FR-2.4 | Unsupported, corrupt, encrypted or macro-enabled files are rejected with a specific error code. | M |
| FR-2.5 | Documents over 20 pages are rejected with `too_many_pages`. | M |
| FR-2.6 | Jobs are processed by a background worker. A direct-flow request that has not finished when the wait ends returns `202 Accepted` with the job ID and status. Outside a configured processing window, the response also gives the time processing will start. | M |
| FR-2.7 | A tenant can get one job with its status, page count, warnings and result. | M |
| FR-2.8 | A tenant can list its jobs, newest first, with paging. | M |
| FR-2.9 | A tenant can delete a job; this removes the file, result and corrections. | M |
| FR-2.10 | When a job finishes, the tenant's webhook receives an HMAC-signed event with the job ID and status only (no extracted data). | S |
| FR-2.11 | If the same tenant uploads an identical file for the same document type, the earlier result is reused without processing it again. | S |
| FR-2.12 | A tenant can submit field corrections for a finished job. | M |
| FR-2.13 | A tenant can list supported document types and their JSON schemas. | M |
| FR-2.14 | **Wait for result (direct flow).** A direct-flow request waits up to 40 seconds for the job to finish (**currently 25 s**: API Gateway's default 29-second limit applies until the quota increase is approved; then `MaxWaitSeconds=40`). If it finishes in time, the response is `200 OK` with the full result. Otherwise it is `202 Accepted` (FR-2.6). The `wait` parameter (0–40 seconds, default 40) lets a client shorten or skip the wait. | M |
| FR-2.15 | If a processing window is configured and closed, direct-flow requests return `202` immediately, without waiting. | M |
| FR-2.16 | `GET /v1/extractions/{id}` accepts a `wait` parameter (0–40 seconds, default 0) and returns the job when it is final or the wait ends. This is how presigned-flow clients fetch results. | M |
| FR-2.17 | Jobs whose client is waiting are processed before jobs nobody is waiting for, so a backlog does not hold up waiting requests. | S |
| FR-2.18 | `document_type` is **optional**. If it is left out (or `auto`), the platform detects the type. The job records whether the type came from the client or was detected, with the detection confidence. | M |
| FR-2.19 | Each job reports one of these statuses: `awaiting_upload`, `scanning`, `queued`, `processing`, `succeeded`, `failed`, `expired`. A presigned job with no upload within 15 minutes becomes `expired`. | M |

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
| FR-3.6 | Fields not present in the document are returned as `null`. Values must never be guessed. | M |
| FR-3.7 | Dates are returned as `YYYY-MM-DD`. Indian formats such as `30/09/2026` are normalised. | M |
| FR-3.8 | Amounts are returned as decimal strings (for example `"11800.00"`) to avoid rounding errors. | M |
| FR-3.9 | Failed consistency checks produce warnings on the job; they do not fail it. | M |
| FR-3.10 | Each type has a schema version. Results record the version used. | M |
| FR-3.11 | Adding a new document type requires only a new schema module. | S |
| FR-3.12 | Each result records how it was produced: `parser` (with the parser ID and verifier) or `ai` (with the model ID). | M |

### FR-4 Masking

| ID | Requirement | Priority |
|---|---|---|
| FR-4.1 | Aadhaar numbers are always masked to `XXXX XXXX 1234` before being stored or returned. No tenant setting can turn this off. | M |
| FR-4.2 | For identity and KYC documents, and for any document showing Aadhaar markers, any 12-digit Aadhaar-like number in any field is also masked. | M |
| FR-4.3 | PAN, bank account numbers, UAN, ID numbers and phone numbers are masked to the last 4 characters by default. | M |
| FR-4.4 | A tenant may be allowed to receive the fields in FR-4.3 unmasked, by kind (`pan`, `account`, `id_number`, `phone`). | S |

### FR-5 Processing window

| ID | Requirement | Priority |
|---|---|---|
| FR-5.1 | The API accepts requests 24 hours a day, 7 days a week. | M |
| FR-5.2 | By default, documents are processed as soon as they arrive, at any time. | M |
| FR-5.3 | If a processing window is configured (for example Monday to Friday, 09:00–19:00 IST), jobs submitted outside it wait in the queue and are processed from the next opening. | S |
| FR-5.4 | The processing window and time zone are configuration, not code. | M |
| FR-5.5 | Public holidays can be added as closed days. | C |

### FR-6 Training data and accuracy improvement

| ID | Requirement | Priority |
|---|---|---|
| FR-6.1 | Files are kept after processing only when the tenant has given training consent and the document type allows it. Otherwise the file is deleted once the result is stored. | M |
| FR-6.2 | `identity_document` and `kyc_form` files are never kept by default. A document whose type was detected is kept only when the detection is highly confident and no identity markers are present. | M |
| FR-6.3 | Corrections are stored with the job they belong to. | M |
| FR-6.4 | It must be possible to find and remove all data from one tenant or one job, including kept copies and evaluation sets. | M |
| FR-6.5 | Kept data is used to build the evaluation set and anonymised examples for prompts. It is not used for model fine-tuning in release 1. | M |

### FR-7 Usage

| ID | Requirement | Priority |
|---|---|---|
| FR-7.1 | Pages processed, model tokens and estimated cost are counted per tenant per day. | M |
| FR-7.2 | Usage can be exported for billing. | S |
| FR-7.3 | Every job response reports its AI usage: model calls, input/output/cached tokens, and the estimated AI cost in USD at list prices, in total and per model (`usage`). *(Added 2026-10-14.)* | M |
| FR-7.4 | A browser **test console** lets a tenant upload documents with their own API key and see results, verification, tokens and AI cost. It is served over HTTPS only, and the API accepts browser calls only from its origin. *(Added 2026-10-14.)* | S |

### FR-8 Chat (reserved)

| ID | Requirement | Priority |
|---|---|---|
| FR-8.1 | `POST /v1/chat/sessions` and `POST /v1/chat/sessions/{id}/messages` exist, require authentication and return `501`. | M |
| FR-8.2 | The model gateway, tenancy and storage layers must be reusable by a chat module without changes. | M |

### FR-9 Rule-based extraction (Rung 0)

| ID | Requirement | Priority |
|---|---|---|
| FR-9.1 | Digital PDF bank statements whose layout matches a supported bank are extracted by a deterministic parser, without AI extraction. | M |
| FR-9.2 | A parser result is accepted only if all consistency checks (FR-3.2) and coverage checks pass: every row parsed, last balance equals closing balance, valid formats. | M |
| FR-9.3 | By default, every accepted parser result is also **verified by an AI model** against the document pages. Any disagreement rejects the parser result. *(Implemented as an independent reading: the model reads the key facts from the page images without seeing the parser output, and code compares them.)* | M |
| FR-9.4 | A tenant can choose `checks_only` verification. Rung 0 documents are then never sent to any AI service, and results are accepted on the deterministic checks alone. | S |
| FR-9.5 | A rejected parser result, or a parser error, never fails the job: the document is extracted by AI instead. The parser result is not passed to the AI. | M |
| FR-9.6 | Adding a bank, or a new version of a bank's layout, requires only a new parser module and its test fixtures. | M |
| FR-9.7 | Parser health is monitored per bank: share of statements accepted, check failures and verifier disagreements, with an alert above 20% disagreement. *(Status: rejections are logged with their reasons; the metric and alarm are not built yet.)* | M |
| FR-9.8 | Parsers for at least the 5 most common banks in the pilot samples are available at go-live. | S |
| FR-9.9 | **Every row and every field** of a digital bank statement (date, description, reference, amounts) and its header fields must match the PDF's own text, in order, with no row missing or extra, before a result passes, whether a parser or an AI produced it. Statement rows are reconciled with real transactions downstream, so nothing may pass wrongly. A parser result that fails is rejected; an AI result that fails climbs the ladder and, if it still fails, carries the warning `text_reconciliation_failed`. *(Added 2026-10-14.)* | M |

## 5. Non-functional requirements

### Security and privacy

| ID | Requirement |
|---|---|
| NFR-S1 | One tenant can never read, list, modify or infer another tenant's data. Isolation is enforced both in code and by AWS IAM. |
| NFR-S2 | All traffic uses TLS 1.2 or later. All stored data is encrypted with KMS. |
| NFR-S3 | Document content, extracted values and original file names never appear in logs, metrics or error messages. |
| NFR-S4 | Every API action is recorded in an audit log: tenant, key ID, action, resource ID, time, result. |
| NFR-S5 | Document text is treated as untrusted input. AI models have no tools, their output is validated against the schema, and an AI verifier can only reject a parser result, never change it. |
| NFR-S6 | Uploaded files are scanned for malware before processing. |
| NFR-S7 | AI models are called only through Amazon Bedrock, only the approved models, and only through the configured geographic inference profile. Bedrock model-invocation logging is turned off. |
| NFR-S8 | Validation errors never echo submitted values back. |
| NFR-S9 | Secrets are kept in AWS Secrets Manager, not in code or plain environment files in AWS. |
| NFR-S10 | The Bedrock data-retention mode is set to `none` (zero data retention) and protected by an organisation policy, so that no model that retains data can be used. |
| NFR-S11 | Our staff have no standing access to tenant documents or results. Emergency access needs a second approver, is time-limited and is alerted. |

### Compliance

| ID | Requirement |
|---|---|
| NFR-C1 | All data is stored in one AWS region (us-east-1 proposed). AI inference stays in that region's geography (US: us-east-1, us-east-2, us-west-2). An AWS policy denies use of other regions. |
| NFR-C2 | Training consent is recorded per tenant with a timestamp (DPDP Act 2023). |
| NFR-C3 | Aadhaar handling follows UIDAI rules (FR-4.1, FR-4.2, FR-6.2). |
| NFR-C4 | Security and audit logs are kept for 180 days, tamper-proof (CERT-In directions, 2022). |
| NFR-C5 | Retention per tenant is one of 7, 30, 90 or 365 days. Expired data is deleted automatically. |

Legal counsel must confirm NFR-C1 to NFR-C5 before the pilot, including whether hosting outside India is acceptable for each tenant.

### Performance and capacity

| ID | Requirement |
|---|---|
| NFR-P1 | Sized for 1,000 pages per day, with room to grow to 5,000 without redesign. |
| NFR-P2 | With no backlog, documents of up to 4 pages (digital or scanned) return their result within the 40-second wait in the direct flow, or are ready within 40 seconds of upload in the presigned flow. Targets to be confirmed in phase 0. |
| NFR-P3 | API responses that do not wait for a job return within 1 second at the 95th percentile. Waiting responses never exceed 40 seconds of waiting plus upload time. |
| NFR-P4 | If a processing window is configured, all jobs waiting at opening time are cleared within one hour at 1,000 pages per day. |

### Reliability

| ID | Requirement |
|---|---|
| NFR-R1 | No submitted job is lost. Temporary failures are retried up to 3 times, then moved to a dead-letter queue and alerted. |
| NFR-R2 | A failing or throttled AI model delays jobs but does not fail them. A failing parser falls back to AI extraction (FR-9.5). |
| NFR-R3 | API availability target: 99.5% per month. |

### Cost

| ID | Requirement |
|---|---|
| NFR-K1 | Monthly AWS cost at 1,000 pages per day stays at or below about USD 150. (Current estimate: ~USD 56; see [cost.md](cost.md).) |
| NFR-K2 | No component is billed by the hour. |
| NFR-K3 | An AWS Budget alert fires at 80% and 100% of the monthly target. |
| NFR-K4 | Each document is processed by the cheapest method that meets the accuracy targets: Rung 0 parsers first, then the cheapest suitable AI model, then stronger models only when checks fail. |

### Accuracy

| ID | Requirement |
|---|---|
| NFR-A1 | Field-level accuracy is measured per document type, per model and per bank parser, on a fixed labelled test set before release. |
| NFR-A2 | Targets per type are agreed in phase 3. Starting proposal: 95% for invoices and payslips, 90% for bank-statement transactions (99% for Rung 0 parser results), 95% for ID numbers and names. |
| NFR-A3 | A new model, profile or parser version is released only if it matches or beats the current one on every type. |
| NFR-A4 | The AI verifier's ability to catch errors is measured on fixtures with deliberately wrong fields, and reported with each release. |

### Operability

| ID | Requirement |
|---|---|
| NFR-O1 | The whole system can be developed and tested locally without an AWS account. *(Implemented as the moto-based test suite, `uv run pytest`: the real app on in-memory DynamoDB, S3 and SQS with a fake model. The originally planned `docker compose` stack was not needed; see architecture §11.)* |
| NFR-O2 | The same code runs locally and in AWS; only configuration differs. |
| NFR-O3 | All AWS infrastructure is defined as code (AWS CloudFormation). |
| NFR-O4 | Dashboards show queue depth, job failures, dead-letter count, Rung 0 parser health, escalation rate, Lambda errors and cost. |
| NFR-O5 | All application code runs on AWS Lambda. No servers or containers run continuously. |

## 6. Constraints

- AWS only, in one region (us-east-1 proposed).
- AI models only through Amazon Bedrock, with zero data retention. Only models that allow zero data retention may be used.
- Release 1 models: Amazon Nova Lite, Claude Haiku 5.5, Claude Sonnet 5.5 (US geographic profiles).
- Development machines have no GPU (8 cores, 15 GB RAM).
- Libraries must have licences compatible with a closed-source SaaS (no AGPL). In particular, use pypdfium2 and pdfplumber, not PyMuPDF.

## 7. Assumptions

- Tenants integrate server to server. No browser calls the API directly.
- About 70% of pages are bank statements, most of them digital PDFs downloaded from net banking, up to 20 pages each. Other documents are mostly 1–4 pages.
- A small number of banks account for most statements, so parsers for the top 5–8 banks cover most digital statements.
- Most documents are in English. Hindi and regional-language content will appear and must be measured.
- Tenants accept results within seconds to minutes.
- Direct-flow clients can hold an HTTP request open for at least 45 seconds, and handle both `200` (result included) and `202` (call again later).

## 8. Acceptance criteria for release 1

1. All **M** requirements are implemented and covered by automated tests.
2. The end-to-end test passes both locally and against AWS, for both upload flows.
3. A test proves tenant A cannot access tenant B's jobs, files or results, with IAM enforcement turned on.
4. Accuracy on the labelled test set meets the targets agreed in phase 3, including Rung 0 parser accuracy and the verifier catch rate.
5. Measured AWS cost for a week of pilot traffic projects to within NFR-K1.
6. Security review and penetration test completed with no open high-severity findings.

## 9. Open questions

| Question | Owner |
|---|---|
| Labelled sample documents per type (30–50 each, anonymised), including statements from the most common banks | Product |
| Actual document mix, and which banks' statements are most common | Product |
| Keep a processing window, or process at any time | Product |
| Result time promised to tenants in contracts | Product |
| Share of Hindi or regional-language documents | Product |
| Hosting region acceptable to each pilot tenant (us-east-1 or another region) | Product, Legal |
| Processor role, consent wording, Aadhaar Data Vault need | Legal |
