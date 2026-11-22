# Workstreams: one builder

Since 22 Nov 2026 the build is one person (the other workstreams are gone). There are no
per-file owners: any file may be edited. The shared contract files below still deserve care,
because every step and source builds on them; say so before changing them.

**Shared contract:** `src/service/models.py` (data shapes) + `src/service/sources/__init__.py`
(DataSources interface) + the step signature in `src/service/steps/__init__.py`.

## Module map
| Area | Files | What it does |
|---|---|---|
| Pipeline | `pipeline.py`, `ledger.py`, `budget.py`, `faults.py`, `cli.py` | Ten steps; steps 2–8 checkpointed; step failure → `partial`; budget stop → `stopped_limit`; crash points for kill drills; `svc run/pending/approve/outbox/cost/logs/purge` |
| Config | `config/service.yaml` | Every rule, limit, price, template and output field (hour-3 change and live fix = edits here) |
| Live data | `sources/live.py`, `sources/live_companies.py`, `sources/live_signals.py`, `sources/live_buyers.py`, `sources/crustdata.py`, `sources/base.py` | Crustdata client (credits estimated vs reported, retries, cache); hiring-first discovery; identity; triggers (jobs, funding, news); locations; buyers + business email tied to employer id |
| Steps | `steps/s01_intake.py` … `steps/s10_send.py` | Intake, discover, identity, triggers, route fit (incl. job post on the lane), exclusions, buyer, draft, approval queue, send once |
| Email + approval | `mailer.py`, `steps/s08_draft.py`, `steps/s09_approve.py`, `src/service/web/` | `.eml` + Outlook/Google-safe `.ics`; approval web page (`python -m service.web`) |
| Privacy | `privacy.py`, `Ledger.log`, `Ledger.purge_person_data` | Emails/phones masked in logs; 30-day retention purge that keeps send-once proof |
| LLM | `llm.py` | Optional; cost logged, budget checked; never a source of facts |
| Evals + tests | `evals/`, `tests/`, `tests/fixtures/` | Hard-case evals (no WRONG before any push); unit, kill and live-mapping tests |
| Story | `README.md`, `docs/`, `scripts/demo.ps1` | Pitch (benchmarks checked, measured cost), architecture, finals demo |

## Done (22 Nov 2026)
- Live Pune → Mumbai job end to end: 8 candidates from fresh job posts → 4 drafts awaiting
  approval, ₹1,179 (Crustdata-reported); cost ledger reconciled with reported credits.
- Kill/restart safety, budget stop, step-failure partial result, config-driven output fields.
- Approval web page, `.ics`, finals demo script (passes on fixtures).
- Privacy: log masking + retention purge. Pitch: no TODOs left.

## Left to do
| | Task | Blocked on |
|---|---|---|
| 1 | Map the kickoff kit (request format + client records) into `s01_intake`, point `client.records_file` at the real records, add a real eval case, re-run live | kickoff kit |
| 2 | Hour-3 requirement change: config/code edit, re-run evals | announcement |
| 3 | Pass discovery's local job posts through as triggers, so MNCs found via a Pune/Mumbai post (Eaton, L'Oréal, Flex) keep an on-lane trigger | — |
| 4 | Buyer ranking prefers seniority (Vertiv picked an Asst Mgr over the Head of Operations) | — |
| 5 | Name lookup for job posts without a Crustdata company id (62 of 100), credits permitting | — |
| 6 | Live demo rehearsal with `scripts/demo.ps1` (approves and "sends" one draft to `data/outbox`: ask first) | user's go-ahead |
| 7 | Live-fix drill (new output field or rule in 5 minutes), 3-minute demo script, judge answers | — |
| 8 | Check DigitalGrowPedia's pricing page by hand before quoting it | manual |

## Running Claude Code here
Open Claude Code in the repo root on `main`. The sync hook in `.claude/settings.json` still
pulls origin/main before every prompt (harmless with one builder; it also keeps a second
machine in step). Commit and push per working change (rules in CLAUDE.md).
