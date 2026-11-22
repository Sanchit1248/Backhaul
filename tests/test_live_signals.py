"""Offline tests for `service.sources.live_signals` (Person A, branch `a/signals`).

Replays saved local smoke evidence (when present at the local evidence directory,
kept strictly outside Git) and verifies all mapping, corroboration, parsing,
error-vs-empty, and BudgetExceeded paths with compact offline mocks.

Every constructed positive example is explicitly labeled SYNTHETIC.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from service.budget import Budget, BudgetExceeded
from service.config import settings
from service.ledger import Ledger
from service.models import ClientRecords, CompanyRef, JobRequest, Location, Prospect, Source, Status
from service.sources.base import SourceUnavailable
from service.sources.live_signals import (
    NEW_LEADER_CONTRACT_GAP,
    collect_triggers,
    corroborates_job_company,
    expansion_triggers,
    extract_published_page,
    find_expansion_passage,
    funding_triggers,
    hiring_triggers,
    is_relevant_logistics_job_title,
    map_expansion_fetch,
    map_funding_triggers,
    map_job_listings,
    map_new_leader_signals,
    new_leader_signals,
    triggers,
)
from service.steps import Ctx, s04_triggers

DEFAULT_SMOKE_DIR = Path("/home/nandanbali/programming/sideprojects/crustdata/checking_api/smoke")
SMOKE_DIR = Path(os.environ.get("SMOKE_EVIDENCE_DIR", str(DEFAULT_SMOKE_DIR)))
HAS_SMOKE_EVIDENCE = (SMOKE_DIR / "raw").is_dir() and (SMOKE_DIR / "freshness" / "raw").is_dir()

RETRIEVED_AT = "2026-11-22T07:37:00Z"


def _envelope(data: Any, url: str = "https://api.crustdata.com/test", retrieved_at: str = RETRIEVED_AT) -> dict[str, Any]:
    return {"data": data, "url": url, "retrieved_at": retrieved_at}


def _load_smoke(rel_path: str) -> Any:
    return json.loads((SMOKE_DIR / rel_path).read_text(encoding="utf-8"))


def _location(city: str, kind: str = "office") -> Location:
    return Location(
        city=city,
        kind=kind,
        source=Source(
            url="https://api.crustdata.com/company/enrich",
            kind="api_response",
            excerpt=f"locations.city = {city!r}",
            field_path="locations.city",
            record_date=date(2026, 11, 1),
            retrieved_at=datetime(2026, 11, 22, 7, 37, tzinfo=UTC),
        ),
    )


class MockClient:
    """Offline mock for `client.call(operation, *, body=None, params=None)`."""

    def __init__(self, handlers: dict[str, Any]) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, dict | None, dict | None]] = []

    def call(self, operation: str, *, body: dict | None = None, params: dict | None = None) -> dict[str, Any]:
        self.calls.append((operation, body, params))
        handler = self.handlers[operation]
        if callable(handler):
            res = handler(body=body, params=params)
        elif isinstance(handler, list):
            res = handler.pop(0)
        else:
            res = handler
        if isinstance(res, BaseException):
            raise res
        return res


def _run_s04(tmp_path: Path, client: MockClient, company: CompanyRef, request_date: date = date(2026, 11, 22)) -> Prospect:
    """Exercise Person D's unmodified `s04_triggers.run` against `live_signals.triggers`."""

    class _Adapter:
        def triggers(self, comp: CompanyRef, since: date):
            return triggers(client, comp, since)

    ledger = Ledger(tmp_path / "s04.sqlite")
    try:
        job = JobRequest(job_id="sig-test", text="test", request_date=request_date)
        ledger.start_run(job.job_id, job.model_dump(mode="json"))
        ctx = Ctx(
            job=job,
            client=ClientRecords(name="Test3PL"),
            ledger=ledger,
            budget=Budget(ledger, job.job_id),
            sources=_Adapter(),
            cfg=settings(),
        )
        prospect = Prospect(prospect_id="p-1", company=company)
        s04_triggers.run(ctx, [prospect])
        return prospect
    finally:
        ledger.close()


# ============================================================================
# 1. Local smoke-evidence replay tests (skipped only when local smoke dir absent)
# ============================================================================


@pytest.mark.skipif(not HAS_SMOKE_EVIDENCE, reason="local smoke evidence directory not present")
def test_replay_t4_empty_30d_and_90d_searches():
    """Replay saved T4 30- and 90-day empty searches: empty result, not provider failure."""
    empty_30 = _load_smoke("raw/T4_jobs_30d_all.json")
    empty_90 = _load_smoke("raw/T4_jobs_90d_all.json")
    honasa = CompanyRef(name="Honasa Consumer Ltd.", crustdata_id="1132667", domain="honasa.in")

    # 30-day search returns empty -> fallback name lookup also returns empty -> []
    client_30 = MockClient({
        "job_search": [
            _envelope(empty_30, "https://api.crustdata.com/job/search"),
            _envelope(empty_30, "https://api.crustdata.com/job/search"),
        ],
    })
    assert hiring_triggers(client_30, honasa, since=date(2026, 10, 23)) == []
    assert len(client_30.calls) == 2

    # 90-day search returns empty -> fallback name lookup also returns empty -> []
    client_90 = MockClient({
        "job_search": [
            _envelope(empty_90, "https://api.crustdata.com/job/search"),
            _envelope(empty_90, "https://api.crustdata.com/job/search"),
        ],
    })
    assert hiring_triggers(client_90, honasa, since=date(2026, 8, 24)) == []
    assert len(client_90.calls) == 2


@pytest.mark.skipif(not HAS_SMOKE_EVIDENCE, reason="local smoke evidence directory not present")
def test_replay_t4_older_honasa_jobs_preserved_for_s04(tmp_path):
    """Replay T4 unbounded: preserves older Honasa jobs with original URL + date_added; s04 rejects."""
    unbounded = _load_smoke("raw/T4_jobs_unbounded.json")
    honasa = CompanyRef(name="Honasa Consumer Ltd.", crustdata_id="1132667", domain="honasa.in")
    env = _envelope(unbounded, "https://api.crustdata.com/job/search")

    found = map_job_listings(env, honasa)
    # 4 Honasa listings preserved; 5th row is boAt (628835) and must be excluded.
    assert len(found) == 4
    newest = found[0]
    assert newest.type == "hiring"
    assert newest.event_date == date(2026, 8, 12)  # original date_added, NOT date_updated (08-15) or retrieved_at
    assert newest.fact.sources[0].url == "https://www.linkedin.com/jobs/view/4453055471"
    assert newest.fact.sources[0].record_date == date(2026, 8, 12)
    assert newest.fact.sources[1].field_path == "job_listings[0].metadata.date_added"
    assert newest.fact.sources[1].record_date == date(2026, 8, 12)
    assert newest.fact.sources[1].retrieved_at.date() == date(2026, 11, 22)

    # Person D's s04_triggers rejects all 4 older Honasa postings on the 30-day window.
    client = MockClient({
        "job_search": env,
        "company_enrich": _envelope([], "https://api.crustdata.com/company/enrich"),
        "web_search": _envelope(_load_smoke("raw/T7_honasa_warehouse.json"), "https://api.crustdata.com/web/search/live"),
    })
    prospect = _run_s04(tmp_path, client, honasa, request_date=date(2026, 11, 22))
    assert prospect.status == Status.DROPPED
    assert "4 older/out-of-window rejected" in (prospect.reason or "")


@pytest.mark.skipif(not HAS_SMOKE_EVIDENCE, reason="local smoke evidence directory not present")
def test_replay_f1_null_company_id_jobs_corroboration_and_relevance(tmp_path):
    """Replay F1 recent India jobs with null company IDs."""
    f1 = _load_smoke("freshness/raw/F1_jobs_india.json")
    empty_t4 = _load_smoke("raw/T4_jobs_30d_all.json")
    f1_env = _envelope(f1, "https://api.crustdata.com/job/search")

    # 1. Name-only employer match WITHOUT corroboration must be rejected.
    kbros_uncorroborated = CompanyRef(name="KBros Aristo Pvt Ltd")
    assert map_job_listings(f1_env, kbros_uncorroborated) == []

    # 2. ID-filtered miss followed by name lookup WITH sourced location corroboration succeeds.
    kbros_corroborated = CompanyRef(
        name="KBros Aristo Pvt Ltd",
        crustdata_id="999999",
        locations=[_location("bengaluru", "office")],
    )
    client = MockClient({
        "job_search": [
            _envelope(empty_t4, "https://api.crustdata.com/job/search"),  # ID query misses
            f1_env,                                                       # name fallback returns F1 rows
        ],
        "company_enrich": _envelope([], "https://api.crustdata.com/company/enrich"),
        "web_search": _envelope({"success": True, "results": []}, "https://api.crustdata.com/web/search/live"),
    })
    prospect = _run_s04(tmp_path, client, kbros_corroborated, request_date=date(2026, 11, 22))
    assert prospect.status == Status.ACTIVE
    assert len(prospect.triggers) == 1
    trig = prospect.triggers[0]
    assert trig.type == "hiring"
    assert trig.event_date == date(2026, 11, 20)
    assert trig.fact.sources[0].url == "https://in.indeed.com/viewjob?jk=a17211d6e2fb16c3"
    assert trig.fact.sources[1].field_path == "job_listings[0].metadata.date_added"
    assert trig.fact.sources[1].record_date == date(2026, 11, 20)
    # Verify ID query ran first, then name lookup.
    job_calls = [c for c in client.calls if c[0] == "job_search"]
    assert len(job_calls) == 2
    assert "company.basic_info.crustdata_company_id" in json.dumps(job_calls[0][1])
    assert "company.basic_info.name" in json.dumps(job_calls[1][1])

    # 3. F1 row 1 ("Admin Executive - Visa & Travel- Logistics") rejected even when company + city match.
    alpha_corroborated = CompanyRef(
        name="ALPHA GLOBAL LOGISTICS",
        locations=[_location("mumbai", "office")],
    )
    assert map_job_listings(f1_env, alpha_corroborated) == []


@pytest.mark.skipif(not HAS_SMOKE_EVIDENCE, reason="local smoke evidence directory not present")
def test_replay_f2_and_t2_funding_mapping(tmp_path):
    """Replay F2 recent funding (VitusCare) and T2 older funding (Honasa, boAt)."""
    f2 = _load_smoke("freshness/raw/F2_recent_funding.json")
    vitus = CompanyRef(name="VitusCare", crustdata_id="43364", domain="vituscare.com")
    f2_env = _envelope(f2, "https://api.crustdata.com/company/enrich")

    vitus_triggers = map_funding_triggers(f2_env, vitus)
    assert len(vitus_triggers) == 1
    vt = vitus_triggers[0]
    assert vt.type == "funding"
    assert vt.event_date == date(2026, 11, 19)
    assert "Series B" in vt.summary
    assert "3,217,189" in vt.fact.claim
    paths = [s.field_path for s in vt.fact.sources]
    assert "companies[0].funding.last_fundraise_date" in paths
    assert "companies[0].funding.last_round_type" in paths
    assert "companies[0].funding.last_round_amount_usd" in paths
    assert all(s.record_date == date(2026, 11, 19) for s in vt.fact.sources)

    # T2 Honasa (2024-09-12) preserved by mapper, rejected as old by s04_triggers.
    t2 = _load_smoke("raw/T2_enrich.json")
    t2_env = _envelope(t2, "https://api.crustdata.com/company/enrich")
    honasa = CompanyRef(name="Honasa Consumer Ltd.", crustdata_id="1132667", domain="honasa.in")
    honasa_funding = map_funding_triggers(t2_env, honasa)
    assert len(honasa_funding) == 1
    assert honasa_funding[0].event_date == date(2024, 9, 12)
    assert "190,769,315" in honasa_funding[0].fact.claim

    # T2 boAt (2024-02-01, undisclosed): latest milestone has amount_usd=null, so do NOT attach 2022's $60.7M.
    boat = CompanyRef(name="boAt Lifestyle", crustdata_id="628835", domain="boat-lifestyle.com")
    boat_funding = map_funding_triggers(t2_env, boat)
    assert len(boat_funding) == 1
    assert boat_funding[0].event_date == date(2024, 2, 1)
    assert "60,762,423" not in boat_funding[0].fact.claim
    assert "Undisclosed" in boat_funding[0].summary

    # Through s04_triggers: VitusCare stays ACTIVE on 2026-11-22.
    client = MockClient({
        "job_search": _envelope({"job_listings": [], "total_count": 0}, "https://api.crustdata.com/job/search"),
        "company_enrich": f2_env,
        "web_search": _envelope({"success": True, "results": []}, "https://api.crustdata.com/web/search/live"),
    })
    prospect = _run_s04(tmp_path, client, vitus, request_date=date(2026, 11, 22))
    assert prospect.status == Status.ACTIVE
    assert prospect.triggers[0].type == "funding"
    assert prospect.triggers[0].event_date == date(2026, 11, 19)


@pytest.mark.skipif(not HAS_SMOKE_EVIDENCE, reason="local smoke evidence directory not present")
def test_replay_f4_and_t7_earnings_articles_as_parser_controls():
    """F4 and T7 Honasa earnings articles validate date + excerpt extraction without false expansion triggers."""
    honasa = CompanyRef(name="Honasa Consumer Ltd.", crustdata_id="1132667", domain="honasa.in")

    # F4 Business Today article
    f4_raw = _load_smoke("freshness/raw/F4_news_fetch.json")
    f4_item = f4_raw[0]
    page_f4 = extract_published_page(f4_item["content"], url=f4_item["url"])
    assert page_f4.publication_date == date(2026, 11, 9)
    assert page_f4.date_source == "meta[article:published_time]"
    assert any("35.9% YoY in Q2 FY27" in s for s in page_f4.sentences)
    assert any("Rs 483.60" in s for s in page_f4.sentences)
    # Even if meta tags are stripped, NewsArticle JSON-LD yields the same 2026-11-09 publication date.
    html_no_meta_tag = f4_item["content"].replace('property="article:published_time"', 'property="x-disabled"')
    page_f4_ld = extract_published_page(html_no_meta_tag, url=f4_item["url"])
    assert page_f4_ld.publication_date == date(2026, 11, 9)
    assert page_f4_ld.date_source == "jsonld.NewsArticle.datePublished"
    # Earnings article MUST NOT produce an expansion trigger!
    assert find_expansion_passage(page_f4, honasa) is None
    assert map_expansion_fetch(_envelope(f4_raw, "https://api.crustdata.com/web/enrich/live"), honasa) == []

    # T7 Simply Wall St fetch control
    t7_raw = _load_smoke("raw/T7_fetch_control.json")
    t7_item = t7_raw[0]
    page_t7 = extract_published_page(t7_item["content"], url=t7_item["url"])
    assert page_t7.publication_date == date(2026, 11, 10)
    assert page_t7.date_source == "meta[iso-8601-publish-date]"
    assert any("half-yearly results" in s and "6.9%" in s for s in page_t7.sentences)
    assert find_expansion_passage(page_t7, honasa) is None
    assert map_expansion_fetch(_envelope(t7_raw, "https://api.crustdata.com/web/enrich/live"), honasa) == []

    # T2 Honasa earnings news URLs must NOT trigger unnecessary web_fetch calls.
    t2_env = _envelope(_load_smoke("raw/T2_enrich.json"), "https://api.crustdata.com/company/enrich")
    t7_empty = _envelope(_load_smoke("raw/T7_honasa_warehouse.json"), "https://api.crustdata.com/web/search/live")
    client = MockClient({
        "job_search": _envelope({"job_listings": [], "total_count": 0}, "https://api.crustdata.com/job/search"),
        "company_enrich": t2_env,
        "web_search": t7_empty,
    })
    found = triggers(client, honasa, since=date(2026, 10, 23))
    assert [t.type for t in found] == ["funding"]
    assert "web_fetch" not in [op for op, _, _ in client.calls]


@pytest.mark.skipif(not HAS_SMOKE_EVIDENCE, reason="local smoke evidence directory not present")
def test_replay_f3_new_leaders_flagged_separately():
    """Replay F3 new supply-chain leaders: flagged separately because TriggerType lacks that value."""
    f3 = _load_smoke("freshness/raw/F3_new_leaders.json")
    f3_env = _envelope(f3, "https://api.crustdata.com/person/search")

    signals = map_new_leader_signals(f3_env)
    assert len(signals) == 2
    assert signals[0].company_name == "Greaves Electric Mobility"
    assert signals[0].title == "Vice President Supply Chain"
    assert signals[0].start_date == date(2026, 11, 1)
    assert signals[0].contract_gap == NEW_LEADER_CONTRACT_GAP
    assert signals[0].fact.sources[1].field_path == "profiles[0].experience.employment_details.current[0].start_date"
    assert signals[0].fact.sources[1].record_date == date(2026, 11, 1)
    assert signals[1].company_name == "ZEISS Group"
    assert signals[1].start_date == date(2026, 11, 1)


# ============================================================================
# 2. Portable offline tests (synthetic positive controls clearly labeled)
# ============================================================================


def test_job_title_relevance_filter():
    assert is_relevant_logistics_job_title("Regional Logistics Manager")
    assert is_relevant_logistics_job_title("Warehouse Manager")
    assert is_relevant_logistics_job_title("Supervisor Logistics")
    assert is_relevant_logistics_job_title("Mamaearth - Assistant Manager - Warehouse Operations")
    assert not is_relevant_logistics_job_title("Admin Executive - Visa & Travel- Logistics")
    assert not is_relevant_logistics_job_title("Travel & Logistics Coordinator")
    assert not is_relevant_logistics_job_title("Event Logistics Executive")
    assert not is_relevant_logistics_job_title("Senior Software Engineer")


def test_corroboration_rules_domain_id_location_and_reject_name_only():
    target = CompanyRef(
        name="Acme Consumer Pvt Ltd",
        crustdata_id="5001",
        domain="acmeconsumer.example",
        locations=[_location("pune", "plant")],
    )

    # Matching company ID -> corroborated
    ok, reason = corroborates_job_company(
        {"company": {"basic_info": {"crustdata_company_id": 5001, "name": "Acme Consumer"}}},
        target,
    )
    assert ok and reason == "company_id"

    # Conflicting company ID -> rejected even if name & domain look similar
    ok, reason = corroborates_job_company(
        {
            "company": {
                "basic_info": {
                    "crustdata_company_id": 9999,
                    "name": "Acme Consumer Pvt Ltd",
                    "primary_domain": "acmeconsumer.example",
                }
            }
        },
        target,
    )
    assert not ok and reason == "company_id_mismatch"

    # Null ID + matching primary_domain -> corroborated
    ok, reason = corroborates_job_company(
        {
            "company": {
                "basic_info": {
                    "crustdata_company_id": None,
                    "name": "Acme Consumer",
                    "primary_domain": "acmeconsumer.example",
                }
            }
        },
        target,
    )
    assert ok and reason == "domain"

    # Null ID + job hosted on company's own careers subdomain -> corroborated
    ok, reason = corroborates_job_company(
        {
            "company": {"basic_info": {"crustdata_company_id": None, "name": "Acme"}},
            "job_details": {"url": "https://careers.acmeconsumer.example/jobs/123"},
        },
        target,
    )
    assert ok and reason == "domain"

    # Null ID + normalized name + matching sourced city -> corroborated
    ok, reason = corroborates_job_company(
        {
            "company": {"basic_info": {"crustdata_company_id": None, "name": "Acme Consumer Private Limited"}},
            "location": {"raw": "Chakan, Pune, Maharashtra"},
        },
        target,
    )
    assert ok and reason == "name_and_location"

    # Null ID + name-only (wrong city, no domain) -> REJECTED
    ok, reason = corroborates_job_company(
        {
            "company": {"basic_info": {"crustdata_company_id": None, "name": "Acme Consumer Pvt Ltd"}},
            "location": {"raw": "Jaipur, Rajasthan"},
        },
        target,
    )
    assert not ok and reason == "name_only_uncorroborated"


def test_undated_job_preserved_and_never_uses_date_updated_or_retrieved_at(tmp_path):
    """SYNTHETIC: job missing `metadata.date_added` stays undated (`event_date=None`) for Person D."""
    company = CompanyRef(name="Synthetic Shipper Ltd", crustdata_id="7001", domain="synship.example")
    synthetic_undated_job = {
        "job_listings": [
            {
                "job_details": {
                    "title": "Warehouse Operations Manager",
                    "url": "https://jobs.example/view/undated-1",
                    "source": "indeed",
                },
                "company": {"basic_info": {"crustdata_company_id": 7001, "name": "Synthetic Shipper Ltd"}},
                "location": {"raw": "Pune, Maharashtra, India"},
                "metadata": {
                    "date_added": None,
                    "date_updated": "2026-11-21T00:00:00",  # refresh time, NOT posting date
                },
            }
        ],
        "total_count": 1,
    }
    client = MockClient({
        "job_search": _envelope(synthetic_undated_job, "https://api.crustdata.com/job/search"),
        "company_enrich": _envelope([], "https://api.crustdata.com/company/enrich"),
        "web_search": _envelope({"success": True, "results": []}, "https://api.crustdata.com/web/search/live"),
    })
    found = hiring_triggers(client, company, since=date(2026, 10, 23))
    assert len(found) == 1
    assert found[0].event_date is None
    assert found[0].fact.sources[0].url == "https://jobs.example/view/undated-1"
    assert found[0].fact.sources[0].record_date is None

    # Person D's s04_triggers marks prospect CANNOT_VERIFY when only undated triggers exist.
    prospect = _run_s04(tmp_path, client, company, request_date=date(2026, 11, 22))
    assert prospect.status == Status.CANNOT_VERIFY
    assert prospect.cannot_verify


def test_synthetic_positive_expansion_requires_published_page_and_exact_passage(tmp_path):
    """SYNTHETIC positive control: published article with meta publish date + verbatim warehouse passage."""
    company = CompanyRef(name="Synthetic Foods Pvt Ltd", crustdata_id="8001", domain="synfoods.example")
    article_url = "https://news.example/synthetic-foods-opens-pune-warehouse"
    exact_sentence = (
        "Synthetic Foods Pvt Ltd on Friday announced it will open a 1.5 lakh sq ft "
        "cold storage warehouse in Chakan, Pune to double western India capacity."
    )
    synthetic_html = f"""<!doctype html>
    <html lang="en">
      <head>
        <title>Synthetic Foods opens new Pune warehouse - Industry Daily</title>
        <meta property="article:published_time" content="2026-11-14T10:30:00+05:30" />
      </head>
      <body>
        <article>
          <p>{exact_sentence}</p>
          <p>The company did not disclose financial terms.</p>
        </article>
      </body>
    </html>"""

    client = MockClient({
        "job_search": _envelope({"job_listings": [], "total_count": 0}, "https://api.crustdata.com/job/search"),
        "company_enrich": _envelope([], "https://api.crustdata.com/company/enrich"),
        "web_search": _envelope(
            {
                "success": True,
                "results": [
                    {
                        "position": 1,
                        "title": "Synthetic Foods opens new Pune warehouse",
                        "url": article_url,
                        "snippet": "Synthetic Foods to open a new warehouse in Pune.",
                    }
                ],
            },
            "https://api.crustdata.com/web/search/live",
        ),
        "web_fetch": _envelope(
            [
                {
                    "url": article_url,
                    "title": "Synthetic Foods opens new Pune warehouse - Industry Daily",
                    "http_status": 200,
                    "success": True,
                    "timestamp": 1795333040,  # retrieval time: 2026-11-22, MUST NOT override publish date
                    "content": synthetic_html,
                }
            ],
            "https://api.crustdata.com/web/enrich/live",
        ),
    })

    prospect = _run_s04(tmp_path, client, company, request_date=date(2026, 11, 22))
    assert prospect.status == Status.ACTIVE
    assert len(prospect.triggers) == 1
    trig = prospect.triggers[0]
    assert trig.type == "expansion"
    assert trig.event_date == date(2026, 11, 14)
    src = trig.fact.sources[0]
    assert src.kind == "web_page"
    assert src.url == article_url
    assert src.excerpt == exact_sentence
    assert src.record_date == date(2026, 11, 14)
    assert src.retrieved_at.date() == date(2026, 11, 22)


def test_synthetic_undated_expansion_page_preserved_for_s04(tmp_path):
    """SYNTHETIC: published page with exact expansion passage but NO publish date -> event_date=None."""
    company = CompanyRef(name="Synthetic Chem Ltd", domain="synchem.example")
    article_url = "https://synchem.example/press/second-plant"
    exact_sentence = "Synthetic Chem Ltd plans to set up a second manufacturing plant near Nashik."
    synthetic_html = f"""<html><head><title>Press Release</title></head>
    <body><p>{exact_sentence}</p></body></html>"""

    client = MockClient({
        "job_search": _envelope({"job_listings": [], "total_count": 0}, "https://api.crustdata.com/job/search"),
        "company_enrich": _envelope([], "https://api.crustdata.com/company/enrich"),
        "web_search": _envelope(
            {
                "success": True,
                "results": [
                    {
                        "title": "Synthetic Chem second plant",
                        "url": article_url,
                        "snippet": exact_sentence,
                    }
                ],
            },
            "https://api.crustdata.com/web/search/live",
        ),
        "web_fetch": _envelope(
            [
                {
                    "url": article_url,
                    "title": "Press Release",
                    "http_status": 200,
                    "success": True,
                    "timestamp": 1795333040,
                    "content": synthetic_html,
                }
            ],
            "https://api.crustdata.com/web/enrich/live",
        ),
    })

    prospect = _run_s04(tmp_path, client, company, request_date=date(2026, 11, 22))
    assert prospect.status == Status.CANNOT_VERIFY
    assert prospect.cannot_verify


def test_expansion_snippet_alone_is_never_sufficient_when_fetch_lacks_passage():
    """SYNTHETIC: search snippet mentions warehouse, but fetched page has no supporting passage -> []."""
    company = CompanyRef(name="Synthetic Retail Ltd", domain="synretail.example")
    article_url = "https://news.example/synretail-earnings"
    client = MockClient({
        "web_search": _envelope(
            {
                "success": True,
                "results": [
                    {
                        "title": "Synthetic Retail new warehouse rumors",
                        "url": article_url,
                        "snippet": "Synthetic Retail may consider a warehouse.",
                    }
                ],
            },
            "https://api.crustdata.com/web/search/live",
        ),
        "web_fetch": _envelope(
            [
                {
                    "url": article_url,
                    "title": "Synthetic Retail Q2 results",
                    "http_status": 200,
                    "success": True,
                    "timestamp": 1795333040,
                    "content": "<html><head><meta property='article:published_time' content='2026-11-15'/></head>"
                               "<body><p>Synthetic Retail reported 12% revenue growth in Q2.</p></body></html>",
                }
            ],
            "https://api.crustdata.com/web/enrich/live",
        ),
    })
    assert expansion_triggers(client, company, since=date(2026, 10, 23)) == []


def test_provider_failure_distinct_from_empty_and_preserves_budget_exceeded():
    """Verify: empty -> [], all-fail -> SourceUnavailable, partial-fail -> partial+recorded, BudgetExceeded -> propagates."""
    company = CompanyRef(name="Synthetic Co", crustdata_id="9001", domain="synco.example")
    since = date(2026, 10, 23)

    # 1. All providers succeed with empty responses -> [] (NOT an error)
    empty_client = MockClient({
        "job_search": _envelope({"job_listings": [], "total_count": 0}, "https://api.crustdata.com/job/search"),
        "company_enrich": _envelope([], "https://api.crustdata.com/company/enrich"),
        "web_search": _envelope({"success": True, "results": []}, "https://api.crustdata.com/web/search/live"),
    })
    res_empty = collect_triggers(empty_client, company, since)
    assert res_empty.triggers == []
    assert res_empty.failures == []
    assert triggers(empty_client, company, since) == []

    # 2. Provider failure with no collected triggers -> raises SourceUnavailable
    failing_client = MockClient({
        "job_search": SourceUnavailable("crustdata.job_search 503"),
        "company_enrich": _envelope([], "https://api.crustdata.com/company/enrich"),
        "web_search": _envelope({"success": True, "results": []}, "https://api.crustdata.com/web/search/live"),
    })
    res_fail = collect_triggers(failing_client, company, since)
    assert res_fail.triggers == []
    assert len(res_fail.failures) == 1
    assert res_fail.failures[0].operation == "job_search"
    with pytest.raises(SourceUnavailable, match="job_search"):
        triggers(failing_client, company, since)

    # 3. web_search payload reporting success=false is treated as provider failure, not empty
    web_fail_client = MockClient({
        "job_search": _envelope({"job_listings": [], "total_count": 0}, "https://api.crustdata.com/job/search"),
        "company_enrich": _envelope([], "https://api.crustdata.com/company/enrich"),
        "web_search": _envelope({"success": False, "results": []}, "https://api.crustdata.com/web/search/live"),
    })
    with pytest.raises(SourceUnavailable, match="web_search"):
        triggers(web_fail_client, company, since)

    # 4. Partial failure: funding succeeds (SYNTHETIC) while web_search fails -> returns funding + records failure
    partial_client = MockClient({
        "job_search": _envelope({"job_listings": [], "total_count": 0}, "https://api.crustdata.com/job/search"),
        "company_enrich": _envelope(
            {
                "companies": [
                    {
                        "crustdata_company_id": 9001,
                        "basic_info": {"name": "Synthetic Co", "primary_domain": "synco.example"},
                        "funding": {
                            "last_fundraise_date": "2026-11-18",
                            "last_round_type": "series_a",
                            "last_round_amount_usd": 5000000.0,
                        },
                    }
                ]
            },
            "https://api.crustdata.com/company/enrich",
        ),
        "web_search": SourceUnavailable("crustdata.web_search timeout"),
    })
    res_partial = collect_triggers(partial_client, company, since)
    assert len(res_partial.triggers) == 1
    assert res_partial.triggers[0].type == "funding"
    assert len(res_partial.failures) == 1
    assert res_partial.failures[0].operation == "web_search/web_fetch"
    # Top-level triggers() returns what we have when at least one trigger succeeded.
    assert len(triggers(partial_client, company, since)) == 1

    # 5. web_fetch failure (success=false) is treated as provider failure, not empty
    fetch_fail_client = MockClient({
        "job_search": _envelope({"job_listings": [], "total_count": 0}, "https://api.crustdata.com/job/search"),
        "company_enrich": _envelope([], "https://api.crustdata.com/company/enrich"),
        "web_search": _envelope(
            {
                "success": True,
                "results": [
                    {
                        "title": "Synthetic Co opens new warehouse",
                        "url": "https://news.example/synco-warehouse",
                        "snippet": "Synthetic Co opens a warehouse.",
                    }
                ],
            },
            "https://api.crustdata.com/web/search/live",
        ),
        "web_fetch": _envelope(
            [{"url": "https://news.example/synco-warehouse", "success": False, "http_status": 503, "content": ""}],
            "https://api.crustdata.com/web/enrich/live",
        ),
    })
    with pytest.raises(SourceUnavailable, match="web_fetch"):
        triggers(fetch_fail_client, company, since)

    # 6. BudgetExceeded MUST propagate untouched from every channel (including web_fetch)
    for op in ("job_search", "company_enrich", "web_search", "web_fetch"):
        handlers: dict[str, Any] = {
            "job_search": _envelope({"job_listings": [], "total_count": 0}, "https://api.crustdata.com/job/search"),
            "company_enrich": _envelope([], "https://api.crustdata.com/company/enrich"),
            "web_search": _envelope(
                {
                    "success": True,
                    "results": (
                        [
                            {
                                "title": "Synthetic Co new warehouse",
                                "url": "https://news.example/synco-warehouse",
                                "snippet": "Synthetic Co opens warehouse.",
                            }
                        ]
                        if op == "web_fetch"
                        else []
                    ),
                },
                "https://api.crustdata.com/web/search/live",
            ),
            "web_fetch": _envelope([], "https://api.crustdata.com/web/enrich/live"),
        }
        handlers[op] = BudgetExceeded(f"cost limit reached before {op}")
        with pytest.raises(BudgetExceeded, match=op):
            triggers(MockClient(handlers), company, since)


def test_undated_funding_preserved_for_s04(tmp_path):
    """SYNTHETIC: funding without `last_fundraise_date` stays undated (`event_date=None`) for Person D."""
    company = CompanyRef(name="Synthetic Biotech Pvt Ltd", crustdata_id="8801", domain="synbio.example")
    client = MockClient({
        "job_search": _envelope({"job_listings": [], "total_count": 0}, "https://api.crustdata.com/job/search"),
        "company_enrich": _envelope(
            {
                "companies": [
                    {
                        "crustdata_company_id": 8801,
                        "basic_info": {"name": "Synthetic Biotech Pvt Ltd", "primary_domain": "synbio.example"},
                        "funding": {
                            "last_fundraise_date": None,
                            "last_round_type": "series_a",
                            "last_round_amount_usd": 4000000.0,
                        },
                    }
                ]
            },
            "https://api.crustdata.com/company/enrich",
        ),
        "web_search": _envelope({"success": True, "results": []}, "https://api.crustdata.com/web/search/live"),
    })
    found = funding_triggers(client, company, since=date(2026, 10, 23))
    assert len(found) == 1
    assert found[0].type == "funding"
    assert found[0].event_date is None
    prospect = _run_s04(tmp_path, client, company, request_date=date(2026, 11, 22))
    assert prospect.status == Status.CANNOT_VERIFY
    assert prospect.cannot_verify


def test_new_leader_signals_helper_stays_separate_from_triggers():
    """SYNTHETIC: new-leader signal carries contract gap and never appears in `triggers()`."""
    company = CompanyRef(name="Synthetic Mobility Ltd", crustdata_id="7777")
    client = MockClient({
        "person_search": _envelope(
            {
                "profiles": [
                    {
                        "crustdata_person_id": 12345,
                        "experience": {
                            "employment_details": {
                                "current": [
                                    {
                                        "company_id": 7777,
                                        "name": "Synthetic Mobility Ltd",
                                        "title": "VP Supply Chain",
                                        "start_date": "2026-11-01T00:00:00",
                                    }
                                ]
                            }
                        },
                    }
                ]
            },
            "https://api.crustdata.com/person/search",
        ),
        "job_search": _envelope({"job_listings": [], "total_count": 0}, "https://api.crustdata.com/job/search"),
        "company_enrich": _envelope([], "https://api.crustdata.com/company/enrich"),
        "web_search": _envelope({"success": True, "results": []}, "https://api.crustdata.com/web/search/live"),
    })
    leaders = new_leader_signals(client, company, since=date(2026, 10, 23))
    assert len(leaders) == 1
    assert leaders[0].start_date == date(2026, 11, 1)
    assert leaders[0].contract_gap == NEW_LEADER_CONTRACT_GAP
    # `triggers()` does not emit new-leader signals into TriggerType.
    assert triggers(client, company, since=date(2026, 10, 23)) == []
    assert "person_search" not in [op for op, _, _ in client.calls[1:]]
