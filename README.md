# ContextCon 05: Services, Replaced. Outbound SDR for a 3PL

> **Team:** _name_ · **Track:** Revenue · **Builders:** _4 names_

The service books a first meeting between a 3PL's sales head and the right buyer at a company that has shown a real expansion trigger in the last 30 days and ships near a route where the 3PL has spare truck capacity. It does the whole sales development job, from the request to the sent invite. A person approves every email and invite before it goes out.

This is our prep repo. **At kickoff, all build commits move to the organizer repo** ([docs/TEAM.md](docs/TEAM.md#kickoff-day-runbook)). Module map and remaining work: [docs/WORKSTREAMS.md](docs/WORKSTREAMS.md). Full plan: [docs/PLAN.md](docs/PLAN.md).

## The job, and who pays for it today
Outbound sales development for a 3PL: find, qualify and book meetings with new shippers. Today 3PLs pay lead-generation and SDR agencies (per meeting or a monthly retainer), or the salaries of an in-house SDR team.

## Input
A request such as `"Book 5 meetings for the Pune to Mumbai route; spare capacity 12 trucks a week"`, plus the client's records: routes, spare capacity, current clients, rivals, do-not-contact list. Example: [evals/cases/fixture_hard_cases.json](evals/cases/fixture_hard_cases.json). _(Update to the kickoff-kit format.)_

## Finished result
For each prospect: the trigger with its source and date, the route-fit reason, the buyer with their source, the approved email, and the sent invite (`.ics`). Otherwise a hold reason (lookalike, no verified buyer, ...) or **"cannot verify"** naming what is missing. Every fact links to the exact field or passage and the record's date.

## Pass mark
Each fact has a source. No trigger older than 30 days. No lookalike company or departed buyer is contacted. No action without approval. No duplicate email or invite after a restart. Each job stays under the cost limit (`config/service.yaml → limits.max_cost_inr`).

## Data sources the service calls
| Source | Used for | Live? | Cost |
|---|---|---|---|
| Crustdata company identify / enrichment | identity, locations, funding, news | yes | identify 0.2 credits per match (unmatched free); enrich 2 credits per company |
| Crustdata job search | hiring triggers (posting date, location) | yes | 0.03 credits per result |
| Crustdata web search + fetch | expansion news, exact passage | yes | 1 credit per page |
| Crustdata person search / enrichment | buyer, current employer, role start, business email (tied to employer id) | yes | search 0.03 per result; enrich 1 per person, +1 with business email |

Our key's limits (from `GET /account/endpoints`, 22 Nov 2026): 5 requests/min on identify, enrich and search (batch up to 25 identifiers per call). **Checked in live runs on 22 Nov 2026:** web search + fetch (`/web/search/live`, `/web/enrich/live`) work, and person enrichment returns **business emails** (`contact.business_emails`, +1 credit); we request nothing else from contact data (no personal emails or phones). Batch work-history verification is not enabled. 1 credit = $0.30 list price (₹25.20 at ₹84/$); a live Pune→Mumbai job cost ₹1,179 (Crustdata-reported) for 4 drafts.
| MCA / GST registry pages | legal name, principal place of business (fallback) | yes | free |
| Company websites (careers, locations, press) | fallback triggers and locations | yes | free |
| Client records (kickoff kit) | routes, capacity, clients, do-not-contact list | n/a | n/a |
| Claude `claude-opus-5-5` / `claude-haiku-5-5` | request parsing fallback, optional email polish; **never a source of facts** | n/a | per token |

## Architecture
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the ten steps, step log, outbox, approval queue, retries, cost ledger.

## Quickstart
```powershell
.\scripts\setup.ps1                 # venv + deps (pip install -e ".[dev]") + .env + tests
python evals\run_evals.py           # hard-case evals on fixtures (must show no WRONG)
python -m service.cli run evals\cases\fixture_hard_cases.json --mode fixtures   # rehearsal
python -m service.cli pending       # drafts waiting for approval (--json for key/company)
python -m service.cli approve <key> --by "Your Name"
python -m service.cli run evals\cases\fixture_hard_cases.json --mode fixtures   # sends approved ones, once
python -m service.cli outbox <job_id>   # each outbox row + whether its .eml/.ics exist
python -m service.cli cost <job_id>     # Rs per op, Crustdata credits estimated vs reported
python -m service.cli cost <job_id> --calls   # every paid call with its request id
python -m service.web                   # approval page on http://127.0.0.1:8000 (APPROVER_PASSWORD in .env)
```
`sources.mode` defaults to **live** (screening and finals): a plain `svc run` calls Crustdata
(key in `.env`, ₹25.20 per credit in `costs.crustdata_inr_per_credit`). Tests and evals ask for
fixtures themselves; add `--mode fixtures` to rehearse without spending credits.

**Finals demo:** `.\scripts\demo.ps1` runs a request live, shows pending approvals, approves one,
crashes the process right after sending (`SERVICE_CRASH_AT=after_send`), runs again, checks the
outbox holds exactly one email and one invite, and prints the cost ledger. Rehearse without
credits: `.\scripts\demo.ps1 -Mode fixtures -Request evals\cases\fixture_hard_cases.json`.

**Live fix: new output field.** Add it to `output.prospect_fields` in `config/service.yaml`: a
named field or any dotted path into the prospect (`buyer.role_start`, `triggers.0.event_date`,
`route_fit.score`). Top-level result fields are in `output.result_fields`. A typo fails at job
start, before any step or send.

**When a step fails** (source down, source not wired, bad response): the job does not crash. It
keeps finished work, holds the remaining prospects as cannot verify, ends `partial`, and logs the
error (`svc logs <job_id>`). Budget used up ends `stopped_limit` the same way. Re-running resumes
at the failed step.

**Kill test:** run a job, kill it partway through (`Ctrl+C` or Task Manager), then run the same command again. Finished steps load from their checkpoints, and each email and invite is in `data/outbox/` exactly once.

**Kill at an exact point (rehearsal):** set `SERVICE_CRASH_AT` and the process hard-exits there, then run again without it. Points: `after:02_discover` … `after:08_draft`, `before_send`, `after_send`. `tests/test_kill.py` does this for every point.
```powershell
$env:SERVICE_CRASH_AT="after_send"; python -m service.cli run evals\cases\fixture_hard_cases.json
Remove-Item Env:SERVICE_CRASH_AT;   python -m service.cli run evals\cases\fixture_hard_cases.json
```

## Screening runbook (one person, cold)
Run from the machine that holds `data/` (ledger, logs, cost records, outbox: not in git).

**1. Setup (once)**
```powershell
.\scripts\setup.ps1                      # venv, pip install -e ".[dev]", creates .env, runs tests
```
In `.env`: `CRUSTDATA_API_KEY=...` and `APPROVER_PASSWORD=...` (approval page login).
Check: `python -m pytest -q` and `python evals\run_evals.py` → no WRONG.

**2. Run the unseen request (live is the default)**
Save the request as JSON, e.g. `data\requests\screening.json`:
```json
{"job_id": "screening-1", "text": "Book 5 meetings for the Pune to Mumbai route; spare capacity 12 trucks a week", "request_date": "2026-11-22"}
```
```powershell
python -m service.cli run data\requests\screening.json
```
- Takes ~5–10 minutes: the Crustdata key allows 5 requests/min; the job takes at most
  `discovery.live_max_candidates` (10) companies. Lines starting `[info]`/`[warn]` are progress
  (429 / timeout warnings are retried automatically).
- Cost limit ₹1,500 per job (`limits.max_cost_inr`; a request's `"budget_inr"` overrides). If it
  is hit the job ends `stopped_limit` with a partial result; raising the limit and re-running
  resumes where it stopped.
- The output lists every company with its status and reason: `awaiting_approval`, `held`,
  `dropped`, `excluded`, `cannot_verify`. Full result with every source: the `full_result` path.

**3. Approve (a person, never the service)**
```powershell
python -m service.web                     # http://127.0.0.1:8000, or:
python -m service.cli pending
python -m service.cli approve <key> --by "Your Name"
python -m service.cli run data\requests\screening.json   # sends approved ones, exactly once
python -m service.cli outbox screening-1  # .eml + .ics per sent email (data\outbox)
```

**4. Stop + restart (judges will do this)**
Kill it any time (`Ctrl+C`, close the terminal, Task Manager), then run the **same command** again.
Finished steps load from checkpoints (`step …: resumed from checkpoint`), approved emails are sent
once. Rehearsal with an exact crash point: `.\scripts\demo.ps1` (or `-Mode fixtures`).

**5. Show logs and cost records**
```powershell
python -m service.cli logs screening-1          # step log (emails/phones masked)
python -m service.cli cost screening-1          # ₹ per call type + Crustdata credits estimated vs reported
python -m service.cli cost screening-1 --calls  # every paid call with its request id
```

**If something goes wrong**
| You see | Meaning / do |
|---|---|
| `status: partial` + a note `step … failed` | a source broke; the rest is held as cannot verify. Fix/wait, re-run the same command: it resumes at that step |
| `stopped_limit` | budget used up; raise `limits.max_cost_inr` (or `budget_inr` in the request) and re-run |
| `CRUSTDATA_API_KEY not set` | add it to `.env` |
| `ValueError: config output…` | a typo in `output.*` fields in `config/service.yaml` |
| No drafts at all | read each company's `reason`; "no trigger within 30 days" and "no verified buyer" are correct holds, not errors |

## Repo layout
```
config/service.yaml        every rule and limit (hour-3 change and live fix = edits here)
src/service/
  models.py                SHARED data contract (Prospect, Fact, Source, Trigger, Buyer, ...)
  pipeline.py              runs steps 2-8 checkpointed, 9-10 idempotent
  steps/s01..s10_*.py      one file per step, one owner each
  sources/                 DataSources interface; fixtures.py (tests), live.py (Crustdata)
  ledger.py                SQLite: step log, approval queue + outbox, cost ledger, events
  mailer.py                .eml + .ics rendering, idempotent send
  budget.py, llm.py, cli.py
evals/                     hard-case eval cases + scorer (WRONG is worse than cannot verify)
tests/                     unit + end-to-end tests, fixtures
docs/                      plan, workstreams, architecture, checklist, pitch, problem statement
```
