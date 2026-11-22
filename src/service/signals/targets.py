"""find_targets(routes, window, count) -> targets with route fit, buyer, status, evidence IDs.

For each lane asked for (or every lane with free capacity this week):
  1. capacity from the route adapter (Fleetbase or file); unknown or zero -> the lane is
     reported, not run (never contact on capacity we cannot show)
  2. the ten-step pipeline with that lane and its free trucks (checkpointed, so a killed
     task resumes where it stopped); drafts land in the approval queue as usual
  3. every fact used (trigger, route fit, buyer) is saved to the evidence store; each
     target lists its evidence IDs and the shared status kept | held | cannot_verify
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import date

from ..budget import Budget
from ..config import settings
from ..core.evidence import EvidenceStore
from ..core.memory import AccountMemory, company_key
from ..core.results import result_status
from ..core.tasks import task
from ..ledger import Ledger
from ..models import Fact, Prospect
from ..pipeline import result_summary
from ..pipeline import run as run_pipeline
from ..routes.adapter import Lane, lane_key
from ..routes.factory import get_adapter
from ..steps.s09_approve import outbox_key

_cfg_lock = threading.Lock()


@contextmanager
def _window(days: int | None):
    """Run with triggers.window_days = days (the pipeline reads config, not params)."""
    with _cfg_lock:
        trig = settings()["triggers"]
        saved = trig["window_days"]
        if days:
            trig["window_days"] = int(days)
        try:
            yield
        finally:
            trig["window_days"] = saved


def _ids(store: EvidenceStore, facts: list[Fact]) -> list[str]:
    return [store.save_fact(f) for f in facts]


def target_of(p: Prospect, job_id: str, store: EvidenceStore, mem: AccountMemory) -> dict:
    trig_ids = [_ids(store, [t.fact])[0] for t in p.triggers]
    fit_ids = _ids(store, p.route_fit.facts) if p.route_fit else []
    buyer_ids = _ids(store, p.buyer.facts) if p.buyer else []
    ckey = company_key(p.company.crustdata_id, p.company.name)
    last = mem.last_contact(ckey)
    return {
        "company": p.company.name, "company_key": ckey, "prospect_id": p.prospect_id,
        "job_id": job_id, "status": result_status(p.status), "pipeline_status": p.status.value,
        "reason": p.reason,
        "triggers": [{"type": t.type, "summary": t.summary,
                      "event_date": t.event_date.isoformat() if t.event_date else None,
                      "evidence_id": eid} for t, eid in zip(p.triggers, trig_ids, strict=True)],
        "route_fit": ({"level": p.route_fit.level, "score": p.route_fit.score,
                       "reason": p.route_fit.reason, "evidence_ids": fit_ids}
                      if p.route_fit else None),
        "buyer": ({"name": p.buyer.name, "title": p.buyer.title, "evidence_ids": buyer_ids}
                  if p.buyer else None),
        "city": (str(p.route_fit.facts[0].value) if p.route_fit and p.route_fit.facts
                 else None),
        "recipient": p.draft.to if p.draft else None,
        "outbox_key": outbox_key(job_id, p.prospect_id) if p.draft else None,
        "last_contact": last,
        "cannot_verify": [c.model_dump() for c in p.cannot_verify],
        "evidence_ids": trig_ids + fit_ids + buyer_ids,
    }


@task("find_targets")
def find_targets(ledger: Ledger, params: dict) -> dict:
    """params: routes [[origin, destination], ...] (optional), window_days, count,
    request_date (ISO, default today), budget_inr (optional)."""
    tid = params["task_id"]
    req_date = date.fromisoformat(params["request_date"]) if params.get("request_date") \
        else date.today()
    count = int(params.get("count") or 5)
    notes: list[str] = []
    adapter = get_adapter(ledger, Budget(ledger, tid), notes)
    if params.get("routes"):
        known = {lane.key: lane for lane in adapter.list_routes()}
        lanes = [known.get(lane_key(a, b), Lane(a.lower(), b.lower()))
                 for a, b in params["routes"]]
    else:
        lanes = adapter.list_routes()

    store, mem = EvidenceStore(ledger), AccountMemory(ledger)
    lanes_out, targets, cost_inr, statuses = [], [], 0.0, []
    for lane in lanes:
        cap = adapter.capacity(lane, req_date)
        row = {"lane": lane.key, "label": lane.label(), "capacity": cap.as_dict()}
        lanes_out.append(row)
        if not cap.known:
            row["skipped"] = f"cannot verify: {cap.reason}"
            continue
        if not cap.free:
            row["skipped"] = f"no free capacity in {cap.week} ({cap.reason})"
            continue
        job_id = f"{tid}-{lane.key}"
        req = {"job_id": job_id, "request_date": req_date.isoformat(),
               "text": (f"Book {count} meetings for the {lane.origin.title()} to "
                        f"{lane.destination.title()} route; spare capacity {cap.free} "
                        "trucks a week"),
               "route": [lane.origin, lane.destination], "meetings": count,
               "spare_trucks_per_week": cap.free}
        if params.get("budget_inr"):
            req["budget_inr"] = params["budget_inr"]
        with _window(params.get("window_days")):
            res = run_pipeline(req, ledger)
        row.update(job_id=job_id, job_status=res.status, notes=res.notes, cost_inr=res.cost_inr)
        cost_inr += res.cost_inr
        statuses.append(res.status)
        targets += [target_of(p, job_id, store, mem) for p in res.prospects]

    order = {"kept": 0, "held": 1, "cannot_verify": 2}
    targets.sort(key=lambda t: (order[t["status"]], -(t["route_fit"] or {}).get("score", 0)))
    best: dict[str, dict] = {}            # one row per company: its best lane result
    for t in targets:
        if t["company_key"] in best:
            best[t["company_key"]].setdefault("other_lanes", []).append(
                {"job_id": t["job_id"], "status": t["status"], "reason": t["reason"]})
        else:
            best[t["company_key"]] = t
    targets = list(best.values())
    kept = [t for t in targets if t["status"] == "kept"]
    status = ("stopped_limit" if "stopped_limit" in statuses else
              "partial" if "partial" in statuses else "done")
    return {"status": status, "request_date": req_date.isoformat(),
            "window_days": params.get("window_days")
            or settings()["triggers"]["window_days"], "count": count,
            "adapter": adapter.name, "synced_at": adapter.synced_at, "notes": notes,
            "lanes": lanes_out, "targets": targets, "kept": len(kept),
            "cost_inr": round(cost_inr, 2)}


@task("job")
def job(ledger: Ledger, params: dict) -> dict:
    """The organizers' request format, as-is: params['request'] -> the configured output
    (pipeline.result_summary: status, prospects, cost_inr + config output fields)."""
    return result_summary(run_pipeline(params["request"], ledger))
