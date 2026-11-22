"""Log masking (privacy.mask_in_logs) and retention purge (privacy.retention_days)."""
import json
import os
import time

from service.ledger import Ledger
from service.privacy import mask, mask_text

DAY = 86400


def test_mask_text_emails_and_phones():
    s = "draft to anita.rao@kaveriagro.in, call +91 98765 43210 or (020) 2345-6789"
    out = mask_text(s)
    assert "anita.rao" not in out and "a***@kaveriagro.in" in out
    assert "98765" not in out and "2345-6789" not in out and out.count("[phone]") == 2


def test_mask_keeps_ids_dates_and_urls():
    s = "employer id 1131908 on 2026-11-22 https://in.indeed.com/viewjob?jk=ed725e178c4f8bbf"
    assert mask_text(s) == s


def test_mask_nested_data_and_sensitive_keys():
    out = mask({"buyer": {"name": "Anita Rao", "business_email": "anita@k.in"},
                "to": "redacted-elsewhere", "notes": ["mail priya@d.in"]})
    assert out["buyer"]["business_email"] == "a***@k.in"
    assert out["to"] == "[masked]"
    assert out["notes"] == ["mail p***@d.in"]
    assert out["buyer"]["name"] == "Anita Rao"   # names are allowed work data


def test_ledger_log_is_masked(tmp_path, capsys):
    led = Ledger(tmp_path / "ledger.sqlite")
    led.log("job", "info", "queued email for anita@kaveri.in", {"to": "anita@kaveri.in"})
    msg, data = led.db.execute("SELECT msg, data FROM events").fetchone()
    assert "anita@" not in msg and "anita@" not in data
    assert "anita@" not in capsys.readouterr().out


def _old_job(led: Ledger, tmp_path, run_id: str, key: str, status: str, age_days: float):
    led.start_run(run_id, {"text": "x"})
    led.step(run_id, "07_buyer", lambda: [{"buyer": {"name": "Anita Rao"}}])
    led.request_approval(run_id, "send_email", key,
                         {"company": "Kaveri", "draft": {"to": "anita@kaveri.in", "body": "Hi"}})
    if status != "pending_approval":
        led.db.execute("UPDATE actions SET status=?, result='{}' WHERE idem_key=?", (status, key))
    for d in ("results", "outbox"):
        (tmp_path / d).mkdir(exist_ok=True)
    (tmp_path / "results" / f"{run_id}.json").write_text("{}")
    (tmp_path / "outbox" / f"{key}.eml").write_text("x")
    old = time.time() - age_days * DAY
    led.db.execute("UPDATE runs SET updated_at=? WHERE run_id=?", (old, run_id))


def test_purge_removes_old_person_data_but_keeps_send_once_proof(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    _old_job(led, tmp_path, "old-sent", "k1", "done", 40)
    _old_job(led, tmp_path, "old-pending", "k2", "pending_approval", 40)
    _old_job(led, tmp_path, "fresh", "k3", "pending_approval", 2)
    (tmp_path / "http_cache").mkdir()
    stale = tmp_path / "http_cache" / "a.json"
    stale.write_text("{}")
    os.utime(stale, (time.time() - 40 * DAY,) * 2)

    counts = led.purge_person_data(30)

    assert counts["runs"] == 2 and counts["actions_expired"] == 1 and counts["cache_files"] == 1
    assert led.db.execute("SELECT COUNT(*) FROM steps WHERE run_id LIKE 'old-%'").fetchone()[0] == 0
    sent = led.get_action("k1")
    assert sent["status"] == "done" and sent["payload"]["redacted"]
    assert "anita" not in json.dumps(sent["payload"])
    assert led.get_action("k2")["status"] == "expired"           # never sent later
    assert [a["key"] for a in led.pending_approvals()] == ["k3"]  # fresh job untouched
    assert not (tmp_path / "outbox" / "k1.eml").exists() and not stale.exists()
    assert (tmp_path / "outbox" / "k3.eml").exists()
    # an old send can never repeat: the key row still says done
    res = led.action("old-sent", "send_email", "k1", {}, lambda p: 1 / 0, needs_approval=True)
    assert res["status"] == "done" and res["deduped"]


def test_purge_dry_run_changes_nothing(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    _old_job(led, tmp_path, "old", "k1", "pending_approval", 40)
    assert led.purge_person_data(30, dry_run=True)["runs"] == 1
    assert led.get_action("k1")["status"] == "pending_approval"
    assert (tmp_path / "outbox" / "k1.eml").exists()
