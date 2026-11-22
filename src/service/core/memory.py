"""Account memory: who we contacted, when, and what happened.

Company-level only (company key + name); never stores a person's details, so the
30-day people-data purge does not need to touch it. find_targets reads it to hold
recently contacted or rejected companies; the chat shows "held: contacted on ...".
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

from ..config import settings
from ..ledger import Ledger
from .evidence import tenant

SCHEMA = """
CREATE TABLE IF NOT EXISTS account_memory (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL,
    company_key  TEXT NOT NULL,         -- crustdata id, else lower-case name
    company_name TEXT NOT NULL,
    action       TEXT NOT NULL,         -- targeted | queued | approved | rejected | sent | held
    outcome      TEXT,
    job_id       TEXT,
    at           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS account_memory_company ON account_memory (tenant_id, company_key);
"""

CONTACT_ACTIONS = ("queued", "approved", "sent")


def company_key(crustdata_id: str | None, name: str) -> str:
    return str(crustdata_id) if crustdata_id else name.strip().lower()


def fmt_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).date().isoformat()


class AccountMemory:
    def __init__(self, ledger: Ledger):
        self.db = ledger.db
        self.db.executescript(SCHEMA)

    def record_action(self, key: str, name: str, action: str, *, outcome: str | None = None,
                      job_id: str | None = None, at: float | None = None) -> None:
        """Idempotent per (company, action, job): a resumed task does not double-count."""
        if job_id and self.db.execute(
                "SELECT 1 FROM account_memory WHERE tenant_id=? AND company_key=? AND action=?"
                " AND job_id=?", (tenant(), key, action, job_id)).fetchone():
            return
        self.db.execute(
            "INSERT INTO account_memory (tenant_id, company_key, company_name, action, outcome,"
            " job_id, at) VALUES (?,?,?,?,?,?,?)",
            (tenant(), key, name, action, outcome, job_id, at or time.time()))

    def last_contact(self, key: str, actions: tuple[str, ...] = CONTACT_ACTIONS) -> dict | None:
        q = ",".join("?" * len(actions))
        row = self.db.execute(
            f"SELECT company_name, action, outcome, job_id, at FROM account_memory"
            f" WHERE tenant_id=? AND company_key=? AND action IN ({q}) ORDER BY at DESC LIMIT 1",
            (tenant(), key, *actions)).fetchone()
        if not row:
            return None
        name, action, outcome, job, at = row
        return {"company": name, "action": action, "outcome": outcome, "job_id": job, "at": at,
                "day": fmt_day(at)}

    def hold_reason(self, key: str, *, exclude_job: str | None = None) -> str | None:
        """Why a target must be held now, from memory (config memory.*), or None."""
        cfg = settings().get("memory", {})
        now = time.time()
        rej_days = float(cfg.get("rejected_hold_days", 0))
        rej = self.last_contact(key, ("rejected",)) if rej_days > 0 else None
        if rej and rej["job_id"] != exclude_job and now - rej["at"] < rej_days * 86400:
            return f"held: rejected by approver on {rej['day']}"
        # queued/approved drafts are covered by the ledger's recontact check (which knows
        # when a draft was later rejected); memory holds on what was actually sent
        last = self.last_contact(key, ("sent",))
        if last and last["job_id"] != exclude_job and \
                now - last["at"] < float(cfg.get("recontact_days", 90)) * 86400:
            return f"held: contacted on {last['day']} ({last['action']})"
        return None

    def history(self, key: str | None = None, limit: int = 200) -> list[dict]:
        sql = ("SELECT id, company_key, company_name, action, outcome, job_id, at"
               " FROM account_memory WHERE tenant_id=?")
        args: list = [tenant()]
        if key:
            sql += " AND company_key=?"
            args.append(key)
        rows = self.db.execute(sql + " ORDER BY at DESC LIMIT ?", (*args, limit)).fetchall()
        return [{"id": i, "company_key": k, "company": n, "action": a, "outcome": o,
                 "job_id": j, "at": t, "day": fmt_day(t)} for i, k, n, a, o, j, t in rows]

    def forget(self, key: str) -> int:
        return self.db.execute("DELETE FROM account_memory WHERE tenant_id=? AND company_key=?",
                               (tenant(), key)).rowcount
