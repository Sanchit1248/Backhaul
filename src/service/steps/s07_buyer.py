"""Step 7: find the buyer and confirm they still work there. OWNER: A.

Rules:
  * never match on name alone: employer_id must equal the company's crustdata_id;
    a company with no crustdata_id is held without searching
  * the buyer's facts are the source's own evidence (exact response fields); this step
    never writes a citation of its own
  * no business email                           -> try next (no personal emails, ever;
                                                   checked before the paid recheck)
  * role_end set (source shows the role ended)  -> has left -> try next person
  * current employer differs after the recheck  -> name match only -> try next
  * current title is no longer a buyer title    -> try next (same-company title change)
  * no fresh employment record (provider down,  -> verification unavailable: record
    no stable identifier, or no match)             CannotVerify, try next
  * nobody left                                 -> hold "no verified buyer" before drafting

Indexed employment (Crustdata search + enrichment recheck) is labelled as indexed in the
facts. Candidates that fail keep their sourced work facts in buyers_rejected.
"""
from __future__ import annotations

from ..models import Buyer, CannotVerify, Prospect
from ..sources.base import SourceUnavailable
from ..sources.live_buyers import (
    EVIDENCE_DEPARTED,
    EVIDENCE_MISMATCH,
    EVIDENCE_RECHECKED,
    title_matches,
)
from . import Ctx, active

NO_BUYER = "no verified buyer"
_TRIED = ["crustdata person_search", "crustdata person_enrich"]
_NO_BATCH = "batch work-history verification is not enabled on this key"


def _reject(p: Prospect, b: Buyer, outcome: str, reason: str) -> None:
    """Keep the sourced candidate in the result; work fields only."""
    p.buyers_rejected.append({
        "name": b.name, "title": b.title, "employer_id": b.employer_id,
        "employer_name": b.employer_name,
        "role_start": str(b.role_start) if b.role_start else None,
        "role_end": str(b.role_end) if b.role_end else None,
        "outcome": outcome, "reason": reason,
        "facts": [f.model_dump(mode="json") for f in b.facts],
    })


def _has(b: Buyer, label: str) -> bool:
    return any(f.value == label for f in b.facts)


def _unverified(p: Prospect, b: Buyer, why: str) -> None:
    p.unverifiable(CannotVerify(
        question=f"Does {b.name} still hold {b.title!r} at {p.company.name}?",
        missing=f"fresh employment record ({why}; {_NO_BATCH})", tried=_TRIED))
    _reject(p, b, "verification_unavailable", f"employment verification unavailable: {why}")


def run(ctx: Ctx, prospects: list[Prospect]) -> list[Prospect]:
    cfg = ctx.cfg["buyer"]
    titles = cfg["titles"]
    for p in active(prospects):
        emp = p.company.crustdata_id
        if not emp:
            p.unverifiable(CannotVerify(
                question=f"Who is the buyer at {p.company.name}?",
                missing="Crustdata company id to match the buyer's employer (no name-only match)",
                tried=[]))
            p.hold(NO_BUYER)
            continue
        try:
            people = ctx.sources.people(p.company, titles)[: cfg["max_candidates"]]
        except SourceUnavailable as e:
            p.unverifiable(CannotVerify(question=f"Who is the buyer at {p.company.name}?",
                                        missing=f"buyer search: source unavailable ({e})",
                                        tried=_TRIED[:1]))
            p.hold(NO_BUYER)
            continue

        for cand in people:
            if cand.employer_id != emp:
                _reject(p, cand, "employer_mismatch", "name match only: employer is "
                        f"{cand.employer_name or cand.employer_id or 'missing'}")
                continue
            if not cand.business_email:
                _reject(p, cand, "missing_business_email",
                        "no business email from the source (employment not rechecked)")
                continue
            try:
                b = ctx.sources.verify_employment(cand)
            except SourceUnavailable as e:
                _unverified(p, cand, f"source unavailable ({e})")
                continue
            if b.role_end:
                _reject(p, b, "departed", f"left on {b.role_end}"
                        + (" (indexed work history)" if _has(b, EVIDENCE_DEPARTED) else ""))
                continue
            if b.employer_id != emp:
                _reject(p, b, "employer_mismatch", "name match only: employer is "
                        f"{b.employer_name or b.employer_id or 'missing'}")
                continue
            if not title_matches(b.title, titles):
                _reject(p, b, "title_mismatch",
                        f"current title {b.title!r} is not a configured buyer title")
                continue
            if not _has(b, EVIDENCE_RECHECKED):
                why = ("no stable identifier (profile URL) to recheck" if not b.profile_url
                       else "enrichment returned no record for this person"
                       if not _has(b, EVIDENCE_MISMATCH) else "recheck inconclusive")
                _unverified(p, b, why)
                continue
            p.buyer = b
            break
        if p.buyer is None:
            p.hold(NO_BUYER)
    return prospects
