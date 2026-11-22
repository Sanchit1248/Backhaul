# CLAUDE.md

Hackathon repo for ContextCon 05 "Services, Replaced". Read docs/CHECKLIST.md first.

Rules for any code change:
- Every fact in output is a `evidence.Fact` with a `Source` (URL, exact excerpt/field, record date). Never output facts from model memory. If no source settles it, return `CannotVerify`.
- Every side effect goes through `Ledger.action()` with an idempotency key built from the business identity (invoice no, payee, amount), never a timestamp or uuid.
- Every paid call goes through `llm.ask()` or `sources/base.fetch_json()` so the cost is logged and the budget is checked.
- Tunables (limits, prices, required output fields, irreversible action kinds) live in `config/service.toml`, not code.
- Before saying a change is done, run: `pytest` and `python evals/run_evals.py`.
