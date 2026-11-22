# Team plan

## Roles (4 builders)
| Role | Owns | Files |
|---|---|---|
| **A: Pipeline lead** | request → result logic, decide step, output format, hour-3 change | `pipeline.py`, `config/` |
| **B: Data sources** | Crustdata + registries + web; parsing; Source citations | `sources/` |
| **C: Reliability & evals** | kill/restart, approvals, test cases incl. hard cases, eval scoring | `ledger.py`, `evals/`, `tests/` |
| **D: Product & pitch** | README sections, architecture diagram, ethics + finance story, demo script, screening Q&A | `README.md`, `docs/` |

Anyone can pick up anything, but **one owner per file at a time** to avoid merge pain.

## Git workflow (7-hour build: keep it light)
- `main` must always run. Before every push: `pytest` + `python evals/run_evals.py`.
- Short branches: `feat/<name>-<thing>`, merge within ~1 hour. Rebase on `main` before merging.
- Commit small and often; **repo history is judged** ("engineering: ... repository history").
- Commit messages: `area: what changed` (e.g. `sources: add MCA registry lookup`).
- Never commit `.env`, keys, or `data/` (both in `.gitignore`).

## Kickoff-day runbook
1. Clone the organizer repo. Copy this repo's contents into it, except `.git/`. Commit: `chore: import team starter`.
2. Read the client name, records and request formats from the kickoff kit. Put the records in `data/client/` (gitignored) or `fixtures/` if they're allowed to be committed.
3. Choose the track (see below). Fill in the README's required sections in draft form.
4. Write 3–5 eval cases for that track now, including a duplicate, a lookalike name and a cannot-verify case. **Open track: 3 cases with answers are due by 11:00 AM.**
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
