# Workstreams: 3 builders, 3 Claude Code sessions, no collisions

**2026-11-22: D has left the team.** D's files and remaining tasks are split between
B and C below; A keeps only the live data layer (critical path). D's finished work
(sources-disagree rule, source-failure handling, 20 eval cases, pitch draft, README
sections) stays as is.

The service is ten steps over shared models. **The shared contract is
`src/service/models.py` (data shapes) + `src/service/sources/__init__.py`
(DataSources interface) + the step signature in `src/service/steps/__init__.py`.**
Everything else has exactly one owner. Fixture sources let B and C build and
test with no Crustdata credits while A builds the live data layer.

## Who owns what

| | A: Data | B: Pipeline, outbox & story | C: Email, approval, rules & evals |
|---|---|---|---|
| **Files** | `sources/live.py`, `sources/live_*.py`, `sources/crustdata.py`, `sources/base.py`, `steps/s02_discover.py`, `steps/s03_identity.py`, `steps/s07_buyer.py`, `scripts/smoke_test.py` | `pipeline.py`, `ledger.py`, `budget.py`, `faults.py`, `config/service.yaml`, `steps/s01_intake.py`, `steps/s05_route_fit.py`, `steps/s06_exclude.py`, `steps/s10_send.py`, `cli.py`, `scripts/demo.ps1`, `README.md`, `docs/` (from D) | `mailer.py`, `steps/s08_draft.py`, `steps/s09_approve.py`, `src/service/web/` (approval page), `llm.py`, `steps/s04_triggers.py`, `evals/`, `tests/fixtures/` (from D) |
| **Done** | Company discovery/identity/locations, buyers, signals, Crustdata client (`live_*.py`, merged) | Kill tests, budget stop, route fit, step failure -> partial, `svc --mode/outbox`, demo script | Approval web app, Outlook/Google-safe `.ics`; (from D) sources-disagree rule, 20 eval cases |
| **Now (blocking)** | Wire `LiveSources` in `sources/live.py` to the `live_*.py` mappers so a live run gets past step 2; set the Crustdata credit price (Rs/credit) with B | Map kickoff request + client records into `s01_intake` when the kit is out; live end-to-end run once A's wiring lands | Eval case "budget almost used up" -> `stopped_limit` with partial result; 5+ eval cases from kickoff data when the kit is out |
| **Hour 3** | keep going | requirement change: lead, apply in config/code | requirement change: update + re-run evals |
| **Hour 3–5** | News/funding triggers live; locations fallback (GST/website); live eval cases (records after 1 Sep 2026) | `docs/PITCH.md` TODOs: agency source URLs, Rs per credit, cost per job/meeting from `svc cost` after a live run | LLM polish with no new facts (optional); masking PII in logs; confirm retention deletion + log masking for `docs/PITCH.md` (tell B) |
| **Hour 5–7** | Crustdata usage vs our cost ledger (with B) | Kill drills, demo rehearsal, live-fix drill, architecture diagram, README final | Demo polish of approval page |

Shared files (`models.py`, `sources/__init__.py`, `steps/__init__.py`): change only after
saying so in team chat, in a small PR on its own, merged first.

## How to run 3 Claude sessions in parallel (live build)
1. Each person: their own clone, **on `main`**, Claude Code opened in the repo root.
   Start a new Claude session after pulling this, so the sync hook loads.
2. Paste your starter prompt below as the first message. It tells Claude which files it owns.
3. **Pulling is automatic**: before every prompt, the hook in `.claude/settings.json` merges
   origin/main into your clone (uncommitted work is stashed and re-applied; on a conflict
   it aborts and says which file). Optional, for when you're not prompting:
   `python scripts/sync.py --watch 60` in a spare terminal.
4. **Pushing is per change**: your Claude commits your own files and pushes to `main` as soon
   as tests + evals pass (rules in CLAUDE.md). Small pushes every few minutes = no big conflicts.
4. Start a fresh Claude session for each new task rather than one giant conversation; it
   burns less of your Pro usage and keeps context sharp. Use the strongest model for
   rule/matching logic, a faster one for plumbing.
5. Before merging anything big: `/code-review` on your branch.

## Starter prompts (paste into Claude Code)

### A: Data
```
Read CLAUDE.md, docs/WORKSTREAMS.md, src/service/models.py and src/service/sources/__init__.py.
I own: src/service/sources/{live,crustdata,base}.py, steps/s02_discover.py, s03_identity.py,
s07_buyer.py, scripts/smoke_test.py. Do not edit other files; if the shared models need a
change, stop and tell me.
Task: implement LiveSources against the Crustdata API (docs.crustdata.com, key in .env).
Start with scripts/smoke_test.py that calls each endpoint we need once (company identify,
company enrichment, job search, person search/enrichment, web search+fetch) for 3 real
Indian mid-size companies, saves raw responses to tests/fixtures/live_samples/, and prints
cost/credits per call and which fields exist (locations? role start dates? post dates?).
Every HTTP call goes through sources/base.fetch_json. Every returned value carries a
models.Source with the exact field path or quoted passage and the record date.
Then make LiveSources.discover/identify/triggers(hiring first)/people/verify_employment
pass a test that runs on the saved samples.
```

### B: Pipeline & outbox
```
Read CLAUDE.md, docs/WORKSTREAMS.md, src/service/models.py and src/service/pipeline.py.
I own: pipeline.py, ledger.py, budget.py, cli.py, config/service.yaml, steps/s01_intake.py,
s05_route_fit.py, s06_exclude.py, s10_send.py. Do not edit other files.
Task 1: adapt s01_intake.parse_request and load_client_records to the kickoff-kit formats
(I'll paste them). Keep JobRequest/ClientRecords unchanged; map into them.
Task 2: write tests/test_kill.py that runs the pipeline in a subprocess, kills it (taskkill /F)
at each step and during send, restarts, and asserts each email/invite exists exactly once
and finished steps were resumed, not redone.
Task 3: route fit: honour job.route_filter; add a small "nearby cities" table in config
(e.g. Chakan, Talegaon -> Pune) giving partial fit with the reason.
Run pytest and python evals/run_evals.py before telling me it's done.
```

### C: Email & approval
```
Read CLAUDE.md, docs/WORKSTREAMS.md, src/service/models.py, steps/s08_draft.py,
steps/s09_approve.py, mailer.py and the outbox methods in ledger.py (request_approval,
get_action, update_payload, decide, pending_approvals).
I own: mailer.py, steps/s08_draft.py, steps/s09_approve.py, src/service/web/, llm.py.
Do not edit other files.
Task: build a small approval web app in src/service/web/ (FastAPI + plain HTML, add deps to
pyproject in a separate tiny commit and tell the team). One page lists pending actions; each
shows the email, every claim with its source link/excerpt/date, the route-fit reason, the
buyer evidence, and the cost so far, with Approve / Edit / Reject buttons that call
ledger.decide / update_payload with the approver's name. Simple password login from .env.
Never send from the web app: sending stays in step 10.
Then check the generated .ics opens correctly in Google Calendar and Outlook.
```

### D: Rules, evals & story (D has left: C owns these files now)
C: add to your starter prompt -> "I also own: steps/s04_triggers.py, evals/, tests/fixtures/."
B: add to your starter prompt -> "I also own: README.md, docs/, scripts/demo.ps1."
Original D prompt kept for reference:
```
Read CLAUDE.md, docs/WORKSTREAMS.md, docs/CHECKLIST.md, src/service/models.py,
steps/s04_triggers.py, evals/ and tests/fixtures/.
I own: steps/s04_triggers.py, evals/, tests/fixtures/, README.md, docs/. Do not edit other files.
Task 1: add the "sources disagree" rule to s04_triggers (same trigger, conflicting facts ->
CannotVerify showing both sources) with a fixture + eval case.
Task 2: add eval cases for every hard case in the plan, including budget almost used up
(expect status stopped_limit with a partial result) and source failure (expect
"cannot verify: source unavailable").
Task 3: fill README's required sections from docs/PLAN.md, and draft docs/PITCH.md
(buyer, price per meeting in rupees, cost-per-meeting formula fed by `svc cost`).
Run pytest and python evals/run_evals.py before telling me it's done.
```
