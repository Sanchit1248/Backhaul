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
