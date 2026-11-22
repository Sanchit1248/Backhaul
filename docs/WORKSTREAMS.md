# Workstreams: 4 builders, 4 Claude Code sessions, no collisions

The service is ten steps over shared models. **The shared contract is
`src/service/models.py` (data shapes) + `src/service/sources/__init__.py`
(DataSources interface) + the step signature in `src/service/steps/__init__.py`.**
Everything else has exactly one owner. Fixture sources let B, C and D build and
test with no Crustdata credits while A builds the live data layer.

## Who owns what

| | A: Data | B: Pipeline & outbox | C: Email & approval | D: Rules, evals & story |
|---|---|---|---|---|
| **Files** | `sources/live.py`, `sources/crustdata.py`, `sources/base.py`, `steps/s02_discover.py`, `steps/s03_identity.py`, `steps/s07_buyer.py`, `scripts/smoke_test.py` | `pipeline.py`, `ledger.py`, `budget.py`, `config/service.yaml`, `steps/s01_intake.py`, `steps/s05_route_fit.py`, `steps/s06_exclude.py`, `steps/s10_send.py`, `cli.py` | `mailer.py`, `steps/s08_draft.py`, `steps/s09_approve.py`, `src/service/web/` (approval page), `llm.py` | `steps/s04_triggers.py`, `evals/`, `tests/fixtures/`, `README.md`, `docs/` |
| **Hour 0–1** | Smoke test against kickoff creds: which endpoints work, cost per call, coverage of mid-size Indian firms | Map kickoff request + client records into `s01_intake` | Approval page skeleton reading `ledger.pending_approvals()` | Write 5+ eval cases from kickoff data; fill README sections |
| **Hour 1–3 (thin slice, job posts only)** | `LiveSources.discover/identify/triggers(hiring)/people/verify_employment` | Live-mode end-to-end run; kill test at each step | Approve / edit / reject on the page → ledger; `.ics` check in Gmail/Outlook | Triggers rules incl. "sources disagree"; eval for every hard case |
| **Hour 3** | (requirement change: B leads, D updates evals, A and C keep going) | apply change in config/code | | re-run evals |
| **Hour 3–5** | News (web search + fetch passage) and funding triggers; locations fallback (GST/website) | Route-fit "near" cities, route_filter; budget stop → partial result | LLM polish with no new facts (optional); masking PII in logs | Cost-per-meeting from ledger; ethics + finance answers in `docs/PITCH.md` |
| **Hour 5–7** | Crustdata usage vs our cost ledger | Kill drills, restart demo script | Demo polish of approval page | Architecture diagram, demo rehearsal, live-fix drill |

Shared files (`models.py`, `sources/__init__.py`, `steps/__init__.py`): change only after
saying so in team chat, in a small PR on its own, merged first.

## How to run 4 Claude sessions in parallel
1. Each person: their own clone, their own branch `<initial>/<thing>`, Claude Code opened in the repo root.
2. Paste your starter prompt below as the first message. It tells Claude which files it owns.
3. Merge to `main` every 45–60 min: `git pull --rebase origin main`, then `pytest` and
   `python evals/run_evals.py` (must show no WRONG), then push and open a quick PR (or push to main if the team agrees).
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

### D: Rules, evals & story
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
