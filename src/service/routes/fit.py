"""route_fit(company_sites) -> matched lanes, score, reason, or "partial".

Same rules as pipeline step 5 (config route_fit.*), callable on its own so signals,
chat and the map can ask "does this company sit on one of our lanes?".
"""
from __future__ import annotations

from ..config import settings
from ..models import Location, Prospect, Route, RouteFit
from ..steps.s05_route_fit import _fit


def route_fit(company_name: str, company_sites: list[Location], lanes: list[Route]) -> dict:
    """Best fit + every matching lane. level: full | partial | none."""
    cfg = settings()["route_fit"]
    stub = Prospect(prospect_id="route-fit", company={"name": company_name})
    fits: list[RouteFit] = [f for loc in company_sites for r in lanes
                            if (f := _fit(stub, loc, r, cfg)) is not None]
    fits.sort(key=lambda f: f.score, reverse=True)
    if not fits:
        known = ", ".join(sorted({s.city for s in company_sites})) or "no locations"
        return {"level": "none", "score": 0.0, "matched": [],
                "reason": f"no site on a lane in scope (known: {known})"}
    best = fits[0]
    return {"level": best.level, "score": best.score, "reason": best.reason,
            "contact_ok": best.score >= cfg["min_score_to_contact"],
            "matched": [{"lane": f"{f.route.origin}-{f.route.destination}", "level": f.level,
                         "score": f.score, "reason": f.reason,
                         "facts": [x.model_dump(mode="json") for x in f.facts]} for f in fits]}
