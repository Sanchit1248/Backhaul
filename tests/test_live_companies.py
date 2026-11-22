"""Offline tests for sources/live_companies.py and steps s02/s03.

Replay tests read the saved smoke responses from $SMOKE_EVIDENCE_DIR (the local
`smoke/` directory with raw/T1_*.json, T2_enrich.json, T3_search_*.json). Raw
evidence stays out of Git, so those tests skip when the variable is unset.
The synthetic tests (empty, failure, budget, request shape) always run.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest

from service.budget import BudgetExceeded
from service.models import ClientRecords, CompanyRef, IdentityMatch, JobRequest, Prospect, Route
from service.sources import live_companies as lc
from service.sources.base import SourceUnavailable
from service.steps import s02_discover, s03_identity

SMOKE = Path(os.environ["SMOKE_EVIDENCE_DIR"]) if os.environ.get("SMOKE_EVIDENCE_DIR") else None
needs_smoke = pytest.mark.skipif(not (SMOKE and (SMOKE / "raw").is_dir()),
                                 reason="set SMOKE_EVIDENCE_DIR to the local smoke directory")
RETRIEVED = "2026-11-22T07:10:00Z"
URLS = {"company_search": "https://api.crustdata.com/company/search",
        "company_identify": "https://api.crustdata.com/company/identify",
        "company_enrich": "https://api.crustdata.com/company/enrich"}
CFG = {"identity": {"lookalike_gap": 0.10, "min_score": 0.60},
       "limits": {"max_candidates": 40},
       "discovery": {"industries": ["manufacturing", "fmcg", "automotive", "pharmaceuticals",
                                    "chemicals", "e-commerce", "retail"],
                     "headcount_min": 200, "headcount_max": 5000}}
JOB = JobRequest(job_id="t", text="x", request_date=date(2026, 11, 20))
RECORDS = ClientRecords(name="3PL", routes=[Route(origin="pune", destination="mumbai",
                                                  spare_trucks_per_week=12)])


def _raw(name: str):
    return json.loads((SMOKE / "raw" / name).read_text(encoding="utf-8"))


class ReplayClient:
    """Mock of CrustdataClient.call that answers from saved smoke responses."""

    def __init__(self, search_file: str = "T3_search_mumbai_projected.json"):
        self.search_file, self.calls = search_file, []

    def call(self, operation, *, body=None, params=None):
        self.calls.append((operation, body))
        if operation == "company_identify":
            if "domains" in body:
                rows = [e for e in _raw("T1_domains.json") if e["matched_on"] in body["domains"]]
            else:
                rows = [e for e in _raw("T1_names.json") if e["matched_on"] in body["names"]]
            data = rows
        elif operation == "company_enrich":
            ids = {str(i) for i in body["crustdata_company_ids"]}
            data = [e for e in _raw("T2_enrich.json") if e["matched_on"] in ids]
        elif operation == "company_search":
            data = _raw(self.search_file)
        else:
            raise AssertionError(operation)
        return {"data": data, "url": URLS[operation], "retrieved_at": RETRIEVED}


class StubClient:
    def __init__(self, data=None, exc: Exception | None = None):
        self.data, self.exc, self.calls = data, exc, []

    def call(self, operation, *, body=None, params=None):
        self.calls.append((operation, body))
        if self.exc:
            raise self.exc
        return {"data": self.data, "url": URLS[operation], "retrieved_at": RETRIEVED}


class Ctx:
    """Just enough of steps.Ctx for s02/s03."""

    def __init__(self, sources):
        self.sources, self.cfg, self.job, self.client, self.notes = sources, CFG, JOB, RECORDS, []

    def log(self, msg, **data):
        pass


class ReplaySources:
    def __init__(self, client):
        self.c = client

    def identify(self, company):
        return lc.identify(self.c, company)

    def discover(self, job, records):
        return lc.discover(self.c, job, records, cfg=CFG)


def resolve(company: CompanyRef, client=None) -> Prospect:
    p = Prospect(prospect_id="p", company=company)
    s03_identity.run(Ctx(ReplaySources(client or ReplayClient())), [p])
    return p


# ------------------------------------------------------------- replay: T1 ---

@needs_smoke
def test_haldiram_name_only_is_held_despite_score_gap():
    p = resolve(CompanyRef(name="Haldiram"))
    top = p.identity_candidates[:2]
    assert [m.score for m in top] == [0.95, 0.8]          # gap passes the 0.10 tie rule
    assert top[0].company.name == "Haldirams"              # 11-50 hospitality page
    assert p.status == "held" and "lookalike: name shared by" in p.reason
    assert p.cannot_verify and "registry" in p.cannot_verify[0].missing
    assert len(p.identity_candidates) == 25                # ranked matches preserved


@needs_smoke
def test_haldiram_shared_domain_is_a_three_way_tie():
    p = resolve(CompanyRef(name="Haldiram Snacks", domain="haldiram.com"))
    names = {m.company.name for m in p.identity_candidates}
    assert names == {"Haldiram Snacks Food Pvt. Ltd.", "HALDIRAM Ethnic Foods Pvt. Ltd.",
                     "Haldiram Marketing Pvt Ltd"}
    assert p.status == "held" and p.reason.startswith("lookalike: ")


@needs_smoke
def test_mahindra_name_and_domain_lookalikes_hold():
    by_name = resolve(CompanyRef(name="Mahindra"))
    assert [m.score for m in by_name.identity_candidates[:2]] == [1.0, 1.0]
    assert by_name.status == "held" and "lookalike" in by_name.reason

    by_domain = resolve(CompanyRef(name="Mahindra", domain="mahindra.com"))
    top = by_domain.identity_candidates
    # equal scores: the record whose primary domain is mahindra.com ranks first
    assert [(m.company.name, m.score) for m in top] == [("Mahindra Group", 1.0),
                                                       ("Tech Mahindra", 1.0)]
    assert by_domain.status == "held"


@needs_smoke
def test_sharma_trading_company_pune_stays_unresolved():
    p = resolve(CompanyRef(name="Sharma Trading Company Pune"))
    assert p.identity_candidates[0].score == 0.6
    assert p.status == "held" and p.company.crustdata_id is None
    assert not any("pune" in m.company.name.lower() for m in p.identity_candidates)


@needs_smoke
def test_exact_domain_resolves_and_keeps_discovery_metadata():
    p = resolve(CompanyRef(name="boAt", domain="boat-lifestyle.com", headcount=1763))
    assert p.active and p.company.crustdata_id == "628835"
    assert p.company.name == "boAt Lifestyle" and p.company.headcount == 1763


@needs_smoke
def test_honasa_name_only_resolves_without_shared_name_rivals():
    p = resolve(CompanyRef(name="Honasa Consumer"))
    assert p.active and p.company.crustdata_id == "1132667"
    assert p.company.domain == "honasa.in"


@needs_smoke
def test_unknown_domain_falls_back_to_name_and_holds_on_mismatch():
    client = ReplayClient()
    p = resolve(CompanyRef(name="Honasa Consumer", domain="mamaearth.example"), client)
    assert [op for op, _ in client.calls] == ["company_identify", "company_identify"]
    assert p.status == "held" and "domain mismatch" in p.reason


@needs_smoke
def test_discovered_id_must_match_identify():
    p = resolve(CompanyRef(name="Honasa", domain="honasa.in", crustdata_id="999"))
    assert p.status == "held" and "id mismatch" in p.reason


# ------------------------------------------------------------- replay: T2 ---

@needs_smoke
def test_boat_locations_cite_exact_fields():
    locs = lc.locations(ReplayClient(), CompanyRef(name="boAt Lifestyle", crustdata_id="628835"))
    got = [(loc.city, loc.kind) for loc in locs]
    assert got == [("DLF Ph-II", "hq"), ("Mumbai", "office"), ("Bangalore", "office")]
    hq, mumbai, blr = locs
    assert hq.source.field_path == "[0].matches[0].company_data.locations.city"
    assert "locations.headquarters = 'DLF Ph-II, Haryāna, India'" in hq.source.excerpt
    assert mumbai.source.field_path.endswith("locations.all_office_addresses[0]")
    assert mumbai.source.excerpt.endswith("= ', Mumbai, Maharashtra, 400093, IN'")
    assert all(loc.source.record_date is None for loc in locs)   # metadata.updated_at is null
    assert all(str(loc.source.retrieved_at).startswith("2026-11-22") for loc in locs)
    assert not any(loc.kind in ("plant", "warehouse") for loc in locs)


@needs_smoke
def test_honasa_city_not_in_hq_text_is_unknown_kind():
    locs = lc.locations(ReplayClient(), CompanyRef(name="Honasa", crustdata_id="1132667"))
    assert [(loc.city, loc.kind) for loc in locs] == [("Samaspur", "unknown")]


# ------------------------------------------------------------- replay: T3 ---

@needs_smoke
def test_discover_maps_search_rows():
    client = ReplayClient("T3_search_mumbai_projected.json")
    found = lc.discover(client, JOB, RECORDS, cfg=CFG)
    assert found[0].name == "Just In Time" and found[0].crustdata_id == "612059"
    assert found[0].domain == "justintime.in" and found[0].headcount == 494
    assert found[0].industry == "Retail"
    assert len(found) == 5 and not any(c.locations for c in found)


@needs_smoke
def test_discover_step_dedupes_and_caps():
    client = ReplayClient("T3_search_india_projected.json")
    ctx = Ctx(ReplaySources(client))
    ctx.cfg = {**CFG, "limits": {"max_candidates": 3}}
    out = s02_discover.run(ctx, [])
    assert [p.company.name for p in out] == ["Mivi", "Sanskar", "Just In Time"]


# ------------------------------------------------------------ synthetic ---

def test_search_body_uses_runtime_config_and_route():
    notes: list[str] = []
    body, extra = lc.search_body(JOB, RECORDS, CFG)
    notes += extra
    conds = {c["field"] + " " + c["type"]: c["value"] for c in body["filters"]["conditions"]}
    assert conds["locations.country ="] == "India"
    assert conds["headcount.total =>"] == 200 and conds["headcount.total =<"] == 5000
    assert conds["locations.city in"] == ["Pune", "Mumbai"]
    assert "Consumer Goods" in conds["basic_info.industries in"]
    assert "headcount.growth_percent.3m >" not in conds        # smoke-only filter dropped
    assert body["limit"] == 40
    assert any("pharmaceuticals" in n and "e-commerce" in n for n in notes)


def test_route_filter_narrows_cities():
    recs = ClientRecords(name="3PL", routes=[
        Route(origin="pune", destination="mumbai", spare_trucks_per_week=12),
        Route(origin="nashik", destination="surat", spare_trucks_per_week=4),
        Route(origin="delhi", destination="jaipur", spare_trucks_per_week=0)])
    job = JOB.model_copy(update={"route_filter": ["nashik"]})
    assert lc.route_cities(JOB, recs) == ["pune", "mumbai", "nashik", "surat"]
    assert lc.route_cities(job, recs) == ["nashik", "surat"]


def test_successful_empty_results_are_empty_lists():
    assert lc.discover(StubClient({"companies": [], "total_count": 0}), JOB, RECORDS,
                       cfg=CFG) == []
    assert lc.identify(StubClient([]), CompanyRef(name="Nobody", domain="nobody.example")) == []
    assert lc.locations(StubClient([{"matched_on": "1", "matches": []}]),
                        CompanyRef(name="X", crustdata_id="1")) == []


@pytest.mark.parametrize("fn,args", [
    (lc.discover, (JOB, RECORDS)),
    (lc.identify, (CompanyRef(name="X"),)),
    (lc.locations, (CompanyRef(name="X", crustdata_id="1"),)),
])
def test_provider_failure_and_bad_shapes_raise_source_unavailable(fn, args):
    kw = {"cfg": CFG} if fn is lc.discover else {}
    with pytest.raises(SourceUnavailable):
        fn(StubClient(exc=SourceUnavailable("503")), *args, **kw)
    with pytest.raises(SourceUnavailable):
        fn(StubClient({"error": {"type": "invalid_request"}}), *args, **kw)
    with pytest.raises(SourceUnavailable):
        fn(StubClient("not json"), *args, **kw)


@pytest.mark.parametrize("fn,args", [
    (lc.discover, (JOB, RECORDS)),
    (lc.identify, (CompanyRef(name="X"),)),
    (lc.locations, (CompanyRef(name="X", crustdata_id="1"),)),
])
def test_budget_exceeded_propagates(fn, args):
    kw = {"cfg": CFG} if fn is lc.discover else {}
    with pytest.raises(BudgetExceeded):
        fn(StubClient(exc=BudgetExceeded("cap")), *args, **kw)


def test_steps_turn_outage_into_cannot_verify_and_keep_budget_stop():
    class Down:
        def discover(self, job, records):
            raise SourceUnavailable("search 503")

        def identify(self, company):
            raise SourceUnavailable("identify 503")

    ctx = Ctx(Down())
    assert s02_discover.run(ctx, []) == []
    assert ctx.notes and ctx.notes[0].startswith("cannot verify: discovery source unavailable")

    p = Prospect(prospect_id="p", company=CompanyRef(name="X"))
    s03_identity.run(Ctx(Down()), [p])
    assert p.status == "cannot_verify" and p.reason.startswith("source unavailable")

    class Broke:
        def identify(self, company):
            raise BudgetExceeded("cap")

    with pytest.raises(BudgetExceeded):
        s03_identity.run(Ctx(Broke()), [Prospect(prospect_id="p", company=CompanyRef(name="X"))])


def test_empty_identify_is_no_match_not_outage():
    class Empty:
        def identify(self, company):
            return []

    p = Prospect(prospect_id="p", company=CompanyRef(name="X"))
    s03_identity.run(Ctx(Empty()), [p])
    assert p.status == "cannot_verify" and p.reason == "no confident identity match"


def test_locations_requires_resolved_id():
    with pytest.raises(ValueError):
        lc.locations(StubClient([]), CompanyRef(name="X"))


def test_office_address_parsing_is_strict():
    assert lc._office_city(", Mumbai, Maharashtra, 400093, IN") == "Mumbai"
    assert lc._office_city(", Bangalore, IN") == "Bangalore"
    assert lc._office_city("Plot 4, MIDC") is None             # no country tail
    assert lc._office_city(", 400093, IN") is None


def test_identity_tie_keeps_corroboration_order():
    ms = [IdentityMatch(company=CompanyRef(name="A"), score=1.0),
          IdentityMatch(company=CompanyRef(name="B"), score=1.0)]

    class S:
        def identify(self, company):
            return ms

    p = Prospect(prospect_id="p", company=CompanyRef(name="A", domain="a.example"))
    s03_identity.run(Ctx(S()), [p])
    assert [m.company.name for m in p.identity_candidates] == ["A", "B"]
    assert p.status == "held"
