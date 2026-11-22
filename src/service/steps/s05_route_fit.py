"""Step 5: score route fit. OWNER: B.

Match the prospect's plant/warehouse/office cities to client routes with spare
capacity. Score is a rule-based ESTIMATE, always with a written reason.
  * plant/warehouse on a route end point with spare trucks -> full
  * only hq/office/unknown known on a route end point     -> partial
  * no location on any route                              -> none -> drop
TODO(B): honour job.route_filter; "near" cities (distance table) -> partial.
"""
from __future__ import annotations

from ..models import Fact, Prospect, RouteFit
from . import Ctx, active


def run(ctx: Ctx, prospects: list[Prospect]) -> list[Prospect]:
    cfg = ctx.cfg["route_fit"]
    routes = [r for r in ctx.client.routes if r.spare_trucks_per_week > 0]
    if ctx.job.route_filter:
        wanted = set(ctx.job.route_filter)
        routes = [r for r in routes if {r.origin, r.destination} & wanted] or routes
    for p in active(prospects):
        p.company.locations = p.company.locations or ctx.sources.locations(p.company)
        best: RouteFit | None = None
        for loc in p.company.locations:
            for r in routes:
                if loc.city.lower() not in (r.origin, r.destination):
                    continue
                full = loc.kind in ("plant", "warehouse")
                fit = RouteFit(
                    level="full" if full else "partial",
                    score=cfg["full_score"] if full else cfg["partial_score"],
                    reason=(f"{loc.kind} in {loc.city.title()}; spare capacity on "
                            f"{r.origin.title()} to {r.destination.title()} "
                            f"({r.spare_trucks_per_week} trucks/week)"),
                    route=r,
                    facts=[Fact(claim=f"{p.company.name} has a {loc.kind} in {loc.city.title()}",
                                value=loc.city, sources=[loc.source])],
                )
                if best is None or fit.score > best.score:
                    best = fit
        if best is None or best.score < cfg["min_score_to_contact"]:
            p.route_fit = best or RouteFit(level="none", score=0.0,
                                           reason="no known location on a route with spare capacity")
            p.drop("no route fit: " + p.route_fit.reason)
        else:
            p.route_fit = best
    return prospects
