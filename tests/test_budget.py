"""Plan rule 'budget almost used up': stop, keep finished work, partial result,
and resume where it stopped once the limit allows."""
import pytest

from service import mailer
from service.budget import BudgetExceeded
from service.config import settings
from service.ledger import Ledger
from service.pipeline import run
from service.sources.fixtures import FixtureSources

REQ = {"job_id": "budget-job", "request_date": "2026-11-20",
       "text": "Book 5 meetings for the Pune to Mumbai route; spare capacity 12 trucks a week"}


@pytest.fixture
def led(tmp_path, monkeypatch):
    monkeypatch.setattr(mailer, "DATA_DIR", tmp_path)
    settings()["sources"]["mode"] = "fixtures"
    led = Ledger(tmp_path / "t.sqlite")
    yield led
    led.close()


def test_limit_hit_mid_job_gives_partial_result_then_resumes(led, monkeypatch):
    real_people = FixtureSources.people

    def broke(self, company, titles):
        raise BudgetExceeded("cost limit Rs 150 reached")

    monkeypatch.setattr(FixtureSources, "people", broke)
    res = run(REQ, led)
    assert res.status == "stopped_limit"
    assert any("stopped before 07_buyer" in n for n in res.notes)
    by = {p.company.name: p for p in res.prospects}
    assert by["Kaveri Agro Foods Pvt Ltd"].status == "held"
    assert "budget limit" in by["Kaveri Agro Foods Pvt Ltd"].reason
    assert by["Sahyadri Auto Components Ltd"].status == "dropped"   # finished work kept
    assert led.pending_approvals() == []                            # nothing half-sent

    monkeypatch.setattr(FixtureSources, "people", real_people)      # e.g. limit raised
    res = run(REQ, led)
    assert res.status == "awaiting_approval"
    msgs = [m for (m,) in led.db.execute("SELECT msg FROM events WHERE run_id='budget-job'")]
    assert "step 06_exclude: resumed from checkpoint" in msgs
    assert msgs.count("step 02_discover: done") == 1


def test_spent_budget_blocks_next_unfinished_step(led):
    from service.config import usd_to_inr
    over = settings()["limits"]["max_cost_inr"] * 1.1 / usd_to_inr(1.0)   # 110% of the limit
    led.cost("budget-job", "crustdata", "test", over)
    res = run(REQ, led)
    assert res.status == "stopped_limit"
    assert res.prospects == []
    assert res.cost_inr > settings()["limits"]["max_cost_inr"]
