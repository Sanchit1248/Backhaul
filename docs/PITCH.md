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

### What firms pay today (published market benchmarks, India, 2025–2026)
These are agency-advertised prices and agency estimates, not audited market averages. Say "published benchmarks" on stage.

| Source | Per qualified meeting | Monthly retainer | Nature |
|---|---|---|---|
| MarginSales (Aug 2026) | ₹8,000–₹40,000 | ₹80,000–₹3,00,000 | Agency's Indian-market guidance |
| Nico Digital (Sep 2026) | ₹8,000–₹25,000 | ₹1,00,000–₹2,50,000 (domestic SDR pods) | Agency's market estimates |
| DigitalGrowPedia (2026) | ₹3,000–₹10,000 per **attended** BANT-qualified meeting + ₹15,000 setup | ₹35,000 (part-time SDR) | Advertised offer |
| GrowthSparx (Oct 2026) | ₹10,000–₹25,000 per verified held meeting | ₹1.3–₹1.8 lakh (dedicated setter) | Modelled from staffing + tools, not a survey |

In-house alternative: junior SDR ₹4.5–₹7 lakh/year OTE, i.e. ₹37,500–₹58,300/month (Wisemonk 2026 India SDR guide); about ₹1.3 lakh/month fully loaded with tools and management (GrowthSparx model, Oct 2026).

> TODO(D): add the exact URLs for the four agency sources and Wisemonk before the finals (the research summary we have names them without links).

### Our cost per job (measured, from the ledger)
Cost per meeting = **job cost in ₹ (`python -m service.cli cost <job_id>`) ÷ meetings sent**.

| Item | Value | Status |
|---|---|---|
| Crustdata credit prices (our key) | identify 0.2/match; company enrich 2; job/person search 0.03 per result; person enrich 1; web search/fetch 1 per page | from `GET /account/endpoints`, 22 Nov 2026 |
| ₹ per Crustdata credit | `costs.crustdata_inr_per_credit` in config | **TODO(A): fill in**; event credits are sponsored, so quote the list price, not ₹0 |
| LLM cost | logged per call by `llm.ask` | measured |
| **Cost per job / per meeting** | _fill from `svc cost` after a live run_ | **TODO(D): measure on the live run** |

Margin story (once measured): price ₹10,000 per held meeting − measured cost per meeting = gross margin per meeting. The agency charges for an SDR's time; we charge for a meeting that happened, so the client pays nothing for held or cannot-verify prospects.

### Why a 3PL would switch
- **Pay per held meeting**, not a retainer: no meeting, no fee.
- **Fresher**: triggers from the last 30 days with dates, not a stale list.
- **Auditable**: every sentence in every email links to its source; the client sees why each prospect was chosen, held or dropped.
- **Safe for the brand**: no lookalike company, departed buyer, current client or do-not-contact name is emailed (each is an eval case that must pass before every push).

## Ethics

| Question | Answer | Where it lives |
|---|---|---|
| What personal data do we touch? | Work data only: name, title, employer, role start/end dates, **business** email | `privacy.person_fields` in config |
| Minimisation: what we never fetch or store | No personal emails, phone numbers, home addresses or personal posts. Our Crustdata key cannot even fetch contact data (contact enrich disabled) | CLAUDE.md rules; s07 |
| Matching the right person | Never match a person on name alone: the buyer's employer id must equal the company's id, and the current role is rechecked before drafting. A same-name person at another company is rejected | s07 buyer step; eval "Vidarbha Steel" |
| Lookalike companies | Two close identity matches, or a domain mismatch → **hold**, never email | s03; evals "Shree Polymers", "Tungabhadra Paper" |
| What happens when we're unsure | "Cannot verify", naming what record is missing, and the prospect goes to a person, never a guess. Undated news, sources that disagree, and failed sources all end here | s04; evals "Indus Chem", "Godavari Foods" |
| Which actions need human approval, and why | Every email and calendar invite (`approval.irreversible`). Both reach a real person and can't be unsent | ledger approval queue + outbox |
| How a decision is explained | The approver sees the email, each claim with its source link, exact excerpt and date, the route-fit reason, the buyer evidence and the cost so far | approval page (C); `pipeline.summarize` |
| Opt-out and do-not-contact | Every email ends with an unsubscribe line. The do-not-contact list is checked at step 6 and again right before sending | config `email.template`; s06 + s10 |
| Retention | Person data kept `privacy.retention_days` (30) days | **TODO: confirm deletion is implemented, else say "configured, deletion job pending"** |
| Logs | Personal fields masked in logs (`privacy.mask_in_logs`) | **TODO(C): confirm implemented** |
| Relevant law | India's Digital Personal Data Protection Act 2023: we process only professional data, for a stated purpose (a business meeting request), with an opt-out in every message and a retention limit | |
| What we deliberately don't do | No auto-send, no scraping personal profiles or posts, no inferred facts about a person, no ML "propensity" scores presented as facts (route fit is labelled a rule-based estimate) | PLAN.md "Cut" list |
