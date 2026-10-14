# SLM Fintech Platform: Cost Estimate

| | |
|---|---|
| Version | 0.2 (draft for review) |
| Date | 2026-10-13 |
| Design | [architecture.md](architecture.md) (Bedrock, us-east-1, Rung 0 parsers + model ladder) |
| Target | NFR-K1: about USD 150 per month at 1,000 pages per day |
| Changes since 0.1 | Workload is mostly bank statements (~70% of pages). Digital statements from known banks are parsed by Python code and verified by Nova Lite (architecture §3.3). Security controls are included in the totals. |

## 1. Result

At **1,000 pages per business day** (22,000 pages a month), the platform costs about **USD 80 per month** all-in: about **USD 0.0036 per page**. That is about half the USD 150 target.

| Scenario | Pages / month | Model (Bedrock) | Infrastructure | Security controls | **Total / month** |
|---|---|---|---|---|---|
| **Base: 1,000 pages per business day** | 22,000 | 36 | 34 | 10 | **USD 80** |
| 1,000 pages every calendar day | 30,000 | 47 | 37 | 10 | **USD 94** |
| Pessimistic (§5.1) | 22,000 | 101 | 34 | 10 | **USD 145** |
| Growth: 5,000 pages per business day (NFR-P1) | 110,000 | 160 | 62 | 10 | **USD 232** |

All figures are in USD, exclude taxes and are rounded. Prices are us-east-1 list prices as of 2026-10-13; Bedrock prices must be confirmed (§2.1).

Key points:

- **Rung 0 does the heavy lifting.** About 43% of pages (digital bank statements from known banks, plus common payslips) are read by Python parsers. Each result is still checked by the cheapest AI model, Nova Lite, which costs ~USD 0.00025 per page, against ~USD 0.0026 per statement page through the AI ladder. Without Rung 0, the model line would be ~USD 54 instead of ~USD 36.
- **Sonnet escalations are still the biggest model cost:** ~USD 15 of the USD 36. Keeping the escalation rate low remains the main tuning lever (§6).
- **About USD 40 is a fixed floor** (WAF, monitoring, keys, secrets, security controls). The rest scales with pages.
- **Nothing is billed by the hour.** With no traffic, only the fixed floor is paid.

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
| **Verification by Nova Lite** | page + parser JSON in (~3,575 for a statement page), ~60 out (disagreements only) |

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

### 4.2 Infrastructure and security cost

| Service | What drives it | Monthly |
|---|---|---|
| AWS WAF | Fixed: 1 web ACL + 3 rules | 8.00 |
| CloudWatch | ~20 custom metrics, ~12 alarms, logs kept 180 days, 1 dashboard | 7.60 |
| Secrets Manager | 1 API-key pepper + ~10 tenant webhook secrets | 4.45 |
| Lambda | API wait loop, worker, verifier, authorizer, intake | 3.80 |
| KMS | 3 customer-managed keys | 3.20 |
| AWS Config | Rule keeping Bedrock invocation logging off | 2.00 |
| GuardDuty Malware Protection | Every uploaded file | 2.00 |
| S3, DynamoDB, SQS | Storage, job records, queues | 1.70 |
| CloudTrail, ECR, Route 53 | Data events, worker image, hosted zone | 1.50 |
| API Gateway | ~10,000 requests | 0.03 |
| **Infrastructure total** | | **USD 34** |
| Security controls ([security.md](security.md) §14): extra Config rules, account-level GuardDuty, Inspector, CloudTrail data events, Object Lock archive | | **USD 10** |

### 4.3 Per-unit cost

| Unit | Model only | All-in |
|---|---|---|
| Per page | USD 0.0016 | USD 0.0036 |
| Per 1,000 pages | USD 1.61 | USD 3.61 |
| Per 10-page bank statement (Rung 0 path) | USD 0.0025 | ~USD 0.022 |
| Per 10-page bank statement (AI path, no escalation) | USD 0.0095 | ~USD 0.029 |

## 5. Sensitivity

### 5.1 Pessimistic case

Assumptions:

- Only 40% of statements are digital, so Rung 0 handles less.
- Second tries rise to 25% and Sonnet escalations to 15%.
- Output is 30% longer.
- The verifier disagrees on 25% of Rung 0 pages.

Model cost rises to **USD 101**, and the total to **USD 145**. That is still within USD 150, but with little room left.

### 5.2 How each assumption moves the monthly total (base case)

| Change | Monthly effect |
|---|---|
| Sonnet escalation rate +5 points (5% → 10%) | **+ USD 15** |
| No Rung 0 at all (everything through the AI ladder) | **+ USD 19** |
| Parsers for fewer banks (80% → 50% of digital statements) | + USD 7 |
| Verifier disagreement 10% → 30% | + USD 5 |
| Output tokens +30% | + USD 4 |
| Second-try rate +10 points | + USD 2 |
| Volume doubles to 44,000 pages | + USD 38 (model, Lambda, GuardDuty) |

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
| Drop AWS WAF; rely on API Gateway throttling | USD 8 | Less protection against abusive traffic |
| `checks_only` verification for tenants that accept it (no AI call for Rung 0 pages) | ~USD 2 | Header fields such as names are no longer AI-checked |
| One KMS key instead of three | USD 2 | Coarser key separation |

None is needed to meet NFR-K1.

## 7. Comparison with other approaches (internal)

| Approach (22,000 pages, statement-heavy mix) | Monthly |
|---|---|
| **This design: Rung 0 parsers + AI verification + model ladder** | **~USD 80** |
| Same design without Rung 0 (AI ladder for every page) | ~USD 98 |
| Sonnet 5.5 for every page | ~USD 400 |
| Previous self-hosted design (GPU, business hours only) | ~USD 180 |
| Bedrock Data Automation, custom blueprints | ~USD 925 |

## 8. Costs not in the monthly figure

| Item | Estimate |
|---|---|
| Dev/test AWS environment | ~USD 15–20 a month |
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
