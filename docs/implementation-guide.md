# SLM Fintech Platform: Implementation Guide

| | |
|---|---|
| Audience | Developers working on this repository |
| Describes | The platform **as built** (2026-10-14): how the code is organised, how to run, test, change, build and deploy it |
| Related | What it does, stage by stage: [implementation-flow.md](implementation-flow.md) · Function-by-function execution order: [execution-flow.md](execution-flow.md) · Design: [architecture.md](architecture.md) · Infrastructure: [infra/cloudformation/README.md](../infra/cloudformation/README.md) · For client developers: [integration-guide.md](integration-guide.md) |

---

## Contents

1. [What you are working on](#1-what-you-are-working-on)
2. [Tools](#2-tools)
3. [Repository layout](#3-repository-layout)
4. [Everyday commands](#4-everyday-commands)
5. [Configuration](#5-configuration)
6. [How the code fits together](#6-how-the-code-fits-together)
7. [Rules every change must follow](#7-rules-every-change-must-follow)
8. [Common tasks, step by step](#8-common-tasks-step-by-step)
9. [Testing](#9-testing)
10. [Build, deploy, release](#10-build-deploy-release)
11. [Project status](#11-project-status)
12. [Pitfalls we hit, and their fixes](#12-pitfalls-we-hit-and-their-fixes)

---

## 1. What you are working on

A multi-tenant **document extraction API** for fintech clients:

- **Input:** a PDF, image or DOCX: bank statement, invoice, payslip, identity document or KYC form.
- **Output:** validated, normalised JSON, with identifiers masked and the AI tokens and estimated cost reported.

How a document is processed:

- **Digital bank statements from known banks** are read by **Python parsers (Rung 0)**. Every row is reconciled with the PDF text, and **Nova Lite** confirms the key facts independently.
- **Everything else** goes through an **AI model ladder on Amazon Bedrock**: Nova Lite → Claude Haiku 5.5 → Claude Sonnet 5.5, cheapest first, climbing only when checks fail.

Where it runs:

- **Everything runs on AWS Lambda** in us-east-1: 7 functions from one zip package, deployed as 4 CloudFormation stacks.

Production lives in the `lyndev` account (`slm-prod-*` stacks). Its API is `https://0vtusv00e4.execute-api.us-east-1.amazonaws.com/prod`.

---

## 2. Tools

| Tool | Version | For |
|---|---|---|
| Python | 3.12 | the platform (`app/`), scripts, tests |
| [uv](https://docs.astral.sh/uv/) | 0.12+ | virtual env + dependencies (`uv sync`), running everything (`uv run ...`) |
| AWS CLI v2 | latest | profiles and a few manual commands |
| `cfn-lint`, `checkov` | via `uvx` | template checks (no install needed) |

**No Docker is needed** for anything: the Lambda package is built from ready-made Linux ARM64 wheels.

First time:

```bash
uv sync                    # creates .venv with app + dev dependencies (pytest, moto, ruff, uvicorn)
uv run pytest -q           # ~150 tests, ~20 s, no AWS account needed
```

---

## 3. Repository layout

```
app/                                  THE PLATFORM (runs in Lambda; identical locally)
├── main.py                           FastAPI app: error handlers, CORS, per-request audit, Mangum `handler`
├── lambda_handlers.py                the 7 Lambda entry points (each function's Handler points here)
├── config.py                         every setting, from SLM_* environment variables (§5)
├── auth/                             authorizer.py (API Gateway authorizer), principal.py (key -> tenant),
│                                     dependencies.py (FastAPI: who is calling, which scope)
├── core/                             ids.py, errors.py (ApiError/JobError/TemporaryError), logging.py
│                                     (SafeLogger: allowlisted fields only), clock.py, secrets.py
├── storage/                          aws.py (platform session + per-tenant sessions), jobs.py (JobRepo),
│                                     blobs.py (S3 per tenant), tenants.py (settings, API keys), queues.py,
│                                     keys.py (every DynamoDB/S3 key format), ddb.py (Decimal conversions)
├── gateway/                          the ONLY code that calls an AI model: bedrock.py, ollama.py, fake.py
├── modules/extraction/
│   ├── api.py                        HTTP routes (thin)
│   ├── service.py                    create (direct/presigned), get with wait, list, delete, corrections
│   ├── worker.py                     one queued job end to end
│   ├── pipeline.py                   Rung 0 or AI ladder -> checks -> reconciliation -> mask
│   ├── preprocess/                   detect.py (real file type), pdf.py, image.py, docx.py
│   ├── parsers/                      Rung 0: fingerprint, generic (any layout), lexicon, base, coverage, reconcile,
│   │                                 verify (Nova check), bank/<bank>_v<n>.py (layout descriptions)
│   ├── schemas/                      one Pydantic model per document type + REGISTRY
│   ├── classify.py, checks.py, masking.py, normalise.py, prompts.py, window.py
│   └── profiles/default.yaml         the model ladder
├── modules/chat/api.py               reserved chat routes (501)
├── intake.py                         upload events -> queued job; scan sweeper
├── webhooks.py, retention.py, usage.py, audit.py
infra/cloudformation/                 data/security/app/observability.yaml, params/<env>.json, README.md
scripts/                              build_lambda, deploy, create_tenant, manage_keys, smoke_test,
                                      _aws (shared: IPv4, session)
tests/                                unit/, integration/test_flow_e2e.py, helpers/ (synthetic PDFs, images)
docs/                                 this guide and the others listed at the top
```

---

## 4. Everyday commands

| Task | Command |
|---|---|
| All tests | `uv run pytest -q` |
| One test file / test | `uv run pytest tests/unit/test_reconcile.py -q` · `uv run pytest -k presigned -q` |
| Lint (must pass) | `uv run ruff check .` (`--fix` repairs import order etc.) |
| Templates | `cd infra/cloudformation && uvx cfn-lint *.yaml --regions us-east-1 && uvx checkov -d .` |
| Run the API locally | `SLM_ENV=local uv run uvicorn app.main:app --reload` (point SLM_* storage settings at your AWS dev resources or emulators) |
| Build the Lambda zip | `uv run python scripts/build_lambda.py` → `dist/lambda-<hash>.zip` |
| Deploy | `uv run python scripts/deploy.py prod <stack…\|all> --profile lyndev` |
| Smoke-test production | `uv run python scripts/smoke_test.py prod --profile lyndev --key-file ~/.slm/smoke.key` |
| Onboard a client | `uv run python scripts/create_tenant.py --env prod --name "<NAME>" --profile lyndev --secret-name slm/prod/api-keys/<name>` |
| Rotate a key | `uv run python scripts/manage_keys.py create\|list\|revoke --env prod --tenant "<NAME>" --profile lyndev` |

---

## 5. Configuration

Every setting is an environment variable `SLM_<NAME>`, read once per process by [app/config.py](../app/config.py). In AWS they are set per function by `app.yaml`. In tests, `tests/conftest.py` sets them.

| Setting | Default | Meaning |
|---|---|---|
| `SLM_ENV` | `local` | `local` turns off AWS-only behaviour (per-tenant IAM sessions, KMS headers, the GuardDuty wait); `dev` / `prod` in AWS |
| `SLM_REGION` | `us-east-1` | region for every AWS client |
| `SLM_TABLE`, `SLM_BUCKET` | `slm-main`, `slm-docs-local` | DynamoDB table and documents bucket |
| `SLM_QUEUE_INTERACTIVE`, `_BACKGROUND`, `_WEBHOOKS` | `jobs-interactive`, … | SQS queue names |
| `SLM_TENANT_DATA_ROLE_ARN` | none | the role assumed per tenant; when unset, the platform credentials are used (local) |
| `SLM_KMS_DOCUMENTS_KEY_ID` | none | KMS key for S3 objects and upload links |
| `SLM_PEPPER_SECRET_ID` | none | Secrets Manager id of the API-key pepper (`slm/prod/api-key-pepper`) |
| `SLM_MODEL_PROVIDER` | `fake` | `bedrock` in AWS, `ollama` on a laptop, `fake` in tests |
| `SLM_PROFILE_NAME` | `default` | model profile file (tenants can override with their `profile` setting) |
| `SLM_MAX_INLINE_BYTES`, `SLM_MAX_UPLOAD_BYTES`, `SLM_MAX_PAGES` | 4 MB, 25 MB, 20 | limits |
| `SLM_MAX_WAIT_SECONDS` | `40` (prod sets **25**) | longest `wait`; must stay below API Gateway's integration timeout |
| `SLM_UPLOAD_URL_TTL_SECONDS` | `900` | upload-link lifetime |
| `SLM_CORS_ORIGINS` | empty | browser origins allowed to call the API (off by default; servers don't need it) |
| `SLM_PROCESSING_WINDOW` | `always` | or e.g. `Mon-Fri 09:00-19:00 Asia/Kolkata` |
| `SLM_DYNAMODB_ENDPOINT`, `SLM_S3_ENDPOINT`, `SLM_SQS_ENDPOINT` | none | only for local emulators; empty in AWS |

**Secrets are never settings.** The pepper and the webhook secrets live in Secrets Manager; settings only hold their ids.

Infrastructure parameters (`CorsAllowOrigin`, `EnableWaf`, `MaxWaitSeconds`, …) are listed in [infra/cloudformation/README.md](../infra/cloudformation/README.md) §10.

---

## 6. How the code fits together

```
API Gateway ──► authorizer λ (auth/authorizer.py) ──► api λ (main.py → modules/extraction/api.py → service.py)
                                                          │ file in S3, job in DynamoDB, message on SQS
S3 + GuardDuty ──EventBridge──► intake λ (intake.py) ─────┤
                                                          ▼
                                           worker λ (worker.py → preprocess → pipeline.py → gateway → Bedrock)
                                                          │ result + usage in DynamoDB; webhook message
                                           webhook λ (webhooks.py) ──► client's https URL
DynamoDB TTL ──stream──► retention-cleaner λ (retention.py)      Scheduler ──► scan-sweeper λ (intake.sweep)
```

Two documents describe this in depth:

- **[implementation-flow.md](implementation-flow.md):** the 14 stages a document goes through, which module handles each, the AWS resources, failure paths and the tests that prove each stage.
- **[execution-flow.md](execution-flow.md):** the exact order of function calls for every entry point: what runs first, what it calls next, and what each call does.

Three layers matter most when you change something:

| Layer | Rule |
|---|---|
| **Tenant data** | Only through `JobRepo`, `Blobs`, `get_tenant_settings` and `usage.record`, which all use `tenant_session(tenant_id)`. IAM then refuses any other tenant's keys |
| **Models** | Only through `get_gateway().generate(...)`. Request formats per model live in `gateway/bedrock.py build_request` |
| **Results** | Schema-validated (Pydantic), checked (`checks.py`), reconciled for digital statements (`parsers/reconcile.py`), masked (`masking.py`) before storage. The worker stores; the pipeline never touches storage |

---

## 7. Rules every change must follow

1. **Never log or return document content.**
   - Log only through `get_logger()`. It drops every field that isn't in `ALLOWED_FIELDS`.
   - Log exceptions by type only.
   - Error messages must be fixed text: never include an input value (NFR-S8).
2. **Never bypass tenant scoping.**
   - No raw `boto3` access to tenant data; use `JobRepo` / `Blobs`.
   - Another tenant's job must answer `404`, exactly like an unknown id.
3. **Money is `Decimal` or a 2-decimal string, never `float`.**
4. **Masking is the last step before storage, on every path.** Aadhaar is masked always, with no exceptions.
5. **Only synthetic documents in git.**
   - Real samples go in `samples/real/`, which is gitignored.
   - Tests use `tests/helpers/simple_pdf.py` and `sample_docs.py`.
6. **Comment generously.** This project's convention:
   - every file opens with a docstring explaining its role;
   - every function has a docstring;
   - non-obvious steps get a "why" comment, with the requirement id where one applies (e.g. FR-4.1).
7. **Every change keeps these green:**
   - `uv run pytest`;
   - `uv run ruff check .`;
   - for templates, `cfn-lint` and `checkov`. Any `checkov` skip carries its reason.
8. **A new AWS permission goes on the one role that needs it** (`app.yaml` / `security.yaml`), scoped to the exact resource.

---

## 8. Common tasks, step by step

### 8.1 Support a new bank's statement layout

This is the main way to cut AI cost: a parsed statement costs ~USD 0.0002 (one Nova Lite verification), against ~USD 0.0006–0.007 through the AI ladder. Most layouts need **no code at all**: the generic parser (`parsers/generic.py`) recognises the transaction table from its column titles and reads the header, rows and wrapped descriptions wherever they are printed. Check that first.

1. **Get an anonymised sample** of the bank's digital e-statement into `samples/real/<bank>/` (gitignored). Never commit a real statement.
2. **Try it locally** (no AI, nothing leaves the laptop). Print counts and issue codes only, never values:
   ```python
   from app.modules.extraction.preprocess import preprocess
   from app.modules.extraction.parsers.fingerprint import pick_parser, FINGERPRINT_PAGES
   from app.modules.extraction.parsers.reconcile import reconcile
   from app.modules.extraction.checks import run_checks
   from app.modules.extraction.parsers.coverage import coverage_problems
   from app.modules.extraction.schemas.bank_statement import BankStatement
   data = open("samples/real/<bank>/statement.pdf", "rb").read(); prep = preprocess(data)
   texts = [p["text"] for p in prep.pages]
   parser = pick_parser("\n".join(texts[:FINGERPRINT_PAGES]))
   st = BankStatement.model_validate(parser.parse(data))
   print(parser.parser_id, len(st.transactions),
         [f.code for f in run_checks("bank_statement", st)], coverage_problems(st, texts), reconcile(st, texts))
   ```
   `generic_v1` with three empty lists: the layout is supported. Done.
3. **The generic parser declines or a check fails?** The log line `generic_parser_declined` gives the reason (`no_column_titles`, `ambiguous_wrapped_line`, `second_opening_balance`, `two_amounts_in_one_cell`, ...). Usually the fix is a word list in `generic.py`:
   - a column title it doesn't know ("Txn Particulars"): add it to `COLUMN_TITLES`;
   - a header label it doesn't know ("A/C Holder"): add it to `HEADER_LABELS`;
   - a balance or end-of-table line it doesn't know: `OPENING_LINE` / `CLOSING_LINE` / `TABLE_END` in `parsers/lexicon.py`;
   - a date or number format: `leading_date` / `parse_amount` in `parsers/lexicon.py` (shared by the parser, coverage and reconciliation, so all three stay in step).
4. **Reproduce the layout in a synthetic fixture** (`tests/helpers/statement_layouts.py`, invented data) and add it to `LAYOUTS`: `tests/unit/test_generic_parser.py` then proves it is read exactly and passes every gate. A layout that must NOT be parsed (ambiguous) gets a decline test.
5. **Only if the layout really defeats the generic logic,** write a bank-specific parser: copy `parsers/bank/demo_bank_v1.py` to `<bank>_v1.py` (`parser_id`, `bank_name`, `markers` = text only this bank prints, `columns`, `header_labels`, `table_end_label`; override `parse_rows` if needed), and register it in `parsers/fingerprint.py` `PARSERS` **before** the generic parser (which must stay last). Keep its fingerprint strict.
6. **Deploy** with `scripts/deploy.py prod app`, then smoke-test. The job's `parser_id` shows which parser read it.

What the generic parser already handles (each feature has a synthetic test): titles only on the first page or on every page; a header and footer on every page; bilingual titles and legacy Sinhala/Hindi fonts; diagonal watermarks; descriptions wrapped above and/or below the date line; Deposits before Withdrawals; one Amount column with a Type, Dr/Cr or sign; `0.00` / `—` in empty cells; dates such as `01-Aug-2026`, `03 Aug 2026`, `01 Aug`, `02/08`, `01.08.2026`; European amounts `10.481,44`; values printed right of, below or above their labels; B/F, Opening Balance, Total and day-heading lines. It **declines** (the statement goes to AI) when it can't be certain: a wrapped line equally near two rows, or several accounts in one file.

### 8.2 Add a document type

1. Create `app/modules/extraction/schemas/<type>.py`: a Pydantic model using `Text`, `Money`, `IsoDate` from `schemas/base.py`, plus `SCHEMA_VERSION`.
2. Add one line to `REGISTRY` in `schemas/__init__.py`. Mark it `sensitive_identity=True` if it holds identity data (strict Aadhaar masking, never kept for training).
3. Add its consistency checks to `checks.py` (`_CHECKS`), and any identifier fields to `masking.py` `FIELD_KINDS`.
4. Add `start_rung` for it to `profiles/default.yaml`.
5. **Automatic:** the extraction prompt is generated from the schema (`prompts.system_prompt`), the type-detection prompt offers every `REGISTRY` type (`prompts.CLASSIFY_SYSTEM`), and `GET /v1/document-types` lists it with its JSON Schema. Add it to `DOCUMENT_TYPES` in `web/src/types.ts` and to the integration guide (§7).

### 8.3 Change models, prices or the ladder

| Change | Where |
|---|---|
| Ladder order / start rung per type / retries | `profiles/default.yaml` (or a new profile, assigned per tenant) |
| A new model id | `gateway/bedrock.py` `APPROVED_PREFIXES` **and** the `WorkerRole` Bedrock statements in `app.yaml` (IAM allowlist) |
| How a model must be called (thinking, temperature, cache point) | `gateway/bedrock.py` `build_request` + `tests/unit/test_bedrock_request.py`. Verify on real Bedrock: the fake gateway can't catch format errors |
| Prices for `estimated_cost_usd` | `app/usage.py` `PRICES` (USD per million tokens: input, output, cache read) |

Anthropic models need the account's one-time **use-case form** and Marketplace subscription before the first call (infra README).

### 8.4 Change the API response

- Fields of the job object: `service.py` `PUBLIC_FIELDS`, plus `public_job`.
- The result's fields come from the schema; `_schema_order` returns them in schema order.
- Update the integration guide (§6) and the e2e tests.

### 8.5 Change infrastructure

- Edit the template, lint it, then `scripts/deploy.py prod <stack>`. The change set lists every change, and `REPLACED` means delete and recreate: stop and think for tables, buckets, keys and secrets.
- **API Gateway gotcha:** adding or changing a *method* needs a new deployment. Rename `ApiDeploymentV3` to `V4`, as the template comment explains.
- CORS origins and other function settings are Lambda environment variables, so changing them needs no API redeployment.

---

## 9. Testing

| Suite | What it covers | Notes |
|---|---|---|
| `tests/unit/` | base64, file-type detection, dates/amounts, checks, masking, the ladder, Rung 0 + verifier, reconciliation, the Bedrock request format, tenant onboarding + key rotation | no AWS; the FakeGateway plays the model |
| `tests/integration/test_flow_e2e.py` | every stage end to end: direct and presigned flows, Rung 0, malware, sweeper, retention, isolation, the AWS authorizer path, audit records, the scan timeout, exact retention, field order, usage and cost | the real FastAPI app on **moto** (in-memory DynamoDB/S3/SQS) |
| `scripts/smoke_test.py` | the **deployed** environment with real Bedrock, S3 and GuardDuty | run after every deployment |

Fixtures (`tests/conftest.py`):
- `aws`: moto with the platform's table, bucket and queues;
- `tenant`: a tenant and its API key;
- `fake_gateway`: a FakeGateway installed in the worker. Queue answers with `.queue(...)` and inspect `.calls`.

Helpers:
- `tests/helpers/simple_pdf.py`: synthetic DEMO BANK statements (`demo_statement`, `sample_transactions`, `demo_reading`);
- `tests/helpers/sample_docs.py`: an invoice image and a statement PDF.

Things the tests can't prove (moto doesn't evaluate IAM, and the fake model can't reject a request format) are covered by the smoke test on AWS.

---

## 10. Build, deploy, release

1. **Build:** `scripts/build_lambda.py` packages the locked dependencies as Linux ARM64 wheels, plus `app/`, into `dist/lambda-<content-hash>.zip` (~48 MB; Lambda's limit is 250 MB unpacked). An identical build produces an identical file name, so an unchanged release changes nothing.
2. **Deploy:** `scripts/deploy.py prod app --profile lyndev`.
   - It lints, uploads the zip to the code bucket, creates a change set, shows it, executes it and waits.
   - Stack order for a fresh environment: `data → security → app → observability`.
3. **Verify:** `scripts/smoke_test.py prod --key-file ~/.slm/smoke.key`. It uses the dedicated `SMOKE-TEST` tenant.
4. **Roll back:** check out the previous commit and run `deploy.py prod app` again (old zips stay in the code bucket for 90 days). If a stack update fails, CloudFormation rolls back by itself.

---

## 11. Project status

| Area | Status |
|---|---|
| API, worker, intake, webhooks, retention, sweeper | ✅ built, tested, deployed |
| Rung 0 framework + DEMO BANK reference parser | ✅ (**real bank parsers: waiting for anonymised samples**) |
| Text reconciliation of every statement row (parser and AI) | ✅ |
| Nova Lite independent verification (read-and-compare) | ✅ measured on real Bedrock |
| AI ladder on Bedrock (Nova Lite, Haiku 5.5, Sonnet 5.5) | ✅ request formats confirmed on Bedrock |
| Token usage + estimated AI cost per job | ✅ in the API |
| Per-request audit, exact retention, key rotation/revocation | ✅ |
| Infrastructure: 5 stacks in `lyndev` prod, KMS, GuardDuty, alarms, budget | ✅ (WAF switched **off** on 2026-10-14 to save cost: `EnableWaf` in `params/prod.json`) |
| Alert email for alarms | ⛔ not set (`AlertEmail` in `params/prod.json`) |
| Bedrock data-retention mode `none` | ⛔ not set (account-wide decision) |
| API Gateway timeout quota (wait 25 s → 40 s) | ⛔ to request |
| CI/CD pipeline (GitHub Actions + OIDC) | ⛔ not built: deploys are manual with `deploy.py` |
| Per-tenant throttling (usage plans) | ⛔ not built |
| Log-leak canary test, evaluation runner | ⛔ not built |
| Organisation SCPs | ⛔ not possible in the shared `lyndev` account |

---

## 12. Pitfalls we hit, and their fixes

| Symptom | Cause | Fix (already in the code) |
|---|---|---|
| A local script hangs on Secrets Manager | The machine's IPv6 is broken; urllib3 tries IPv6 first | Local scripts call `_aws.prefer_ipv4()` |
| Claude calls fail: `AccessDeniedException ... Marketplace` | The account hadn't submitted Anthropic's use-case form | One-time form + a first call per model (infra README) |
| Haiku 5.5: `temperature is deprecated` | Claude 5.5 rejects `temperature` | `temperature: 0` is sent to Nova only |
| Sonnet 5.5 rejects `thinking: disabled` | Per-model rule | Haiku uses `disabled`, Sonnet `between_tools` (`build_request`) |
| The verifier rejects correct statements | Nova Lite asked to "list differences" made them up | The model reads facts, code compares (`verify.py`) |
| A browser pre-flight gets 401 | API Gateway serves the deployment made before the OPTIONS method existed | OPTIONS goes to Lambda and is always present; deployment renamed V3 |
| Results' fields in random order | DynamoDB doesn't keep map key order | `_schema_order` in `service.py` |
| The same file returns an old result | Duplicate detection (same bytes, same type) | Intended; the response says `deduplicated: true` |
| Expired jobs still readable for days | DynamoDB TTL is lazy | `JobRepo._expired` hides them at once |
