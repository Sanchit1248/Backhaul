# LaneLead: plan → code

The team work split (A signals, B routes and map, C chat/approval/memory, D platform)
mapped onto this repo. Folders follow the plan: `signals/`, `routes/`, `chat/`, `core/`,
`web/`. The ten-step pipeline stays the engine; the new layers wrap it.

## Interfaces (all typed, all tested)

| Interface | Code | Returns |
|---|---|---|
| `find_targets(routes, window, count)` | `signals/targets.py` (task `find_targets`) | targets: company, triggers, route fit, buyer, recipient, status, evidence IDs |
| `check_churn(clients, window)` | `signals/churn.py` (task `check_churn`) | clients: risk level, signals, evidence IDs |
| `route_fit(company_sites)` | `routes/fit.py` | matched lanes, score, reason, or partial |
| Route adapter (`list_routes`, `capacity`, `loads`, `list_clients`, `client_volume`) | `routes/adapter.py`, `files.py`, `fleetbase.py` | lane data with sync time |
| `save_fact()`, `get_fact()` | `core/evidence.py` | evidence ID; fact with source, field, record date |
| Account memory (`last_contact()`, `record_action()`) | `core/memory.py` | contact history and outcomes |
| Task runner (`run_task(type, params)`) | `core/tasks.py` | task ID; result; resumes after a kill |
| Outbox (`queue_action()`, `approve()`) | `core/outbox.py` over `ledger.py` | action status; sends once (step 10) |
| Config | `config/service.yaml` (`routes`, `memory`, `churn`, `chat`, `tenant_id`) | all rules and limits |

Shared result rule: every target/client carries `kept | held | cannot_verify` + reason +
evidence IDs (`core/results.py`).

## Pages (`python -m service.web`, one login)
- `/`: approval queue. Approve/reject is written to account memory.
- `/chat`: question → parsed task shown → Run (paid search) → answer. Each fact has
  its evidence and sources. Follow-ups work ("draft an email to the second one").
- `/map`: lanes coloured by free capacity, target and client pins, last sync, sync button.
- `/memory`: what LaneLead holds (account memory, conversations), with delete buttons.
- `POST /api/request`: request API (body = the request JSON, returns the configured
  output; `?wait=false` → task id, poll `GET /api/tasks/{id}`). Auth is the login
  session or `Authorization: Bearer $LANELEAD_API_TOKEN`.

## Status against the plan

| Area | Done here | Not done |
|---|---|---|
| A signals | Funding/job/news triggers, identity + lookalike hold, buyer + role-end check, dedupe, hard cases (pipeline); `find_targets`; `check_churn` from order volume; memory hold before queueing | LLM decision layer with three bands; new-leader trigger; labelled decision set; Crustdata churn signals (layoffs, leadership change) |
| B routes | Adapter interface; file adapter (JSON + CSV); Fleetbase adapter; capacity calculator; "capacity unknown"; `route_fit`; map; sync job; seed script; field doc (`FLEETBASE.md`) | Seed script not yet run against a live Fleetbase key; daily scans/alerts/digest |
| C chat | Approval screen; draft writer; chat parser (rules + Claude tool calling fallback) with 20 test questions; answer writer that checks evidence IDs; conversation memory; "held: contacted on …"; churn questions; memory page | Approver reason codes; team notes / search by meaning; scheduling jobs from chat |
| D platform | Ledger (SQLite), evidence store, account memory, task runner, outbox, request API, cost ledger + budget, kill tests, evals, tenant id on new tables | Freshness limits per fact type; scheduler interface beyond the sync loop |

Decision kept from the existing rules: a rejected draft does not block the company in a
later job (`tests/test_recontact.py`). The plan's "hold rejected companies" is available
as `memory.rejected_hold_days` (0 = off).
