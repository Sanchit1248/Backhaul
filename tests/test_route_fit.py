"""Route-fit rules: exact lane from the request, nearby towns, HQ-only partial."""
from datetime import date

from service.config import settings
from service.models import CompanyRef, JobRequest, Location, Prospect, Route, Source
from service.steps.s05_route_fit import _fit, lanes_in_scope

ROUTES = [Route(origin="pune", destination="mumbai", spare_trucks_per_week=12),
          Route(origin="nashik", destination="mumbai", spare_trucks_per_week=6),
          Route(origin="pune", destination="nagpur", spare_trucks_per_week=0)]
SRC = Source(url="https://x.example", kind="web_page", excerpt="plant at ...")


def job(route, trucks=None):
    return JobRequest(job_id="j", text="", route_filter=route, spare_trucks_per_week=trucks,
                      request_date=date(2026, 11, 20))


def test_two_cities_means_exact_lane_either_direction():
    assert [(r.origin, r.destination) for r in lanes_in_scope(job(["mumbai", "pune"]), ROUTES)] \
        == [("pune", "mumbai")]


def test_request_capacity_overrides_records():
    (lane,) = lanes_in_scope(job(["pune", "mumbai"], trucks=20), ROUTES)
    assert lane.spare_trucks_per_week == 20


def test_unknown_lane_built_from_request():
    (lane,) = lanes_in_scope(job(["pune", "goa"], trucks=4), ROUTES)
    assert (lane.origin, lane.destination, lane.spare_trucks_per_week) == ("pune", "goa", 4)


def test_unknown_lane_without_capacity_is_out_of_scope():
    assert lanes_in_scope(job(["pune", "goa"]), ROUTES) == []


def test_one_city_and_no_filter_skip_zero_capacity():
    assert {r.destination for r in lanes_in_scope(job(["pune"]), ROUTES)} == {"mumbai"}
    assert len(lanes_in_scope(job([]), ROUTES)) == 2


def fit(city, kind):
    p = Prospect(prospect_id="p", company=CompanyRef(name="Acme"))
    return _fit(p, Location(city=city, kind=kind, source=SRC), ROUTES[0],
                settings()["route_fit"])


def test_levels():
    full, near, hq = fit("Pune", "plant"), fit("Chakan", "warehouse"), fit("Pune", "hq")
    assert (full.level, near.level, hq.level) == ("full", "partial", "partial")
    assert full.score > near.score > hq.score
    assert "Chakan (near Pune)" in near.reason
    assert "only head office" in hq.reason
    assert fit("Goa", "plant") is None
    assert full.facts[0].sources == [SRC]


# ---- route fit from a hiring post on the lane (live: MNCs whose enriched office is abroad)
from types import SimpleNamespace  # noqa: E402

from service.models import Fact, Status, Trigger  # noqa: E402
from service.sources.base import SourceUnavailable  # noqa: E402
from service.steps import s05_route_fit  # noqa: E402


def _post(title, raw, day):
    src = Source(url=f"https://jobs.example/{day}", kind="api_response",
                 excerpt=f"job_details.title = {title!r}; location.raw = {raw!r}; "
                         f"metadata.date_added = '2026-11-{day:02d}T10:00:00'",
                 record_date=date(2026, 11, day))
    return Trigger(type="hiring", summary=f"{title} job posted in {raw}",
                   event_date=date(2026, 11, day),
                   fact=Fact(claim=f"Acme posted a {title} job", value=title, sources=[src]))


class _Src:
    def __init__(self, locs=None, exc=None):
        self.locs, self.exc = locs or [], exc

    def locations(self, company):
        if self.exc:
            raise self.exc
        return self.locs


def _run(prospect, sources):
    ctx = SimpleNamespace(cfg=settings(), job=job(["pune", "mumbai"], trucks=12),
                          client=SimpleNamespace(routes=ROUTES), sources=sources, notes=[])
    return s05_route_fit.run(ctx, [prospect])[0]


def test_hiring_post_on_lane_gives_partial_fit_and_is_cited_first():
    abroad, mumbai = _post("Logistics Supervisor", "Hannibal, MO 63401", 13), \
        _post("Logistics Manager", "Powai, Mumbai, Maharashtra", 3)
    p = Prospect(prospect_id="p", company=CompanyRef(name="Acme"), triggers=[abroad, mumbai])
    office = Location(city="Minneapolis", kind="hq", source=SRC)
    out = _run(p, _Src([office]))
    assert out.status == Status.ACTIVE and out.route_fit.level == "partial"
    assert "Mumbai" in out.route_fit.reason and "site type not stated" in out.route_fit.reason
    assert out.route_fit.facts[0].sources == [mumbai.fact.sources[0]]
    assert out.triggers[0] is not abroad and "Mumbai" in out.triggers[0].summary


def test_locations_outage_is_cannot_verify_for_that_prospect_only():
    p = Prospect(prospect_id="p", company=CompanyRef(name="Acme"),
                 triggers=[_post("Warehouse Lead", "Columbus, IN", 11)])
    out = _run(p, _Src(exc=SourceUnavailable("ConnectError")))
    assert out.status == Status.CANNOT_VERIFY
    assert out.cannot_verify[0].missing == "source unavailable: company locations"


def test_locations_outage_with_post_on_lane_still_fits():
    p = Prospect(prospect_id="p", company=CompanyRef(name="Acme"),
                 triggers=[_post("Supply Chain Planner", "Pune, Maharashtra", 2)])
    out = _run(p, _Src(exc=SourceUnavailable("ConnectError")))
    assert out.status == Status.ACTIVE and out.route_fit.level == "partial"
    assert out.cannot_verify and out.cannot_verify[0].label == "cannot verify"


def test_on_lane_post_merged_into_event_is_promoted_to_lead():
    abroad, pune = _post("Warehouse Supervisor", "Ciudad Juarez, Chih.", 12), \
        _post("Logistics Lead", "Chakan, Pune, Maharashtra", 11)
    abroad.extra_sources = list(pune.fact.sources)          # step 4 folded them into one event
    p = Prospect(prospect_id="p", company=CompanyRef(name="Acme"), triggers=[abroad])
    out = _run(p, _Src([Location(city="Austin", kind="hq", source=SRC)]))
    assert out.status == Status.ACTIVE and "Chakan" in out.route_fit.reason
    lead = out.triggers[0]
    assert lead.fact.sources == pune.fact.sources and "Chakan" in lead.summary
    assert lead.fact.value == "Logistics Lead" and lead.event_date == date(2026, 11, 11)
    assert abroad.fact.sources[0] in lead.extra_sources      # nothing dropped


def test_site_fit_still_leads_with_local_post():
    abroad, pune = _post("Warehouse Coordinator", "Pelzer, SC", 14), \
        _post("Warehouse Lead", "Pune, Maharashtra", 9)
    p = Prospect(prospect_id="p", company=CompanyRef(name="Acme"), triggers=[abroad, pune])
    office = Location(city="Pune", kind="office", source=SRC)
    out = _run(p, _Src([office]))
    assert out.status == Status.ACTIVE
    assert "Pune" in out.triggers[0].summary
