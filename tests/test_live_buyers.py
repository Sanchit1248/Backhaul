"""Offline tests for the live buyer lookup (sources/live_buyers.py) and step 7.

No network: client.call is mocked. Two kinds of data:
  * REPLAY: the saved smoke responses T5 (person search) and T6 (person enrichment).
    Raw person responses stay out of Git, so these tests read them from
    $CRUSTDATA_SMOKE_DIR (default: ../checking_api/smoke beside the repo) and skip if absent.
  * SYNTHETIC controls (fictional people, labelled synthetic) where the saved evidence
    cannot show a case: a business email (the key has no contact enrichment), a
    departure, an employer mismatch, a missing employer id and provider failure.
"""
from __future__ import annotations

import copy
import json
import os
from datetime import date
from pathlib import Path

import pytest

from service.budget import Budget, BudgetExceeded
from service.config import settings
from service.ledger import Ledger
from service.models import Buyer, ClientRecords, CompanyRef, JobRequest, Prospect
from service.sources import live_buyers as lb
from service.sources.base import SourceUnavailable
from service.steps import Ctx, s07_buyer

# default: the smoke checkout kept beside this repo (outside Git)
SMOKE = Path(os.environ.get(
    "CRUSTDATA_SMOKE_DIR",
    Path(__file__).resolve().parents[2] / "checking_api" / "smoke")) / "raw"
TITLES = settings()["buyer"]["titles"]
HONASA, BOAT = "1132667", "628835"
SEARCH_URL = "https://api.crustdata.com/person/search"
ENRICH_URL = "https://api.crustdata.com/person/enrich"


def _sample(name: str):
    path = SMOKE / name
    if not path.exists():
        pytest.skip(f"saved smoke sample {name} not available offline")
    return json.loads(path.read_text(encoding="utf-8"))


class FakeClient:
    """Stands in for CrustdataClient.call(operation, *, body=None, params=None)."""

    def __init__(self, **responses):
        self.responses, self.calls = responses, []

    def call(self, operation, *, body=None, params=None):
        self.calls.append((operation, body))
        r = self.responses[operation]
        if isinstance(r, Exception):
            raise r
        url = SEARCH_URL if operation == "person_search" else ENRICH_URL
        return {"data": copy.deepcopy(r), "url": url, "retrieved_at": "2026-11-22T07:10:50Z"}


def _role(name, cid, title, start, end=None, default=True):
    return {"name": name, "crustdata_company_id": cid, "title": title, "start_date": start,
            "end_date": end, "is_default": default}


def _person(pid, name, current, past=(), url=None, updated="2026-11-15T00:00:00+00:00"):
    return {"crustdata_person_id": pid, "metadata": {"updated_at": updated},
            "basic_profile": {"name": name},
            "social_handles": {"professional_network_identifier": {
                "profile_url": url or f"https://www.linkedin.com/in/synthetic-{pid}"}},
            "experience": {"employment_details": {"current": list(current), "past": list(past)}}}


def _enrich(person):
    return [{"matched_on": person["social_handles"]["professional_network_identifier"]["profile_url"],
             "match_type": "professional_network_profile_url", "match_status": "matched",
             "matches": [{"confidence_score": 1.0, "person_data": person}]}]


# ----------------------------------------------------------- title rules ---

def test_title_matching_uses_configured_titles():
    assert lb.title_matches("Head of Manufacturing and Supply Chain", TITLES)
    assert lb.title_matches("Sr Vice President Supply Chain", TITLES)       # vp supply chain
    assert lb.title_matches("Head of Logistics", TITLES)
    assert not lb.title_matches("Director - IT and Digital Supply Chain", TITLES)
    assert not lb.title_matches("CSCO", TITLES)                             # not configured
    assert "supply chain" in lb.search_keywords(TITLES)
    assert "logistics" in lb.search_keywords(TITLES)


# ---------------------------------------------------------- replay: T5 ---

def test_replay_t5_honasa_employer_specific_match():
    client = FakeClient(person_search=_sample("T5_people_honasa.json"))
    found = lb.people(client, CompanyRef(name="Honasa", crustdata_id=HONASA), TITLES)
    op, body = client.calls[0]
    assert op == "person_search"
    assert body["filters"]["conditions"][0] == {
        "field": "experience.employment_details.current.company_id", "type": "=",
        "value": int(HONASA)}
    # Head of Logistics matches a configured title; the CSCO title is not configured
    assert [b.title for b in found] == ["Head of Logistics"]
    b = found[0]
    assert b.employer_id == HONASA and b.role_start == date(2025, 3, 1)
    assert b.role_end is None and b.business_email is None and b.profile_url
    fact = b.facts[0]
    assert fact.value == lb.EVIDENCE_INDEXED and "Indexed employment" in fact.claim
    paths = {s.field_path: s for s in fact.sources}
    src = paths["profiles[1].experience.employment_details.current[0].crustdata_company_id"]
    assert src.excerpt.endswith("= 1132667") and src.url == SEARCH_URL
    assert src.record_date == date(2026, 11, 14)                     # metadata.updated_at
    assert src.retrieved_at.isoformat().startswith("2026-11-22T07:10:50")


def test_replay_t5_boat_concurrent_role_uses_employer_row():
    client = FakeClient(person_search=_sample("T5_people_boat.json"))
    found = lb.people(client, CompanyRef(name="boAt", crustdata_id=BOAT), TITLES)
    # the two IT directors do not match a buyer title; the concurrent boAt role does,
    # even though the default role is at a JV with a missing (0) employer id
    assert len(found) == 1
    b = found[0]
    assert b.title == "Head of Manufacturing and Supply Chain"
    assert b.employer_name == "boAt Lifestyle" and b.employer_id == BOAT
    assert b.role_start == date(2024, 7, 1)
    assert any(s.field_path == "profiles[2].experience.employment_details.current[1].title"
               for s in b.facts[0].sources)


def test_replay_employer_mismatch_returns_nobody():
    client = FakeClient(person_search=_sample("T5_people_honasa.json"))
    assert lb.people(client, CompanyRef(name="boAt", crustdata_id=BOAT), TITLES) == []


def test_missing_company_id_never_searches():
    client = FakeClient(person_search=AssertionError("must not be called"))
    assert lb.people(client, CompanyRef(name="Honasa"), TITLES) == []
    assert lb.people(client, CompanyRef(name="Honasa", crustdata_id="cd-1001"), TITLES) == []
    assert client.calls == []


def test_synthetic_missing_employer_id_on_role_is_skipped():
    """SYNTHETIC: right employer name, but the role row carries no employer id."""
    data = {"profiles": [_person(9001, "Synthetic Person A",
                                 [_role("Honasa Consumer Ltd.", 0, "Head of Logistics",
                                        "2025-01-01T00:00:00")])]}
    found = lb.people(FakeClient(person_search=data),
                      CompanyRef(name="Honasa", crustdata_id=HONASA), TITLES)
    assert found == []


# ---------------------------------------------------------- replay: T6 ---

def _avinash(title="CSCO"):
    return Buyer(name="Avinash Dhagat", title=title, person_id="39322982",
                 profile_url="https://www.linkedin.com/in/avinash-dhagat-4800a210",
                 employer_id=HONASA, employer_name="Honasa Consumer Ltd.")


def test_replay_t6_recheck_by_profile_url_keeps_current_role():
    client = FakeClient(person_enrich=_sample("T6_person_enrich.json"))
    b = lb.verify_employment(client, _avinash())
    op, body = client.calls[0]
    assert op == "person_enrich"
    assert body["professional_network_profile_urls"] == [
        "https://www.linkedin.com/in/avinash-dhagat-4800a210"]
    # a past Honasa title ended on 2025-09-01, but the current Honasa role continues
    assert b.role_end is None and b.employer_id == HONASA and b.title == "CSCO"
    assert b.role_start == date(2025, 4, 1)
    fact = next(f for f in b.facts if f.value == lb.EVIDENCE_RECHECKED)
    assert "not independently verified" in fact.claim
    assert {s.field_path for s in fact.sources} >= {
        "[0].matches[0].person_data.experience.employment_details.current[0].crustdata_company_id",
        "[0].matches[0].person_data.experience.employment_details.current[0].end_date"}


def test_replay_t6_same_company_title_change():
    client = FakeClient(person_enrich=_sample("T6_person_enrich.json"))
    b = lb.verify_employment(client, _avinash("Sr Vice President Supply Chain"))
    assert b.role_end is None and b.title == "CSCO"
    change = next(f for f in b.facts if f.value == lb.EVIDENCE_TITLE_CHANGE)
    end = next(s for s in change.sources if s.field_path.endswith("past[0].end_date"))
    assert "2025-09-01" in end.excerpt


def test_synthetic_departure_from_t6():
    """SYNTHETIC: T6 with the current Honasa role moved to past and given an end date."""
    data = _sample("T6_person_enrich.json")
    ed = data[0]["matches"][0]["person_data"]["experience"]["employment_details"]
    row = ed["current"].pop(0)
    row["end_date"] = "2026-10-31T00:00:00+00:00"
    ed["past"].insert(0, row)
    b = lb.verify_employment(FakeClient(person_enrich=data), _avinash())
    assert b.role_end == date(2026, 10, 31)
    dep = next(f for f in b.facts if f.value == lb.EVIDENCE_DEPARTED)
    assert any(s.field_path.endswith("past[0].end_date") for s in dep.sources)


def test_synthetic_recheck_shows_other_employer():
    """SYNTHETIC: enrichment shows no role at the expected employer at all."""
    person = _person(9002, "Synthetic Person B",
                     [_role("Other Co", 555, "Head of Logistics", "2026-01-01T00:00:00")])
    buyer = Buyer(name="Synthetic Person B", title="Head of Logistics", person_id="9002",
                  profile_url="https://www.linkedin.com/in/synthetic-9002", employer_id=HONASA)
    b = lb.verify_employment(FakeClient(person_enrich=_enrich(person)), buyer)
    assert b.employer_id == "555" and b.role_end is None
    assert any(f.value == lb.EVIDENCE_MISMATCH for f in b.facts)


def test_no_profile_url_or_no_match_leaves_buyer_unverified():
    client = FakeClient(person_enrich=[{"matched_on": "x", "match_status": "not_found",
                                        "matches": []}])
    no_url = _avinash().model_copy(update={"profile_url": None})
    assert lb.verify_employment(client, no_url) == no_url and client.calls == []
    assert lb.verify_employment(client, _avinash()).facts == []


def test_budget_and_provider_failure_propagate():
    with pytest.raises(BudgetExceeded):
        lb.verify_employment(FakeClient(person_enrich=BudgetExceeded("cap")), _avinash())
    with pytest.raises(SourceUnavailable):
        lb.people(FakeClient(person_search=SourceUnavailable("down")),
                  CompanyRef(name="Honasa", crustdata_id=HONASA), TITLES)


# --------------------------------------------------------------- step 7 ---

class LiveBuyerSources:
    """The two buyer methods of LiveSources, wired to a fake client (A5 wires the real one)."""

    def __init__(self, client):
        self.client = client

    def people(self, company, titles):
        return lb.people(self.client, company, titles)

    def verify_employment(self, buyer):
        return lb.verify_employment(self.client, buyer)


class EmailingSources(LiveBuyerSources):
    """SYNTHETIC: pretends a business contact source supplied a work email, which this key
    cannot do. Used only to show the step accepts a buyer once both requirements hold."""

    def people(self, company, titles):
        return [b.model_copy(update={"business_email": "buyer@synthetic.example"})
                for b in super().people(company, titles)]


@pytest.fixture
def ctx(tmp_path):
    led = Ledger(tmp_path / "t.sqlite")
    job = JobRequest(job_id="buyer-test", text="t", request_date=date(2026, 11, 22))

    def make(sources):
        return Ctx(job=job, client=ClientRecords(name="3PL"), ledger=led,
                   budget=Budget(led, job.job_id), sources=sources, cfg=settings())
    yield make
    led.close()


def _prospect(cid=HONASA, name="Honasa Consumer Ltd."):
    return Prospect(prospect_id=f"p-{cid}", company=CompanyRef(name=name, crustdata_id=cid))


def _synthetic_search(pid=9100):
    return {"profiles": [_person(pid, "Synthetic Buyer", [
        _role("Honasa Consumer Ltd.", int(HONASA), "Head of Logistics", "2025-03-01T00:00:00")])]}


def test_step_replay_missing_business_email_holds_before_recheck(ctx):
    client = FakeClient(person_search=_sample("T5_people_honasa.json"),
                        person_enrich=AssertionError("no paid recheck without an email"))
    [p] = s07_buyer.run(ctx(LiveBuyerSources(client)), [_prospect()])
    assert p.status == "held" and p.reason == "no verified buyer" and p.buyer is None
    [rej] = p.buyers_rejected
    assert rej["outcome"] == "missing_business_email" and rej["name"] == "zuber khan"
    assert rej["facts"][0]["value"] == lb.EVIDENCE_INDEXED          # sourced candidate kept
    assert [op for op, _ in client.calls] == ["person_search"]


def test_step_synthetic_valid_match_uses_source_evidence(ctx):
    person = _synthetic_search()["profiles"][0]
    client = FakeClient(person_search=_synthetic_search(), person_enrich=_enrich(person))
    [p] = s07_buyer.run(ctx(EmailingSources(client)), [_prospect()])
    assert p.active and p.buyer.name == "Synthetic Buyer"
    labels = [f.value for f in p.buyer.facts]
    assert labels == [lb.EVIDENCE_INDEXED, lb.EVIDENCE_RECHECKED]
    # every citation points at a real response field, none written by the step
    assert all(s.url in (SEARCH_URL, ENRICH_URL) and s.field_path
               for f in p.buyer.facts for s in f.sources)


def test_step_synthetic_departure_is_a_departure(ctx):
    person = _synthetic_search()["profiles"][0]
    ed = person["experience"]["employment_details"]
    ed["past"] = [dict(ed.pop("current")[0], end_date="2026-10-31T00:00:00")]
    ed["current"] = []
    client = FakeClient(person_search=_synthetic_search(), person_enrich=_enrich(person))
    [p] = s07_buyer.run(ctx(EmailingSources(client)), [_prospect()])
    assert p.status == "held" and p.reason == "no verified buyer"
    [rej] = p.buyers_rejected
    assert rej["outcome"] == "departed" and rej["role_end"] == "2026-10-31"
    assert not p.cannot_verify                                        # a known departure


def test_step_synthetic_verification_unavailable(ctx):
    client = FakeClient(person_search=_synthetic_search(),
                        person_enrich=SourceUnavailable("crustdata.person_enrich 503"))
    [p] = s07_buyer.run(ctx(EmailingSources(client)), [_prospect()])
    assert p.status == "held"
    [rej] = p.buyers_rejected
    assert rej["outcome"] == "verification_unavailable"
    [cv] = p.cannot_verify
    assert "source unavailable" in cv.missing and "batch work-history" in cv.missing


def test_step_synthetic_no_match_on_recheck_is_unavailable_not_departure(ctx):
    client = FakeClient(person_search=_synthetic_search(),
                        person_enrich=[{"matched_on": "x", "match_status": "not_found",
                                        "matches": []}])
    [p] = s07_buyer.run(ctx(EmailingSources(client)), [_prospect()])
    assert p.buyers_rejected[0]["outcome"] == "verification_unavailable"
    assert "no record" in p.cannot_verify[0].missing


def test_step_replay_title_change_rejects_non_buyer_title(ctx):
    """T6 replay: an indexed SVP Supply Chain is now CSCO, not a configured buyer title."""
    avinash = _avinash("Sr Vice President Supply Chain").model_copy(
        update={"business_email": "buyer@synthetic.example"})          # SYNTHETIC email

    class Src(LiveBuyerSources):
        def people(self, company, titles):
            return [avinash]
    client = FakeClient(person_enrich=_sample("T6_person_enrich.json"))
    [p] = s07_buyer.run(ctx(Src(client)), [_prospect()])
    assert p.status == "held"
    assert p.buyers_rejected[0]["outcome"] == "title_mismatch"


def test_step_missing_company_id_and_search_outage(ctx):
    client = FakeClient(person_search=SourceUnavailable("crustdata.person_search 503"))
    no_id, down = _prospect(cid=None, name="Unresolved Co"), _prospect()
    s07_buyer.run(ctx(LiveBuyerSources(client)), [no_id, down])
    assert no_id.status == "held" and "company id" in no_id.cannot_verify[0].missing
    assert down.status == "held" and "source unavailable" in down.cannot_verify[0].missing
    assert len(client.calls) == 1                                    # no search for no_id
