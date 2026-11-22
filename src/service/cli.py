"""Command line.

    svc run <request.json> [--mode live]   run (or resume) a job; prints the summary
                                           (--mode overrides config sources.mode)
    svc pending [--json]                   actions waiting for approval
    svc approve <key> --by NAME            approve; then re-run the same request to send
    svc reject  <key> --by NAME
    svc cost <job_id>                      cost ledger breakdown
    svc logs <job_id>                      step log
    svc outbox <job_id>                    sent/queued actions + email/invite files
    svc purge [--days N] [--dry-run]       delete person data older than
                                           privacy.retention_days (also runs before `run`)

(`svc` = `python -m service.cli`)
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import DATA_DIR, settings, usd_to_inr
from .ledger import Ledger
from .pipeline import result_summary, run
from .privacy import retention_days


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="svc")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("request_file")
    r.add_argument("--mode", choices=["fixtures", "live"],
                   help="override config sources.mode for this run")
    pe = sub.add_parser("pending")
    pe.add_argument("--json", action="store_true", help="machine-readable list (key, company)")
    for name in ("approve", "reject"):
        a = sub.add_parser(name)
        a.add_argument("key")
        a.add_argument("--by", required=True)
    c = sub.add_parser("cost")
    c.add_argument("job_id")
    c.add_argument("--calls", action="store_true",
                   help="every paid call with its request id (to match provider usage logs)")
    lg =sub.add_parser("logs")
    lg.add_argument("job_id")
    ob = sub.add_parser("outbox")
    ob.add_argument("job_id")
    pg = sub.add_parser("purge")
    pg.add_argument("--days", type=int, default=None,
                    help="override config privacy.retention_days")
    pg.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    ledger = Ledger()
    if args.cmd == "run":
        with open(args.request_file, encoding="utf-8") as f:
            req = json.load(f)
        req = req.get("request", req)  # accept eval case files directly
        if args.mode:
            settings()["sources"]["mode"] = args.mode
        ledger.purge_person_data(retention_days())  # privacy.retention_days, every run
        res = run(req, ledger)
        out = DATA_DIR / "results" / f"{res.job_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(res.model_dump_json(indent=2), encoding="utf-8")
        print(json.dumps(result_summary(res, full_result=str(out)), indent=2))
    elif args.cmd == "pending":
        pending = ledger.pending_approvals()
        if args.json:
            print(json.dumps([{"key": a["key"], "run_id": a["run_id"],
                               "company": a["payload"]["company"],
                               "to": a["payload"]["draft"]["to"]} for a in pending], indent=2))
            return 0
        for a in pending:
            d = a["payload"]["draft"]
            print(f"\n=== {a['key']}  {a['payload']['company']}  -> {d['to']}")
            print(f"route fit: {a['payload']['route_fit']}")
            print(f"subject: {d['subject']}\n{d['body']}")
    elif args.cmd in ("approve", "reject"):
        ledger.decide(args.key, args.cmd == "approve", by=args.by)
        print(f"{args.cmd}d {args.key} by {args.by}. Re-run the request to send.")
    elif args.cmd == "cost":
        for row in ledger.cost_breakdown(args.job_id):
            print(*row, sep="\t")
        usd = ledger.run_cost(args.job_id)
        print(f"TOTAL\t${usd:.6f}\tRs {usd_to_inr(usd):.2f}")
        credits = ledger.credit_breakdown(args.job_id)
        if credits:
            print("\nCREDITS\tprovider\top\tcalls\tstatuses\testimated_max\treported"
                  "\tbudgeted\tcalls_without_reported_credits")
            for a in credits:
                sts = ",".join(f"{k}x{v}" for k, v in sorted(a["statuses"].items()))
                print(f"\t{a['provider']}\t{a['op']}\t{a['calls']}\t{sts}"
                      f"\t{a['estimated_max']:g}\t{a['reported']:g}\t{a['budgeted']:g}"
                      f"\t{a['unreported_calls']}")
            rep = sum(a["reported"] for a in credits)
            bud = sum(a["budgeted"] for a in credits)
            unrep = sum(a["unreported_calls"] for a in credits)
            print(f"CREDITS TOTAL\treported {rep:g}\tbudgeted {bud:g}"
                  f"\t{unrep} call(s) without a reported figure (budgeted at the estimate, or 0 if rejected)")
            print("Compare 'reported' with Crustdata's usage for this run's time window;"
                  " --calls lists request ids.")
        if args.calls:
            print("\nCALLS\tprovider\top\tstatus\treported\tbudgeted\trequest_id\tstarted_at")
            for r in ledger.cost_rows(args.job_id):
                u = r["units"]
                print(f"\t{r['provider']}\t{r['op']}\t{u.get('status')}"
                      f"\t{u.get('reported_credits')}\t{u.get('budgeted_credits')}"
                      f"\t{u.get('request_id')}\t{u.get('started_at')}")
    elif args.cmd == "logs":
        for row in ledger.db.execute(
            "SELECT datetime(at,'unixepoch'), level, msg FROM events WHERE run_id=? ORDER BY id",
            (args.job_id,),
        ):
            print(*row, sep="  ")
    elif args.cmd == "purge":
        days = args.days if args.days is not None else retention_days()
        counts = ledger.purge_person_data(days, dry_run=args.dry_run)
        print(("would purge" if args.dry_run else "purged") + f" (older than {days} days): "
              + json.dumps(counts))
    elif args.cmd == "outbox":
        out = DATA_DIR / "outbox"
        n_eml = n_ics = 0
        for a in ledger.actions_for_run(args.job_id):
            # count the files the mailer wrote for this key (the action result can omit
            # the .ics path when a send is finished after a crash)
            has_eml = (out / f"{a['key']}.eml").exists()
            has_ics = (out / f"{a['key']}.ics").exists()
            n_eml += has_eml
            n_ics += has_ics
            print(f"{a['key']}	{a['status']}	{a['payload']['company']}	"
                  f"approved_by={a['approved_by']}	email={'yes' if has_eml else 'no'}	"
                  f"invite={'yes' if has_ics else 'no'}")
        files = sorted(p.name for p in out.glob("*")) if out.exists() else []
        print(f"emails={n_eml} invites={n_ics} files_in_outbox={len(files)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
