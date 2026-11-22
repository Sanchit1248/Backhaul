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
    op, cbody = fake.calls[1]
    assert op == "company_search"
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
    fields = [(c.get("field"), c.get("type")) for c in fake.calls[1][1]["filters"]["conditions"]]
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
