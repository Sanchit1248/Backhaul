"""End-to-end on fixtures: hard-case rules + kill/restart sends each email once."""
import pytest

from service import mailer
from service.config import settings
from service.ledger import Ledger
from service.pipeline import run
from service.steps.s09_approve import outbox_key

REQ = {"job_id": "test-job", "request_date": "2026-11-20",
       "text": "Book 5 meetings for the Pune to Mumbai route; spare capacity 12 trucks a week"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(mailer, "DATA_DIR", tmp_path)
    settings()["sources"]["mode"] = "fixtures"
    led = Ledger(tmp_path / "t.sqlite")
    yield led, tmp_path
    led.close()


def by_name(res):
    return {p.company.name: p for p in res.prospects}


def test_hard_cases(env):
    led, _ = env
    p = by_name(run(REQ, led))
    assert p["Kaveri Agro Foods Pvt Ltd"].status == "awaiting_approval"
    assert len(p["Kaveri Agro Foods Pvt Ltd"].triggers) == 1          # deduped
    assert len(p["Kaveri Agro Foods Pvt Ltd"].triggers[0].extra_sources) == 1
    assert p["Sahyadri Auto Components Ltd"].status == "dropped"
    assert "lookalike" in p["Shree Polymers"].reason
    assert p["Deccan Pharma Ltd"].buyer.name == "Priya Nair"         # Rahul left
    assert p["Konkan Retail Pvt Ltd"].status == "excluded"
    assert p["Indus Chem Ltd"].status == "cannot_verify"
    assert p["Vidarbha Steel Ltd"].reason == "no verified buyer"     # same-name person


def test_every_draft_claim_is_sourced(env):
    led, _ = env
    for pr in run(REQ, led).prospects:
        if pr.draft:
            assert pr.draft.claims and all(c.sources for c in pr.draft.claims)
            assert "unsubscribe" in pr.draft.body


def test_nothing_sent_without_approval(env):
    led, tmp = env
    run(REQ, led)
    run(REQ, led)
    assert not list((tmp / "outbox").glob("*.eml")) if (tmp / "outbox").exists() else True


def test_kill_after_approval_sends_once(env):
    led, tmp = env
    res = run(REQ, led)
    kav = by_name(res)["Kaveri Agro Foods Pvt Ltd"]
    key = outbox_key(res.job_id, kav.prospect_id)
    led.decide(key, approve=True, by="tester")

    # simulate a kill mid-send: email written, but ledger never marked 'done'
    payload = {**led.get_action(key)["payload"], "idempotency_key": key}
    mailer.send(payload)
    led._set_action(key, "started")

    for _ in range(3):  # restart several times
        res = run(REQ, led)
    assert by_name(res)["Kaveri Agro Foods Pvt Ltd"].status == "sent"
    assert len(list((tmp / "outbox").glob("*.eml"))) == 1
    assert len(list((tmp / "outbox").glob("*.ics"))) == 1


def test_steps_resume_from_checkpoint(env):
    led, _ = env
    run(REQ, led)
    run(REQ, led)
    msgs = [m for (m,) in led.db.execute("SELECT msg FROM events WHERE run_id='test-job'")]
    assert any("resumed from checkpoint" in m for m in msgs)
