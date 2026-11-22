"""Run every case in evals/cases and score it. OWNER: C (from D).

    python evals/run_evals.py            # all cases
    python evals/run_evals.py hard       # cases whose file name contains "hard"

Scoring mirrors screening: a WRONG answer is worse than "cannot verify".
WRONG = we would contact someone we should not (or vice versa), or the wrong buyer.
Exit code 1 if anything is WRONG -> run before every merge.

Optional case fields:
  "simulate_spend": {"before_step": "07_buyer", "fraction_of_limit": 1.01}
      writes a real cost row to the ledger right before that step, as if earlier paid
      calls had used the budget; the pipeline's own budget check must then stop the job.
  "expected_job": {"status": "stopped_limit", "notes_contain": "...", "queued_actions": 0}
      job-level checks, scored like a prospect (mismatch = WRONG).
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service import mailer, pipeline
from service.config import settings, usd_to_inr
from service.ledger import Ledger
from service.pipeline import run

CONTACT = {"awaiting_approval", "sent"}


def verdict(got, exp: dict) -> str:
    if got is None:
        return "missing"
    status = got.status.value
    if status == "cannot_verify" and exp["status"] != "cannot_verify":
        return "cannot_verify"  # safe miss: counts against us less than WRONG
    if status != exp["status"]:
        return "WRONG"
    if "buyer" in exp and (got.buyer is None or got.buyer.name != exp["buyer"]):
        return "WRONG"
    if "reason_contains" in exp and exp["reason_contains"] not in (got.reason or ""):
        return "WRONG"
    return "pass"


def _spend_before(real, ledger: Ledger, spend: dict):
    """Wrap pipeline._do_step: charge `fraction_of_limit` of the job budget before a step."""
    def do_step(ctx, name, fn, prospects):
        if name == spend["before_step"]:
            inr = ctx.budget.max_cost_inr * float(spend.get("fraction_of_limit", 1.01))
            ledger.cost(ctx.job.job_id, "eval", "simulated earlier spend",
                        inr / usd_to_inr(1.0), {"inr": round(inr, 2)})
        return real(ctx, name, fn, prospects)
    return do_step


def job_verdict(res, queued: int, exp: dict) -> tuple[str, str]:
    got = f"status={res.status}, queued_actions={queued}, notes={res.notes}"
    if res.status != exp.get("status", res.status):
        return "WRONG", f"expected status {exp['status']}; got {got}"
    if (n := exp.get("notes_contain")) and not any(n in x for x in res.notes):
        return "WRONG", f"expected a note containing {n!r}; got {got}"
    if "queued_actions" in exp and queued != exp["queued_actions"]:
        return "WRONG", f"expected {exp['queued_actions']} queued actions; got {got}"
    return "pass", got


def main(filter_: str = "") -> int:
    totals: dict[str, int] = {}
    # glob("**.json") is invalid on Python < 3.13, so filter by name instead
    cases = [p for p in sorted((ROOT / "evals" / "cases").glob("*.json")) if filter_ in p.name]
    for path in cases:
        case = json.loads(path.read_text(encoding="utf-8"))
        cfg = settings()
        saved = dict(cfg["sources"])
        cfg["sources"]["mode"] = case.get("sources", saved["mode"])
        if case.get("fixtures_file"):
            cfg["sources"]["fixtures_file"] = case["fixtures_file"]
        with tempfile.TemporaryDirectory() as d:
            mailer.DATA_DIR = Path(d)
            ledger = Ledger(Path(d) / "eval.sqlite")
            real_do_step = pipeline._do_step
            if spend := case.get("simulate_spend"):
                pipeline._do_step = _spend_before(real_do_step, ledger, spend)
            try:
                res = run(case["request"], ledger)
                queued = len(ledger.actions_for_run(res.job_id))
            finally:
                pipeline._do_step = real_do_step
                ledger.close()
                cfg["sources"] = saved
        by_name = {p.company.name: p for p in res.prospects}
        print(f"\n== {case['id']}  status={res.status}  cost=Rs {res.cost_inr}")
        if exp_job := case.get("expected_job"):
            v, why = job_verdict(res, queued, exp_job)
            totals[v] = totals.get(v, 0) + 1
            print(f"  [{v:>13}] job: {why}")
        for name, exp in case["expected"].items():
            v = verdict(by_name.get(name), exp)
            totals[v] = totals.get(v, 0) + 1
            got = by_name.get(name)
            got_s = f"{got.status.value} ({got.reason})" if got else "-"
            print(f"  [{v:>13}] {name}: expected {exp['status']}, got {got_s}")
    print("\nTOTAL", "  ".join(f"{k}={v}" for k, v in sorted(totals.items())))
    return 1 if totals.get("WRONG") or totals.get("missing") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
