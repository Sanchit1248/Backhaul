"""Step 3: resolve identity -> one Crustdata company id, or hold as lookalike. OWNER: A.

Rules (config identity.*):
  * no match, or best score < min_score        -> cannot verify identity
  * top-2 scores within lookalike_gap           -> hold "lookalike: <names>"
  * we had a domain and the best match's differs -> hold "lookalike: domain mismatch"
TODO(A): MCA/GST legal-name fallback when identify is ambiguous.
"""
from __future__ import annotations

from ..models import CannotVerify, Prospect
from . import Ctx, active, norm


def run(ctx: Ctx, prospects: list[Prospect]) -> list[Prospect]:
    cfg = ctx.cfg["identity"]
    for p in active(prospects):
        matches = sorted(ctx.sources.identify(p.company), key=lambda m: -m.score)
        p.identity_candidates = matches
        if not matches or matches[0].score < cfg["min_score"]:
            p.unverifiable(CannotVerify(question=f"Which company is {p.company.name}?",
                                        missing="no confident identity match",
                                        tried=["crustdata company identify"]), stop=True)
            continue
        if len(matches) > 1 and matches[0].score - matches[1].score < cfg["lookalike_gap"]:
            p.hold("lookalike: " + " / ".join(m.company.name for m in matches[:3]))
            continue
        best = matches[0].company
        if p.company.domain and best.domain and norm(p.company.domain) != norm(best.domain):
            p.hold(f"lookalike: domain mismatch ({p.company.domain} vs {best.domain})")
            continue
        p.company = best.model_copy(update={"locations": p.company.locations})
    return prospects
