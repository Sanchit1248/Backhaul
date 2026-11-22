"""Step 2: discover candidate companies. OWNER: A.

Indian companies in target industries + headcount band (config discovery.*), near
the route end points, with logistics job posts or expansion news in the window.
Cap at config limits.max_candidates; cheap filters before paid enrichment.
"""
from __future__ import annotations

from ..ledger import idem_key
from ..models import Prospect
from . import Ctx


def run(ctx: Ctx, prospects: list[Prospect]) -> list[Prospect]:
    found = ctx.sources.discover(ctx.job, ctx.client)[: ctx.cfg["limits"]["max_candidates"]]
    out = [Prospect(prospect_id=idem_key(ctx.job.job_id, c.domain or c.name.lower()), company=c)
           for c in found]
    ctx.log(f"discover: {len(out)} candidates")
    return out
