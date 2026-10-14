# Execution Flow: what runs, in what order, and what each function does

| | |
|---|---|
| Audience | Developers who need to follow the code call by call (debugging, reviews, changes) |
| Describes | The exact order of function calls for every entry point of the platform, as implemented (2026-10-14) |
| Companion docs | Stage-level overview: [implementation-flow.md](implementation-flow.md) · Repository and tasks: [implementation-guide.md](implementation-guide.md) |

## How to read this document

Each trace is a numbered call sequence. Indentation means "called by the line above".

| Mark | Meaning |
|---|---|
| `→` | a function call in our code (file and function name) |
| `⇢` | a call to an AWS service (what is read or written) |
| `✗` | a failure branch: what happens instead |
| **[cached]** | the result is reused for later calls in the same Lambda container |

Contents:

0. [Cold start: what runs when a Lambda container starts](#0-cold-start-what-runs-when-a-lambda-container-starts)
1. [Trace A: POST /v1/extractions with a file (direct flow)](#trace-a-post-v1extractions-with-a-file-direct-flow)
2. [Trace B: the worker processes one job](#trace-b-the-worker-processes-one-job)
3. [Trace C: POST /v1/extractions without a file (upload-link flow)](#trace-c-post-v1extractions-without-a-file-upload-link-flow)
4. [Trace D: GET, list, delete, corrections](#trace-d-get-list-delete-corrections)
5. [Trace E: webhook delivery](#trace-e-webhook-delivery)
6. [Trace F: retention clean-up](#trace-f-retention-clean-up)
7. [Trace G: scan sweeper (every 5 minutes)](#trace-g-scan-sweeper-every-5-minutes)
8. [Trace H: operator scripts](#trace-h-operator-scripts)
9. [Timeline of one document, end to end](#9-timeline-of-one-document-end-to-end)

---

## 0. Cold start: what runs when a Lambda container starts

All 7 functions run the same zip. Each function's **Handler** names an attribute of [app/lambda_handlers.py](../app/lambda_handlers.py), for example `app.lambda_handlers.worker`. When a new container starts, Lambda imports that module once:

1. `import app.lambda_handlers` imports **every** entry-point module:
   `auth.authorizer`, `intake`, `main`, `modules.extraction.worker`, `retention` and `webhooks`.
2. That import chain runs these, once per container:
   1. **`app/config.py`** creates `settings = Settings()`, which reads every `SLM_*` environment variable **[cached]** for the container's lifetime.
   2. **`core/logging.get_logger(...)`**, on its first call, runs `_configure_root`: one stdout handler, JSON lines, and noisy libraries (boto3, pdfminer, httpx) set to WARNING.
   3. **`app/main.py`** builds the FastAPI `app`:
      - registers the error handlers;
      - the `audit_every_request` middleware;
      - `CORSMiddleware` (only if `SLM_CORS_ORIGINS` is set);
      - `GET /healthz`;
      - the extraction routes (`/v1/...`) and chat routes;
      - finally `handler = Mangum(app)`.
   4. The schemas and profiles modules define their classes and `REGISTRY`. No AWS calls happen at import time.
3. Caches that fill **on first use** and are then reused by every later invocation in the same container:

| Cache | Filled by | Holds | Lifetime |
|---|---|---|---|
| `storage/aws.platform_session()` | first AWS call | boto3 session with the function's own role | container |
| `storage/aws.tenant_session(t)` | first tenant data access | STS credentials of `tenant-data`, tagged `tenant_id=t` | renewed while > 16 min remain (1-hour sessions) |
| `storage/aws.queue_url(name)` | first send | SQS queue URL | container |
| `gateway.get_gateway()` | first model call | `BedrockGateway` (one bedrock-runtime client) | container |
| `core/secrets.get_secret_string(id)` | first secret read | secret value | 5 minutes |
| `profiles.load_profile(name)` | first job | parsed `default.yaml` | container |

---

## Trace A: POST /v1/extractions with a file (direct flow)

A client sends a ≤ 4 MB file and waits for the result in the same request.

### A1. Edge: WAF and API Gateway (no code of ours)

1. **AWS WAF** checks the per-IP rate, known bad inputs and IP reputation. ✗ blocked → 403 from WAF.
2. **API Gateway** applies stage throttling. ✗ → 429 `rate_limited`. A missing `Authorization` header → 401 `unauthorized` (gateway response).
3. API Gateway looks up its **authorizer cache**, which holds the Authorization header's answer for 60 s. Hit → skip to A3. Miss → A2.

### A2. Authorizer Lambda: is the key valid? (`slm-prod-authorizer`)

1. `auth/authorizer.handler(event)`
   1. lower-cases the header names and reads `authorization`.
   2. `auth/principal.parse_bearer(header)` takes `slm_live_…` out of `Bearer slm_live_…`. ✗ not a Bearer header → None.
   3. `auth/principal.resolve_api_key(key)`
      1. `storage/tenants.hash_api_key(key)` = HMAC-SHA256(pepper, key)
         - `core/secrets.api_key_pepper()` → `get_secret_string("slm/prod/api-key-pepper")` **[cached 5 min]**
           - ⇢ **Secrets Manager** `GetSecretValue` (decrypted with the `logs` KMS key)
      2. `storage/aws.dynamodb_table()` with the **platform** session
         - ⇢ **DynamoDB** `GetItem` `APIKEY#<hash>` / `APIKEY` → `{tenant_id, key_id, scopes, status}`
         - ✗ no item, or `status != active` (revoked) → `ApiError(401)`
      3. ⇢ **DynamoDB** `GetItem` `TENANT#<t>` / `PROFILE`. ✗ tenant not `active` → `ApiError(403, tenant_suspended)`
      4. returns `Principal(tenant_id, key_id, scopes)`
   4. `_policy(key_id, "Allow", methodArn, context)` builds an IAM policy for the **whole stage** (`api/stage/*`), so the cached answer serves every route, and adds `context = {tenant_id, key_id, scopes}`.
   - ✗ 401 → raises `Exception("Unauthorized")`, which API Gateway turns into 401. ✗ 403 → returns an explicit **Deny** policy → 403.

### A3. API Lambda: create the job (`slm-prod-api`)

1. `main.handler` is **Mangum**: it converts the API Gateway event into an ASGI request, keeping the original event in `scope["aws.event"]`.
2. **Middleware, outermost first:**
   1. `main.audit_every_request` calls `call_next(request)` first and writes its record **after** the response (step 4).
   2. `CORSMiddleware` (when configured) adds `Access-Control-Allow-Origin` to the response if the `Origin` header is an allowed origin.
3. **Routing:** `POST /v1/extractions` → `modules/extraction/api.create_extraction(request, wait, principal)`.
   1. **Dependency first:** `auth/dependencies.require_scope("extractions:write")`
      1. `current_principal(request)`: in AWS, it reads `requestContext.authorizer` from the event, with no database call. ✗ empty context → 401.
         *(Locally, with `SLM_ENV=local`, it calls `resolve_api_key` itself, exactly as in A2.)*
      2. `Principal.require("extractions:write")`. ✗ → 403 `insufficient_scope`.
      3. Stores the caller in `request.state.principal`, for the audit record.
   2. **Read the file:**
      - `multipart/form-data` → `await request.form()`. The file is read up to **4 MB + 1 byte**. ✗ more → 413 `file_too_large` (the rest is never read).
      - JSON → `service.decode_base64_strict(file.content_base64)`:
        1. strips a `data:…;base64,` prefix and whitespace;
        2. checks the size **before** decoding. ✗ → 413;
        3. `base64.b64decode(validate=True)`. ✗ bad characters → 400 `invalid_base64`.
   3. `service.validate_doc_type(document_type)`: `auto` or none → None (detect later). ✗ unknown → 400.
   4. `service.create_direct(principal, doc_type, data, wait)`:
      1. Size checks again, then `preprocess/detect.detect_type(data)` checks the magic bytes (PDF, JPEG, PNG, or a ZIP containing `word/document.xml`). ✗ → 415 `unsupported_file_type`. **Nothing has been stored yet.**
      2. `storage/tenants.get_tenant_settings(t)`
         - `storage/aws.tenant_session(t)` **[cached ~1 h]**: on a miss, ⇢ **STS** `AssumeRole(tenant-data, Tags=[tenant_id=t])`
         - ⇢ **DynamoDB** `GetItem` `TENANT#t` / `PROFILE` → `TenantSettings` (retention, masking, verification mode, webhook, profile)
      3. `JobRepo(t)` and `Blobs(t)`: a DynamoDB table and an S3 client, **both on the tenant session**.
      4. **Dedup:** `sha256(data)` → `JobRepo.find_dedup(doc_type, sha)`
         - ⇢ **DynamoDB** `GetItem` `TENANT#t` / `DEDUP#<type>#<sha>`. `_expired()` ignores entries past retention.
         - If hit: `JobRepo.get(previous)`, and if `succeeded` → `audit(... "ok:dedup")` → `public_job(previous)` + `"deduplicated": true` → **return 200**. No processing, no tokens.
      5. `core/ids.new_job_id()` → `ext_<ULID>`.
      6. `Blobs.put(keys.source_key(t, job), data, content_type)`
         - `_check(key)`: the key must start with `tenants/<t>/`.
         - ⇢ **S3** `PutObject` with SSE-KMS (documents key) and encryption context `{tenant_id}`. S3 calls ⇢ **KMS** `GenerateDataKey`; the Bucket Key caches it.
         - (S3 now raises an *Object Created* event, and GuardDuty starts scanning the object: see A5.)
      7. `JobRepo.create(job, status="queued", flow="direct", doc_type, retention_days, sha256)`
         - ⇢ **DynamoDB** `PutItem` `TENANT#t` / `JOB#<id>` with `expires_at` = now + retention. The condition is "must not exist".
      8. `window.processing_open()`: `always` → True.
      9. `storage/queues.send_job(t, job, interactive = wait > 0 and window open)`
         - `queue_url(name)` **[cached]**: ⇢ **SQS** `GetQueueUrl` once.
         - ⇢ **SQS** `SendMessage` with body `{"tenant_id", "job_id"}`. **This wakes the worker: Trace B starts in parallel.**
      10. `audit(... "extraction.create", job, "ok")` writes a JSON line to the `audit` logger.
      11. Window closed → return 202 with `processing_starts_at`.
      12. `service.wait_for_job(repo, job, wait)` loops until the job is final or `min(wait, SLM_MAX_WAIT_SECONDS=25)` seconds pass:
          - `JobRepo.get(job, consistent=True)` ⇢ **DynamoDB** `GetItem` (strongly consistent);
          - `asyncio.sleep` 0.25 s, growing ×1.5, capped at 1 s.
      13. **Final** → `public_job(job, blobs)` → **200**:
          - keeps only `PUBLIC_FIELDS`;
          - `_in_order` sorts `usage` and `by_model` into a fixed key order;
          - `_schema_order(document_type, result)` re-validates the result through its schema, giving fields in schema order;
          - a large result (`result_key`) is read back with `Blobs.get` ⇢ **S3**;
          - a failed job gets `error.code`.

          **Not final** → 202 + `Location: /v1/extractions/<id>`.
   5. `JSONResponse(status, body, headers)`.
4. **Back through the middleware:** `audit_every_request` writes **one audit record**:
   - tenant and key id;
   - `action = "POST /v1/extractions"`, built by `_route_template`, which turns ids into `{job_id}`;
   - the job id;
   - `ok`, `denied` or `error:<status>`;
   - the source IP.
5. Mangum → API Gateway → client.

### A5. Side effects of the S3 upload (harmless for the direct flow)

- **EventBridge** *Object Created* → intake Lambda → `on_object_created` → `JobRepo.transition(["awaiting_upload"] → "scanning")`. ✗ The job is `queued`, so the conditional update fails and is ignored (logged `status: ignored`).
- **GuardDuty** scans the object and writes the tag `GuardDutyMalwareScanStatus`. It then emits a scan-result event → `on_scan_result` → the transition from `scanning`/`awaiting_upload` fails, so nothing happens. The **worker** reads the tag itself (Trace B, step 4).

---

## Trace B: the worker processes one job

`slm-prod-worker`, triggered by SQS. The event source mapping delivers **1 message per invocation**, with at most 10 concurrent workers (interactive queue) and 3 (background).

1. `modules/extraction/worker.handle_sqs_event(event)`: for each record, it reads `{tenant_id, job_id}` and the SQS `ApproximateReceiveCount` → `process_job(t, job, receive_count)`.
2. `process_job`:
   1. **Claim:** `JobRepo(t).claim(job, LEASE_SECONDS=150)`
      - ⇢ **DynamoDB** `UpdateItem`: `status = processing`, `lease_until = now + 150 s`, `ADD attempts 1`, on the condition `status = queued`, **or** `processing` with an expired lease.
      - ✗ condition fails (already done, or another worker holds it) → log `job_skipped` → **return** (the message is deleted as successful).
   2. `JobRepo.get(job, consistent=True)`. ✗ None (retention ended while it was queued, `_expired`) → log `job_expired_before_processing` → return.
   3. `Blobs(t)`; `source = keys.source_key(t, job)`.
   4. **Direct flow only:** `_wait_for_clean_scan(blobs, source)` (skipped when `SLM_ENV=local`). It loops for up to 30 s:
      - `Blobs.tags(source)` ⇢ **S3** `GetObjectTagging`;
      - `NO_THREATS_FOUND` → continue;
      - `THREATS_FOUND` → `Blobs.delete` + `JobError("malware_detected")`;
      - `UNSUPPORTED`, `ACCESS_DENIED` or `FAILED` → `JobError("scan_failed")`;
      - no tag yet → sleep 2 s. ✗ time out → `ScanPending` (step 2.13).
   5. `Blobs.get(source)` ⇢ **S3** `GetObject`; S3 ⇢ **KMS** `Decrypt`.
   6. `preprocess.preprocess(data)` builds `Prepared(kind, pages=[{text, image, scanned}])`:
      - `detect_type(data)`. ✗ 415 becomes `JobError(code)`.
      - **PDF** → `pdf.pdf_pages(data)`:
        1. `open_pdf`: PDFium. ✗ password → `encrypted_document`; corrupt → `corrupt_file`; more than 20 pages → `too_many_pages`.
        2. For each page: `get_textpage().get_text_range()`. Fewer than 50 characters → **scanned** (no text kept).
        3. `page.render(scale = 100 DPI digital / 150 DPI scan, capped at 4,000 px)`.
        4. `image.downscale(1000 / 1568 px)` → `image.to_jpeg`.
      - **DOCX** → `docx.docx_pages`: `check_zip` (zip bomb or macros → `JobError`) → `docx_to_text` → ~3,000-character "pages".
      - **JPEG/PNG** → `image.prepare_image`: decode (bomb → `file_too_complex`) → `exif_transpose` → `downscale(1568)` → `to_jpeg` → one scanned page.
   7. `get_tenant_settings(t)` (tenant session) → `profiles.load_profile(tenant.profile)` **[cached]**.
   8. `pipeline.extract_document(data, prepared, requested_type, tenant, profile, gateway=get_gateway(), job)`. Detailed in **B-extract** below. It returns an `ExtractionOutcome`: masked result, warnings, method, model and parser ids, and **every model call** made.
   9. **Store the result:**
      - JSON of at most 100 KB → inline; otherwise `Blobs.put(result.json)` ⇢ **S3**, keeping `result_key`.
      - `usage.summarize(calls)` → `{model_calls, input/output/cache tokens, estimated_cost_usd, by_model[...]}`. `estimate_cost` multiplies tokens by `PRICES`.
      - `JobRepo.complete(job, ...)` → `transition(["processing"] → "succeeded", ...)` ⇢ **DynamoDB** `UpdateItem` (conditional). It sets the result (or `result_key`), document type and source, schema version, page count, warnings, method, model, parser and verifier ids, profile, and `usage`, and removes `lease_until`.
   10. **Source file:**
       - `_keep_for_training(...)`: consent **and** not identity/KYC **and** no Aadhaar markers **and** (not detected, or confidence ≥ 0.95) → `Blobs.copy(source → training/...)` ⇢ **S3** `CopyObject`.
       - Then always `Blobs.delete(source)` ⇢ **S3** `DeleteObject`.
   11. `JobRepo.save_dedup(doc_type, sha, job, expires_at)` ⇢ **DynamoDB** `PutItem` `DEDUP#…`.
   12. Then:
       - `usage.record(t, pages, calls, method)` ⇢ **DynamoDB** `UpdateItem` `TENANT#t` / `USAGE#<day>`: `ADD jobs, pages, tokens, model_calls, est_cost_usd, jobs_<method>`;
       - `webhooks.enqueue(t, job, "succeeded")` ⇢ **SQS** `SendMessage` (webhooks queue) → **Trace E**;
       - log `job_succeeded`: ids, type, method, pages, warning count, duration.
   13. **Exception branches:**
       - `JobError(code)` (the document is the problem):
         1. `JobRepo.fail(job, code, usage=summarize(calls))`
         2. `Blobs.delete(source)`
         3. `usage.record(..., method="failed")` if any model was called
         4. webhook `failed`
         5. the message is **deleted** (no retry).
       - `ScanPending`:
         - attempts left → `transition(processing → queued)` → raise `TemporaryError`;
         - last attempt (3) → `fail("scan_timeout")` + delete the source + webhook.
       - Any other exception (Bedrock throttling becomes `TemporaryError` too):
         - attempts left → back to `queued` → raise `TemporaryError`;
         - attempt 3 → `fail("processing_error")` + webhook → raise.
3. `handle_sqs_event` lists the messages that raised `TemporaryError` in `batchItemFailures`. SQS makes them visible again after the 180 s visibility timeout. After **3** receives, SQS moves the message to the **dead-letter queue**, and an alarm fires.

### B-extract: `pipeline.extract_document` call by call

1. `page_texts` = the text layer of every page (empty for scans).
2. **Rung 0**, if `profile.rung0_enabled` and the requested type is `None`, `auto` or `bank_statement`: `parsers.try_rung0(...)`:
   1. `prepared.is_digital_pdf`. ✗ any scanned page → return `(None, [], attempted=False)`.
   2. `parsers/fingerprint.pick_parser(text of the first 3 pages)` → each registered parser's `matches(text)`, in order:
      - bank-specific parsers (`parsers/bank/*.py`): **all** marker texts and **all** column labels must be present;
      - `GenericStatementParser` (always **last**): some line names a date, a balance and an amount column (`COLUMN_TITLES`).

      ✗ no match → `(None, [], False)`.
   3. `parser.parse(data)`. For the generic parser (`parsers/generic.py`):
      1. pdfplumber opens the PDF; `base.page_words` per page drops **watermark** characters (rotated against the page's text direction), words in **legacy-script and symbol fonts** (e.g. Sinhala FM-fonts, which read as garbage), and `(cid:…)` glyphs. `group_into_lines` groups the words into lines;
      2. **column titles:** the first line where `find_columns` recognises a date, a balance and an amount column. Words that aren't titles join the nearest title (bilingual titles); a lone unknown group is an `other` column. ✗ none → `Decline("no_column_titles")` → `{"transactions": [], "_declined": ...}`, and `try_rung0` returns `(None, [], False)`: perhaps not a statement at all;
      3. `read_header(lines above the table)`: for each label (multi-word labels first), the value to the **right** (also on lines that vertically overlap the label: two-line captions like "OPENING / BALANCE"), else **below** (summary tables), else **above** (caption lines like "HOLDER  PERIOD"). The value must have the field's form (an account token with 4+ digits, an amount, a date range, a short name);
      4. `Table(columns, table_lines)`: `_calibrate` finds where each amount column's figures really are (their right edges, left edges or centres form one tight cluster per amount column);
      5. `_rows`: per line, `Table.route` puts each word in a column; a line with a date in the date column **and** an amount is a row; `B/F` / "Opening Balance" lines give the opening balance; "Total" / "Closing balance" end the table; repeated title lines, day headings and page headers/footers are skipped;
      6. `_assign_extras`: wrapped description lines join the row they are **vertically chained** to (above or below it). A line about equally near two rows → `Decline("ambiguous_wrapped_line")`. A second opening balance or rows after the table's end → `Decline` (two accounts in one file);
      7. `_finish_row`: description and reference in reading order; amounts per column; one Amount column → the side from the Type/Dr-Cr cell, a `Cr`/`Dr` suffix, the sign, or the balance movement; `0.00` and `—` → empty.

      Any `Decline` returns no rows: the coverage check rejects the result and AI extracts the statement. **The parser never guesses.**
      ✗ exception → log `rung0_parser_error` → `(None, [], True)`. **A parser bug never fails the job.**
   4. `BankStatement.model_validate(dict)` runs `parse_date` / `parse_money` per field. ✗ `ValidationError` → `rung0_rejected: schema_invalid` → `(None, [], True)`.
   5. Three checks, each listing failures:
      1. `checks.run_checks("bank_statement")` → `check_bank_statement`: running balance per row, opening + credits − debits = closing, IFSC format (when there is an IFSC);
      2. `coverage.coverage_problems`: row count = `lexicon.text_row_starts` (lines of the PDFium text that start with a date, not balance lines, periods or a wrapped range's second date), last balance = closing, account number / period / opening / closing present;
      3. `reconcile.reconcile(statement, page_texts)`:
         - `reconcile_header`: account number, IFSC, holder and branch in the text; opening and closing balance among the amounts; the period as a printed range (or its two dates);
         - `reconcile_rows`: the same row starts (count = rows), then per row: the date; `_amounts_match`: reading on from the date line, the row's non-zero amounts appear exactly, before any other amount; every word of the description and reference between the previous and next rows.

      ✗ any failure → `rung0_rejected: checks_failed` (with the names) → `(None, [], True)`.
   6. Tenant `verification_mode == "checks_only"` → `Rung0Outcome(statement, parser_id, verified_by=None)`. **No AI call at all.**
   7. `parsers/verify.verify(pages, statement, gateway, nova_lite)`:
      1. `gateway.generate(model=nova-lite, system=VERIFY_SYSTEM, pages=ALL pages, instruction=VERIFY_INSTRUCTION, max_tokens=400)` → `bedrock.build_request(...)`:
         - one user message with `<page number='n'>`, the image and the text layer per page, then the instruction;
         - Nova gets `temperature: 0`.

         ⇢ **Bedrock** `Converse` (`us.amazon.nova-lite-v1:0`) → `GatewayResult(text, tokens)`.
      2. `json.loads(strip_code_fences(text))`: the model's **independent reading** of holder, account, IFSC, branch, period, first and last transaction.
      3. `compare(reading, parser_view(statement))`: normalise both sides (dates → ISO, a year-less date takes the statement's year; text → no spaces, case-folded). **Required** (account number, period): must be read and equal. **Optional** (holder, IFSC, branch, first/last row): compared only when both sides have a value; holder, branch and descriptions match when one reading contains the other.
      4. Log `rung0_verified(agree, issue_paths)` → `VerifyResult`.
      - ✗ disagree → `rung0_rejected: verifier_disagreed` → `(None, [call], True)`.
   8. Agree → `Rung0Outcome(statement, parser_id, verified_by=nova-lite)`.
   - **Accepted:** `masking.mask(normalise.to_result(statement), strict = Aadhaar markers in the text, unmasked_kinds)` → `ExtractionOutcome(method="parser", doc_type_source="parser")` → **return**.
   - **Rejected but attempted**, with the type not given → `requested_type = "bank_statement"`.
3. **Document type:**
   - given → `(type, "client", None)`.
   - otherwise → `classify.classify(pages, gateway, [nova-lite, haiku], 0.8)`:
     1. for each model: `gateway.generate(page 1 image + first 1,500 characters, CLASSIFY_SYSTEM, max_tokens=100)` ⇢ **Bedrock** → parse `{"type", "confidence"}`;
     2. known type with confidence ≥ 0.8 → return; else try the next model;
     3. none → the best answer at 0.5 or above; ✗ else `JobError("unrecognised_document_type")`, carrying the calls.
4. **Start rung:** `profile.rung0_fallback_rung` (Haiku) for a statement Rung 0 already tried; otherwise `profile.start_rung[type]`.
5. `pipeline.run_ladder(type, pages, profile, gateway, start)`. A **bank statement longer than 3 pages** (`profile.statement_chunk_pages`) goes to `run_chunked_statement` instead (step 5b). Otherwise `_climb`:
   1. `spec = REGISTRY[type]`; `system = prompts.system_prompt(spec)`: rules (plus `TYPE_RULES`, e.g. the bank-statement rules) and the JSON schema, identical on every call, so Bedrock caches it.
   2. `attempt_plan(profile, start)`, with the default profile starting at Haiku: `[(Haiku, no thinking), (Haiku, thinking), (Sonnet, no thinking)]`.
   3. For each attempt:
      1. `gateway.generate(model, system, pages, prompts.instruction(problems), thinking)` → `build_request`:
         - Claude gets a `cachePoint` after the system prompt;
         - thinking **on** → `{"type": "adaptive"}`;
         - thinking **off** → Haiku `{"type": "disabled"}`, Sonnet `{"type": "between_tools"}`;
         - no `temperature` for Claude.

         ⇢ **Bedrock** `Converse`. ✗ throttling or timeout → `TemporaryError` (the job is retried by SQS).
      2. `parse_and_validate(result, spec.model)`:
         - ✗ `max_tokens` → problem "cut off: return the COMPLETE JSON, every row" (never "shorter": for a statement that means dropped rows);
         - ✗ not JSON or not an object → problem;
         - ✗ `ValidationError` → problems name the **fields and error types only**.

         Any problem → next attempt.
      3. `failures = checks.run_checks(type, parsed) + text_reconciliation_failures(type, parsed, pages)`. The second applies to digital bank statements only: `reconcile` against the text, and any issue becomes `text_reconciliation_failed`.
      4. Keep the attempt with the fewest failures as `best`. Log `ladder_attempt`.
      5. `blocking` = failures except `DOCUMENT_ONLY` (`document_expired`, `required_fields_missing`). Empty → **stop**; otherwise `problems` = the failure messages, sent to the next attempt.
   4. ✗ no usable answer in any attempt → `JobError("extraction_failed")`, carrying every call.

   **5b. `run_chunked_statement`** (long bank statements):
   1. split the pages into chunks of 3 (pages 1–3, 4–6, ...);
   2. each chunk runs its own `_climb` with `instruction(problems, chunk=(first, last, total))` ("pages 4 to 6 of a 15-page statement: every row printed on these pages"). Its check: the running balance from row to row inside the chunk, and (digital PDFs) `reconcile_rows` against **those pages'** text. A failing chunk climbs the ladder **alone**;
   3. chunks run **in parallel** (`ThreadPoolExecutor`, at most 5) when `gateway.parallel_safe` (Bedrock); one after another for the FakeGateway and Ollama;
   4. `merge_statement_chunks`: each header field from the first chunk that has it, transactions in page order;
   5. the whole statement is checked again (balances across chunk borders, opening + credits − debits = closing, full text reconciliation): remaining failures become warnings. The reported model is the strongest one any chunk needed.
6. `normalise.to_result(parsed)` gives every field, with `null` for missing ones. `warnings` = failure codes, plus `document_type_mismatch` if a client-named type produced an empty result.
7. `masking.mask(result, strict = identity/KYC type or Aadhaar markers in text or result, unmasked_kinds)`:
   - walks every field;
   - Aadhaar fields → `XXXX XXXX 1234`;
   - identifier fields → last 4 characters (unless the tenant allows that kind);
   - in strict mode, any 12-digit number in any text.
8. Return `ExtractionOutcome(method="ai", model_id, warnings, calls, ...)`.

---

## Trace C: POST /v1/extractions without a file (upload-link flow)

1. **A1–A3.3.1 as above**, up to the dependency. No file is found → `service.create_presigned(principal, doc_type)`:
   1. `get_tenant_settings(t)` (tenant session ⇢ STS, DynamoDB).
   2. `new_job_id()`; `key = keys.source_key(t, job)`.
   3. `Blobs.presigned_upload(key, 25 MB, 900 s)`:
      - conditions: the exact key, 1 byte to 25 MB, SSE-KMS with our key;
      - `s3_client(tenant session).generate_presigned_post(...)`: signed **locally** with the tenant's temporary credentials (no AWS call). The session has more than 16 minutes left, so the link stays valid for its full 15.
   4. `JobRepo.create(job, status="awaiting_upload", flow="presigned", pending_deadline = now + 20 min)` ⇢ **DynamoDB** `PutItem`, including `pending_pk` (it enters the sparse `pending` index).
   5. `audit("extraction.create_upload")` → **201** `{id, status, upload: {url, fields, max_bytes, expires_at}}`.
2. **The client POSTs the form to S3:** every field first, the file last. ⇢ **S3** checks the signed policy (✗ 403) and stores the object with SSE-KMS → `204`.
3. **EventBridge** *Object Created* → `slm-prod-intake` → `intake.handle_event(event)` → `on_object_created(key)`:
   1. `keys.parse_source_key(key)` → `(t, job)`. ✗ not a job source (e.g. `result.json`) → return.
   2. `JobRepo(t).transition(job, ["awaiting_upload"] → "scanning", uploaded_at, pending_deadline = now + 5 min)` ⇢ **DynamoDB** `UpdateItem` (conditional).
4. **GuardDuty** scans the object → tags it → EventBridge *Scan Result* → `handle_event` → `on_scan_result(key, status)`:
   - `NO_THREATS_FOUND` → `transition(["scanning", "awaiting_upload"] → "queued")`, removing the pending markers → `queues.send_job(t, job, interactive=True)` ⇢ **SQS** → **Trace B**. The scan wait is skipped, because the flow is `presigned`.
   - `THREATS_FOUND` → `JobRepo.fail(job, "malware_detected")` → `Blobs.delete(key)` → `webhooks.enqueue(failed)`.
   - anything else → `fail("scan_failed")` → webhook.
5. The client polls `GET /v1/extractions/{id}?wait=20` → **Trace D**.

---

## Trace D: GET, list, delete, corrections

All four go through A1–A3.2. The authorizer is usually a **cache hit**, so it costs nothing.

- **`GET /v1/extractions/{id}?wait=N`** → `api.get_extraction` → `require_scope("extractions:read")` → `service.get_job(principal, id, wait)`:
  1. `core/ids.is_job_id(id)` checks the format **before** any database call. ✗ → 404.
  2. `JobRepo(t)` → `wait_for_job` (when `wait > 0`) or `JobRepo.get`. `_expired` hides jobs past retention. ✗ None (unknown, another tenant's, or expired) → **404**, identical in every case.
  3. `Retry-After: 2` while not final → `public_job(job, Blobs(t))` → 200.
- **`GET /v1/extractions`** → `service.list_jobs`:
  1. `JobRepo.list(limit, cursor)` ⇢ **DynamoDB** `Query` `PK=TENANT#t`, `begins_with(SK, "JOB#")`, newest first; expired jobs are filtered out;
  2. `public_job` without `result`;
  3. `next_cursor`.
- **`DELETE /v1/extractions/{id}`** → `service.delete_job`:
  1. `is_job_id` → `JobRepo.get`. ✗ 404.
  2. `Blobs.delete_prefix(job folder)` ⇢ **S3** `ListObjectsV2` + `DeleteObjects`.
  3. Same for the training folder.
  4. `JobRepo.delete` ⇢ **DynamoDB** `Query` (corrections) + `BatchWriteItem` deletes.
  5. `audit("extraction.delete")` → 204.
- **`POST /v1/extractions/{id}/corrections`** → `service.add_corrections`:
  1. checks the job is `succeeded` (✗ 409);
  2. keeps at most 500 corrections, paths at most 200 characters;
  3. `JobRepo.add_corrections` ⇢ **DynamoDB** `PutItem` `CORR#<job>#<id>`, with the job's `expires_at` → 201.
- **`GET /v1/document-types`** → `REGISTRY` → each model's `model_json_schema()` (no AWS call).
- **`OPTIONS /v1/...`** (browser pre-flight) has **no authorizer** → the api Lambda → `CORSMiddleware` answers 200 with CORS headers for the allowed origin. Any other origin gets none, so the browser refuses.

---

## Trace E: webhook delivery

`slm-prod-webhook`, triggered by the SQS webhooks queue (batches of up to 10).

1. `webhooks.handle_sqs_event(event)` → for each record → `deliver(t, job, status)`:
   1. `get_tenant_settings(t)`: tenant session ⇢ STS (cached) and DynamoDB.
   2. `core/secrets.webhook_secret(tenant.webhook_secret_id)` ⇢ **Secrets Manager** `GetSecretValue` (`slm/prod/webhook/<t>`, cached 5 min).
   3. No URL or no secret → return True (nothing to send).
   4. `is_safe_url(url)`: https only; the host must resolve **only** to public IPs (no private, loopback, link-local or reserved addresses). ✗ unsafe → log `webhook_blocked_url` → True (never retried).
   5. `build_event(job, status)` → `{"event": "extraction.<status>", job_id, status, occurred_at}`, with no data.
   6. `sign(secret, body)` → `X-Signature: t=<unix>,v1=<HMAC-SHA256(secret, "t." + body)>`.
   7. `httpx.post(url, timeout=10, follow_redirects=False)`. 2xx → True; anything else or a network error → False.
2. Failed records → `batchItemFailures` → SQS retries after 90 s, at most **5** times → dead-letter queue + alarm.

---

## Trace F: retention clean-up

1. **DynamoDB TTL** deletes a job item some time after its `expires_at`, usually within days. (Until then, `JobRepo._expired` already hides it from every read.)
2. The table **stream** delivers the deletion. The event source mapping's filter passes **only** TTL deletions (`userIdentity.principalId = dynamodb.amazonaws.com`).
3. `retention.handle_stream(event)`: for each `REMOVE` record, it reads `OldImage.SK` (`JOB#<id>`) and `tenant_id` → `Blobs(t).delete_prefix(keys.job_prefix(t, job))` ⇢ **S3** list + delete (through the tenant session).
4. Corrections and dedup items carry the same `expires_at`, so TTL deletes them too. An S3 lifecycle rule (400 days) is the backstop.

---

## Trace G: scan sweeper (every 5 minutes)

1. **EventBridge Scheduler** → `slm-prod-scan-sweeper` → `intake.sweep()`.
2. `dynamodb_table()` (platform role) ⇢ **DynamoDB** `Query` index `pending`: `pending_pk = PENDING` and `pending_deadline < now`. The index is sparse, so only waiting jobs are read.
3. For each item:
   - `awaiting_upload` → `JobRepo(t).transition(→ "expired")`, removing the pending markers;
   - `scanning` → `JobRepo(t).fail("scan_timeout")` → `webhooks.enqueue(failed)`.
4. Log `sweep_done`.

---

## Trace H: operator scripts

All scripts run **locally** with an AWS profile. `scripts/_aws.prefer_ipv4()` is called first, to avoid IPv6 hangs.

- **`create_tenant.py --env prod --name X [--secret-name N]`:**
  1. `parse_args`;
  2. `configure_environment("prod")` sets `SLM_TABLE` / `SLM_PEPPER_SECRET_ID` **before** importing `app`;
  3. `claim_name` ⇢ DynamoDB `PutItem TENANTNAME#x` (conditional: never twice);
  4. optional webhook secret ⇢ Secrets Manager `CreateSecret`;
  5. `tenants.create_tenant` ⇢ `PutItem TENANT#t/PROFILE`;
  6. `tenants.create_api_key`:
     - new key → `hash_api_key` (pepper ⇢ Secrets Manager);
     - ⇢ `PutItem APIKEY#<hash>`;
     - ⇢ `PutItem TENANT#t/KEY#<id>`, with `key_hash` (for revocation);
  7. the key is printed once, written to `--key-file`, or stored with `store_key_in_secrets_manager` ⇢ `CreateSecret`/`PutSecretValue`.
- **`manage_keys.py list | create | revoke`:**
  1. tenant name → `TENANTNAME#` lookup → `list_api_keys` / `create_api_key` / `revoke_api_key`;
  2. revoke reads `KEY#<id>.key_hash` → `UpdateItem APIKEY#<hash> status=revoked` + `KEY#<id> revoked_at`.
- **`deploy.py prod <stacks>`:** for each stack in order:
  1. `lint` (cfn-lint);
  2. params from `params/prod.json`;
  3. `app` only: `build_lambda.build()` → upload `code/lambda-<hash>.zip` (skipped if already there);
  4. upload the template to the code bucket if it's over 51 KB;
  5. `create_change_set` → wait → print the changes → (confirm) → `execute_change_set` → wait → `print_failures` on error.
- **`smoke_test.py prod --key-file F`:** `/healthz` → 401 without a key → 200 with the key → a direct-flow statement (expects `method=parser`) → an upload-link invoice (expects Claude Haiku, total 17,700.00) → prints ✓/✗.

---

## 9. Timeline of one document, end to end

A 1-page digital bank statement sent with `wait=25`, as measured on production:

| t (s) | Where | What runs |
|---|---|---|
| 0.00 | API Gateway | WAF → authorizer cache hit (or authorizer Lambda: pepper, 2 DynamoDB reads) |
| 0.05 | api λ | `create_extraction` → `create_direct`: detect, tenant settings, dedup miss, S3 put, job create, SQS send |
| 0.3 | worker λ | `handle_sqs_event` → `claim` → wait for the GuardDuty tag (usually 1–3 s) |
| ~2 | worker λ | `Blobs.get` → `preprocess` (PDFium text + render) |
| ~2.3 | worker λ | `try_rung0`: fingerprint → parse (bank-specific or generic parser, pdfplumber) → schema → checks → coverage → **reconcile** |
| ~2.5–5 | worker λ → Bedrock | `verify` → Nova Lite reads the key facts → `compare` → agree |
| ~5 | worker λ | mask → `complete` (result + usage, ~USD 0.0002) → delete source → dedup → usage → webhook queued |
| 0.25 → ~5 | api λ | `wait_for_job` has been polling DynamoDB; it sees `succeeded` → `public_job` → **200** with the result |
| ~6 | webhook λ | signed `extraction.succeeded` POSTed to the client (if configured) |

An invoice image through Claude Haiku follows the same path, with `classify` (if no type was given) and `run_ladder` in place of `try_rung0`, typically 8–9 s end to end at ~USD 0.0005.
