"""Step 6: exclude current clients, rivals and do-not-contact. OWNER: B.

Also called again by step 10 right before sending (the plan checks DNC twice).
Match on normalised name AND domain AND buyer email domain.
"""
from __future__ import annotations

from ..models import ClientRecords, Prospect
from . import Ctx, active, norm


def exclusion_reason(p: Prospect, client: ClientRecords) -> str | None:
    keys = {norm(p.company.name), norm(p.company.domain)}
    if p.buyer and p.buyer.business_email:
        keys |= {norm(p.buyer.business_email), norm(p.buyer.business_email.split("@")[-1])}
    keys.discard("")
    for label, lst in (("current client", client.current_clients), ("rival", client.rivals),
                       ("do-not-contact list", client.do_not_contact)):
        if keys & {norm(x) for x in lst}:
            return f"on {label}"
    return None


def run(ctx: Ctx, prospects: list[Prospect]) -> list[Prospect]:
    for p in active(prospects):
        if reason := exclusion_reason(p, ctx.client):
            p.exclude(reason)
    return prospects
