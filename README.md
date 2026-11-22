# ContextCon 05: Services, Replaced

> **Team:** _name_ · **Track:** _TBD at kickoff_ · **Builders:** _4 names_

This is our prep repo. **At kickoff we get an organizer repo, and all build commits must go there.** On day one, copy this repo's contents into it (see [docs/TEAM.md](docs/TEAM.md#kickoff-day-runbook)).

---

## Required README sections (the organizers check these at screening)

### The job, and who pays for it today
_Example: "Outsourced payables firms are paid per invoice to pay each vendor invoice exactly once..."_

### Input
_The kickoff kit's request format. Link to a sample in `evals/cases/`._

### Finished result
_What the client gets at the end: posted entries, a cleared vendor with evidence, a booked meeting..._

### Pass mark
_How we score a run: N/N hidden-style cases correct, 0 wrong answers ("cannot verify" is allowed), under ₹X and Y seconds per job._

### Data sources the service calls
| Source | What for | Live? | Cost per call |
|---|---|---|---|
| Crustdata API | company / people / jobs | yes | _credits_ |
| _e.g. MCA21 / GST registry_ | legal name, status | yes | free |
| Anthropic Claude (`claude-opus-5-5`) | extraction, matching, explanations (never a source of facts) | n/a | per token |

### Architecture
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Quickstart

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env   # then fill in the keys
pytest
python evals/run_evals.py
python -m service.cli run evals/cases/example_bank_lines.json
```

## Repo layout

```
config/service.toml     every limit, price and output rule; edit this for the hour-3 change
src/service/
  pipeline.py           intake -> gather -> decide -> act -> report (fill in per track)
  ledger.py             SQLite: step checkpoints, at-most-once actions, cost ledger, logs
  evidence.py           Fact / Source / CannotVerify; a fact without a source fails to build
  budget.py             per-run cost/time/call limits
  llm.py                Claude wrapper that logs the cost of every call
  sources/              live data clients (retry + cache + cost logging)
  cli.py                run / pending / approve / reject / cost / logs
evals/                  test cases (incl. hard cases) + scorer
tests/                  unit tests for kill-safety and sourcing rules
docs/                   problem statement, architecture, team plan, pitch prep
```

## Operating the service

| Command | What it does |
|---|---|
| `python -m service.cli run <file>` | Runs a request. Running it again resumes the run; nothing happens twice |
| `python -m service.cli pending` | Lists actions waiting for a person's approval |
| `python -m service.cli approve <key> --by NAME` | Approves one action, then re-run to carry it out |
| `python -m service.cli cost <run_id>` | Cost breakdown per provider and operation |
| `python -m service.cli logs <run_id>` | The run's event log |

**Kill test:** start a run, press `Ctrl+C` partway through, run the same command again. Finished steps load from their checkpoints and actions that already ran are skipped.
