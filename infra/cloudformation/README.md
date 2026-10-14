# Infrastructure guide (AWS CloudFormation)

This folder creates the **whole AWS side** of the SLM Fintech platform: the API, the functions that process documents, the database, the file storage, the security controls and the alarms.

It is written for someone new to the project and new to AWS. Read sections 1 to 4 once to understand the system; use sections 7 to 11 when you deploy or change something.

| | |
|---|---|
| Tool | **AWS CloudFormation**, plain YAML templates, deployed with the AWS CLI |
| Region | `us-east-1` (N. Virginia). Bedrock inference may also run in `us-east-2` and `us-west-2` |
| Environments | `dev` and `prod` (four stacks each), ideally in **separate AWS accounts** |
| Cost | ~USD 20/month infrastructure in prod at 1,000 pages per business day; ~USD 5 for dev (section 6) |
| First tenant | **LYNEAR** (section 8.3) |
| How the code uses these resources | [docs/implementation-flow.md](../../docs/implementation-flow.md), stage by stage |

---

## Contents

1. [Concepts in five minutes](#1-concepts-in-five-minutes)
2. [Why CloudFormation, and why there is no `serverless.yml`](#2-why-cloudformation-and-why-there-is-no-serverlessyml)
3. [The big picture](#3-the-big-picture)
4. [The four stacks, resource by resource](#4-the-four-stacks-resource-by-resource)
5. [How security works in the infrastructure](#5-how-security-works-in-the-infrastructure)
6. [What it costs, and why it is cheap](#6-what-it-costs-and-why-it-is-cheap)
7. [Before the first deployment (once per account)](#7-before-the-first-deployment-once-per-account)
8. [Deploying step by step](#8-deploying-step-by-step)
9. [Releasing a new version, and rolling back](#9-releasing-a-new-version-and-rolling-back)
10. [Parameters reference](#10-parameters-reference)
11. [Checks before every merge](#11-checks-before-every-merge)
12. [Deleting an environment](#12-deleting-an-environment)
13. [Troubleshooting](#13-troubleshooting)
14. [Known limits and next steps](#14-known-limits-and-next-steps)

---

## Enable the Claude models once per account

Someone with admin rights must do this once in each AWS account, in us-east-1, or every AI call fails with `AccessDeniedException`:
1. Bedrock console → **Model catalog** → the yellow banner → **Submit use case details** (Anthropic's one-time form).
2. Open **Claude Haiku 5.5** and **Claude Sonnet 5.5** in **Playground → Chat** and send one message to each. That creates the Marketplace subscription.

Amazon Nova Lite needs neither step.

---

## 1. Concepts in five minutes

| Term | What it means here |
|---|---|
| **Template** | A YAML file (`data.yaml`, ...) that *describes* AWS resources: "a DynamoDB table named X with these settings". You never click in the console to create them. |
| **Stack** | One deployed copy of a template, e.g. `slm-dev-data`. CloudFormation remembers what it created and updates or deletes exactly those resources. |
| **Parameter** | A value passed in at deploy time (e.g. `Env=dev`). Our values live in `params/dev.json` and `params/prod.json`. |
| **Output / Export** | A value a stack publishes, e.g. the table name. Another stack reads it with `Fn::ImportValue`. This is how our four stacks connect. |
| **Change set** | A preview: "these resources will be added, changed, or replaced". `scripts/deploy.py` always shows one and asks before applying it. |
| **Lambda function** | Our code, run by AWS only when something triggers it. No servers; billed per millisecond. |
| **Code package** | One zip file with our code plus its libraries, built by `scripts/build_lambda.py` (no Docker needed). All 7 functions run the **same** zip, each with a different `Handler`. |
| **IAM role** | An identity with a list of permissions. Each function has its own role and can do only what is listed. |
| **KMS key** | An encryption key AWS stores for us. Data is encrypted with it; using the key needs permission and is logged. |
| **Intrinsic functions** | `!Ref`, `!GetAtt`, `!Sub`, `Fn::ImportValue`: template helpers that insert names, ARNs and text. |
| **ARN** | Amazon Resource Name, the unique id of a resource: `arn:aws:dynamodb:us-east-1:123456789012:table/slm-dev-main`. |

---

## 2. Why CloudFormation, and why there is no `serverless.yml`

The platform is **serverless**: Lambda, API Gateway, SQS, DynamoDB and S3. Nothing runs while there is no work, and there are no servers to patch.

"Serverless" describes the architecture, not a tool. We define it with **plain CloudFormation**, which is built into AWS. That was a project decision (2026-10-14):

- We only use AWS, so we don't need a multi-cloud tool such as Terraform.
- CloudFormation keeps track of what is deployed itself, so there is no state file or state bucket to run and protect.
- No extra framework, account or licence. **AWS SAM and the Serverless Framework are deliberately not used**, so there is no `template.yaml` (SAM) and no `serverless.yml` (Serverless Framework).

The serverless resources are in **`app.yaml`**: the 7 Lambda functions, their triggers, the queues, API Gateway and EventBridge.

---

## 3. The big picture

```
                         ┌──────────────────────────── slm-<env>-app ───────────────────────────────┐
 Tenant (e.g. LYNEAR)    │                                                                           │
 backend server ──HTTPS──┼─► WAF ─► API Gateway (REST) ─► authorizer λ ─► api λ ──┐                  │
        │                │          ANY /v1/{proxy+}        (API key→tenant)      │ queue job        │
        │ presigned      │                                                         ▼                 │
        │ upload         │                              SQS jobs-interactive / jobs-background       │
        ▼                │                                         │  (3 failures → DLQ + alarm)     │
   S3 documents ─────────┼─► GuardDuty scan ─► EventBridge ─► intake λ ─┘                            │
   bucket (data)         │                                         ▼                                 │
        ▲                │                                    worker λ ──► Amazon Bedrock            │
        │ read/write     │                                         │      (Nova Lite → Haiku → Sonnet)│
        │ via tenant-data│                                         ▼                                 │
   DynamoDB table ◄──────┼──────── results, status ──────── SQS webhooks ─► webhook λ ─► tenant URL  │
   (data)        stream ─┼─► retention-cleaner λ          Scheduler (5 min) ─► scan-sweeper λ       │
                         └───────────────────────────────────────────────────────────────────────────┘
   KMS keys, secrets, CloudTrail: data + security stacks     Alarms, budget, dashboard: observability
```

**One document, direct flow (small files, ≤ 4 MB):**
1. LYNEAR's server calls `POST /v1/extractions` with the file. **WAF** checks the request rate and known attack patterns.
2. **API Gateway** asks the **authorizer** function whether the API key is valid. The answer is cached for 60 s.
3. The **api** function stores the file in **S3** and the job in **DynamoDB**, then puts `{tenant_id, job_id}` on the **jobs-interactive** queue.
4. **GuardDuty** scans the new file and tags it `NO_THREATS_FOUND`.
5. The **worker** function picks the job off the queue and waits for the clean tag. It then reads the file, runs the bank-statement parser or the AI model ladder on **Bedrock**, masks identifiers and stores the result.
6. The api function has been checking DynamoDB about once a second. It sees the job finish and returns the JSON in the same HTTP response.

**Presigned flow (up to 25 MB):** the api function returns a one-time **upload URL**. LYNEAR uploads straight to S3. S3 raises an "Object Created" event and GuardDuty raises a "scan result" event; both reach the **intake** function through **EventBridge**, which queues the job. From then on it is the same as step 5.

**Afterwards:**
- When the job finishes, the **webhook** function sends LYNEAR a signed "finished" event. It contains no data.
- When the retention period ends, DynamoDB **TTL** deletes the job record. The **retention-cleaner** function sees that deletion on the table's stream and deletes the job's S3 files.
- Every 5 minutes the **scan-sweeper** function expires uploads that never arrived and fails scans that never finished.

---

## 4. The four stacks, resource by resource

Deploy order is **data → security → app → observability**, plus **web** (independent) for the client console (`deploy.py` builds and uploads the code zip for `app`). Each stack imports values exported by the ones before it.

### 4.1 `data.yaml`: everything that stores something (stack `slm-<env>-data`)

These resources hold data, so they are separate from the code. You can delete and recreate `app` without losing a single job. All of them have `DeletionPolicy: Retain`: deleting the stack does **not** delete them.

| Resource | Name | Key settings and why |
|---|---|---|
| `DocumentsKey` | `alias/slm-<env>-documents` | Encrypts every S3 object. The key policy grants nothing directly; only IAM roles we allow (tenant-data, GuardDuty) may use it, and only through S3. |
| `TableKey` | `alias/slm-<env>-table` | Encrypts DynamoDB and SQS. Usable by account principals **only through** those two services (`kms:ViaService`). |
| `LogsKey` | `alias/slm-<env>-logs` | Encrypts log groups, secrets and CloudTrail files. Statements for the CloudWatch Logs and CloudTrail services. |
| `Table` | `slm-<env>-main` | The single DynamoDB table (architecture §7.1). Keys `PK`/`SK`. On-demand billing (no idle cost). **TTL** on `expires_at` deletes expired jobs for free. **Stream** (old image) feeds the retention cleaner. **PITR** allows restoring to any second in the last 35 days. Deletion protection is on. Sparse GSI **`pending`**: only waiting jobs carry `pending_pk`, so the sweeper reads very little. |
| `Bucket` | `slm-<env>-docs-<account>-<region>` | All tenant files under `tenants/<tenant_id>/`. Public access fully blocked; ACLs off. Default encryption with the documents key and **Bucket Key** (about 99% fewer KMS calls). **EventBridge notifications on**, which the intake function and GuardDuty need. Backstop lifecycle deletes anything older than 400 days. **No versioning**, on purpose: a deleted document must really be gone. |
| `BucketPolicy` | | Denies any non-TLS request, and any upload that names a KMS key other than ours. |
| `CodeBucket` | `slm-<env>-code-<account>-<region>` | Holds the Lambda zip packages (`lambda-<content-hash>.zip`) and large templates. Old packages expire after 90 days. |

### 4.2 `security.yaml`: who may touch tenant data (stack `slm-<env>-security`)

| Resource | Name | What it does |
|---|---|---|
| `TenantDataRole` | `slm-<env>-tenant-data` | The **only** role that can read or write tenant data. See section 5.1. |
| `ApiKeyPepper` | secret `slm/<env>/api-key-pepper` | A secret mixed into API-key hashes. **Secrets Manager generates the value**, so it never appears in a file or a terminal. Changing it invalidates every API key, so it is retained on delete. |
| `MalwareScanRole` | `slm-<env>-guardduty-malware-scan` | Lets GuardDuty read new uploads, decrypt them and tag them with the scan result. |
| `TrailBucket`, `TrailBucketPolicy`, `Trail` | `slm-<env>-trail` | **CloudTrail**: a record of every AWS API call (who did what, when), multi-region, with log-file validation. Management events only (free for the first trail). Switch off with `CreateTrail=false` if your AWS Organization already has an organisation trail. |

### 4.3 `app.yaml`: everything that runs (stack `slm-<env>-app`)

**Queues (SQS).** Messages hold only `{tenant_id, job_id}`, never document content. All are KMS-encrypted and keep messages for 14 days.

| Queue | Consumer | Settings |
|---|---|---|
| `slm-<env>-jobs-interactive` | worker | For jobs a client is waiting for. Visibility timeout 180 s (> worker timeout 120 s). After **3** failed tries → `...-dlq`. |
| `slm-<env>-jobs-background` | worker | The backlog, so it never slows down waiting clients. Same settings. |
| `slm-<env>-webhooks` | webhook | Visibility 90 s. After **5** failed deliveries → `...-dlq`. |

**Log groups.** One per function, created *before* the functions so we control encryption and retention: 30 days, but 180 days for `api` because it carries the audit records. Plus the API Gateway access log (180 days). Logs never contain document content: the app's logger only allows a fixed list of fields.

**The 7 functions** (all arm64, Python 3.12, no VPC, the same zip package):

| Function | Entry point | Memory / timeout | Triggered by | Its role may also |
|---|---|---|---|---|
| `slm-<env>-authorizer` | `app.lambda_handlers.authorizer` | 256 MB / 5 s | API Gateway, each request (cached 60 s) | read `APIKEY#*` / `TENANT#*` items (read-only), read the pepper |
| `slm-<env>-api` | `...api` | 1 GB / 45 s | API Gateway | assume tenant-data; send to both job queues |
| `slm-<env>-worker` | `...worker` | 2 GB / 120 s | both job queues, **1 job per invocation** | assume tenant-data; consume job queues; send webhooks; invoke **only** the 3 approved Bedrock models through `us.` profiles |
| `slm-<env>-intake` | `...intake` | 128 MB / 10 s | EventBridge rules `upload-created`, `scan-result` | assume tenant-data; send to the interactive and webhook queues |
| `slm-<env>-webhook` | `...webhook` | 128 MB / 15 s | webhooks queue | assume tenant-data; read secrets `slm/<env>/webhook/*` |
| `slm-<env>-retention-cleaner` | `...retention_cleaner` | 128 MB / 30 s | table stream, **filtered to TTL deletions only** | assume tenant-data; read the stream |
| `slm-<env>-scan-sweeper` | `...scan_sweeper` | 128 MB / 60 s | Scheduler, every 5 minutes | query the `pending` index; assume tenant-data; send webhooks |

Every role is named `slm-<env>-fn-<name>`. The tenant-data role trusts exactly that name pattern.

**Concurrency control.** The two worker triggers have `MaximumConcurrency` (dev 5 + 2, prod 10 + 3). The backlog can never take the capacity of waiting clients, and Bedrock quotas aren't flooded.

**Events and schedules.**
- `UploadCreatedRule`: S3 "Object Created" under `tenants/` → intake.
- `ScanResultRule`: GuardDuty scan result for our bucket → intake.
- `MalwareProtectionPlan`: GuardDuty scans every new object under `tenants/` and tags it with `GuardDutyMalwareScanStatus`.
- `ScanSweeperSchedule`: every 5 minutes.
- `WindowOpen*` / `WindowClose*`: created **only** if `ProcessingWindow` is not `always`. At closing time they switch the worker's queue triggers off, and at opening time back on. Jobs wait safely in SQS in between.

**API Gateway (REST API `slm-<env>-api`).**
- Routes: one catch-all `ANY /v1/{proxy+}` → api function, plus a public `GET /healthz`. FastAPI does the real routing, so a new endpoint needs **no infrastructure change**.
- **REST API, not HTTP API:** only the REST type lets the integration timeout go above 29 s, which the 40-second result wait needs (section 7).
- `BinaryMediaTypes: multipart/form-data`, so file uploads aren't corrupted on the way in.
- Gateway responses give 401, 403, 429 and 5xx errors the same `{"error": {...}}` shape as the app.
- Stage `<env>`: throttling for the whole API, metrics on, **execution logging off** (it could capture request data), and access logs with ids, status and timings only.
- `ApiGatewayAccount`: the account-wide role API Gateway needs to write access logs (see section 13).

**WAF (`slm-<env>-api`)**, created only when `EnableWaf=true` (prod). It has three rules: a rate limit per IP, AWS "known bad inputs", and AWS "IP reputation". The AWS "common rule set" is **not** used: its 8 KB body limit would block every file upload.

### 4.4 `observability.yaml`: knowing when something is wrong (stack `slm-<env>-observability`)

| Resource | What it watches | Fires when |
|---|---|---|
| `AlertsTopic` | | Sends every alert to `AlertEmail`. **You must click the confirmation email.** Not KMS-encrypted on purpose: CloudWatch and Budgets can't publish to an `aws/sns`-encrypted topic, and alerts contain no data. |
| `JobsInteractiveDlqAlarm`, `JobsBackgroundDlqAlarm` | dead-letter queues | any message arrives (a job failed 3 times) |
| `WebhooksDlqAlarm` | webhook dead-letter queue | a webhook failed 5 times (tenant endpoint down?) |
| `InteractiveBacklogAlarm` | age of the oldest waiting job | > 2 minutes for 3 minutes (throttling? broken worker?) |
| `WorkerErrorsAlarm` | worker crashes or timeouts | ≥ 3 in 5 minutes |
| `Api5xxAlarm` | API server errors | ≥ 5 in 5 minutes |
| `SecurityChangeRule` | CloudTrail events | **instantly** when someone turns on Bedrock invocation logging (it would copy documents into logs), or disables or schedules deletion of a KMS key |
| `MonthlyBudget` | the AWS bill | 80% and 100% of the budget, and a forecast over 100% |
| `Dashboard` `slm-<env>` | one page: API traffic, worker health, queue depths, DLQs | |


### 4.5 `web.yaml`: the test console for clients (stack `slm-<env>-web`)

A React app (`web/`) that clients open in a browser to upload documents and see results, tokens and AI cost. Each client signs in with **their own** API key, which stays in their browser tab.

| Resource | What it does |
|---|---|
| `WebBucket` | **Private** bucket with the built app. Nothing in it is public |
| `WebOriginAccessControl` + `WebBucketPolicy` | Only this CloudFront distribution may read the bucket |
| `WebDistribution` | Serves the app over **HTTPS only** at `https://<id>.cloudfront.net` |
| `WebSecurityHeaders` | Content-Security-Policy (the page may only talk to API Gateway and S3), HSTS, no framing, no MIME sniffing, no referrer |

Publish a new version with `uv run python scripts/deploy_web.py prod --profile <profile>`. It builds the app, writes `config.json` (the API URL only, never a key), uploads, and clears the CloudFront cache.

The API only accepts browser calls from the console's address: `CorsAllowOrigin` in `params/<env>.json` (data and app blocks) is set to the web stack's `WebOrigin` output.

---

## 5. How security works in the infrastructure

### 5.1 Tenant isolation through IAM (the most important idea)

Functions **cannot** read tenant data with their own roles. To touch LYNEAR's data, code calls `tenant_session("t_...")` in `app/storage/aws.py`, which:

1. calls `sts:AssumeRole` on `slm-<env>-tenant-data` with the session tag `tenant_id = t_...`;
2. receives temporary credentials (valid 1 hour, cached and renewed in time);
3. inside that session, IAM replaces `${aws:PrincipalTag/tenant_id}` with LYNEAR's id.

The role's policy only allows:
- DynamoDB items whose partition key is `TENANT#<that id>`;
- S3 objects under `tenants/<that id>/`, and listing only that prefix;
- the documents KMS key, only through S3.

So if a bug ever builds a key for another tenant, **AWS itself answers AccessDenied**. The trust policy also requires the caller to be one of our function roles (`slm-<env>-fn-*`), and the session to carry exactly one tag, `tenant_id`, of the form `t_*`.

Only two roles see more than one tenant, and both are read-only and narrow:
- **authorizer:** `GetItem` on `APIKEY#*` and `TENANT#*`, because it runs before we know the tenant;
- **scan-sweeper:** `Query` on the `pending` index only.

### 5.2 Encryption

| Data | Encrypted with |
|---|---|
| S3 files and results | documents key (SSE-KMS + Bucket Key) |
| DynamoDB, SQS messages | table key |
| Log groups, secrets, CloudTrail | logs key |
| In transit | TLS everywhere; the bucket policy rejects non-TLS requests |

Optional extra layer: `EnforceTenantEncryptionContext=true` makes KMS decrypt an S3 object only for a session tagged with the **same** tenant. It turns Bucket Key off, so KMS requests rise (a few USD a month).

### 5.3 Bedrock

The worker may call **only** these three, and only through the `us.` inference profiles:
- `us.amazon.nova-lite-v1:0`
- `us.anthropic.claude-haiku-5-5*`
- `us.anthropic.claude-sonnet-5-5*`

Any other model, or a `global.` profile, gets AccessDenied. Calls use the function's IAM role (no API keys).

### 5.4 Secrets

Environment variables contain only names and ARNs, never secret values. The pepper is generated by Secrets Manager; webhook secrets are created by `scripts/create_tenant.py`; both are read at run time and cached for 5 minutes.

---

## 6. What it costs, and why it is cheap

Full model in [docs/cost.md](../../docs/cost.md) §4.2. Prod at 1,000 pages per business day (22,000 pages ≈ 5,500 documents a month):

| Resource | USD / month |
|---|---|
| AWS WAF (fixed: 1 ACL + 3 rules) | 8.01 |
| Lambda (~330,000 GB-seconds; covered by the always-free tier) | 4.46 → **0** |
| KMS (3 keys) | 3.15 |
| GuardDuty malware scanning | 1.74 |
| Secrets Manager (pepper + LYNEAR webhook) | 0.86 |
| CloudWatch (6 alarms, logs) | 0.81 |
| SQS (mostly Lambda's idle long-polling) | 0.40 |
| DynamoDB, S3 (documents + code), API Gateway, CloudTrail | 0.58 |
| EventBridge, Scheduler, SNS, Budgets | 0.00 |
| **Total** | **~20 gross, ~15.50 net** |

Add **~USD 36 for Bedrock** (the AI models) and the platform costs **~USD 56 a month**, well under the USD 150 target. **Dev costs ~USD 5 a month** because WAF is off there.

Why it is cheap, by design:
- **Nothing is billed by the hour:** no servers, no GPUs, no NAT gateways, no VPC endpoints (a VPC alone would cost USD 60–70 a month).
- **On-demand everything:** DynamoDB, Lambda, SQS and API Gateway cost nothing while idle.
- **Small KMS bills:** S3 Bucket Key, SQS data-key reuse (1 hour) and secret caching keep KMS requests in the hundreds, not millions.
- **Files are deleted after processing,** so S3 storage stays near zero.
- **No paid add-ons where a free one works:** EventBridge security alerts instead of AWS Config; CloudTrail management events only (free); no X-Ray; no API cache.
- **Cheapest model first:** the AI ladder starts with the cheapest model that is good enough.

Costs that grow over time: KMS key rotation adds USD 1 per key per month for each of the first two yearly rotations, so about USD 6 in year 2 and USD 9 from year 3.

---

## 7. Before the first deployment (once per account)

Do these in order; most take minutes, the quota requests can take days.

| # | Step | Why |
|---|---|---|
| 1 | Use **separate AWS accounts** for dev and prod. | Two environments in one account would share the API Gateway logging role and need two CloudTrail decisions (section 13). |
| 2 | **Service Quotas → Lambda → Concurrent executions**: request at least **100** (often 1,000 is granted). | New accounts can start at **10**. The worker alone may use 13 in prod, so jobs would be throttled. |
| 3 | **Service Quotas → API Gateway → Maximum integration timeout in milliseconds**: request **45,000**. | The default 29 s caps the "wait for result" at 25 s. After approval, set `ApiIntegrationTimeoutMs=45000` and `MaxWaitSeconds=40`. |
| 4 | **Bedrock console → Model access**: enable Nova Lite, Claude Haiku 5.5, Claude Sonnet 5.5. Check the tokens-per-minute quotas for each. | Without access, the worker gets AccessDenied. |
| 5 | **Bedrock data retention**: set the account mode to `none` in us-east-1, us-east-2 and us-west-2 (security.md §5.2). | Bedrock must keep nothing. This is an account setting, not a CloudFormation resource. |
| 6 | Make sure Bedrock **model invocation logging is OFF** (Bedrock console → Settings). | It would copy documents into logs. The `SecurityChangeRule` alerts if anyone turns it on later. |
| 7 | Decide **CloudTrail**: if your organisation already has an organisation trail, set `"CreateTrail": "false"` in `params/<env>.json`. | Avoids a duplicate trail. |
| 8 | Put a real address in `"AlertEmail"` in `params/<env>.json`. | Alarms and budget alerts go there. |
| 9 | Install tools: AWS CLI v2, Python 3, `uv` (no Docker needed). Then `uvx cfn-lint --version`. |  |
| 10 | Sign in with admin credentials for the target account: `aws sts get-caller-identity`. | Creating IAM roles needs admin rights. |

---

## 8. Deploying step by step

All commands run from the repository root.

### 8.1 First deployment of an environment

All four stacks in order, with one command (each change set is shown and confirmed):

```bash
uv run python scripts/deploy.py prod all --profile lyndev
```

Or one stack at a time:

```bash
# 1. Storage, keys, image repository (~3 min)
uv run python scripts/deploy.py dev data --profile <profile>

# 2. Tenant-data role, pepper secret, GuardDuty role, CloudTrail (~2 min)
uv run python scripts/deploy.py dev security --profile <profile>

# 3. Functions, queues, API, WAF, GuardDuty plan (~5 min). deploy.py builds the code zip
#    (scripts/build_lambda.py, no Docker) and uploads it to the code bucket first.
uv run python scripts/deploy.py dev app --profile <profile>

# 5. Alarms, budget, dashboard, security alerts (~1 min). Then CONFIRM the SNS email.
uv run python scripts/deploy.py dev observability --profile <profile>
```

What `scripts/deploy.py` does for each stack:
1. runs `cfn-lint` on the template;
2. reads that stack's block from `params/<env>.json`;
3. creates a **change set** and prints a table of planned changes;
4. asks `Execute this change set? [y/N]`, then executes and waits.

In the table, **`Replacement = True` means the resource will be deleted and recreated**. Stop and think if that's a table, bucket, key or secret. (They are retained, but the new one would be empty.)

### 8.2 Check it works

```bash
API=$(aws cloudformation describe-stacks --stack-name slm-dev-app \
      --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text)
curl "$API/healthz"                       # → {"ok":true}
curl -i "$API/v1/document-types"          # → 401 {"error":{"code":"unauthorized",...}}  (no key yet: correct)
```

Then open CloudWatch → Dashboards → `slm-dev`.

### 8.3 Onboard the first tenant: LYNEAR

```bash
uv run python scripts/create_tenant.py --env dev --name LYNEAR
# options: --webhook-url https://...   --retention-days 7|30|90|365
#          --verification-mode ai|checks_only   --unmasked pan account ...   --read-only
```

- The **API key is printed once** (and the webhook secret, if you passed `--webhook-url`). Only a hash is stored. Send it to LYNEAR over a secure channel; if it is lost, create a new key.
- Running it again with the same name creates nothing; the script tells you the existing tenant id.
- Test with the key: `curl -H "Authorization: Bearer <key>" "$API/v1/document-types"` → 200.

---

## 9. Releasing a new version, and rolling back

**Release** = step 3 of section 8.1 again: `uv run python scripts/deploy.py prod app --profile <profile>`. It rebuilds the zip; its name is a hash of its contents, so unchanged code is not uploaded again.

The change set should show only the 7 `AWS::Lambda::Function` resources as `Modify` with `Replacement = False`.

**Rollback** = check out the previous release's commit and run `uv run python scripts/deploy.py prod app --profile <profile>` again (old packages stay in the code bucket for 90 days).

If a stack update **fails**, CloudFormation rolls back to the previous working state by itself. Check the stack's **Events** tab for the first `FAILED` line.

**Changing a setting** (for example the wait time after the quota increase): edit `params/<env>.json`, then run `uv run python scripts/deploy.py <env> app --profile <profile>`.

---

## 10. Parameters reference

| Stack | Parameter | Default | Meaning |
|---|---|---|---|
| all | `Env` | (none) | `dev` or `prod`; part of every resource name and export |
| data, security | `EnforceTenantEncryptionContext` | `false` | KMS decrypts only for the matching tenant session; turns S3 Bucket Key off. **Must be equal in both stacks.** |
| security | `CreateTrail` | `true` | Create this account's CloudTrail trail |
| app | `CodeKey` | (none) | S3 key of the Lambda zip (set by `deploy.py`) |
| data, app | `CorsAllowOrigin` | `""` | Web origin allowed to call the API from a browser. Empty = none. Never `*` in production |
| app | `ApiIntegrationTimeoutMs` | `29000` | API Gateway wait for the api function. 45000 after the quota increase |
| app | `MaxWaitSeconds` | `25` | Longest `?wait=`; keep a few seconds below the timeout. 40 after the quota increase |
| app | `ApiThrottleRate` / `ApiThrottleBurst` | `50` / `100` | Requests per second for the whole API |
| app | `EnableWaf` | `true` | WAF on/off (dev params: `false`) |
| app | `WafRateLimitPerIp` | `1000` | Requests per IP per 5 minutes before WAF blocks |
| app | `InteractiveMaxConcurrency` / `BackgroundMaxConcurrency` | `10` / `3` | Max parallel workers per queue (minimum 2) |
| app | `LogLevel` | `INFO` | App log level |
| app | `ProcessingWindow` | `always` | e.g. `Mon-Fri 09:00-19:00 Asia/Kolkata` |
| app | `WindowOpenCron` / `WindowCloseCron` / `WindowTimezone` | 09:00 / 19:00 Mon–Fri, `Asia/Kolkata` | Must describe the same window as `ProcessingWindow` |
| observability | `AlertEmail` | (none) | Where alerts go |
| observability | `MonthlyBudgetUsd` | `150` | Budget for alerts (dev params: 50) |

**Naming convention:** everything is `slm-<env>-<thing>`. Roles that may assume tenant-data are `slm-<env>-fn-<function>`; secrets are `slm/<env>/...`; exports are `slm-<env>-<Name>`.

---

## 11. Checks before every merge

```bash
cd infra/cloudformation
uvx cfn-lint --regions us-east-1 -- *.yaml   # template errors against the AWS resource specs
uvx checkov -d .                              # security scan; reads .checkov.yaml
cd ../.. && uv run pytest -q && uv run ruff check .
```

All must pass. Current result: cfn-lint clean; checkov **174 passed, 0 failed, 11 skipped**. Every skip has its reason written down:
- for all Lambdas, in `.checkov.yaml`: no VPC, no async DLQ, no reserved concurrency, environment variables without a customer-managed key;
- for single resources, in their `Metadata`: KMS principal `*` limited to the account, S3 versioning off, code bucket without access logs or versioning, no API cache, no X-Ray, public `/healthz`, unencrypted alerts topic, trail-bucket logging.

When you add a skip, write **why** next to it.

---

## 12. Deleting an environment

Delete in **reverse** order (an export can't disappear while another stack still imports it):

```bash
for s in web observability app security data; do
  aws cloudformation delete-stack --stack-name slm-dev-$s
  aws cloudformation wait stack-delete-complete --stack-name slm-dev-$s
done
```

**Retained** on purpose; delete by hand only if you really mean it:
- the KMS keys (schedule deletion, 30-day wait);
- the DynamoDB table (turn deletion protection off first);
- the documents bucket and trail bucket (empty them first);
- the pepper secret.

The code bucket must be emptied before the data stack can delete it.

---

## 13. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `deploy.py`: "Export slm-dev-X cannot be updated/deleted as it is in use" | A later stack imports that value | Change or delete the importing stack first (section 12 order) |
| `app` stack fails creating a function: code not found | Zip not uploaded, or wrong `CodeKey` | Run `uv run python scripts/deploy.py <env> app` (it builds and uploads); don't pass `CodeKey` by hand |
| `app` stack: "CloudWatch Logs role ARN must be set in account settings" | `ApiGatewayAccount` not yet applied | Re-run; the stage already depends on it. If another stack owns this setting, remove `ApiGatewayAccount` and `ApiGatewayCloudWatchRole` from `app.yaml` |
| `security` stack: trail creation fails | An organisation trail exists, or the bucket policy is not ready | Set `CreateTrail=false`, or re-run once |
| Every API call returns **401** | Missing header, or not `Authorization: Bearer slm_...` | Check the header; the key must be from the same environment |
| **403 `tenant_suspended`** | Tenant `status` is not `active` | Check the tenant's `PROFILE` item |
| **504** after ~29 s | API Gateway's default integration timeout | Use `?wait=` ≤ `MaxWaitSeconds`, or get the quota increase (section 7) |
| Jobs stay `queued`, worker `Throttles` > 0 | Account Lambda concurrency quota too low | Section 7 step 2 |
| Worker log `AccessDeniedException` from Bedrock | Model access not enabled, or a model id outside the allowlist | Section 7 step 4; models must be the `us.` profiles in `WorkerRole` |
| `AccessDenied` on DynamoDB/S3 in a function log | Code used a key outside the tenant's prefix (the isolation **working**), or the tenant id doesn't start with `t_` | Fix the code path; never widen the tenant-data policy |
| Presigned uploads always end as `expired` | No `Object Created` events | `EventBridgeEnabled` on the bucket, and the `upload-created` rule targets intake |
| Jobs fail with `scan_timeout` | GuardDuty plan not active, or role missing a permission | GuardDuty console → Malware Protection → plan status; check `MalwareScanRole` |
| No alarm emails | SNS subscription not confirmed | Confirm the email; check the topic's subscriptions |
| Retention cleaner never runs | Expected: it only runs on **TTL** deletions, which AWS performs usually within a few days after `expires_at` | Nothing to fix |

---

## 14. Known limits and next steps

- **Not yet tested on a real account.** Templates pass cfn-lint and checkov, but the first dev deployment is the real test. Two things to check then:
  - the GuardDuty scan-result event field names (`app/intake.py`);
  - the Scheduler payload that switches the worker triggers (`{"UUID": ..., "Enabled": ...}`, only used with a processing window).
- **Per-tenant throttling:** today the stage throttles the whole API and WAF limits each IP. Per-tenant usage plans (FR-1.8) need the authorizer to return a `usageIdentifierKey`.
- **CI/CD:** a GitHub Actions workflow (OIDC to AWS, build, push, `AUTO_APPROVE=1 uv run python scripts/deploy.py dev app --profile <profile> $TAG`) is still to be written (implementation guide §7.4).
- **Organisation controls:** SCPs for the region lock, the Bedrock retention lock and the ban on Bedrock API keys belong to an organisation-level stack, not written yet.
- **Cost alarms from the app's numbers** (Sonnet escalation rate, verifier disagreement, cost per day, cost.md §9) need CloudWatch metric filters on the usage logs, about USD 0.30 per metric.
- **Audit log group:** audit records go to the `api` log group (kept 180 days) rather than a separate `audit` group.
- **Custom domain:** the API uses the default `execute-api` URL. A custom domain (ACM certificate + Route 53, ~USD 0.50 a month) can be added in front later.
