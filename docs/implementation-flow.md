# How a document flows through the implementation

| | |
|---|---|
| Audience | Developers joining the project (intern level). No prior knowledge of the code assumed. |
| Describes | The code **as it is** in `app/`, stage by stage: which module and function runs, on which AWS resource, what it reads and writes, and what happens when something goes wrong. |
| Verified by | [tests/integration/test_flow_e2e.py](../tests/integration/test_flow_e2e.py) runs every stage below end to end (real FastAPI app, in-memory AWS, fake AI model). Plus 105 unit tests. **120 tests pass.** |
| Related | Function-by-function call order: [execution-flow.md](execution-flow.md) · Design: [architecture.md](architecture.md) §4–6 · Infrastructure: [infra/cloudformation/README.md](../infra/cloudformation/README.md) · Security: [security.md](security.md) · Developer handbook: [implementation-guide.md](implementation-guide.md) |
| Date | 2026-10-14 |

---

## Contents

0. [The map: code, functions and AWS resources](#0-the-map-code-functions-and-aws-resources)
1. [Stage 1: the tenant chooses a flow](#stage-1-the-tenant-chooses-a-flow)
2. [Stage 2: edge (WAF and API Gateway)](#stage-2-edge-waf-and-api-gateway)
3. [Stage 3: authentication and the audit record](#stage-3-authentication-and-the-audit-record)
4. [Stage 4a: direct flow, the API takes the file](#stage-4a-direct-flow-the-api-takes-the-file)
5. [Stage 4b: presigned flow, upload, scan and intake](#stage-4b-presigned-flow-upload-scan-and-intake)
6. [Stage 5: queueing](#stage-5-queueing)
7. [Stage 6: the worker claims the job](#stage-6-the-worker-claims-the-job)
8. [Stage 7: preprocessing](#stage-7-preprocessing)
9. [Stage 8: Rung 0, Python parser + AI verification](#stage-8-rung-0-python-parser--ai-verification)
10. [Stage 9: document type](#stage-9-document-type)
11. [Stage 10: the AI model ladder](#stage-10-the-ai-model-ladder)
12. [Stage 11: normalise and mask](#stage-11-normalise-and-mask)
13. [Stage 12: store the result and clean up](#stage-12-store-the-result-and-clean-up)
14. [Stage 13: delivering the result](#stage-13-delivering-the-result)
15. [Stage 14: retention and deletion](#stage-14-retention-and-deletion)
16. [Cross-cutting: things every stage relies on](#cross-cutting-things-every-stage-relies-on)
17. [Job statuses: who changes what](#job-statuses-who-changes-what)
18. [Error codes and where they come from](#error-codes-and-where-they-come-from)
19. [What was verified, what was fixed, what is still open](#what-was-verified-what-was-fixed-what-is-still-open)

---

## 0. The map: code, functions and AWS resources

```
app/
├── main.py                 FastAPI app, error handlers, audit middleware, Mangum `handler`   ← api λ
├── lambda_handlers.py      the 7 Lambda entry points (one image, ImageConfig.Command picks one)
├── config.py               every setting, from SLM_* environment variables
├── auth/                   authorizer.py (authorizer λ), principal.py, dependencies.py
├── core/                   ids, errors, logging (SafeLogger), clock, secrets
├── storage/                aws.py (sessions), jobs.py (JobRepo), blobs.py (S3), queues.py, tenants.py, keys.py
├── gateway/                bedrock.py, ollama.py, fake.py: the ONLY code that talks to an AI model
├── modules/extraction/
│   ├── api.py, service.py  HTTP routes and their logic                                          ← api λ
│   ├── worker.py           one job from start to finish                                         ← worker λ
│   ├── pipeline.py         Rung 0 or AI ladder → checks → mask
│   ├── preprocess/         detect.py, pdf.py, image.py, docx.py
│   ├── parsers/            Rung 0: fingerprint, base, coverage, verify, bank/demo_bank_v1.py
│   ├── classify.py, checks.py, masking.py, normalise.py, prompts.py, window.py
│   ├── schemas/            one Pydantic model per document type
│   └── profiles/           default.yaml: the model ladder
├── intake.py               upload events → queued job; scan sweeper                             ← intake λ, scan-sweeper λ
├── webhooks.py             signed delivery                                                      ← webhook λ
├── retention.py            delete S3 files when DynamoDB TTL deletes a job                      ← retention-cleaner λ
├── usage.py, audit.py      metering and audit records
```

| Lambda (in `infra/cloudformation/app.yaml`) | Entry point in [lambda_handlers.py](../app/lambda_handlers.py) | Real function |
|---|---|---|
| `slm-<env>-authorizer` | `authorizer` | [auth/authorizer.py `handler`](../app/auth/authorizer.py#L44) |
| `slm-<env>-api` | `api` | [main.py `handler`](../app/main.py) (Mangum wrapping FastAPI `app`) |
| `slm-<env>-worker` | `worker` | [worker.py `handle_sqs_event`](../app/modules/extraction/worker.py#L194) |
| `slm-<env>-intake` | `intake` | [intake.py `handle_event`](../app/intake.py#L79) |
| `slm-<env>-scan-sweeper` | `scan_sweeper` | [intake.py `sweep`](../app/intake.py#L93) |
| `slm-<env>-webhook` | `webhook` | [webhooks.py `handle_sqs_event`](../app/webhooks.py#L96) |
| `slm-<env>-retention-cleaner` | `retention_cleaner` | [retention.py `handle_stream`](../app/retention.py#L24) |

**At a glance:**

| # | Stage | Runs in | Main code | AWS resources | Proven by test |
|---|---|---|---|---|---|
| 1 | Choose flow | tenant | n/a | n/a | n/a |
| 2 | Edge | AWS | n/a (infrastructure) | WAF, API Gateway | (infra) |
| 3 | Authenticate + audit | authorizer λ, api λ | `authorizer.handler`, `dependencies.require_scope`, `main.audit_every_request` | DynamoDB, Secrets Manager | `test_aws_path_...`, `test_no_or_bad_key_is_401`, `test_every_request_is_audited_...` |
| 4a | Direct flow | api λ | `api.create_extraction` → `service.create_direct` | S3, DynamoDB, SQS | `test_direct_flow_end_to_end`, `..._uses_rung0`, `..._415_...` |
| 4b | Presigned flow | api λ, intake λ, sweeper λ | `service.create_presigned`, `intake.on_object_created` / `on_scan_result` / `sweep` | S3, GuardDuty, EventBridge | `test_presigned_flow_end_to_end`, `..._malware_...`, `test_sweeper_expires_...` |
| 5 | Queue | api λ / intake λ | `queues.send_job` | SQS ×2 | all flow tests |
| 6 | Claim job | worker λ | `worker.process_job`, `JobRepo.claim` | DynamoDB, S3 | all flow tests, `..._scan_timeout` |
| 7 | Preprocess | worker λ | `preprocess.preprocess` | n/a | `test_detect.py`, flow tests |
| 8 | Rung 0 | worker λ | `parsers.try_rung0` | Bedrock (Nova Lite) | `test_rung0.py`, `..._uses_rung0` |
| 9 | Document type | worker λ | `classify.classify` | Bedrock | `test_ladder.py` |
| 10 | AI ladder | worker λ | `pipeline.run_ladder` | Bedrock | `test_ladder.py`, `test_bad_document_fails_...` |
| 11 | Normalise + mask | worker λ | `normalise.to_result`, `masking.mask` | n/a | `test_normalise.py`, `test_masking.py` |
| 12 | Store + clean up | worker λ | `JobRepo.complete`, `Blobs.delete`, `usage.record` | DynamoDB, S3, SQS | `test_direct_flow_end_to_end` |
| 13 | Deliver | api λ, webhook λ | `service.get_job`, `webhooks.deliver` | DynamoDB, SQS | flow tests |
| 14 | Retention | retention λ, api λ | `retention.handle_stream`, `service.delete_job` | DynamoDB stream, S3 | `test_retention_cleaner_...`, `test_direct_flow_end_to_end` |

---

## Stage 1: the tenant chooses a flow

There is **one endpoint**, `POST /v1/extractions`, and the request body decides the flow ([api.py `create_extraction`](../app/modules/extraction/api.py#L31)):

| The request contains | Flow | Answer |
|---|---|---|
| a file: multipart part `file`, or JSON `{"file": {"content_base64": "..."}}`, ≤ 4 MB | **direct** | `200` with the result if it is ready within `wait` seconds, otherwise `202` |
| no file | **presigned** | `201` with a one-time S3 upload URL (files up to 25 MB) |

Optional fields:
- `document_type`: `invoice`, `bank_statement`, `payslip`, `identity_document`, `kyc_form`, or `auto` / absent to detect it ([service.py `validate_doc_type`](../app/modules/extraction/service.py#L48));
- `wait`: 0–25 seconds today (`SLM_MAX_WAIT_SECONDS`; 40 once the API Gateway timeout quota is raised), a query parameter or JSON field.

---

## Stage 2: edge (WAF and API Gateway)

No application code runs here; this is all infrastructure (`app.yaml`).

- **AWS WAF** (prod) blocks an IP above 1,000 requests per 5 minutes, known exploit patterns, and IPs with a bad reputation.
- **API Gateway** (REST API) has one catch-all route, `ANY /v1/{proxy+}`, plus an unauthenticated `OPTIONS` route for browser pre-flight checks that the api function answers (CORS is off unless `CorsAllowOrigin` is set). It throttles the whole API, refuses requests without an `Authorization` header (401) and forwards everything else, through the authorizer, to the api Lambda. Uploads with `multipart/form-data` pass through as binary.

---

## Stage 3: authentication and the audit record

**In AWS (two Lambdas):**

1. API Gateway calls the **authorizer** Lambda: [authorizer.py `handler`](../app/auth/authorizer.py#L44).
   - [principal.py `parse_bearer`](../app/auth/principal.py#L33) takes the key out of `Authorization: Bearer slm_...`.
   - [principal.py `resolve_api_key`](../app/auth/principal.py#L41):
     1. hashes the key with [tenants.py `hash_api_key`](../app/storage/tenants.py#L66), HMAC-SHA256 with the **pepper** read by [core/secrets.py `api_key_pepper`](../app/core/secrets.py) from Secrets Manager;
     2. reads `APIKEY#<hash>` from DynamoDB;
     3. reads the tenant's `PROFILE` and checks that `status` is `active`.
   - It returns an IAM *Allow* policy for the whole stage, plus a context `{tenant_id, key_id, scopes}`. API Gateway caches the answer for 60 s.
   - A bad key raises `Exception("Unauthorized")`, which API Gateway turns into a 401. A suspended tenant gets an explicit *Deny*, which becomes a 403.
2. API Gateway calls the **api** Lambda. Mangum turns the event into a normal request for FastAPI.
3. Each route declares `Depends(require_scope("extractions:read" | "extractions:write"))` ([dependencies.py](../app/auth/dependencies.py#L42)). `current_principal` reads the tenant **from the authorizer context**, never from the request, and `Principal.require` checks the scope (403 `insufficient_scope`).

**Locally** there is no API Gateway, so `current_principal` calls the same `resolve_api_key` itself.

**Audit (NFR-S4).** [main.py `audit_every_request`](../app/main.py#L63) writes one record per request with:
- tenant, key id, route template (`GET /v1/extractions/{job_id}`), job id, result (`ok`, `denied`, `error:404`) and client IP;
- never request content, and never the key.

Business events (`extraction.create`, `ok:dedup`, `extraction.delete`) are audited as well, inside `service.py`. Records go through [audit.py](../app/audit.py) to the logger `audit`.

---

## Stage 4a: direct flow, the API takes the file

| Step | Code | What happens |
|---|---|---|
| Read the file | [api.py `create_extraction`](../app/modules/extraction/api.py#L31) | Multipart: reads at most 4 MB + 1 byte, so a bigger file is refused **without** being read fully (413). JSON: [service.py `decode_base64_strict`](../app/modules/extraction/service.py#L62) accepts a data-URL prefix and line breaks, checks the size **before** decoding (413), and rejects bad characters (400 `invalid_base64`). |
| Real file type | [preprocess/detect.py `detect_type`](../app/modules/extraction/preprocess/detect.py#L18) | Looks at the first bytes, never the file name. PDF, JPEG, PNG, or a ZIP containing `word/document.xml` (DOCX). Anything else → **415** before anything is stored. |
| Tenant settings | [tenants.py `get_tenant_settings`](../app/storage/tenants.py#L71) | Retention days, masking settings, verification mode, webhook. |
| Dedup (FR-2.11) | [jobs.py `find_dedup`](../app/storage/jobs.py#L198) | SHA-256 of the file. The same tenant sending the same file for the same type gets the earlier **succeeded** result at once (200, same job id, no model call). |
| Store the file | [blobs.py `Blobs.put`](../app/storage/blobs.py#L47) | S3 `tenants/<t>/jobs/<job>/source`, SSE-KMS with the documents key. |
| Create the job | [jobs.py `JobRepo.create`](../app/storage/jobs.py#L49) | DynamoDB `TENANT#<t>` / `JOB#<ext_...>`, status `queued`, flow `direct`, `expires_at` = now + retention (TTL). |
| Queue it | [queues.py `send_job`](../app/storage/queues.py#L28) | **interactive** queue if the client waits (`wait > 0`) and the processing window is open ([window.py `processing_open`](../app/modules/extraction/window.py#L35)); otherwise **background**. |
| Wait | [service.py `wait_for_job`](../app/modules/extraction/service.py#L114) | Strongly consistent reads, every 0.25 s rising to 1 s, until the job is final or `wait` (max `SLM_MAX_WAIT_SECONDS`) runs out. `asyncio.sleep` frees the server while it waits. |
| Answer | [service.py `public_job`](../app/modules/extraction/service.py#L94) | Only public fields (no PK/SK/TTL). `200` with `result` if succeeded, `error.code` if failed, else `202` + `Location: /v1/extractions/<id>`. Outside a processing window: `202` at once with `processing_starts_at`. |

The whole stage is in [service.py `create_direct`](../app/modules/extraction/service.py#L135).

---

## Stage 4b: presigned flow, upload, scan and intake

**1. Get an upload URL** ([service.py `create_presigned`](../app/modules/extraction/service.py#L183)):
- [blobs.py `Blobs.presigned_upload`](../app/storage/blobs.py#L94) signs an S3 *POST policy* with the **tenant's** credentials. It allows only that one key, 1 byte to 25 MB, our KMS key, and 15 minutes.
- [JobRepo.create](../app/storage/jobs.py#L49) creates the job with status `awaiting_upload` and puts it in the sparse **`pending`** index with a deadline of 15 + 5 minutes.
- The API answers `201` with `upload.url` and `upload.fields`. The tenant POSTs those fields with the file **last**. The upload never touches our API.

**2. The file arrives** ([intake.py](../app/intake.py), the intake Lambda, triggered by EventBridge):

| Event | Function | Status change |
|---|---|---|
| S3 *Object Created* | [`on_object_created`](../app/intake.py#L43) | `awaiting_upload` → `scanning` (new deadline: 5 minutes for the scan). [keys.py `parse_source_key`](../app/storage/keys.py) ignores keys that aren't job sources. |
| GuardDuty `NO_THREATS_FOUND` | [`on_scan_result`](../app/intake.py#L55) | `scanning` → `queued`, leaves the pending index, sent to the **interactive** queue |
| GuardDuty `THREATS_FOUND` | same | → `failed` / `malware_detected`; the object is **deleted**; a "failed" webhook is queued |
| GuardDuty other result | same | → `failed` / `scan_failed` (we can't prove the file is safe) |

Every change is conditional ([JobRepo.transition](../app/storage/jobs.py#L95)), so repeated or late events change nothing.

**3. Nothing arrives** ([intake.py `sweep`](../app/intake.py#L93), scan-sweeper Lambda, every 5 minutes). It queries only the `pending` index for deadlines in the past:
- `awaiting_upload` → `expired`;
- `scanning` → `failed` / `scan_timeout`, plus a webhook.

**4.** The client calls `GET /v1/extractions/<id>?wait=20` for the result (stage 13).

---

## Stage 5: queueing

- **What is sent:** [queues.py `send_job`](../app/storage/queues.py#L28) sends `{"tenant_id": ..., "job_id": ...}`. It never sends document content: a message is a pointer to work.
- **The two job queues:**
  - `jobs-interactive` is for clients who are waiting;
  - `jobs-background` holds the backlog.

  Each worker trigger has its own `MaximumConcurrency`, so the backlog can't slow down waiting clients.
- **Retries:** after 3 failed receives, a message moves to the dead-letter queue and an alarm fires.
- **Locally:** [worker.py `run_local_loop`](../app/modules/extraction/worker.py#L208) polls the queues (interactive first) and calls the same `process_job`.

---

## Stage 6: the worker claims the job

[worker.py `handle_sqs_event`](../app/modules/extraction/worker.py#L194) receives a batch (size 1) and calls [`process_job`](../app/modules/extraction/worker.py#L92) with the receive count. Failed messages are reported one by one (`batchItemFailures`), so SQS retries only those.

1. **Claim** ([JobRepo.claim](../app/storage/jobs.py#L132)): `queued` → `processing` with a **lease** of 150 s. If a worker crashes mid-job, the retry may take over once the lease has expired; otherwise the job would be stuck in `processing` forever. A job that is already finished, or held by another worker, is skipped (`job_skipped`). That makes duplicate SQS deliveries harmless.
2. **Direct flow only, in AWS only:** [`_wait_for_clean_scan`](../app/modules/extraction/worker.py#L62) waits up to 30 s for GuardDuty's tag `GuardDutyMalwareScanStatus`.
   - `NO_THREATS_FOUND`: carry on.
   - `THREATS_FOUND`: fail with `malware_detected` and delete the file.
   - No tag yet: `ScanPending`, the job goes back to `queued` and is retried; on the last attempt it fails with `scan_timeout`.
   - Presigned uploads skip this step, because intake only queued them after a clean scan.
3. **Read the file** with [Blobs.get](../app/storage/blobs.py#L52), using the tenant's session.

---

## Stage 7: preprocessing

[preprocess/__init__.py `preprocess`](../app/modules/extraction/preprocess/__init__.py#L39) turns the file into a list of **pages** `{"text", "image", "scanned"}`, wrapped in `Prepared`.

| Type | Code | Output per page | Protections |
|---|---|---|---|
| PDF | [pdf.py `pdf_pages`](../app/modules/extraction/preprocess/pdf.py#L49) (pypdfium2) | Text layer, if it has ≥ 50 characters; otherwise the page counts as **scanned**. Plus a JPEG rendered at 100 DPI (digital) or 150 DPI (scan) and downscaled to a 1,000 / 1,568 px long edge. | Password → `encrypted_document`; broken → `corrupt_file`; > 20 pages → `too_many_pages`; render size capped at 4,000 px; PDF JavaScript is never run |
| JPEG / PNG | [image.py `prepare_image`](../app/modules/extraction/preprocess/image.py#L37) (Pillow) | One scanned page: rotation fixed from EXIF, downscaled to 1,568 px, re-encoded as JPEG (which drops metadata such as GPS) | Decompression bombs → `file_too_complex`; unreadable → `corrupt_file` |
| DOCX | [docx.py `docx_pages`](../app/modules/extraction/preprocess/docx.py#L59) (python-docx) | Text (paragraphs, then tables), ~3,000 characters per "page" | [`check_zip`](../app/modules/extraction/preprocess/docx.py#L26) first: too many entries, too large, too compressed (zip bomb) → `file_too_complex`; macros → `macro_not_allowed` |

Why images are downscaled: models bill images by pixel count, and a 4,000 px photo costs about 6× more than a 1,568 px one without being read any better.

`Prepared.is_digital_pdf` is true only when **every** page has a text layer. That is the entry condition for Rung 0.

---

## Stage 8: Rung 0, Python parser + AI verification

Entry: [pipeline.py `extract_document`](../app/modules/extraction/pipeline.py#L149) calls [parsers/__init__.py `try_rung0`](../app/modules/extraction/parsers/__init__.py#L41) when Rung 0 is enabled in the profile and the requested type is `bank_statement` or not given.

| Step | Code | Rejects the parser result when |
|---|---|---|
| 1. Eligible? | `Prepared.is_digital_pdf` | any page is scanned (no attempt) |
| 2. Which bank layout? | [fingerprint.py `pick_parser`](../app/modules/extraction/parsers/fingerprint.py#L20) → `StatementParser.matches` | page 1 doesn't contain all marker texts **and** all column labels (no attempt) |
| 3. Parse | [parsers/base.py `StatementParser.parse`](../app/modules/extraction/parsers/base.py) (pdfplumber word positions → lines → columns; a line starting with a date starts a new row) | the parser crashes (logged as `rung0_parser_error`; **never fails the job**) |
| 4. Schema | `BankStatement.model_validate` ([schemas/bank_statement.py](../app/modules/extraction/schemas/bank_statement.py)) | a date or amount can't be read (`schema_invalid`) |
| 5. Arithmetic | [checks.py `check_bank_statement`](../app/modules/extraction/checks.py#L65) | running balance wrong on any row; opening + credits − debits ≠ closing; bad IFSC |
| 6. Coverage | [coverage.py `coverage_problems`](../app/modules/extraction/parsers/coverage.py#L27) | parsed rows ≠ date-led lines on the pages; last balance ≠ closing; header field missing |
| 6b. Text reconciliation | [reconcile.py `reconcile`](../app/modules/extraction/parsers/reconcile.py) | any row's date, amounts, reference or description, or any header field, doesn't match the PDF text re-read by pypdfium2 (a different library from the parser's pdfplumber), in order, with no row missing or extra. No AI |
| 7. AI check | [verify.py `verify`](../app/modules/extraction/parsers/verify.py) with **Nova Lite** (~150 output tokens, temperature 0) | **Read independently, compare in code.** Nova reads the key facts itself (holder, account number, IFSC, branch, period, first and last transaction) without seeing the parser's output; [`compare`](../app/modules/extraction/parsers/verify.py) checks them against the parser (case, spaces and date formats normalised). Any mismatch, a required field it couldn't read, or an unreadable answer rejects. The verifier can only **reject**, never change a value. Amounts are not re-read: arithmetic already proved them. |

- **Skipped verification:** with tenant setting `verification_mode = checks_only`, step 7 is skipped. The statement then never reaches any AI service.
- **Accepted result:** method `parser`, `parser_id` (e.g. `demo_bank_v1`), `verified_by` = the Nova Lite model id.
- **Rejected result:** the document goes to the AI ladder starting at **Haiku** (`rung0.fallback_rung`), because Nova has already seen it.

---

## Stage 9: document type

In [`extract_document`](../app/modules/extraction/pipeline.py#L149):

- **The tenant sent a type:** it is used, with source `client`.
- **A Rung 0 parser matched the layout** (even if its result was then rejected): `bank_statement`.
- **Otherwise:** [classify.py `classify`](../app/modules/extraction/classify.py#L32) asks about **page 1 only** (image + first 1,500 characters, max 100 output tokens):
  1. Nova Lite first; if its answer is unreadable, below 0.8 confidence, or `other` → Claude Haiku;
  2. still no answer at 0.8 or above: the best answer at 0.5 or above is accepted, with its low confidence recorded;
  3. otherwise the job fails with `unrecognised_document_type`. The tokens of these calls are still metered.

---

## Stage 10: the AI model ladder

[pipeline.py `run_ladder`](../app/modules/extraction/pipeline.py#L111), driven by the profile [profiles/default.yaml](../app/modules/extraction/profiles/default.yaml) (loaded by [`load_profile`](../app/modules/extraction/profiles/__init__.py#L37)).

**The plan** ([`attempt_plan`](../app/modules/extraction/pipeline.py#L64)): start on `start_rung[doc_type]` (Haiku for every type until the phase-0 evaluation says otherwise). Haiku gets one retry **with thinking**, then the ladder climbs, with at most 1 + `max_climbs` = 3 attempts:

```
Haiku 5.5  →  Haiku 5.5 + thinking  →  Sonnet 5.5
```

**Each attempt:**

| Step | Code | Passes when |
|---|---|---|
| Build the prompt | [prompts.py `system_prompt`](../app/modules/extraction/prompts.py#L35) (rules + JSON schema; cached by Bedrock) and [`instruction`](../app/modules/extraction/prompts.py#L40) (the request, plus what was wrong last time) | n/a |
| Call the model | [gateway `get_gateway`](../app/gateway/__init__.py#L13) → [bedrock.py `BedrockGateway.generate`](../app/gateway/bedrock.py#L55) (Bedrock **Converse** API; pages wrapped in `<page>` tags so document text stays data; refuses model ids outside the approved list) | n/a |
| Gate 1: JSON | [`parse_and_validate`](../app/modules/extraction/pipeline.py#L95) + `strip_code_fences` | the answer is one JSON object and wasn't cut off (`max_tokens`) |
| Gate 2: schema | `model.model_validate` (Pydantic, [schemas/](../app/modules/extraction/schemas/)) | every field has the right shape; dates and amounts normalise; unknown fields are **dropped** |
| Gate 3: checks | [checks.py `run_checks`](../app/modules/extraction/checks.py#L147) | sums add up within 0.01; PAN, GSTIN and IFSC formats are valid |
| Gate 3b: text reconciliation | [pipeline.py `text_reconciliation_failures`](../app/modules/extraction/pipeline.py) | digital bank statements only: every AI-extracted row must match the PDF text exactly (same check as Rung 0 step 6b). A mismatch counts as a failed check: the ladder climbs, and at the top it becomes the warning `text_reconciliation_failed` |

- **A gate fails:** the problems go into the next attempt's instruction. Field names and error types are included, plus check messages with numbers (model only), but **never** the document values from validation errors.
- **A pass:** the ladder stops.
- **No pass after the last attempt:** the result with the fewest failed checks is kept, and the failed check codes become `warnings`.
- **Not counted as failures:** `document_expired` and `required_fields_missing` describe the document, not the extraction, so they never trigger a climb.
- **No usable JSON from any attempt:** the job fails with `extraction_failed`, and its tokens are still metered.
- **Bedrock throttling or a timeout:** `TemporaryError`, and the job is retried through SQS.

---

## Stage 11: normalise and mask

The last steps before anything is stored ([`extract_document`](../app/modules/extraction/pipeline.py#L149)):

1. **Normalise.** [normalise.py `to_result`](../app/modules/extraction/normalise.py#L17) dumps the model with every field present, using `null` for missing ones (FR-3.6). Dates become `YYYY-MM-DD` and amounts become 2-decimal strings, through the field types in [schemas/base.py](../app/modules/extraction/schemas/base.py): `parse_date` tries 13 Indian date formats, and `parse_money` handles `₹1,18,000.50`, `(250.00)` and `Cr`/`Dr`.
2. **Type sanity check.** If the tenant named a type but the result is completely empty, the warning `document_type_mismatch` is added.
3. **Mask** with [masking.py `mask`](../app/modules/extraction/masking.py#L59), which walks every field:
   - **Aadhaar fields:** always `XXXX XXXX 1234`. No setting can turn this off.
   - **PAN, account, UAN, ID and phone fields:** last 4 characters only, unless the tenant's `unmasked_kinds` allows that kind.
   - **Strict mode:** for identity and KYC documents, or when the page or result mentions Aadhaar/UIDAI, **any** 12-digit Aadhaar-like number in **any** text field is masked. Otherwise 12-digit UPI references on bank statements are kept.

The Rung 0 path is masked with the same function.

---

## Stage 12: store the result and clean up

Back in [`process_job`](../app/modules/extraction/worker.py#L92):

| Step | Code | Detail |
|---|---|---|
| Store the result | [JobRepo.complete](../app/storage/jobs.py#L157) | `processing` → `succeeded`. The result sits **inline** in the item if ≤ 100 KB, otherwise in S3 `.../result.json` with `result_key`. Also stores how it was produced: `method`, `model_id`, `parser_id`, `verified_by`, `profile_id`, `prompt_version`, `warnings`, `page_count`, and **`usage`**: the job's token counts and `estimated_cost_usd` (AI cost at list prices), in total and per model ([usage.py `summarize`](../app/usage.py)), returned to the tenant with the result. |
| Training copy | [`_keep_for_training`](../app/modules/extraction/worker.py#L80) → [Blobs.copy](../app/storage/blobs.py#L88) | Only with the tenant's written consent; never identity or KYC documents; never when the page shows Aadhaar markers; for detected types only at ≥ 0.95 confidence. |
| Delete the source | [Blobs.delete](../app/storage/blobs.py#L72) | Always, once processed (or after a permanent failure). |
| Dedup entry | [JobRepo.save_dedup](../app/storage/jobs.py#L203) | So the same file is answered instantly next time. |
| Usage | [usage.py `record`](../app/usage.py#L35) | Atomic `ADD` on `TENANT#<t>` / `USAGE#<day>`: jobs, pages, tokens, model calls, estimated USD, `jobs_ai` or `jobs_parser`. Failed jobs that used a model are metered too. |
| Webhook | [webhooks.py `enqueue`](../app/webhooks.py#L70) | `{tenant_id, job_id, status}` on the webhooks queue. |
| Log | `log.info("job_succeeded", ...)` | ids, type, method, pages, warning count, duration. **No values.** |

**When something fails:**

| Raised | Meaning | Outcome |
|---|---|---|
| `JobError(code)` | the document itself is the problem | job `failed` with `code` and the `usage` of any model calls already made, source deleted, webhook, **no retry** |
| `ScanPending` | GuardDuty tag not there yet | back to `queued`, retried; last attempt → `failed` / `scan_timeout` |
| `TemporaryError` or any other exception | throttling, a timeout, a bug | back to `queued`, retried; after 3 attempts → `failed` / `processing_error`, and the message goes to the DLQ (alarm) |

---

## Stage 13: delivering the result

**Polling** (`GET /v1/extractions/<id>?wait=0..40`):
- [service.py `get_job`](../app/modules/extraction/service.py#L216) checks the id format first ([ids.py `is_job_id`](../app/core/ids.py)), waits with `wait_for_job`, and returns `public_job`.
- Results come back with their fields in the document type's schema order ([service.py `_schema_order`](../app/modules/extraction/service.py)); DynamoDB itself doesn't keep key order.
- **Another tenant's id returns 404**, exactly like an id that doesn't exist, so nobody can tell whether it exists. The query runs under the caller's tenant key, and the IAM tenant role would refuse it anyway.
- `Retry-After: 2` is sent while the job is not final.
- Results stored in S3 are read back transparently.

**Webhook** (webhook Lambda, [webhooks.py `handle_sqs_event`](../app/webhooks.py#L96) → [`deliver`](../app/webhooks.py#L75)):
1. Reads the tenant's `webhook_url` and secret id ([core/secrets.py `webhook_secret`](../app/core/secrets.py)). No webhook configured means nothing to do.
2. [`is_safe_url`](../app/webhooks.py#L54): `https://` only, and the host must resolve to **public** IP addresses. This blocks SSRF to internal addresses. Redirects are never followed.
3. Body: `{"event": "extraction.succeeded", "job_id", "status", "occurred_at"}`. **No extracted data.**
4. Header `X-Signature: t=<unix>,v1=<HMAC-SHA256(secret, "<t>." + body)>` ([`sign`](../app/webhooks.py#L48)). The timestamp is inside the signature, so old events can't be replayed.
5. A non-2xx answer is retried by SQS, up to 5 times, then goes to the DLQ (alarm).

---

## Stage 14: retention and deletion

- **Automatic.** Every job item has `expires_at` = creation + retention (7, 30, 90 or 365 days). DynamoDB **TTL** deletes it, usually within a few days of expiry. Until then the item still exists, so **every read checks `expires_at` itself** ([jobs.py `_expired`](../app/storage/jobs.py)): from the moment retention ends, the job, its result and its dedup entry behave as already deleted (`get`, `list`, `find_dedup`, and the worker skips a job that expired while queued). The table stream, filtered to TTL deletions, invokes [retention.py `handle_stream`](../app/retention.py#L24), which deletes the job's S3 prefix with [Blobs.delete_prefix](../app/storage/blobs.py#L76). Corrections and dedup items carry the same `expires_at`. An S3 lifecycle rule (400 days) is the backstop.
- **On request.** `DELETE /v1/extractions/<id>` → [service.py `delete_job`](../app/modules/extraction/service.py#L236) deletes the S3 job prefix, any training copy, the job item and all its corrections ([JobRepo.delete](../app/storage/jobs.py#L174)), and writes an audit record.
- **Corrections.** `POST /v1/extractions/<id>/corrections` ([service.py `add_corrections`](../app/modules/extraction/service.py#L253)) stores `{path, value}` pairs (max 500), only for succeeded jobs (409 otherwise). They expire with the job.

---

## Cross-cutting: things every stage relies on

| Concern | Code | How it works |
|---|---|---|
| **Settings** | [config.py](../app/config.py) | Every value from an `SLM_*` environment variable (set by `app.yaml` in AWS; in tests by `tests/conftest.py`). `settings.is_local` turns off IAM role switching, KMS and the GuardDuty wait. |
| **Tenant isolation** | [storage/aws.py `tenant_session`](../app/storage/aws.py#L49) | All tenant data access (`JobRepo`, `Blobs`, `get_tenant_settings`, `usage.record`) goes through a session of the `tenant-data` role tagged `tenant_id=<t>`. IAM allows only `TENANT#<t>` keys and `tenants/<t>/` objects. Sessions last 1 hour and are renewed while more than 16 minutes remain, so presigned URLs stay valid. `Blobs._check` also refuses keys outside the prefix in code. |
| **Ids** | [core/ids.py](../app/core/ids.py) | `ext_<ULID>` jobs (time-sortable, so listing is newest first), `t_<ulid>` tenants, `key_...` keys. Ids are validated before they are used in a database key. |
| **Error shape** | [main.py](../app/main.py#L28) | Every error is `{"error": {"code", "message"}}`. Validation errors list the field and error type only, never the submitted value (NFR-S8). Unexpected errors log the exception **type** only. |
| **Logging** | [core/logging.py `SafeLogger`](../app/core/logging.py) | JSON lines to stdout (CloudWatch in AWS). Only allowlisted field names survive; anything else is dropped silently. `print()` is forbidden by the linter. |
| **Model gateway** | [gateway/](../app/gateway/) | `SLM_MODEL_PROVIDER` = `bedrock` (AWS), `ollama` (laptop) or `fake` (tests). Nothing else calls an AI model. |
| **Processing window** | [window.py](../app/modules/extraction/window.py) | `always` by default. With a window, the API answers 202 at once outside it, and Scheduler pauses the worker triggers (`app.yaml`). |

---

## Job statuses: who changes what

```
                 create_presigned                     create_direct
                       │                                   │
                       ▼                                   │
              awaiting_upload ──(sweep: deadline)──► expired
                       │ on_object_created                 │
                       ▼                                   │
                   scanning ──(sweep: deadline)──► failed: scan_timeout
                       │  on_scan_result                   │
      THREATS_FOUND ◄──┤                                   │
  failed: malware_...  │ NO_THREATS_FOUND                  │
                       ▼                                   ▼
                    queued ◄─────────────────────────── queued
                       │ JobRepo.claim (lease 150 s)
                       ▼
                  processing ──(TemporaryError / ScanPending, attempts left)──► queued
                       │
          ┌────────────┼──────────────────────────┐
          ▼            ▼                          ▼
     succeeded   failed: <JobError code>   failed: processing_error / scan_timeout (last attempt)
```

Every arrow is a **conditional** DynamoDB update ([JobRepo.transition](../app/storage/jobs.py#L95)): "set status X only if it is currently Y". If two processes race, exactly one wins, and the other gets `False` and does nothing. Statuses only move forward.

---

## Error codes and where they come from

**Returned by the API immediately** (`ApiError`):

| HTTP | `code` | Raised in |
|---|---|---|
| 400 | `invalid_json`, `invalid_base64`, `empty_file`, `invalid_document_type`, `invalid_cursor`, `invalid_correction`, `invalid_request` | `api.py`, `service.py`, `main.py` (validation) |
| 401 | `unauthorized` | `principal.py`, `dependencies.py`, API Gateway |
| 403 | `tenant_suspended`, `insufficient_scope` | `principal.py` |
| 404 | `not_found` (also for other tenants' ids) | `service.py` |
| 409 | `job_not_finished` | `service.add_corrections` |
| 413 | `file_too_large` | `api.py`, `service.py` |
| 415 | `unsupported_file_type` | `preprocess/detect.py` |
| 429 | `rate_limited` | API Gateway stage throttling |
| 403 | (WAF's own response, no JSON body) | WAF: IP over the rate limit, or a bad-input / bad-reputation match |
| 500 | `internal_error` | `main.py` |
| 501 | `not_implemented` | `modules/chat/api.py` (reserved chat routes) |

**On a failed job** (`job.error.code`, set by the worker or intake):

| `code` | Cause | Raised in |
|---|---|---|
| `unsupported_file_type` | bytes are not PDF, DOCX, JPEG or PNG | `preprocess/__init__.py` |
| `corrupt_file`, `encrypted_document`, `too_many_pages` | the file can't be opened, needs a password, or has > 20 pages | `preprocess/pdf.py`, `image.py`, `docx.py` |
| `file_too_complex`, `macro_not_allowed` | decompression or zip bomb; DOCX with macros | `image.py`, `docx.py` |
| `unrecognised_document_type` | the classifier can't tell what it is | `classify.py` |
| `extraction_failed` | no model returned usable JSON | `pipeline.run_ladder` |
| `malware_detected`, `scan_failed`, `scan_timeout` | GuardDuty verdict, or none in time | `intake.py`, `worker.py` |
| `processing_error` | unexpected error on the last attempt | `worker.py` |

**Warnings** (`job.warnings`, the job still succeeds):
- failed check codes, such as `total_mismatch`, `running_balance_mismatch` and `pan_format`;
- `document_expired`;
- `required_fields_missing`;
- `document_type_mismatch`.

---

## What was verified, what was fixed, what is still open

**Verified on 2026-10-14** by [tests/integration/test_flow_e2e.py](../tests/integration/test_flow_e2e.py), all passing. It uses the real app, moto for DynamoDB, S3 and SQS, and the FakeGateway for the model.

| Test | Proves |
|---|---|
| `test_no_or_bad_key_is_401`, `test_health_needs_no_key` | stage 3 locally |
| `test_aws_path_authorizer_then_api_lambda` | stage 3 **the AWS way**: real API Gateway event shapes, authorizer policy and context, Mangum, and 401 when the context is missing |
| `test_direct_flow_end_to_end` | stages 4a, 5–13 for an image invoice through the AI path: queue choice, masking, source deleted, usage counters, webhook, dedup, list, delete |
| `test_direct_flow_bank_statement_uses_rung0` | multipart upload → Rung 0 parser → one Nova Lite verification call only |
| `test_direct_flow_unsupported_file_is_415_...` | rejected before anything is stored |
| `test_bad_document_fails_job_with_code` | ladder exhausted → `extraction_failed` |
| `test_direct_flow_scan_never_arrives_...` | stage 6 GuardDuty wait → retry → `scan_timeout` |
| `test_presigned_flow_end_to_end` | stage 4b: `awaiting_upload` → `scanning` → `queued` → `succeeded` |
| `test_presigned_upload_with_malware_...` | malware → failed, file deleted, never reaches the worker |
| `test_sweeper_expires_upload_...` | the sweeper expires uploads that never arrived |
| `test_retention_cleaner_...` | stage 14: TTL stream record → S3 files deleted |
| `test_expired_job_is_invisible_before_ttl_...` | stage 14: once retention ends the job is 404, gone from the list, and no longer used for dedup, even though TTL hasn't deleted it yet |
| `test_other_tenant_gets_404_...` | isolation: get, delete, corrections and list across tenants all fail |
| `test_every_request_is_audited_...` | one audit record per request, route template only, never the key |

**Fixed during this verification:**
1. **Audit gap (NFR-S4).** Only create and delete were audited, though `require_scope` already saved the caller for an audit middleware that didn't exist. Added `audit_every_request` in `main.py`.
2. **Wrong failure reason.** A direct-flow file whose malware scan never finished failed as `processing_error`, and each retry logged an error. It now has its own handling and fails with `scan_timeout`.
3. **Retention wasn't exact (NFR-C5).** DynamoDB TTL deletes expired items only "usually within a few days", and no read checked `expires_at`, so an expired job and its result could still be fetched for days. `JobRepo` now treats expired items as deleted in `get`, `list` and `find_dedup`, and the worker skips a job that expired while queued.
4. **Earlier today** (infrastructure review): tenant sessions now outlive the presigned URLs they sign.

**Verified on real AWS** (lyndev account, us-east-1, 2026-10-14, end-to-end smoke test):
- the whole presigned flow: S3 upload → GuardDuty scan → EventBridge → intake → worker → result in ~9 s;
- Rung 0 on digital statements (1 and 2 pages) with Nova Lite verification, in ~5–8 s;
- the AI ladder on Claude Haiku 5.5, plus Sonnet 5.5 and thinking retries;
- the Converse request format per model (`bedrock.py` "CONFIRMED"): Claude 5.5 rejects `temperature`; thinking off is `disabled` on Haiku but `between_tools` on Sonnet;
- token usage in responses.

**Not verified yet:**
- the field names of GuardDuty's scan-result event (noted in `intake.py`);
- IAM enforcement itself: moto doesn't evaluate IAM policies, so the tests prove the **code** isolation, and the AWS dev environment must prove the **IAM** layer (security.md §4.4);
- the timing of the 40-second wait behind API Gateway.
