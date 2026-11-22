"""Step 5: score route fit. OWNER: B.

Match the prospect's plant/warehouse/office cities to the lanes in scope. The
score is a rule-based ESTIMATE, always with a written reason.

Lanes in scope:
  * request names two cities ("Pune to Mumbai") -> exactly that lane, either direction;
    capacity from the request if it states one, else from the client records; if the
    records have no such lane, the lane is built from the request
  * request names one city                      -> client lanes touching it
  * request names none                          -> every client lane with spare capacity

Levels (config route_fit.*):
  * plant/warehouse in a lane end point           -> full    (full_score)
  * plant/warehouse in a satellite town (nearby:) -> partial (nearby_score)
  * only hq/office/unknown location matches       -> partial (partial_score)
  * nothing matches                               -> none -> drop
"""
from __future__ import annotations

from ..models import Fact, JobRequest, Location, Prospect, Route, RouteFit
from . import Ctx, active, norm


def lanes_in_scope(job: JobRequest, client_routes: list[Route]) -> list[Route]:
    routes = list(client_routes)
    wanted = [norm(c) for c in job.route_filter]
    if len(wanted) >= 2:
        a, b = wanted[0], wanted[1]
        lane = [r for r in routes if {r.origin, r.destination} == {a, b}]
        if not lane:
            lane = [Route(origin=a, destination=b, spare_trucks_per_week=0)]
        if job.spare_trucks_per_week:
            lane = [r.model_copy(update={"spare_trucks_per_week": job.spare_trucks_per_week})
                    for r in lane]
        routes = lane
    elif len(wanted) == 1:
        routes = [r for r in routes if wanted[0] in (r.origin, r.destination)]
    return [r for r in routes if r.spare_trucks_per_week > 0]


def _fit(p: Prospect, loc: Location, r: Route, cfg: dict) -> RouteFit | None:
    city = norm(loc.city)
    hub = cfg.get("nearby", {}).get(city)
    if city in (r.origin, r.destination):
        on_lane, where = True, loc.city.title()
    elif hub in (r.origin, r.destination):
        on_lane, where = False, f"{loc.city.title()} (near {hub.title()})"
    else:
        return None
    site = loc.kind in ("plant", "warehouse")
    if site and on_lane:
        level, score = "full", cfg["full_score"]
    elif site:
        level, score = "partial", cfg["nearby_score"]
    else:
        level, score = "partial", cfg["partial_score"]
    return RouteFit(
        level=level, score=score, route=r,
        reason=(f"{loc.kind} in {where}; spare capacity on {r.origin.title()} to "
                f"{r.destination.title()} ({r.spare_trucks_per_week} trucks/week)"
                + ("" if site else "; only head office/office location known")),
        facts=[Fact(claim=f"{p.company.name} has a {loc.kind} in {loc.city.title()}",
                    value=loc.city, sources=[loc.source])],
    )


def run(ctx: Ctx, prospects: list[Prospect]) -> list[Prospect]:
    cfg = ctx.cfg["route_fit"]
    routes = lanes_in_scope(ctx.job, ctx.client.routes)
    if not routes:
        ctx.notes.append("no lane in scope has spare capacity; nothing to fit")
    for p in active(prospects):
        p.company.locations = p.company.locations or ctx.sources.locations(p.company)
        fits = [f for loc in p.company.locations for r in routes
                if (f := _fit(p, loc, r, cfg)) is not None]
        best = max(fits, key=lambda f: f.score, default=None)
        if best is None or best.score < cfg["min_score_to_contact"]:
            p.route_fit = best or RouteFit(
                level="none", score=0.0,
                reason="no known location on a lane with spare capacity"
                       + (f" (known: {', '.join(sorted({loc.city for loc in p.company.locations}))})"
                          if p.company.locations else " (no locations found)"))
            p.drop("no route fit: " + p.route_fit.reason)
        else:
            p.route_fit = best
    return prospects
