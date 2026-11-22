"""Step 9: queue each draft for a person's approval.

No double contact across jobs: before queueing, the ledger is checked for an email from
another job to the same recipient (or the same company, config approval.recontact_scope)
that is pending, approved or sent within approval.recontact_days. If there is one, the
prospect is excluded with that job named, and nothing is queued.

The approver sees email + evidence + route-fit reason + cost, then approves,
edits or rejects (CLI today: `svc pending|approve|reject`; C builds the web
approval page on top of ledger.pending_approvals / decide / update_payload).
The outbox key = hash(job_id, prospect_id, "send_email") -> one email+invite ever.
"""
from __future__ import annotations

import time
from datetime import datetime

from ..ledger import idem_key
from ..models import Prospect, Status
from . import Ctx


def outbox_key(job_id: str, prospect_id: str) -> str:
    return idem_key(job_id, prospect_id, "send_email")


def _route_fit_detail(p: Prospect) -> dict:
    f = p.route_fit
    return {"level": f.level, "score": f.score, "reason": f.reason,
            "route": f.route.model_dump(mode="json") if f.route else None,
            "facts": [x.model_dump(mode="json") for x in f.facts]}


def _buyer_evidence(p: Prospect) -> dict | None:
    """Only the person fields privacy allows (config privacy.person_fields) + their facts."""
    b = p.buyer
    if b is None:
        return None
    return {"name": b.name, "title": b.title, "employer_name": b.employer_name,
            "employer_id": b.employer_id, "company_id": p.company.crustdata_id,
            "role_start": b.role_start.isoformat() if b.role_start else None,
            "role_end": b.role_end.isoformat() if b.role_end else None,
            "business_email": b.business_email,
            "facts": [x.model_dump(mode="json") for x in b.facts]}


def recontact_reason(ctx: Ctx, p: Prospect, statuses: tuple[str, ...] | None = None) -> str | None:
    """'already contacted ...' if another job emailed/queued this recipient or company
    inside approval.recontact_days, else None."""
    cfg = ctx.cfg.get("approval", {})
    days = cfg.get("recontact_days", 90)
    if not days or p.draft is None:
        return None
    scope = cfg.get("recontact_scope", ["recipient", "company"])
    company = p.company.crustdata_id if "company" in scope else None
    email = p.draft.to if "recipient" in scope else None
    kw = {"statuses": statuses} if statuses else {}
    hits = ctx.ledger.prior_contacts(ctx.job.job_id, email, str(company) if company else None,
                                     time.time() - float(days) * 86400, **kw)
    if not hits:
        return None
    h = hits[-1]
    when = datetime.fromtimestamp(h["at"]).strftime("%Y-%m-%d")
    who = "this recipient" if h["same"] == "recipient" else f"{p.company.name}"
    return (f"already contacted: {who} has an email {h['status']} in job {h['run_id']} "
            f"({when}); recontact_days={days}")


def run(ctx: Ctx, prospects: list[Prospect]) -> list[Prospect]:
    for p in prospects:
        if p.draft is None or p.status not in (Status.ACTIVE, Status.AWAITING_APPROVAL):
            continue
        key = outbox_key(ctx.job.job_id, p.prospect_id)
        if ctx.ledger.get_action(key) is None and (why := recontact_reason(ctx, p)):
            p.exclude(why)
            continue
        payload = {"prospect_id": p.prospect_id, "company": p.company.name,
                   "route_fit": p.route_fit.reason,
                   "route_fit_detail": _route_fit_detail(p),
                   "buyer": _buyer_evidence(p),
                   "draft": p.draft.model_dump(mode="json"),
                   "cost_inr_so_far": round(ctx.budget.spent_inr, 2)}
        status = ctx.ledger.request_approval(ctx.job.job_id, "send_email", key, payload)
        p.status = {"pending_approval": Status.AWAITING_APPROVAL,
                    "rejected": Status.REJECTED, "done": Status.SENT}.get(status, Status.ACTIVE)
        if p.status == Status.REJECTED:
            p.reason = "rejected by approver"
    return prospects
