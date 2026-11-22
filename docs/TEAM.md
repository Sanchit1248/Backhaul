# Team plan

## Roles
See [WORKSTREAMS.md](WORKSTREAMS.md): A data, B pipeline & outbox, C email & approval, D rules, evals & story. One owner per file; each person runs their own Claude Code session with the starter prompt there.

## Git workflow (7-hour build: keep it light)
- `main` must always run. Before every push: `pytest` + `python evals/run_evals.py`.
- Short branches: `feat/<name>-<thing>`, merge within ~1 hour. Rebase on `main` before merging.
- Commit small and often; **repo history is judged** ("engineering: ... repository history").
- Commit messages: `area: what changed` (e.g. `sources: add MCA registry lookup`).
- Never commit `.env`, keys, or `data/` (both in `.gitignore`).

## Kickoff-day runbook
1. Clone the organizer repo. Copy this repo's contents into it, except `.git/`. Commit: `chore: import team starter`.
2. Read the client name, records and request formats from the kickoff kit. B maps them in `steps/s01_intake.py`; point `client.records_file` in config at them.
3. Track is decided (Revenue: outbound SDR, see [PLAN.md](PLAN.md)). Check the kickoff request type matches our discovery flow; if it is inbound-lead routing, regroup for 15 minutes.
4. D writes 5+ eval cases from kickoff data (lookalike, old trigger, departed buyer, DNC, undated news).
5. Build the pipeline end to end with one live source first, then widen.

## Timeline (7 h build)
| Hour | Goal |
|---|---|
| 0–0.5 | Kickoff kit read, track chosen, starter imported, eval cases drafted |
| 0.5–2.5 | Thin end-to-end: one request → sourced result with one live source; approval + ledger wired |
| **3** | **Requirement change announced → change config/output, re-run evals, commit** |
| 3–5 | More sources, hard cases (duplicates, lookalikes, cannot verify), cost tuning |
| 5–6 | Kill test drills, logs, README + diagram final, cost per job measured |
| 6–7 | Freeze prep: demo script, dry run of screening, **code freeze** |

## Screening & finals drills
- [ ] Kill mid-run (`Ctrl+C` and also closing the terminal), restart: no duplicate actions
- [ ] Unseen request run cold by the person presenting
- [ ] Live fix rehearsal: practise a 5-minute change (new output field, new rule in config)
- [ ] 3-minute demo script including the kill
- [ ] Answers ready: 100× volume, second client, personal data handling, price in ₹
