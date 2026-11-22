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
  * a fresh hiring post located on the lane       -> partial (partial_score), "site type
    not stated"; cites the job post. That trigger is moved first so the email cites
    the post on the lane, not one abroad.
  * nothing matches                               -> none -> drop
A failed locations lookup is "cannot verify" for that prospect only (unless a job post
on the lane already settles it).
"""
from __future__ import annotations

import re

from ..models import CannotVerify, Fact, JobRequest, Location, Prospect, Route, RouteFit
from ..sources.base import SourceUnavailable
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


_LOC_RAW = re.compile(r"location\.raw = '([^']+)'")


def _post_fits(p: Prospect, routes: list[Route], cfg: dict) -> list[tuple[RouteFit, int]]:
    """Route fits from hiring posts whose stated location is a lane end or a satellite town."""
    out: list[tuple[RouteFit, int]] = []
    for i, t in enumerate(p.triggers):
        if t.type != "hiring":
            continue
        src = next((s for s in t.fact.sources if _LOC_RAW.search(s.excerpt)), None)
        if src is None:
            continue
        raw = _LOC_RAW.search(src.excerpt).group(1)
        for part in (x.strip() for x in raw.split(",")):
            city = norm(part)
            hub = cfg.get("nearby", {}).get(city)
            for r in routes:
                if city in (r.origin, r.destination):
                    where = part.title()
                elif hub in (r.origin, r.destination):
                    where = f"{part.title()} (near {hub.title()})"
                else:
                    continue
                when = f" on {t.event_date.isoformat()}" if t.event_date else ""
                out.append((RouteFit(
                    level="partial", score=cfg["partial_score"], route=r,
                    reason=(f"hiring for a {t.fact.value} role in {where} (job post{when}); "
                            f"spare capacity on {r.origin.title()} to {r.destination.title()} "
                            f"({r.spare_trucks_per_week} trucks/week); site type not stated"),
                    facts=[Fact(claim=f"{p.company.name} posted a {t.fact.value} job located "
                                      f"in {raw}{when}", value=raw, sources=[src])]), i))
                break
    return out


def run(ctx: Ctx, prospects: list[Prospect]) -> list[Prospect]:
    cfg = ctx.cfg["route_fit"]
    routes = lanes_in_scope(ctx.job, ctx.client.routes)
    if not routes:
        ctx.notes.append("no lane in scope has spare capacity; nothing to fit")
    for p in active(prospects):
        post_fits = _post_fits(p, routes, cfg)
        if not p.company.locations:
            try:
                p.company.locations = ctx.sources.locations(p.company)
            except SourceUnavailable as e:
                cv = CannotVerify(question=f"Where are {p.company.name}'s sites?",
                                  missing="source unavailable: company locations",
                                  tried=[f"crustdata company enrich ({e})"])
                p.unverifiable(cv, stop=not post_fits)
                if not post_fits:
                    continue
        fits = [(f, None) for loc in p.company.locations for r in routes
                if (f := _fit(p, loc, r, cfg)) is not None] + post_fits
        best, trig = max(fits, key=lambda ft: ft[0].score, default=(None, None))
        if best is None or best.score < cfg["min_score_to_contact"]:
            p.route_fit = best or RouteFit(
                level="none", score=0.0,
                reason="no known location on a lane with spare capacity"
                       + (f" (known: {', '.join(sorted({loc.city for loc in p.company.locations}))})"
                          if p.company.locations else " (no locations found)"))
            p.drop("no route fit: " + p.route_fit.reason)
        else:
            p.route_fit = best
            if trig is not None:  # cite the post on the lane first
                p.triggers.insert(0, p.triggers.pop(trig))
    return prospects
