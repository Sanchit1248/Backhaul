"""Request -> result. Runs the ten steps in order. OWNER: B.

Steps 2-8 are checkpointed in the ledger (kill + restart resumes at the first
unfinished step). Steps 9-10 run every time and are idempotent through the
approval queue / outbox, so re-running the same request after approval sends
each email+invite exactly once.
"""
from __future__ import annotations

from .budget import Budget, BudgetExceeded
from .config import settings, usd_to_inr
from .faults import maybe_crash
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
    for name, fn in CHECKPOINTED:
        try:
            data = ledger.step(job.job_id, name, lambda fn=fn, ps=prospects, name=name: _do_step(
                ctx, name, fn, ps))
        except BudgetExceeded as e:
            # Plan rule "budget almost used up": stop, keep finished work, report partial.
            # The failed step is NOT checkpointed, so raising the limit and re-running
            # resumes exactly here.
            ledger.log(job.job_id, "error", f"{name}: {e}")
            ctx.notes.append(f"stopped before {name}: {e}; partial result")
            for p in prospects:
                if p.active:
                    p.hold(f"budget limit reached before step {name}")
            status = "stopped_limit"
            break
        prospects = [Prospect.model_validate(d) for d in data]
        maybe_crash(f"after:{name}")
    for _name, fn in ALWAYS:  # free + idempotent: always finish queued/approved actions
        prospects = fn(ctx, prospects)

    if status == "done" and any(p.status == Status.AWAITING_APPROVAL for p in prospects):
        status = "awaiting_approval"
    cost_usd = ledger.run_cost(job.job_id)
    result = JobResult(job_id=job.job_id, status=status, meetings_wanted=job.meetings_wanted,
                       prospects=prospects, cost_usd=round(cost_usd, 6),
                       cost_inr=round(usd_to_inr(cost_usd), 2), notes=ctx.notes)
    ledger.set_run(job.job_id, status, {"summary": summarize(result)})
    return result


def _do_step(ctx: Ctx, name: str, fn, prospects: list[Prospect]) -> list[dict]:
    """Runs only when the step has no checkpoint yet (ledger.step skips it otherwise)."""
    ctx.budget.check(f"before {name}")
    return [p.model_dump(mode="json") for p in fn(ctx, prospects)]


def summarize(r: JobResult) -> list[dict]:
    """One line per prospect: what happened and why (also the approver's overview)."""
    return [{"company": p.company.name, "status": p.status.value, "reason": p.reason,
             "trigger": p.triggers[0].summary if p.triggers else None,
             "route_fit": p.route_fit.reason if p.route_fit else None,
             "buyer": f"{p.buyer.name}, {p.buyer.title}" if p.buyer else None,
             "cannot_verify": [c.missing for c in p.cannot_verify]}
            for p in r.prospects]
