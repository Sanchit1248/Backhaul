"""Request -> result. Runs the ten steps in order. OWNER: B.

Steps 2-8 are checkpointed in the ledger (kill + restart resumes at the first
unfinished step). Steps 9-10 run every time and are idempotent through the
approval queue / outbox, so re-running the same request after approval sends
each email+invite exactly once.
"""
from __future__ import annotations

from .budget import Budget, BudgetExceeded
from .config import settings, usd_to_inr
from .ledger import Ledger
from .models import JobResult, Prospect, Status
from .sources import get_sources
from .steps import (
    Ctx,
    s01_intake,
    s02_discover,
    s03_identity,
    s04_triggers,
    s05_route_fit,
    s06_exclude,
    s07_buyer,
    s08_draft,
    s09_approve,
    s10_send,
)

CHECKPOINTED = [
    ("02_discover", s02_discover.run),
    ("03_identity", s03_identity.run),
    ("04_triggers", s04_triggers.run),
    ("05_route_fit", s05_route_fit.run),
    ("06_exclude", s06_exclude.run),
    ("07_buyer", s07_buyer.run),
    ("08_draft", s08_draft.run),
]
ALWAYS = [("09_approve", s09_approve.run), ("10_send", s10_send.run)]


def run(raw_request: dict, ledger: Ledger | None = None) -> JobResult:
    cfg = settings()
    ledger = ledger or Ledger()
    job = s01_intake.parse_request(raw_request)
    client = s01_intake.load_client_records(cfg["client"]["records_file"])
    fresh = ledger.start_run(job.job_id, raw_request)
    ledger.log(job.job_id, "info", "job started" if fresh else "job resumed")
    budget = Budget(ledger, job.job_id, job.budget_inr)
    ctx = Ctx(job=job, client=client, ledger=ledger, budget=budget,
              sources=get_sources(ledger, budget), cfg=cfg)

    prospects: list[Prospect] = []
    status = "done"
    try:
        for name, fn in CHECKPOINTED:
            data = ledger.step(job.job_id, name, lambda fn=fn, ps=prospects: [
                p.model_dump(mode="json") for p in fn(ctx, ps)])
            prospects = [Prospect.model_validate(d) for d in data]
        for _name, fn in ALWAYS:
            prospects = fn(ctx, prospects)
    except BudgetExceeded as e:
        ledger.log(job.job_id, "error", str(e))
        ctx.notes.append(f"stopped: {e}; partial result")
        status = "stopped_limit"

    if status == "done" and any(p.status == Status.AWAITING_APPROVAL for p in prospects):
        status = "awaiting_approval"
    cost_usd = ledger.run_cost(job.job_id)
    result = JobResult(job_id=job.job_id, status=status, meetings_wanted=job.meetings_wanted,
                       prospects=prospects, cost_usd=round(cost_usd, 6),
                       cost_inr=round(usd_to_inr(cost_usd), 2), notes=ctx.notes)
    ledger.set_run(job.job_id, status, {"summary": summarize(result)})
    return result


def summarize(r: JobResult) -> list[dict]:
    """One line per prospect: what happened and why (also the approver's overview)."""
    return [{"company": p.company.name, "status": p.status.value, "reason": p.reason,
             "trigger": p.triggers[0].summary if p.triggers else None,
             "route_fit": p.route_fit.reason if p.route_fit else None,
             "buyer": f"{p.buyer.name}, {p.buyer.title}" if p.buyer else None,
             "cannot_verify": [c.missing for c in p.cannot_verify]}
            for p in r.prospects]
