"""Durable state in SQLite: run checkpoints, idempotent actions, cost ledger.

Why: in the finals a judge kills the process at a random moment; after restart
"each action happens once". So:
  * every side effect goes through `Ledger.action(key, ...)` with a deterministic
    idempotency key -> a second call with the same key returns the stored result
    instead of redoing the effect;
  * every pipeline step checkpoints its output -> restart resumes, not restarts;
  * every paid call (LLM, API) writes a cost row -> cost per job is measured.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    request     TEXT NOT NULL,
    status      TEXT NOT NULL,          -- running | waiting_approval | done | failed
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    result      TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    run_id  TEXT NOT NULL,
    step    TEXT NOT NULL,
    output  TEXT NOT NULL,
    at      REAL NOT NULL,
    PRIMARY KEY (run_id, step)
);
CREATE TABLE IF NOT EXISTS actions (
    idem_key    TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL,          -- pending_approval | approved | rejected | started | done | failed
    result      TEXT,
    approved_by TEXT,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS costs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL,
    provider  TEXT NOT NULL,            -- anthropic | crustdata | web | ...
    op        TEXT NOT NULL,
    units     TEXT,                     -- JSON: tokens, credits, ...
    cost_usd  REAL NOT NULL,
    ms        INTEGER,
    at        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL,
    level   TEXT NOT NULL,
    msg     TEXT NOT NULL,
    data    TEXT,
    at      REAL NOT NULL
);
"""


def idem_key(*parts: Any) -> str:
    """Deterministic key from the business identity of an action (NOT a timestamp/uuid)."""
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class Ledger:
    def __init__(self, path: Path | str | None = None):
        path = Path(path or DATA_DIR / "ledger.sqlite")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    # ---- runs -----------------------------------------------------------
    def start_run(self, run_id: str, request: dict) -> bool:
        """Returns True if new, False if resuming an existing run."""
        now = time.time()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO runs VALUES (?,?,?,?,?,NULL)",
            (run_id, json.dumps(request), "running", now, now),
        )
        return cur.rowcount == 1

    def set_run(self, run_id: str, status: str, result: Any = None) -> None:
        self.db.execute(
            "UPDATE runs SET status=?, result=COALESCE(?, result), updated_at=? WHERE run_id=?",
            (status, json.dumps(result, default=str) if result is not None else None,
             time.time(), run_id),
        )

    def get_run(self, run_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        cols = [d[0] for d in self.db.execute("SELECT * FROM runs LIMIT 0").description]
        return dict(zip(cols, row))

    # ---- step checkpoints ----------------------------------------------
    def step(self, run_id: str, name: str, fn: Callable[[], Any]) -> Any:
        """Run fn once per (run, step); on restart return the checkpointed output."""
        row = self.db.execute(
            "SELECT output FROM steps WHERE run_id=? AND step=?", (run_id, name)
        ).fetchone()
        if row:
            self.log(run_id, "info", f"step {name}: resumed from checkpoint")
            return json.loads(row[0])
        out = fn()
        self.db.execute(
            "INSERT OR REPLACE INTO steps VALUES (?,?,?,?)",
            (run_id, name, json.dumps(out, default=str), time.time()),
        )
        self.log(run_id, "info", f"step {name}: done")
        return out

    # ---- idempotent side effects ---------------------------------------
    def action(self, run_id: str, kind: str, key: str, payload: dict,
               effect: Callable[[dict], Any], needs_approval: bool) -> dict:
        """Perform a side effect at most once.

        Status machine: pending_approval -> approved -> started -> done
        A crash between `started` and `done` leaves the row `started`: on restart we
        do NOT blindly retry; the effect function must check the provider (e.g. look
        up the payment by idempotency key) - pass `key` to the provider as its
        idempotency key so the provider dedupes too.
        """
        now = time.time()
        with self.tx() as db:
            row = db.execute("SELECT status, result FROM actions WHERE idem_key=?",
                             (key,)).fetchone()
            if row is None:
                status = "pending_approval" if needs_approval else "approved"
                db.execute("INSERT INTO actions VALUES (?,?,?,?,?,NULL,NULL,?)",
                           (key, run_id, kind, json.dumps(payload, default=str), status, now))
            else:
                status = row[0]

        if status == "done":
            return {"status": "done", "result": json.loads(row[1]), "deduped": True}
        if status in ("pending_approval", "rejected"):
            return {"status": status, "key": key}

        self._set_action(key, "started")
        try:
            result = effect({**payload, "idempotency_key": key})
        except Exception as e:
            self._set_action(key, "failed", {"error": str(e)})
            raise
        self._set_action(key, "done", result)
        return {"status": "done", "result": result, "deduped": False}

    def _set_action(self, key: str, status: str, result: Any = None, by: str | None = None):
        self.db.execute(
            "UPDATE actions SET status=?, result=COALESCE(?, result),"
            " approved_by=COALESCE(?, approved_by), updated_at=? WHERE idem_key=?",
            (status, json.dumps(result, default=str) if result is not None else None,
             by, time.time(), key),
        )

    def decide(self, key: str, approve: bool, by: str) -> None:
        self._set_action(key, "approved" if approve else "rejected", by=by)

    def pending_approvals(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT idem_key, run_id, kind, payload FROM actions WHERE status='pending_approval'"
            " ORDER BY rowid"
        ).fetchall()
        return [{"key": k, "run_id": r, "kind": kd, "payload": json.loads(p)}
                for k, r, kd, p in rows]

    def request_approval(self, run_id: str, kind: str, key: str, payload: dict) -> str:
        """Queue an irreversible action for a person (no-op if already queued).
        Returns the current status of that action."""
        self.db.execute(
            "INSERT OR IGNORE INTO actions VALUES (?,?,?,?,'pending_approval',NULL,NULL,?)",
            (key, run_id, kind, json.dumps(payload, default=str), time.time()),
        )
        return self.get_action(key)["status"]

    def get_action(self, key: str) -> dict | None:
        row = self.db.execute(
            "SELECT idem_key, run_id, kind, payload, status, result, approved_by"
            " FROM actions WHERE idem_key=?", (key,)).fetchone()
        if not row:
            return None
        k, r, kd, p, st, res, by = row
        return {"key": k, "run_id": r, "kind": kd, "payload": json.loads(p), "status": st,
                "result": json.loads(res) if res else None, "approved_by": by}

    def update_payload(self, key: str, payload: dict, by: str) -> None:
        """Approver edited the draft. Only allowed before it is sent."""
        cur = self.db.execute(
            "UPDATE actions SET payload=?, approved_by=?, updated_at=? WHERE idem_key=?"
            " AND status IN ('pending_approval','approved')",
            (json.dumps(payload, default=str), by, time.time(), key))
        if cur.rowcount != 1:
            raise ValueError(f"action {key} cannot be edited (missing or already sent)")

    def actions_for_run(self, run_id: str) -> list[dict]:
        keys = self.db.execute("SELECT idem_key FROM actions WHERE run_id=?", (run_id,))
        return [self.get_action(k) for (k,) in keys.fetchall()]

    # ---- cost ledger ----------------------------------------------------
    def cost(self, run_id: str, provider: str, op: str, cost_usd: float,
             units: dict | None = None, ms: int | None = None) -> None:
        self.db.execute(
            "INSERT INTO costs (run_id, provider, op, units, cost_usd, ms, at)"
            " VALUES (?,?,?,?,?,?,?)",
            (run_id, provider, op, json.dumps(units or {}), cost_usd, ms, time.time()),
        )

    def run_cost(self, run_id: str) -> float:
        return self.db.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM costs WHERE run_id=?", (run_id,)
        ).fetchone()[0]

    def cost_breakdown(self, run_id: str) -> list[tuple]:
        return self.db.execute(
            "SELECT provider, op, COUNT(*), ROUND(SUM(cost_usd),6) FROM costs"
            " WHERE run_id=? GROUP BY provider, op", (run_id,)
        ).fetchall()

    def cost_rows(self, run_id: str) -> list[dict]:
        """Every cost row with its units decoded (credits, status, request id)."""
        rows = self.db.execute(
            "SELECT provider, op, units, cost_usd, ms, at FROM costs WHERE run_id=? ORDER BY id",
            (run_id,)).fetchall()
        return [{"provider": pr, "op": op, "units": json.loads(u) if u else {},
                 "cost_usd": c, "ms": ms, "at": at} for pr, op, u, c, ms, at in rows]

    def credit_breakdown(self, run_id: str) -> list[dict]:
        """Per provider+op: calls, credits we estimated (max), credits the provider
        reported, credits we budgeted, and calls with no reported credits."""
        agg: dict[tuple, dict] = {}
        for r in self.cost_rows(run_id):
            u = r["units"]
            if "budgeted_credits" not in u:
                continue  # not a credit-priced call (e.g. LLM tokens)
            a = agg.setdefault((r["provider"], r["op"]), {
                "provider": r["provider"], "op": r["op"], "calls": 0, "estimated_max": 0.0,
                "reported": 0.0, "budgeted": 0.0, "unreported_calls": 0, "statuses": {}})
            a["calls"] += 1
            a["estimated_max"] += u.get("estimated_max_credits") or 0.0
            a["budgeted"] += u.get("budgeted_credits") or 0.0
            if u.get("reported_credits") is None:
                a["unreported_calls"] += 1
            else:
                a["reported"] += u["reported_credits"]
            st = str(u.get("status"))
            a["statuses"][st] = a["statuses"].get(st, 0) + 1
        return list(agg.values())

    # ---- structured log -------------------------------------------------
    def log(self, run_id: str, level: str, msg: str, data: Any = None) -> None:
        self.db.execute(
            "INSERT INTO events (run_id, level, msg, data, at) VALUES (?,?,?,?,?)",
            (run_id, level, msg, json.dumps(data, default=str) if data else None, time.time()),
        )
        print(f"[{level}] {run_id[:8]} {msg}", flush=True)
