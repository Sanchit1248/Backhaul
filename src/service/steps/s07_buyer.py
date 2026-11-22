"""Step 7: find the buyer and confirm they still work there. OWNER: A.

Rules:
  * never match on name alone: employer_id must equal the company's crustdata_id
  * role_end set, or current employer differs   -> has left -> try next person
  * no business email                           -> try next (no personal emails, ever)
  * nobody left                                 -> hold "no verified buyer"
"""
from __future__ import annotations

from ..models import Fact, Prospect, Source
from . import Ctx, active


def run(ctx: Ctx, prospects: list[Prospect]) -> list[Prospect]:
    cfg = ctx.cfg["buyer"]
    for p in active(prospects):
        people = ctx.sources.people(p.company, cfg["titles"])[: cfg["max_candidates"]]
        for cand in people:
            b = ctx.sources.verify_employment(cand)
            if b.role_end:
                p.buyers_rejected.append({"name": b.name, "reason": f"left on {b.role_end}"})
                continue
            if b.employer_id != p.company.crustdata_id:
                p.buyers_rejected.append({"name": b.name, "reason": "name match only: employer is "
                                          f"{b.employer_name or b.employer_id}"})
                continue
            if not b.business_email:
                p.buyers_rejected.append({"name": b.name, "reason": "no business email"})
                continue
            src = Source(url=b.profile_url or f"crustdata:person/{b.person_id}",
                         kind="api_response",
                         excerpt=f"current_employer_id = {b.employer_id}; title = {b.title}; "
                                 f"role_start = {b.role_start}",
                         field_path="employment.current")
            b.facts.append(Fact(claim=f"{b.name} is {b.title} at {p.company.name}",
                                value=b.title, sources=[src]))
            p.buyer = b
            break
        if p.buyer is None:
            p.hold("no verified buyer")
    return prospects
