"""Step 10: send approved email + invite exactly once. OWNER: B.

Sends the payload STORED in the ledger (what the person approved, incl. edits),
not the in-memory draft. Re-checks the exclusion lists right before sending.
Crash-safety: ledger.action marks 'started' before the send and 'done' after;
the mailer must itself be idempotent on the key (file mailer: file exists ->
already sent; SMTP: check sent folder / Message-ID before retrying).
"""
from __future__ import annotations

from ..core.memory import AccountMemory, company_key
from ..faults import maybe_crash
from ..mailer import send
from ..models import Prospect, Status
from . import Ctx
from .s06_exclude import exclusion_reason
from .s09_approve import outbox_key, recontact_reason


def _send(payload: dict) -> dict:
    maybe_crash("before_send")   # outbox row is 'started', nothing written yet
    res = send(payload)
    maybe_crash("after_send")    # email written, outbox row not yet 'done'
    return res


def run(ctx: Ctx, prospects: list[Prospect]) -> list[Prospect]:
    for p in prospects:
        key = outbox_key(ctx.job.job_id, p.prospect_id)
        a = ctx.ledger.get_action(key)
        if a is None or a["status"] not in ("approved", "started", "failed", "done"):
            continue
        if a["status"] != "done" and (reason := exclusion_reason(p, ctx.client)):
            p.exclude(reason + " (re-checked before send)")
            continue
        # two jobs' drafts to one person approved together: only the first is sent
        if a["status"] == "approved" and (
                why := recontact_reason(ctx, p, statuses=("started", "done"))):
            ctx.ledger._set_action(key, "expired", {"reason": why})
            p.exclude(why + " (re-checked before send)")
            continue
        res = ctx.ledger.action(ctx.job.job_id, "send_email", key, a["payload"], _send,
                                needs_approval=True)
        p.outbox = [res]
        if res["status"] == "done":
            p.status = Status.SENT
            AccountMemory(ctx.ledger).record_action(
                company_key(p.company.crustdata_id, p.company.name), p.company.name, "sent",
                job_id=ctx.job.job_id)
    return prospects
