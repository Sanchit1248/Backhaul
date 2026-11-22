# CLAUDE.md

ContextCon 05 "Services, Replaced", Revenue track: an outbound SDR service for a 3PL that
books a first meeting with the right buyer at a company with a fresh (<=30 days) expansion
trigger near a route with spare truck capacity. Plan: docs/PLAN.md. Ownership: docs/WORKSTREAMS.md.

## Ownership
Edit only the files your workstream owns (docs/WORKSTREAMS.md). Shared contract files
(`models.py`, `sources/__init__.py`, `steps/__init__.py`) change only when the user says
the team agreed. If a task needs a change outside your files, stop and say so.

## Rules for any code change
- Every fact in output is a `models.Fact` with a `models.Source` (URL, exact excerpt/field,
  record date). Never output facts from model memory. If no source settles it, record
  `CannotVerify` and hold, never guess. A wrong meeting costs more than a held one.
- Steps follow the contract in `steps/__init__.py`: act only on active prospects; record
  every decision with hold/drop/exclude/unverifiable + reason.
- Data only via `ctx.sources` (DataSources); HTTP only via `sources/base.fetch_json`; LLM only
  via `llm.ask` (cost logged, budget checked).
- Irreversible actions (email, invite) go through the ledger approval queue + outbox with
  key = hash(job_id, prospect_id, action). Never send without approval.
- People data: name, title, employer, role dates, business email only. Never personal emails,
  phones, personal posts. Never match a person on name alone (employer id must match).
- Tunables (window, weights, titles, limits, template, output fields) live in
  `config/service.yaml`, not code.
- Fixture data is for tests only; screening/finals run `sources.mode: live`.
- Before saying a change is done: `pytest` and `python evals/run_evals.py` (no WRONG).
