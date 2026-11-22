# Pitch prep: ethics (25) and financial feasibility (25)

Half the finals points come from these two judges. Every number below is either **published** (with its source), **measured** (from our cost ledger), or **our assumption** (labelled). Never blur the three on stage.

## Financial feasibility

### Who pays, and for what
| Question | Answer |
|---|---|
| Buyer | The 3PL's **Head of Sales / Business Development**. Budget line: lead generation (today an SDR agency retainer or in-house SDR salaries). |
| What we sell | One **qualified, held** first meeting with the verified buyer at a shipper with a sourced trigger from the last 30 days, near a lane where the 3PL has spare trucks. |
| Billable meeting | Prospect matches the client's target profile **and** the buyer's role and employer are verified **and** the buyer accepted **and** the meeting took place. A booked-but-unattended invite is not billed. |
| Our price | **₹10,000 per qualified, held meeting** *(our assumption, not a vendor quote; inside every published range below; willingness to pay still needs customer validation)* |
| Optional plan | Monthly per watched route *(stretch; not priced yet)* |

### What firms pay today (published benchmarks, checked 22 Nov 2026)
Agency-published prices and vendor guides, not audited market averages. Say "published
benchmarks" on stage. Only figures we found on the cited page are listed; USD figures are
converted at our config rate of ₹84/$ (conversion is ours).

| Source | Per qualified meeting | Monthly retainer | Nature |
|---|---|---|---|
| [MarginSales, "How much does outsourced cold calling cost?" (Aug 2026)](https://marginsales.com/blog/outsourced-cold-calling-cost) | $50–$200 per qualified appointment (≈ ₹4,200–₹16,800) | $2,000–$6,000 (page states ₹80,000–₹3,00,000) | Agency's own pricing guide |
| [MarginSales, "Sales outreach agency pricing" (Aug 2026)](https://marginsales.com/blog/sales-outreach-agency-pricing) | $250–$1,000 per qualified meeting on pay-per-meeting deals (≈ ₹21,000–₹84,000) | tiers ₹80k / ₹1.5L / ₹3L | Agency's own pricing guide |
| [GrowthSparx, "Cost per lead in India 2026" (Oct 2026)](https://growthsparx.com/blog/india-lead-generation-benchmarks-2026.html) | No India pay-per-meeting price list found (their finding); cites Leadium: $50–$300 SMB, $300–$600 mid-market, $600–$1,500+ C-suite | n/a | Benchmark roundup, second-hand for meeting prices |

In-house alternative: junior SDR ₹4.5–₹7 lakh/year on-target earnings, i.e. ₹37,500–₹58,300/month before tools and management ([Wisemonk India SDR hiring guide, updated Aug 2026](https://www.wisemonk.io/blogs/sales-development-representatives-hiring-guide-india); an employer-of-record vendor, so a marketing source).

Our ₹10,000 per held meeting (≈ $119) sits inside MarginSales' $50–$200 per qualified appointment and Leadium's SMB range, and well under MarginSales' $250–$1,000 pay-per-meeting deals.

Removed after checking (do not quote on stage): MarginSales "₹8,000–₹40,000 per meeting" (not on the page),
GrowthSparx "₹10,000–₹25,000 per held meeting" and "₹1.3–₹1.8 lakh/month setter" (not on the page),
DigitalGrowPedia figures (pricing page refused automated reading, HTTP 402: check by hand before quoting),
Nico Digital (no page found).

### Our cost per job (measured, from the ledger)
Cost per meeting = **job cost in ₹ (`python -m service.cli cost <job_id>`) ÷ meetings sent**.

| Item | Value | Status |
|---|---|---|
| Crustdata credit prices (our key) | identify 0.2/match; company enrich 2; job/person search 0.03 per result; person enrich 1; web search/fetch 1 per page | from `GET /account/endpoints`, 22 Nov 2026 |
| ₹ per Crustdata credit | **₹25.20** (1 credit = $0.30 × ₹84/$) | list price, Crustdata billing page (pay-as-you-go), 22 Nov 2026: search $9 / 1k records = 0.03 credits each; person enrich from $0.30 = 1 credit; company enrich from $0.60 = 2 credits. Event credits are sponsored; we quote list price, not ₹0 |
| LLM cost | logged per call by `llm.ask` | measured |
| **Cost per job (live, measured)** | **₹1,179** (46.77 credits reported by Crustdata) for one Pune→Mumbai job: 8 candidates from fresh job posts → 4 drafts awaiting approval. Our ledger books ₹1,380 (54.77 credits) because calls that time out are booked at the estimate | measured 22 Nov 2026, jobs `live-pune-mumbai-2026-11-22-r4` + `-r6` (`svc cost`) |
| **Cost per draft** | **₹295** (Crustdata-reported) / ₹345 (our ledger) | measured; LLM cost ₹0 (template drafts, no LLM call) |
| **Cost per held meeting** | cost per draft ÷ share of drafts that become held meetings. At 1 in 5 (**our assumption**, not measured): ≈ ₹1,475–₹1,725 | assumption until real replies come in |

Margin story: price ₹10,000 per held meeting − ≈ ₹1,475–₹1,725 data cost per held meeting (measured cost per draft × our assumed 1-in-5 conversion) ≈ **₹8,275–₹8,525 gross margin per meeting**, before approver time and hosting. Say "measured cost per draft, assumed conversion" on stage. The agency charges for an SDR's time; we charge for a meeting that happened, so the client pays nothing for held or cannot-verify prospects.

### Why a 3PL would switch
- **Pay per held meeting**, not a retainer: no meeting, no fee.
- **Fresher**: triggers from the last 30 days with dates, not a stale list.
- **Auditable**: every sentence in every email links to its source; the client sees why each prospect was chosen, held or dropped.
- **Safe for the brand**: no lookalike company, departed buyer, current client or do-not-contact name is emailed (each is an eval case that must pass before every push).

## Ethics

| Question | Answer | Where it lives |
|---|---|---|
| What personal data do we touch? | Work data only: name, title, employer, role start/end dates, **business** email | `privacy.person_fields` in config |
| Minimisation: what we never fetch or store | No personal emails, phone numbers, home addresses or personal posts. We request only `contact.business_emails` from Crustdata person enrichment, and keep an email only if Crustdata ties it to the buyer's verified employer id | CLAUDE.md rules; `sources/live_buyers.py` (`EMAIL_FIELDS`); s07 |
| Matching the right person | Never match a person on name alone: the buyer's employer id must equal the company's id, and the current role is rechecked before drafting. A same-name person at another company is rejected | s07 buyer step; eval "Vidarbha Steel" |
| Lookalike companies | Two close identity matches, or a domain mismatch → **hold**, never email | s03; evals "Shree Polymers", "Tungabhadra Paper" |
| What happens when we're unsure | "Cannot verify", naming what record is missing, and the prospect goes to a person, never a guess. Undated news, sources that disagree, and failed sources all end here | s04; evals "Indus Chem", "Godavari Foods" |
| Which actions need human approval, and why | Every email and calendar invite (`approval.irreversible`). Both reach a real person and can't be unsent | ledger approval queue + outbox |
| How a decision is explained | The approver sees the email, each claim with its source link, exact excerpt and date, the route-fit reason, the buyer evidence and the cost so far | approval page (C); `pipeline.summarize` |
| Opt-out and do-not-contact | Every email ends with an unsubscribe line. The do-not-contact list is checked at step 6 and again right before sending | config `email.template`; s06 + s10 |
| No double contact | A person or company that another job already emailed, or has queued for approval, in the last 90 days is excluded (`approval.recontact_days`, `recontact_scope`); re-checked right before sending, and kept as a hash after the 30-day purge | s09 + s10, `Ledger.prior_contacts`; `tests/test_recontact.py` |
| Retention | Person data is deleted after `privacy.retention_days` (30): step checkpoints, result files, sent `.eml`/`.ics`, raw API cache, and the draft inside each outbox row. The outbox key and status stay, so an old email can never be sent twice; an approval still pending after 30 days expires and is never sent. Runs automatically before every job, or `svc purge [--dry-run]` | `Ledger.purge_person_data`, `cli.py`; `tests/test_privacy.py` |
| Logs | Emails and phone-like numbers are masked in every log line and log payload (`anita.rao@x.in` → `a***@x.in`) when `privacy.mask_in_logs` is on (default). Names, titles and employers stay: they are the work data the approver needs | `privacy.py`, `Ledger.log`; `tests/test_privacy.py` |
| Relevant law | India's Digital Personal Data Protection Act 2023: we process only professional data, for a stated purpose (a business meeting request), with an opt-out in every message and a retention limit | |
| What we deliberately don't do | No auto-send, no scraping personal profiles or posts, no inferred facts about a person, no ML "propensity" scores presented as facts (route fit is labelled a rule-based estimate) | PLAN.md "Cut" list |
