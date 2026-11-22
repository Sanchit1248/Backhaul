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
    _prospect_fields()  # fail on an output-config typo before any step or send runs
    _result_keys()
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
        except Exception as e:  # noqa: BLE001 - a broken source/step must not kill the job
            # Unexpected failure (source not wired, missing key, bad response shape).
            # Keep finished work, hold the rest as cannot-verify, report partial. The
            # step is NOT checkpointed, so a re-run after the fix resumes here.
            err = f"{type(e).__name__}: {e}"
            ledger.log(job.job_id, "error", f"{name} failed: {err}")
            ctx.notes.append(f"cannot verify: step {name} failed ({err}); partial result")
            for p in prospects:
                if p.active:
                    p.hold(f"cannot verify: step {name} failed")
            status = "partial"
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


# Named prospect output fields; anything else in config output.prospect_fields is a
# dotted path into the Prospect (e.g. "buyer.role_start", "triggers.0.event_date"),
# so a new output field at the live fix is a config edit, not a code change.
PROSPECT_FIELDS = {
    "company": lambda p: p.company.name,
    "status": lambda p: p.status.value,
    "reason": lambda p: p.reason,
    "trigger": lambda p: p.triggers[0].summary if p.triggers else None,
    "route_fit": lambda p: p.route_fit.reason if p.route_fit else None,
    "buyer": lambda p: f"{p.buyer.name}, {p.buyer.title}" if p.buyer else None,
    "cannot_verify": lambda p: [c.missing for c in p.cannot_verify],
}
DEFAULT_PROSPECT_FIELDS = list(PROSPECT_FIELDS)


def _path(data, path: str):
    for part in path.split("."):
        if isinstance(data, list):
            data = data[int(part)] if part.isdigit() and int(part) < len(data) else None
        elif isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data


def _prospect_fields() -> list[str]:
    fields = settings().get("output", {}).get("prospect_fields") or DEFAULT_PROSPECT_FIELDS
    roots = set(Prospect.model_fields)
    bad = [f for f in fields if f not in PROSPECT_FIELDS and f.split(".")[0] not in roots]
    if bad:
        raise ValueError(f"config output.prospect_fields: unknown field(s) {bad}; use a name "
                         f"from {sorted(PROSPECT_FIELDS)} or a path into Prospect {sorted(roots)}")
    return fields


def summarize(r: JobResult) -> list[dict]:
    """One line per prospect: what happened and why (also the approver's overview).
    Fields come from config output.prospect_fields."""
    fields = _prospect_fields()
    out = []
    for p in r.prospects:
        dumped = None
        row = {}
        for f in fields:
            if f in PROSPECT_FIELDS:
                row[f] = PROSPECT_FIELDS[f](p)
            else:
                dumped = dumped or p.model_dump(mode="json")
                row[f] = _path(dumped, f)
        out.append(row)
    return out


def _result_keys() -> list[str]:
    ocfg = settings().get("output", {})
    keys: list[str] = []
    for k in [*ocfg.get("required_fields", []), *ocfg.get("result_fields", [])]:
        if k not in keys:
            keys.append(k)
    unknown = [k for k in keys if k not in JobResult.model_fields]
    if unknown:
        raise ValueError(f"config output fields not on JobResult: {unknown}; "
                         f"use {sorted(JobResult.model_fields)}")
    return keys


def result_summary(r: JobResult, **extra) -> dict:
    """The printed job result: config output.required_fields first (always present),
    then output.result_fields, then extras (e.g. the full-result path)."""
    keys = _result_keys()
    dumped = r.model_dump(mode="json")
    out = {k: summarize(r) if k == "prospects" else dumped[k] for k in keys}
    out.update(extra)
    return out
