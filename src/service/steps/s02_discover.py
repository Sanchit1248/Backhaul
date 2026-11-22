"""Step 2: discover candidate companies. OWNER: A.

Indian companies in target industries + headcount band (config discovery.*), near
the route end points, with logistics job posts or expansion news in the window.
Cap at config limits.max_candidates; cheap filters before paid enrichment.

Outcomes:
  * provider failure (SourceUnavailable) -> no prospects + a "cannot verify" note,
    so an outage is never reported as "no companies matched"
  * successful empty search              -> no prospects + a note saying so
  * BudgetExceeded                       -> propagates to the pipeline
"""
from __future__ import annotations

from ..ledger import idem_key
from ..models import Prospect
from ..sources.base import SourceUnavailable
from . import Ctx


def run(ctx: Ctx, prospects: list[Prospect]) -> list[Prospect]:
    try:
        found = ctx.sources.discover(ctx.job, ctx.client)
    except SourceUnavailable as e:
        ctx.notes.append(f"cannot verify: discovery source unavailable ({e})")
        ctx.log("discover: source unavailable", error=str(e))
        return []
    out: list[Prospect] = []
    seen: set[str] = set()
    for c in found:
        pid = idem_key(ctx.job.job_id, c.domain or c.name.lower())
        if pid in seen:
            continue
        seen.add(pid)
        out.append(Prospect(prospect_id=pid, company=c))
        if len(out) >= ctx.cfg["limits"]["max_candidates"]:
            break
    if not out:
        ctx.notes.append("discover: search succeeded but returned no candidates")
    ctx.log(f"discover: {len(out)} candidates")
    return out
