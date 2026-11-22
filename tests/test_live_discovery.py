"""Hiring-first live discovery (live_companies.discover_hiring_first) on a fake client."""
from datetime import date

from service.models import ClientRecords, JobRequest, Route
from service.sources import live_companies as lc

CFG = {"limits": {"max_candidates": 2},
       "triggers": {"window_days": 30},
       "route_fit": {"nearby": {"chakan": "pune", "bhiwandi": "mumbai", "sinnar": "nashik"}},
       "discovery": {"industries": ["manufacturing"], "headcount_min": 200,
                     "headcount_max": 5000, "job_search_limit": 50}}
JOB = JobRequest(job_id="t", text="x", route_filter=["pune", "mumbai"],
                 request_date=date(2026, 11, 22))
RECORDS = ClientRecords(name="3PL", routes=[
    Route(origin="pune", destination="mumbai", spare_trucks_per_week=12),
    Route(origin="nashik", destination="surat", spare_trucks_per_week=0)])


def _post(title, cid, day, name="Co"):
    return {"job_details": {"title": title},
            "company": {"basic_info": {"name": name, "crustdata_company_id": cid}},
            "metadata": {"date_added": f"2026-11-{day:02d}T10:00:00"}}


class Fake:
    def __init__(self, posts, companies):
        self.posts, self.companies, self.calls = posts, companies, []

    def call(self, operation, *, body=None, params=None):
        self.calls.append((operation, body))
        if operation == "job_search":
            data = {"job_listings": self.posts}
        elif operation == "company_identify":
            data = self.identify(body) if hasattr(self, "identify") else []
        else:
            data = {"companies": self.companies}
        return {"data": data, "url": f"https://api.example/{operation}",
                "retrieved_at": "2026-11-22T00:00:00Z"}


def _company(cid, name):
    return {"crustdata_company_id": cid, "basic_info": {"name": name, "primary_domain": f"{cid}.in"},
            "headcount": {"total": 900}, "taxonomy": {"professional_network_industries": ["Manufacturing"]}}


def test_job_cities_include_satellite_towns_of_served_route_only():
    assert lc.job_cities(JOB, RECORDS, CFG) == ["Pune", "Mumbai", "Chakan", "Bhiwandi"]


def test_hiring_first_ranks_by_posts_and_filters_by_company_index():
    posts = [_post("Warehouse Manager", 11, 20), _post("Logistics Executive", 11, 19),
             _post("Supply Chain Lead", 22, 21), _post("Warehouse Incharge", None, 21),
             _post("Admin - Visa & Travel for Logistics Industry", 33, 21),
             _post("Logistics Coordinator", 44, 18)]
    # index drops 44 (outside filters); returns in arbitrary order
    fake = Fake(posts, [_company(22, "Beta"), _company(11, "Alpha")])
    notes: list[str] = []
    refs = lc.discover_hiring_first(fake, JOB, RECORDS, cfg=CFG, notes=notes)

    assert [r.name for r in refs] == ["Alpha", "Beta"]          # 2 posts beats 1
    op, jbody = fake.calls[0]
    assert op == "job_search"
    conds = jbody["filters"]["conditions"]
    assert {"field": "metadata.date_added", "type": "=>", "value": "2026-10-23"} in conds
    assert conds[-1]["value"] == ["Pune", "Mumbai", "Chakan", "Bhiwandi"]
    op, cbody = next(c for c in fake.calls if c[0] == "company_search")
    ccond = cbody["filters"]["conditions"]
    assert {"field": "crustdata_company_id", "type": "in", "value": [11, 22, 44]} in ccond
    assert not any(c.get("field") == "locations.city" for c in ccond)
    assert any("1 posts without a company id" in n and "1 non-freight" in n for n in notes)


def test_hiring_first_no_posts_returns_empty_without_company_search():
    fake = Fake([], [])
    assert lc.discover_hiring_first(fake, JOB, RECORDS, cfg=CFG, notes=[]) == []
    assert [c[0] for c in fake.calls] == ["job_search"]


def test_hiring_first_can_drop_hq_country_and_headcount_cap():
    cfg = {**CFG, "discovery": {**CFG["discovery"],
                                "hiring_first": {"hq_country_filter": False, "headcount_max": None}}}
    fake = Fake([_post("Warehouse Manager", 11, 20)], [_company(11, "Alpha")])
    lc.discover_hiring_first(fake, JOB, RECORDS, cfg=cfg, notes=[])
    cbody = next(c for c in fake.calls if c[0] == "company_search")[1]
    fields = [(c.get("field"), c.get("type")) for c in cbody["filters"]["conditions"]]
    assert ("locations.country", "=") not in fields
    assert ("headcount.total", "=<") not in fields
    assert ("headcount.total", "=>") in fields


# ---- business email from person enrichment (live_buyers.verify_employment)
from service.models import Buyer  # noqa: E402
from service.sources import live_buyers as lb  # noqa: E402


class _EnrichFake:
    def __init__(self, emails):
        self.emails, self.calls = emails, []

    def call(self, operation, *, body=None, params=None):
        self.calls.append((operation, body))
        person = {"crustdata_person_id": 7, "metadata": {"updated_at": "2026-11-16T00:00:00"},
                  "experience": {"employment_details": {"current": [
                      {"title": "Head of Operations", "name": "Vertiv", "crustdata_company_id": 635084,
                       "start_date": "2020-09-01"}], "past": []}},
                  "contact": {"business_emails": self.emails}}
        return {"data": [{"match_status": "matched", "matches": [{"person_data": person}]}],
                "url": "https://api.example/person/enrich", "retrieved_at": "2026-11-22T00:00:00Z"}


def _buyer():
    return Buyer(name="K M", title="Head of Operations", profile_url="https://x/in/km", person_id="7",
                 employer_id="635084", employer_name="Vertiv")


def test_business_email_kept_only_when_tied_to_employer_id():
    fake = _EnrichFake([{"crustdata_company_id": 999, "email": "km@other.com", "status": "valid"},
                        {"crustdata_company_id": 635084, "email": "km@vertiv.com", "status": None}])
    out = lb.verify_employment(fake, _buyer())
    assert out.business_email == "km@vertiv.com"
    assert "contact.business_emails" in fake.calls[0][1]["fields"]
    assert not any("personal" in f or "phone" in f for f in fake.calls[0][1]["fields"])
    fact = out.facts[-1]
    assert fact.value == "km@vertiv.com" and "635084" in fact.sources[0].excerpt


def test_no_email_when_only_other_employers_listed():
    fake = _EnrichFake([{"crustdata_company_id": 999, "email": "km@other.com", "status": "valid"}])
    assert lb.verify_employment(fake, _buyer()).business_email is None


# ---- discovery posts kept and merged into step-4 triggers (LiveSources.triggers)
from service.budget import Budget  # noqa: E402
from service.ledger import Ledger  # noqa: E402
from service.sources import live_signals as lsig  # noqa: E402
from service.sources.base import SourceUnavailable  # noqa: E402
from service.sources.live import LiveSources  # noqa: E402


def _envelope(rows):
    return {"data": {"job_listings": rows}, "url": "https://api.example/job/search",
            "retrieved_at": "2026-11-22T00:00:00Z"}


def _row(title, cid, raw, url):
    return {"job_details": {"title": title, "url": url},
            "company": {"basic_info": {"name": "Eaton", "crustdata_company_id": cid}},
            "location": {"raw": raw}, "metadata": {"date_added": "2026-11-10T10:00:00"}}


def test_discovery_posts_come_first_and_survive_restart(tmp_path, monkeypatch):
    from service.models import CompanyRef
    led = Ledger(tmp_path / "l.sqlite")
    disc = _envelope([_row("Warehouse Lead", 6061963, "Pune, Maharashtra", "https://j/1"),
                      _row("Logistics Exec", 999, "Mumbai", "https://j/2")])
    abroad = lsig.map_job_listings(_envelope([
        _row("Warehouse Supervisor", 6061963, "Ohio", "https://j/3"),
        _row("Warehouse Lead", 6061963, "Pune, Maharashtra", "https://j/1")]),
        CompanyRef(name="Eaton", crustdata_id="6061963"))
    monkeypatch.setattr(lsig, "triggers", lambda client, company, since: abroad)
    eaton = CompanyRef(name="Eaton", crustdata_id="6061963")

    src = LiveSources(led, Budget(led, "job1", 100), client=object())
    src._save_posts(disc)
    first = src.triggers(eaton, date(2026, 10, 23))
    assert [t.summary for t in first] == ["Warehouse Lead job posted in Pune, Maharashtra",
                                          "Warehouse Supervisor job posted in Ohio"]  # no dup

    fresh = LiveSources(led, Budget(led, "job1", 100), client=object())   # after a restart
    assert [t.summary for t in fresh.triggers(eaton, date(2026, 10, 23))] == \
        [t.summary for t in first]


def test_company_search_outage_still_keeps_local_posts(tmp_path, monkeypatch):
    from service.models import CompanyRef
    led = Ledger(tmp_path / "l.sqlite")

    def down(client, company, since):
        raise SourceUnavailable("job_search 503")
    monkeypatch.setattr(lsig, "triggers", down)
    src = LiveSources(led, Budget(led, "job2", 100), client=object())
    src._save_posts(_envelope([_row("Warehouse Lead", 6061963, "Pune", "https://j/1")]))
    out = src.triggers(CompanyRef(name="Eaton", crustdata_id="6061963"), date(2026, 10, 23))
    assert len(out) == 1 and "Pune" in out[0].summary


class _PeopleFake:
    def __init__(self, profiles):
        self.profiles, self.calls = profiles, []

    def call(self, operation, *, body=None, params=None):
        self.calls.append((operation, body))
        return {"data": {"profiles": self.profiles}, "url": "https://api.example/person/search",
                "retrieved_at": "2026-11-22T00:00:00Z"}


def _profile(name, country, title="Head of Supply Chain"):
    return {"crustdata_person_id": hash(name) % 10000, "metadata": {"updated_at": "2026-11-16"},
            "basic_profile": {"name": name, "location": {"country": country}},
            "social_handles": {"professional_network_identifier": {"profile_url": f"https://x/{name}"}},
            "experience": {"employment_details": {"current": [
                {"title": title, "name": "L'Oreal", "crustdata_company_id": 1035606,
                 "start_date": "2024-01-01"}]}}}


def test_buyer_search_limited_to_lane_country_with_sourced_location(monkeypatch):
    from service.models import CompanyRef
    monkeypatch.setattr(lb, "_countries", lambda: ["India"])
    fake = _PeopleFake([_profile("Canada COO", "Canada"), _profile("Pune Head", "India")])
    out = lb.people(fake, CompanyRef(name="L'Oreal", crustdata_id="1035606"),
                    ["head of supply chain"])
    conds = fake.calls[0][1]["filters"]["conditions"]
    assert {"field": "basic_profile.location.country", "type": "in", "value": ["India"]} in conds
    assert [b.name for b in out] == ["Pune Head"]
    assert any("location.country = 'India'" in s.excerpt for f in out[0].facts for s in f.sources)


# ---- employer-name lookup for posts without a company id
def _noid(title, name, day=20, raw="Pune, Maharashtra", url=None):
    return {"job_details": {"title": title, "url": url or f"https://j/{name}/{day}"},
            "company": {"basic_info": {"name": name, "crustdata_company_id": None}},
            "location": {"raw": raw}, "metadata": {"date_added": f"2026-11-{day:02d}T10:00:00"}}


def _identify_answers(answers):
    def identify(body):
        return [{"matched_on": n, "match_type": "name",
                 "matches": [{"confidence_score": sc, "company_data": {
                     "crustdata_company_id": cid, "basic_info": {"name": nm, "primary_domain": f"{cid}.com"}}}
                     for cid, nm, sc in answers.get(n, [])]} for n in body["names"]]
    return identify


def test_name_lookup_resolves_confident_names_and_skips_agencies():
    posts = [_noid("Logistics Manager", "LANXESS", 20), _noid("Warehouse Lead", "LANXESS", 18),
             _noid("Supply Chain Exec", "Lookalike Foods", 19),
             _noid("Warehouse Executive", "SBS HR", 19)]
    fake = Fake(posts, [_company(77, "LANXESS")])
    fake.identify = _identify_answers({
        "LANXESS": [(77, "LANXESS", 0.95)],
        "Lookalike Foods": [(5, "Lookalike Foods Ltd", 0.80), (6, "Lookalike Food Co", 0.78)]})
    names: dict = {}
    notes: list[str] = []
    refs = lc.discover_hiring_first(fake, JOB, RECORDS, cfg={**CFG, "identity": {
        "min_score": 0.6, "lookalike_gap": 0.1}}, notes=notes, names_out=names)
    asked = next(b for op, b in fake.calls if op == "company_identify")["names"]
    assert "SBS HR" not in asked                                  # agency skipped, no credits
    assert set(names) == {"lanxess"}                              # lookalike left out
    assert [r.name for r in refs] == ["LANXESS"]
    cbody = next(b for op, b in fake.calls if op == "company_search")
    assert {"field": "crustdata_company_id", "type": "in", "value": [77]} in \
        cbody["filters"]["conditions"]
    assert any("1 of 2 names resolved" in n for n in notes)


def test_resolved_name_post_becomes_trigger_citing_identify():
    from service.models import CompanyRef
    fake = Fake([], [])
    fake.identify = _identify_answers({"LANXESS": [(77, "LANXESS", 0.95)]})
    names = lc.resolve_post_employers(fake, ["LANXESS"], {"identity": {}})
    env = {"data": {"job_listings": [_noid("Logistics Manager", "LANXESS", 20)]},
           "url": "https://api.example/job/search", "retrieved_at": "2026-11-22T00:00:00Z"}
    lanxess = CompanyRef(name="LANXESS", crustdata_id="77")
    assert lsig.map_job_listings(env, lanxess) == []              # no id: not matched alone
    (t,) = lsig.map_job_listings(env, lanxess, names)
    excerpts = " | ".join(s.excerpt for s in t.fact.sources)
    assert "matched_on = 'LANXESS'" in excerpts and "confidence_score = 0.95" in excerpts
    other = CompanyRef(name="LANXESS India", crustdata_id="78")
    assert lsig.map_job_listings(env, other, names) == []         # resolved to 77, not 78


def test_single_post_companies_are_not_cut_before_index_filters():
    posts = [_post("Warehouse Manager", 100 + i, 20) for i in range(36)]
    fake = Fake(posts, [_company(135, "Late Co")])
    lc.discover_hiring_first(fake, JOB, RECORDS, cfg={**CFG, "limits": {"max_candidates": 10}},
                             notes=[])
    cbody = next(b for op, b in fake.calls if op == "company_search")
    ids = next(c["value"] for c in cbody["filters"]["conditions"]
               if c.get("field") == "crustdata_company_id")
    assert len(ids) == 36 and 135 in ids
