"""Kill-on-stage rehearsal: hard-kill the real CLI process at every step and
mid-send, restart, and check each step ran once and each email/invite exists once.
Uses os._exit via SERVICE_CRASH_AT (same effect as taskkill /F)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from service.faults import EXIT_CODE
from service.ledger import Ledger
from service.pipeline import CHECKPOINTED
from service.steps.s09_approve import outbox_key

ROOT = Path(__file__).resolve().parents[1]
REQ = {"job_id": "kill-job", "request_date": "2026-11-20",
       "text": "Book 5 meetings for the Pune to Mumbai route; spare capacity 12 trucks a week"}


def cli(tmp: Path, *args: str, crash: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "SERVICE_DATA_DIR": str(tmp), "PYTHONIOENCODING": "utf-8"}
    env.pop("SERVICE_CRASH_AT", None)
    if crash:
        env["SERVICE_CRASH_AT"] = crash
    return subprocess.run([sys.executable, "-m", "service.cli", *args], check=False, cwd=ROOT,
                          env=env,
                          capture_output=True, text=True, timeout=60)


def run_job(tmp: Path, crash: str | None = None) -> subprocess.CompletedProcess:
    req = tmp / "req.json"
    if not req.exists():
        req.write_text(json.dumps(REQ), encoding="utf-8")
    return cli(tmp, "run", str(req), "--mode", "fixtures", crash=crash)


def events(tmp: Path) -> list[str]:
    led = Ledger(tmp / "ledger.sqlite")
    try:
        return [m for (m,) in led.db.execute(
            "SELECT msg FROM events WHERE run_id='kill-job' ORDER BY id")]
    finally:
        led.close()


@pytest.mark.parametrize("step", [name for name, _ in CHECKPOINTED])
def test_kill_after_each_step_resumes(tmp_path, step):
    r = run_job(tmp_path, crash=f"after:{step}")
    assert r.returncode == EXIT_CODE, r.stderr
    r = run_job(tmp_path)
    assert r.returncode == 0, r.stderr
    ev = events(tmp_path)
    for name, _ in CHECKPOINTED:
        assert ev.count(f"step {name}: done") == 1, f"{name} ran twice or never"
    assert f"step {step}: resumed from checkpoint" in ev
    assert json.loads(r.stdout[r.stdout.index("{"):])["status"] == "awaiting_approval"


def outbox(tmp: Path, ext: str) -> list[Path]:
    d = tmp / "outbox"
    return list(d.glob(f"*.{ext}")) if d.exists() else []


def test_kill_during_send_sends_once(tmp_path):
    assert run_job(tmp_path).returncode == 0
    led = Ledger(tmp_path / "ledger.sqlite")
    pending = led.pending_approvals()
    assert len(pending) == 2
    key = pending[0]["key"]
    led.close()
    assert cli(tmp_path, "approve", key, "--by", "judge").returncode == 0

    assert run_job(tmp_path, crash="before_send").returncode == EXIT_CODE
    assert outbox(tmp_path, "eml") == []
    assert run_job(tmp_path, crash="after_send").returncode == EXIT_CODE
    assert len(outbox(tmp_path, "eml")) == 1          # written, but ledger not 'done'

    for _ in range(2):                                # restart, and restart again
        assert run_job(tmp_path).returncode == 0
    assert len(outbox(tmp_path, "eml")) == 1
    assert len(outbox(tmp_path, "ics")) == 1

    led = Ledger(tmp_path / "ledger.sqlite")
    try:
        a = led.get_action(key)
        assert a["status"] == "done" and a["approved_by"] == "judge"
        other = [p for p in led.pending_approvals() if p["key"] != key]
        assert len(other) == 1                        # unapproved one still waits
    finally:
        led.close()


def test_outbox_key_is_stable():
    assert outbox_key("j", "p") == outbox_key("j", "p")
    assert outbox_key("j", "p") != outbox_key("j", "p2")
