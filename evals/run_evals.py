"""Run every case in evals/cases against the pipeline and score it.

    python evals/run_evals.py            # all cases
    python evals/run_evals.py bank       # cases whose file name contains "bank"

Scoring mirrors screening: a wrong answer is worse than "cannot verify".
Adapt `compare()` to the kickoff-kit output format.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service.ledger import Ledger
from service.pipeline import run

CANNOT = "cannot verify"


def compare(got: dict, exp: dict) -> str:
    """Return 'pass' | 'cannot_verify' | 'wrong'."""
    g_status = (got.get("result") or {}).get("label") or got.get("status")
    if exp.get("status") == CANNOT:
        return "pass" if g_status == CANNOT else "wrong"
    if g_status == CANNOT:
        return "cannot_verify"
    for k, v in exp.items():
        if str(got.get(k, "")).lower() != str(v).lower():
            return "wrong"
    return "pass"


def main(filter_: str = "") -> int:
    cases = sorted((ROOT / "evals" / "cases").glob(f"*{filter_}*.json"))
    totals = {"pass": 0, "cannot_verify": 0, "wrong": 0}
    for path in cases:
        case = json.loads(path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as d:
            ledger = Ledger(Path(d) / "eval.sqlite")
            try:
                out = run(case["request"], ledger)
            finally:
                ledger.close()
        got = out.get("decisions", [])
        print(f"\n== {case['id']}  cost=${out.get('cost_usd', 0)}  status={out['status']}")
        for i, exp in enumerate(case["expected"]):
            g = got[i] if i < len(got) else {}
            verdict = compare(g, exp)
            totals[verdict] += 1
            print(f"  [{verdict:>13}] item {i}: expected {exp}")
    n = sum(totals.values()) or 1
    print(f"\nTOTAL pass {totals['pass']}/{n}  cannot_verify {totals['cannot_verify']}"
          f"  WRONG {totals['wrong']}")
    return 1 if totals["wrong"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
