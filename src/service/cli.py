"""Command line: run a request, approve actions, inspect costs and logs.

    python -m service.cli run evals/cases/example.json
    python -m service.cli pending
    python -m service.cli approve <key> --by "Name"
    python -m service.cli reject  <key> --by "Name"
    python -m service.cli cost <run_id>
    python -m service.cli logs <run_id>
"""
from __future__ import annotations

import argparse
import json
import sys

from .ledger import Ledger
from .pipeline import run


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="svc")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("request_file")
    sub.add_parser("pending")
    for name in ("approve", "reject"):
        a = sub.add_parser(name); a.add_argument("key"); a.add_argument("--by", required=True)
    c = sub.add_parser("cost"); c.add_argument("run_id")
    lg = sub.add_parser("logs"); lg.add_argument("run_id")
    args = p.parse_args(argv)

    ledger = Ledger()
    if args.cmd == "run":
        with open(args.request_file, encoding="utf-8") as f:
            req = json.load(f)
        req = req.get("request", req)  # accept eval case files directly
        print(json.dumps(run(req, ledger), indent=2, default=str))
    elif args.cmd == "pending":
        for a in ledger.pending_approvals():
            print(json.dumps(a, indent=2))
    elif args.cmd in ("approve", "reject"):
        ledger.decide(args.key, args.cmd == "approve", by=args.by)
        print(f"{args.cmd}d {args.key} by {args.by}. Re-run the request to execute.")
    elif args.cmd == "cost":
        total = ledger.run_cost(args.run_id)
        for row in ledger.cost_breakdown(args.run_id):
            print(*row, sep="\t")
        print(f"TOTAL\t${total:.6f}")
    elif args.cmd == "logs":
        for row in ledger.db.execute(
            "SELECT datetime(at,'unixepoch'), level, msg FROM events WHERE run_id=? ORDER BY id",
            (args.run_id,),
        ):
            print(*row, sep="  ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
