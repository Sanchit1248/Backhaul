"""Run every case in evals/cases and score it. OWNER: D.

    python evals/run_evals.py            # all cases
    python evals/run_evals.py hard       # cases whose file name contains "hard"

Scoring mirrors screening: a WRONG answer is worse than "cannot verify".
WRONG = we would contact someone we should not (or vice versa), or the wrong buyer.
Exit code 1 if anything is WRONG -> run before every merge.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service import mailer
from service.config import settings
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


def main(filter_: str = "") -> int:
    totals: dict[str, int] = {}
    for path in sorted((ROOT / "evals" / "cases").glob(f"*{filter_}*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        cfg = settings()
        saved = dict(cfg["sources"])
        cfg["sources"]["mode"] = case.get("sources", saved["mode"])
        if case.get("fixtures_file"):
            cfg["sources"]["fixtures_file"] = case["fixtures_file"]
        with tempfile.TemporaryDirectory() as d:
            mailer.DATA_DIR = Path(d)
            ledger = Ledger(Path(d) / "eval.sqlite")
            try:
                res = run(case["request"], ledger)
            finally:
                ledger.close()
                cfg["sources"] = saved
        by_name = {p.company.name: p for p in res.prospects}
        print(f"\n== {case['id']}  status={res.status}  cost=Rs {res.cost_inr}")
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
