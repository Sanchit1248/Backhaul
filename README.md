# ContextCon 05: Services, Replaced. Outbound SDR for a 3PL

> **Team:** _name_ · **Track:** Revenue · **Builders:** _4 names_

The service books a first meeting between a 3PL's sales head and the right buyer at a company that has shown a real expansion trigger in the last 30 days and ships near a route where the 3PL has spare truck capacity. It does the whole sales development job, from the request to the sent invite. A person approves every email and invite before it goes out.

This is our prep repo. **At kickoff, all build commits move to the organizer repo** ([docs/TEAM.md](docs/TEAM.md#kickoff-day-runbook)). Who builds what: [docs/WORKSTREAMS.md](docs/WORKSTREAMS.md). Full plan: [docs/PLAN.md](docs/PLAN.md).

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
| Crustdata company identify / enrichment | identity, locations, funding | yes | _credits/call (smoke test)_ |
| Crustdata job search | hiring triggers (posting date, location) | yes | _credits_ |
| Crustdata web search + fetch | expansion news, exact passage | yes | _credits_ |
| Crustdata person search / enrichment | buyer, current employer, role start | yes | _credits_ |
| MCA / GST registry pages | legal name, principal place of business (fallback) | yes | free |
| Company websites (careers, locations, press) | fallback triggers and locations | yes | free |
| Client records (kickoff kit) | routes, capacity, clients, do-not-contact list | n/a | n/a |
| Claude `claude-opus-5-5` / `claude-haiku-5-5` | request parsing fallback, optional email polish; **never a source of facts** | n/a | per token |

## Architecture
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the ten steps, step log, outbox, approval queue, retries, cost ledger.

## Quickstart
```powershell
.\scripts\setup.ps1                 # venv + deps + .env + tests
python evals\run_evals.py           # hard-case evals on fixtures (must show no WRONG)
python -m service.cli run evals\cases\fixture_hard_cases.json
python -m service.cli pending       # see drafts waiting for approval
python -m service.cli approve <key> --by "Your Name"
python -m service.cli run evals\cases\fixture_hard_cases.json   # sends approved ones, once
python -m service.cli cost <job_id>
```
Set `sources.mode: live` in `config/service.yaml` for real runs (screening and finals).

**Kill test:** run a job, kill it partway through (`Ctrl+C` or Task Manager), then run the same command again. Finished steps load from their checkpoints, and each email and invite is in `data/outbox/` exactly once.

**Kill at an exact point (rehearsal):** set `SERVICE_CRASH_AT` and the process hard-exits there, then run again without it. Points: `after:02_discover` … `after:08_draft`, `before_send`, `after_send`. `tests/test_kill.py` does this for every point.
```powershell
$env:SERVICE_CRASH_AT="after_send"; python -m service.cli run evals\cases\fixture_hard_cases.json
Remove-Item Env:SERVICE_CRASH_AT;   python -m service.cli run evals\cases\fixture_hard_cases.json
```

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
