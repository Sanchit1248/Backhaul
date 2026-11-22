"""Step 3: resolve identity -> one Crustdata company id, or hold as lookalike. OWNER: A.

Rules (config identity.*), applied in order:
  * identify source failed                         -> cannot verify: source unavailable
  * no match, or best score < min_score            -> cannot verify identity
  * top-2 scores within lookalike_gap              -> hold "lookalike: <names>"
  * name-only lookup and another candidate above
    min_score carries the same name                -> hold "lookalike: <names>"
  * discovery gave an id and the best match's differs -> hold "lookalike: id mismatch"
  * we had a domain and the best match's differs   -> hold "lookalike: domain mismatch"
Every lookalike hold also records what would settle it (registry or corroborated
domain). Ranked matches are kept in p.identity_candidates for review.
TODO(A): MCA/GST legal-name fallback when identify is ambiguous.
"""
from __future__ import annotations

import re

from ..models import CannotVerify, IdentityMatch, Prospect
from ..sources.base import SourceUnavailable
from . import Ctx, active, norm


def _name(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", norm(s)).strip()


def _shares_name(query: str, other: str) -> bool:
    q, o = _name(query), _name(other)
    return bool(q and o) and (q in o or o in q)


def _hold_lookalike(p: Prospect, reason: str, matches: list[IdentityMatch]) -> None:
    p.hold(reason)
    p.unverifiable(CannotVerify(
        question=f"Which legal entity is {p.company.name}?",
        missing="registry record (MCA/GST) or a corroborated company domain to separate "
                + " / ".join(m.company.name for m in matches[:3]),
        tried=["crustdata company identify"]))


def run(ctx: Ctx, prospects: list[Prospect]) -> list[Prospect]:
    cfg = ctx.cfg["identity"]
    for p in active(prospects):
        try:
            found = ctx.sources.identify(p.company)
        except SourceUnavailable as e:
            p.unverifiable(CannotVerify(question=f"Which company is {p.company.name}?",
                                        missing="source unavailable: crustdata company identify",
                                        tried=[f"crustdata company identify ({e})"]), stop=True)
            continue
        # stable sort keeps the source's corroboration order inside equal scores
        matches = sorted(found, key=lambda m: -m.score)
        p.identity_candidates = matches
        if not matches or matches[0].score < cfg["min_score"]:
            p.unverifiable(CannotVerify(question=f"Which company is {p.company.name}?",
                                        missing="no confident identity match",
                                        tried=["crustdata company identify"]), stop=True)
            continue
        if len(matches) > 1 and matches[0].score - matches[1].score < cfg["lookalike_gap"]:
            _hold_lookalike(p, "lookalike: " + " / ".join(m.company.name for m in matches[:3]),
                            matches)
            continue
        best = matches[0].company
        if not p.company.domain and not p.company.crustdata_id:
            rivals = [m for m in matches[1:] if m.score >= cfg["min_score"]
                      and _shares_name(p.company.name, m.company.name)]
            if rivals:
                _hold_lookalike(p, "lookalike: name shared by "
                                + " / ".join(m.company.name for m in [matches[0], *rivals][:3]),
                                [matches[0], *rivals])
                continue
        if p.company.crustdata_id and best.crustdata_id \
                and str(p.company.crustdata_id) != str(best.crustdata_id):
            _hold_lookalike(p, f"lookalike: id mismatch ({p.company.crustdata_id} vs "
                               f"{best.crustdata_id} {best.name})", matches)
            continue
        if p.company.domain and best.domain and norm(p.company.domain) != norm(best.domain):
            _hold_lookalike(p, f"lookalike: domain mismatch ({p.company.domain} vs {best.domain})",
                            matches)
            continue
        p.company = best.model_copy(update={
            "locations": p.company.locations,
            "industry": best.industry or p.company.industry,
            "headcount": best.headcount if best.headcount is not None else p.company.headcount,
        })
    return prospects
