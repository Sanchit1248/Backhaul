"""Step 4 rules in isolation (owner D): sources disagree, dedupe, window, source failure."""
from datetime import date

import pytest

from service.budget import Budget
from service.config import settings
from service.ledger import Ledger
from service.models import ClientRecords, CompanyRef, JobRequest, Prospect, Status, Trigger
from service.sources.base import SourceUnavailable
from service.steps import Ctx, s04_triggers

REQ_DATE = date(2026, 11, 20)


def trig(typ, day, value=None, excerpt=None, url=None):
    excerpt = excerpt or f"{typ} on {day}: {value}"
    return Trigger(type=typ, summary=f"{typ} {value}", event_date=day,
                   fact={"claim": f"{typ} {value}", "value": value,
                         "sources": [{"url": url or f"https://src.example/{typ}/{day}/{value}",
                                      "kind": "web_page", "excerpt": excerpt,
                                      "record_date": day}]})


class Stub:
    def __init__(self, triggers=None, fail=False):
        self._t, self._fail = triggers or [], fail

    def triggers(self, company, since):
        if self._fail:
            raise SourceUnavailable("crustdata.job_search failed after 4 attempts: 503")
        return [t.model_copy(deep=True) for t in self._t]


@pytest.fixture
def make(tmp_path):
    led = Ledger(tmp_path / "t.sqlite")

    def _run(sources):
        job = JobRequest(job_id="t4", text="x", request_date=REQ_DATE)
        ctx = Ctx(job=job, client=ClientRecords(name="c"), ledger=led,
                  budget=Budget(led, "t4"), sources=sources, cfg=settings())
        p = Prospect(prospect_id="p", company=CompanyRef(name="Acme"))
        return s04_triggers.run(ctx, [p])[0]

    yield _run
    led.close()


def test_funding_amounts_disagree_cannot_verify_quotes_both(make):
    p = make(Stub([trig("funding", date(2026, 11, 12), "Series B, INR 120 crore",
                        excerpt="amount_inr: 120 crore", url="https://a.example"),
                   trig("funding", date(2026, 11, 13), "Series B, INR 80 crore",
                        excerpt="raised Rs 80 crore", url="https://b.example")]))
    assert p.status == Status.CANNOT_VERIFY
    assert p.reason.startswith("sources disagree")
    cv = p.cannot_verify[0]
    for s in ("amount_inr: 120 crore", "raised Rs 80 crore", "https://a.example",
              "https://b.example"):
        assert s in cv.missing
    assert set(cv.tried) == {"https://a.example", "https://b.example"}
    assert p.triggers == []


def test_disputed_event_is_skipped_but_other_trigger_keeps_prospect(make):
    p = make(Stub([trig("expansion", date(2026, 11, 3), "pune"),
                   trig("expansion", date(2026, 11, 4), "nagpur"),
                   trig("hiring", date(2026, 11, 15), "Logistics Manager")]))
    assert p.status == Status.ACTIVE
    assert [t.type for t in p.triggers] == ["hiring"]          # email can't cite the dispute
    assert any("Sources disagree" in c.question for c in p.cannot_verify)


def test_same_value_twice_is_one_event_with_both_sources(make):
    p = make(Stub([trig("funding", date(2026, 11, 8), "Series A, INR 40 crore"),
                   trig("funding", date(2026, 11, 9), "series a, inr 40 crore ")]))
    assert p.status == Status.ACTIVE and not p.cannot_verify
    assert len(p.triggers) == 1 and len(p.triggers[0].extra_sources) == 1


def test_different_hiring_posts_are_not_a_conflict(make):
    p = make(Stub([trig("hiring", date(2026, 11, 10), "Logistics Manager"),
                   trig("hiring", date(2026, 11, 11), "Warehouse Supervisor")]))
    assert p.status == Status.ACTIVE and not p.cannot_verify


def test_same_type_outside_dedupe_window_are_separate_events(make):
    p = make(Stub([trig("funding", date(2026, 10, 25), "Seed"),
                   trig("funding", date(2026, 11, 15), "Series A")]))
    assert p.status == Status.ACTIVE and len(p.triggers) == 2


@pytest.mark.parametrize("day,kept", [(date(2026, 10, 21), True),    # exactly 30 days
                                      (date(2026, 10, 20), False),   # 31 days
                                      (date(2026, 11, 20), True),    # request day
                                      (date(2026, 11, 21), False)])  # future: bad data
def test_window_boundaries(make, day, kept):
    p = make(Stub([trig("hiring", day, "Logistics Manager")]))
    assert (p.status == Status.ACTIVE) is kept


def test_source_failure_is_cannot_verify_not_a_crash(make):
    p = make(Stub(fail=True))
    assert p.status == Status.CANNOT_VERIFY
    assert p.reason == "cannot verify: source unavailable"
    assert "503" in p.cannot_verify[0].missing
