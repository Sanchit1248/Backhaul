"""No double contact across jobs (approval.recontact_days / recontact_scope)."""
import time

import pytest

from service import mailer
from service.config import settings
from service.ledger import Ledger
from service.pipeline import run
from service.steps.s09_approve import outbox_key

TEXT = "Book 5 meetings for the Pune to Mumbai route; spare capacity 12 trucks a week"
JOB1 = {"job_id": "job-1", "request_date": "2026-11-20", "text": TEXT}
JOB2 = {"job_id": "job-2", "request_date": "2026-11-20", "text": TEXT}
KAVERI, DECCAN = "Kaveri Agro Foods Pvt Ltd", "Deccan Pharma Ltd"


@pytest.fixture
def led(tmp_path, monkeypatch):
    monkeypatch.setattr(mailer, "DATA_DIR", tmp_path)
    settings()["sources"]["mode"] = "fixtures"
    led = Ledger(tmp_path / "t.sqlite")
    yield led
    led.close()


def by_name(res):
    return {p.company.name: p for p in res.prospects}


def test_second_job_does_not_queue_people_already_queued(led):
    first = by_name(run(JOB1, led))
    assert first[KAVERI].status == "awaiting_approval"
    second = by_name(run(JOB2, led))
    for name in (KAVERI, DECCAN):
        assert second[name].status == "excluded"
        assert "already contacted" in second[name].reason and "job-1" in second[name].reason
    assert {a["run_id"] for a in led.pending_approvals()} == {"job-1"}   # nothing new queued


def test_rerunning_the_same_job_is_not_a_recontact(led):
    run(JOB1, led)
    again = by_name(run(JOB1, led))
    assert again[KAVERI].status == "awaiting_approval"


def test_rejected_or_expired_earlier_drafts_do_not_block(led):
    run(JOB1, led)
    for a in led.pending_approvals():
        led.decide(a["key"], False, by="t")
    assert by_name(run(JOB2, led))[KAVERI].status == "awaiting_approval"


def test_two_jobs_approved_together_send_once(led, monkeypatch):
    run(JOB1, led)
    monkeypatch.setitem(settings()["approval"], "recontact_days", 0)    # both get queued
    run(JOB2, led)
    monkeypatch.setitem(settings()["approval"], "recontact_days", 90)
    for a in led.pending_approvals():
        led.decide(a["key"], True, by="t")
    r1, r2 = by_name(run(JOB1, led)), by_name(run(JOB2, led))
    assert r1[KAVERI].status == "sent"
    assert r2[KAVERI].status == "excluded" and "re-checked before send" in r2[KAVERI].reason
    key2 = outbox_key("job-2", r2[KAVERI].prospect_id)
    assert led.get_action(key2)["status"] == "expired"
    sent = [a for a in led.db.execute("SELECT status FROM actions WHERE status='done'")]
    assert len(sent) == 2                                    # Kaveri + Deccan, once each


def test_purged_old_email_still_blocks_recontact(led):
    run(JOB1, led)
    for a in led.pending_approvals():
        led.decide(a["key"], True, by="t")
    run(JOB1, led)                                           # sent
    led.db.execute("UPDATE runs SET updated_at=? WHERE run_id='job-1'",
                   (time.time() - 40 * 86400,))
    led.purge_person_data(30)
    assert "anita" not in str(led.actions_for_run("job-1"))  # person data gone ...
    assert by_name(run(JOB2, led))[KAVERI].status == "excluded"   # ... contact hash kept
