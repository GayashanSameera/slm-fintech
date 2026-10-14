# SLM Fintech Platform: Cost Estimate

| | |
|---|---|
| Version | 0.4 (model cost recalculated after the accuracy work, §2.5; infrastructure unchanged from 0.3) |
| Date | 2026-10-14 |
| Design | [architecture.md](architecture.md) (Bedrock, us-east-1, Rung 0 parsers + model ladder) |
| Target | NFR-K1: about USD 150 per month at 1,000 pages per day |
| Changes since 0.2 | Infrastructure cost is now priced resource by resource from `infra/cloudformation/*.yaml` (§4.2) instead of estimated: ~USD 20 instead of USD 44. Model cost unchanged. |

## 1. Result

At **1,000 pages per business day** (22,000 pages a month), the platform costs about **USD 56 per month**: about **USD 0.0025 per page**. That is well under the USD 150 target. Lambda's always-free tier brings the real bill to about USD 52.

| Scenario | Pages / month | Model (Bedrock) | Infrastructure (templates) | **Total / month** |
|---|---|---|---|---|
| **Base: 1,000 pages per business day** | 22,000 | 36 | 20 | **USD 56** |
| 1,000 pages every calendar day | 30,000 | 47 | 22 | **USD 69** |
| Pessimistic (§5.1) | 22,000 | 101 | 20 | **USD 121** |
| Growth: 5,000 pages per business day (NFR-P1) | 110,000 | 160 | 47 | **USD 207** |
| Dev environment (WAF off, ~500 test documents' pages) | 500 | ~1 | 5 | **~USD 6** |

All figures are in USD, exclude taxes and are rounded. Infrastructure is shown **before** free tiers. Prices are us-east-1 list prices as of 2026-10-13; Bedrock prices must be confirmed (§2.1).

Key points:

- **Rung 0 does the heavy lifting.** About 43% of pages (digital bank statements from known banks, plus common payslips) are read by Python parsers. Each result is still checked by the cheapest AI model, Nova Lite, which costs ~USD 0.00025 per page, against ~USD 0.0026 per statement page through the AI ladder. Without Rung 0, the model line would be ~USD 54 instead of ~USD 36.
- **Sonnet escalations are still the biggest single cost:** ~USD 15 of the USD 56. Keeping the escalation rate low remains the main tuning lever (§6).
- **The fixed floor is about USD 13** (WAF 8, KMS keys 3, secrets 0.80, alarms 0.60, idle queue polling 0.40). Everything else scales with pages.
- **Nothing is billed by the hour.** With no traffic, only the fixed floor is paid.
- **Optional security add-ons are not in the totals** (security.md §14: account-wide GuardDuty, Inspector, Object Lock archive, S3 data events): about **+USD 10** if adopted.

## 2. Unit prices used

### 2.1 Amazon Bedrock (USD per million tokens, US geographic profile)

Anthropic documents a 10% premium for geographic routing over global routing, so Claude prices below are list × 1.10. Nova Lite prices come from third-party price tables. **Confirm all of them on the Bedrock pricing page in phase 0.**

| Model | Input | Output | Cache read | Cache write (1-hour TTL) |
|---|---|---|---|---|
| Amazon Nova Lite, `us.` profile | 0.06 | 0.24 | ~0.015 | n/a |
| Claude Haiku 5.5, `us.` profile | 0.11 | 0.55 | 0.011 | 0.22 |
| Claude Sonnet 5.5, `us.` profile | 2.20 | 11.00 | 0.22 | 4.40 |

### 2.2 Other AWS services (us-east-1)

| Service | Price used |
|---|---|
| Lambda (arm64) | USD 0.0000133334 per GB-second, USD 0.20 per million requests |
| API Gateway REST API | USD 3.50 per million requests |
| AWS WAF | USD 5 per web ACL + USD 1 per rule per month + USD 0.60 per million requests |
| DynamoDB on-demand | USD 0.625 per million write units, USD 0.125 per million read units, USD 0.25 per GB, PITR USD 0.20 per GB |
| S3 Standard | USD 0.023 per GB, USD 0.005 per 1,000 PUTs, USD 0.0004 per 1,000 GETs |
| SQS | USD 0.40 per million requests (first million free) |
| KMS | USD 1 per key per month, USD 0.03 per 10,000 requests |
| Secrets Manager | USD 0.40 per secret per month |
| GuardDuty Malware Protection for S3 | USD 0.09 per GB scanned + about USD 0.215 per 1,000 objects |
| CloudWatch | Logs USD 0.50 per GB ingested, USD 0.03 per GB stored; USD 0.30 per custom metric; USD 0.10 per alarm |

**Free tiers are not subtracted.** Lambda's always-free tier would cover all our Lambda use, so the real bill should come in a few dollars lower.

## 2.5 Cost after the accuracy work (v0.4, measured 2026-10-14)

The accuracy changes add free checks (identifier check digits, date order and period, opening and closing
balance, text grounding) and change how scanned documents are read. Only two of them cost model tokens.
Every figure below was measured with the real models on labelled test documents (`eval/`), at the list prices
in §2.1, on deliberately degraded scans.

| Change | Cost effect | Measured |
|---|---|---|
| Identifier, date, balance and grounding checks | none (plain Python) | USD 0 |
| **Scanned bank statements read one page per call** (was 3 pages per call) | **cheaper**: fewer pages fail their checks and escalate to Sonnet | **USD 0.0016 per page, was 0.0028** (25 real bank layouts, 87 pages) |
| Targeted page repair (a failed whole-statement check re-reads only the pages it points at, with Sonnet) | included in the line above; Sonnet is 42% of that cost | included |
| Second reading of **scanned invoices, payslips, identity documents, KYC forms** | **more expensive**, only on scans | USD 0.0044 per document, was 0.0004 (digital documents: unchanged, no second reading) |
| Second reading of **scanned bank statements** | **off by default**: 5 to 7 times the page cost, no measured accuracy gain | USD 0.008 to 0.012 per page if switched on |

**Monthly model cost, base case (22,000 pages, 22 business days).** The unchanged lines keep the §4.1 assumptions
(including a 5% Sonnet escalation rate for pages not measured here). Assumed split of non-statement pages: half are scans.

| Line | Pages | Cost per page | Monthly |
|---|---|---|---|
| Rung 0 verification by Nova Lite | 8,600 | 0.00025 | 2 |
| Digital statements without a parser, plus parser results the verifier rejects | 3,000 | 0.0025 | 7.5 |
| **Scanned bank statements** (page by page) | 4,600 | **0.0016** | **7.4** |
| Invoices, payslips, identity, KYC: digital | 3,300 | 0.0025 | 8.3 |
| Invoices, payslips, identity, KYC: **scanned, with second reading** | 3,300 | 0.0025 + 0.0040 | **21.5** |
| Type detection and prompt-cache writes | | | 5 |
| **Model total** | | | **about USD 52** |

| Scenario | Model per month | All-in with infrastructure (USD 20 gross) |
|---|---|---|
| Before the accuracy work (v0.3) | 36 | 56 |
| **Now, defaults** | **~52** | **~72** |
| Second reading of non-statement scans switched off | ~39 | ~59 |
| Second reading of scanned statements also switched on | ~83 | ~103 |

All scenarios stay under the USD 150 target (NFR-K1). The share of scans among non-statement documents is the
biggest unknown: every 10 points move the total by about USD 2.6. The first month of real traffic replaces these
assumptions: each job already returns `usage.estimated_cost_usd`.

**The retention lock adds no measurable cost:** one Lambda invocation per deployment and a small log group.

**Accuracy measured with the same runs (honest limits).** Digital bank statements: every row is proven against the PDF
text. Scanned statements, 25 real layouts degraded hard, default configuration: 99.9% of fields right, 22 of 25
documents fully right, 2 documents with an error and no warning (a closing balance left out, and reference text
invented on a page without any). Those two are open items, not covered by a check yet.

## 3. Workload assumptions

| Assumption | Value | Basis |
|---|---|---|
| Pages per day | 1,000 | NFR-P1 |
| Days per month | 22 business days (base); 30 in the second scenario | FR-5 |
| Mix by pages | **Bank statements 70%**, invoices 10%, payslips 10%, identity 5%, KYC 5% | Most processing is bank statements (2026-10-13); exact split to confirm |
| Bank statements that are digital PDFs (e-statements) | 70% | Estimate; most statements are downloaded from net banking |
| Digital statements from banks with a Rung 0 parser | 80% | First parsers for the top 5–8 banks in the samples |
| Payslips handled by Rung 0 | 36% (60% digital × 60% known layouts) | Estimate |
| Rung 0 results the verifier disagrees with (sent to the AI ladder) | 10% | Estimate; measure in phase 0 |
| Pages needing a second AI try (Haiku with thinking) | 15% of AI-ladder pages | Estimate |
| Pages escalated to Sonnet 5.5 | 5% of AI-ladder pages | Estimate |

**Result:**

- About **43% of pages** go to Rung 0. The verifier sends 10% of those, about 4% of all pages, on to the AI ladder.
- So about **39% of pages** are finished by Rung 0 + verification alone.
- About **61%** go through the AI ladder: the 57% that never qualified for Rung 0, plus the 4% sent back.

### 3.1 Tokens per page

| Part | Tokens |
|---|---|
| Page image (scanned 1,568 px ≈ 2,300; digital 1,000 px ≈ 950) + text layer + instructions, averaged | ~2,375 input |
| Cached prefix (system prompt, schema, examples) | ~3,000 at the cache-read price |
| Output, bank statement page (~30 transactions) | ~1,200 |
| Output, invoice / payslip / identity / KYC page | ~450 / 350 / 200 / 300 |
| **Weighted average output** (statement-heavy mix) | **~945** |
| **Verification by Nova Lite** | statement page images in (~2,500 for 1 page, ~5,000 for 2), ~150 out (the key facts it reads; the parser's output is not sent). Measured: USD 0.0002–0.0003 per statement |

## 4. Base case in detail: 22,000 pages a month

### 4.1 Model cost

| Step | Pages | Cost per page | Monthly |
|---|---|---|---|
| Rung 0: Python parser | ~9,400 | 0 | 0 |
| Rung 0: verification by Nova Lite | ~9,400 | ~USD 0.00025 | USD 2 |
| AI ladder, first pass (Haiku 5.5, thinking off) | ~13,500 | ~USD 0.00075 average (USD 0.00095 for statements) | USD 10 |
| AI ladder, second try (Haiku 5.5, thinking on) | ~2,000 | ~USD 0.0017 | USD 3 |
| AI ladder, escalation to Sonnet 5.5 | ~680 | ~USD 0.022 (USD 0.026 for statements) | USD 15 |
| Prompt-cache writes, type detection | | | USD 5 |
| **Model total** | | **~USD 0.0016 per page on average** | **USD 36** |

### 4.2 Infrastructure cost, from the CloudFormation templates

Every resource in `infra/cloudformation/` priced with its real settings. Workload: 22,000 pages ≈ 5,500 documents (about 4 pages each).

| Resource (template) | What drives the cost | Monthly |
|---|---|---|
| AWS WAF (`app.yaml`) | Fixed: 1 web ACL USD 5 + 3 rules USD 3; requests negligible. **Currently off in prod too** (`EnableWaf=false` since 2026-10-14, owner's decision): this line is then 0 and the total ~USD 12 | 8.01 |
| KMS (`data.yaml`) | Fixed: 3 customer-managed keys USD 3. Requests stay tiny thanks to S3 Bucket Key, SQS data-key reuse (1 h) and secret caching | 3.15 |
| GuardDuty Malware Protection (`app.yaml`) | Every object under `tenants/` (~5,800 objects, ~6 GB) | 1.74 |
| Secrets Manager (`security.yaml`, `create_tenant.py`) | API-key pepper + 1 webhook secret (LYNEAR) at USD 0.40 each; +0.40 per extra tenant with a webhook | 0.86 |
| CloudWatch (`app.yaml`, `observability.yaml`) | 6 alarms × USD 0.10; ~0.3 GB of logs; 1 dashboard (free up to 3 per account) | 0.81 |
| SQS (`app.yaml`) | Lambda long-polls 3 queues **even when idle** (~1.9 M requests a month; first 1 M free) | 0.40 |
| DynamoDB (`data.yaml`) | ~50 write + ~35 read units per document, ~0.2 GB stored, PITR; stream reads by Lambda are free | 0.27 |
| Code bucket (`data.yaml`) | Lambda zip packages, ~48 MB each, expire after 90 days | 0.05 |
| S3 documents bucket (`data.yaml`) | PUT/GET requests; files are deleted after processing, so storage is near zero | 0.10 |
| API Gateway REST (`app.yaml`) | ~16,500 requests at USD 3.50 per million | 0.06 |
| CloudTrail (`security.yaml`) | Management events: the first trail is free; ~1 GB of log files in S3 | 0.05 |
| Lambda (`app.yaml`) | ~330,000 GB-seconds (worker ~20 s at 2 GB per document, API wait ~20 s at 1 GB) | 4.46 |
| EventBridge, Scheduler, SNS, Budgets | AWS service events and the first 14 M schedule runs are free; email alerts free; first 2 budgets free | 0.00 |
| Client API keys in Secrets Manager (`--secret-name`, optional) | USD 0.40 per stored key (e.g. `slm/prod/api-keys/lynear`) | 0.40 each |
| **Infrastructure total (gross)** | | **USD 19.99** |
| Lambda always-free tier (400,000 GB-s and 1 M requests a month) | | −4.46 |
| **Infrastructure total (net)** | | **USD 15.53** |

Notes:

- **KMS key rotation** is on (yearly). AWS adds USD 1 per key per month for each of the first two rotations, so the KMS line becomes ~USD 6 in year 2 and ~USD 9 from year 3.
- **Not in the templates, so not in the total:** custom CloudWatch metrics for the cost alarms in §9 (about USD 0.30 each when added), a custom domain (Route 53 USD 0.50 per hosted zone), and the security add-ons above.
- The calculation script is simple arithmetic on the numbers in this table; re-run it with phase-0 measurements (seconds per document, objects per job).

### 4.3 Per-unit cost

| Unit | Model only | All-in |
|---|---|---|
| Per page | USD 0.0016 | USD 0.0025 |
| Per 1,000 pages | USD 1.61 | USD 2.55 |
| Per 10-page bank statement (Rung 0 path) | USD 0.0025 | ~USD 0.012 |
| Per 10-page bank statement (AI path, no escalation) | USD 0.0095 | ~USD 0.019 |

## 5. Sensitivity

### 5.1 Pessimistic case

Assumptions:

- Only 40% of statements are digital, so Rung 0 handles less.
- Second tries rise to 25% and Sonnet escalations to 15%.
- Output is 30% longer.
- The verifier disagrees on 25% of Rung 0 pages.

Model cost rises to **USD 101**, and the total to **USD 121**. That is still within USD 150.

### 5.2 How each assumption moves the monthly total (base case)

| Change | Monthly effect |
|---|---|
| Sonnet escalation rate +5 points (5% → 10%) | **+ USD 15** |
| No Rung 0 at all (everything through the AI ladder) | **+ USD 19** |
| Parsers for fewer banks (80% → 50% of digital statements) | + USD 7 |
| Verifier disagreement 10% → 30% | + USD 5 |
| Output tokens +30% | + USD 4 |
| Second-try rate +10 points | + USD 2 |
| Volume doubles to 44,000 pages | + USD 40 (model 36, GuardDuty 2, Lambda 2 once past the free tier) |

### 5.3 Starting the AI ladder on Nova Lite

Pages that are not finished by Rung 0 start on Haiku 5.5 in the base case. Starting them on Nova Lite instead (architecture §3.2) changes the model cost as follows:

| Share of pages Nova Lite fails | Model cost | Compared with base (USD 36) |
|---|---|---|
| 15% | ~USD 31 | −5 |
| 30% | ~USD 34 | −2 |
| 50% | ~USD 39 | +3 |

Decide per document type from phase-0 measurements.

## 6. Ways to cut cost, best first

| Lever | Saving | Trade-off |
|---|---|---|
| Lower the Sonnet escalation rate (better prompts, few-shot examples, escalate only on errors that matter) | Up to ~USD 12 | Evaluation work in phase 3 |
| Add Rung 0 parsers for more banks, as sample data shows which banks are common | ~USD 2 per 10 points of coverage | ~1–2 days per bank layout, plus upkeep |
| Drop AWS WAF in prod too (`EnableWaf=false`); rely on API Gateway throttling | USD 8 | Less protection against abusive traffic (dev already runs without it) |
| `checks_only` verification for tenants that accept it (no AI call for Rung 0 pages) | ~USD 2 | Header fields such as names are no longer AI-checked |
| One KMS key instead of three | USD 2 (USD 6 from year 3, with rotation charges) | Coarser key separation |

None is needed to meet NFR-K1.

## 7. Comparison with other approaches (internal)

| Approach (22,000 pages, statement-heavy mix) | Monthly |
|---|---|
| **This design: Rung 0 parsers + AI verification + model ladder** | **~USD 56** |
| Same design without Rung 0 (AI ladder for every page) | ~USD 74 |
| Sonnet 5.5 for every page | ~USD 400 |
| Previous self-hosted design (GPU, business hours only) | ~USD 180 |
| Bedrock Data Automation, custom blueprints | ~USD 925 |

## 8. Costs not in the monthly figure

| Item | Estimate |
|---|---|
| Dev/test AWS environment (separate account, WAF off) | ~USD 6 a month (§1) |
| Accuracy evaluation runs (~250 labelled documents per run) | ~USD 2 per run on Haiku; ~USD 11 with Sonnet comparisons |
| AWS Support plan (optional; Business starts at USD 100 a month) | Not included |
| Rung 0 parser development and upkeep | Engineering time, not AWS cost |
| Taxes | Not included |

## 9. Budget controls (NFR-K3)

- **AWS Budgets:** USD 150 for the production account, with alerts at 80% and 100%, plus a forecast alert.
- **Same-day cost per tenant:** tokens and estimated cost are recorded per job on `USAGE#<date>`.
- **Alarms:**
  - Sonnet escalation rate above 15%;
  - Rung 0 verifier disagreement above 20% for any bank layout (usually means the bank changed its format);
  - daily cost above 1.5× the 7-day average.

## 10. To confirm in phase 0

1. Bedrock prices for Nova Lite, Haiku 5.5 and Sonnet 5.5 on `us.` profiles.
2. The real document mix, and which banks appear most in the samples.
3. Share of statements that are digital PDFs.
4. Tokens per page, especially statement output tokens and Nova Lite image tokens.
5. Verifier disagreement rate on Rung 0, plus second-try and escalation rates on the AI ladder.
6. Recalculate this document with the measured values before the pilot.
