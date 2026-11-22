"""Conversation memory: each turn's question, parsed task and task id.

Follow-ups ("draft an email to the second one") resolve against the latest
find_targets result in the same conversation. Viewable and deletable on /memory.
"""
from __future__ import annotations

import json
import secrets
import time

from ..core.evidence import tenant
from ..core.tasks import get_task
from ..ledger import Ledger

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_turns (
    turn_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    conv_id   TEXT NOT NULL,
    author    TEXT NOT NULL,
    question  TEXT NOT NULL,
    parsed    TEXT NOT NULL,          -- ParsedTask JSON
    task_id   TEXT,                   -- set once the task is started
    at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_turns_conv ON chat_turns (tenant_id, conv_id);
"""


def new_conv_id() -> str:
    return "c_" + secrets.token_hex(6)


class ConversationMemory:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.db = ledger.db
        self.db.executescript(SCHEMA)

    def add_turn(self, conv_id: str, author: str, question: str, parsed: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO chat_turns (tenant_id, conv_id, author, question, parsed, at)"
            " VALUES (?,?,?,?,?,?)",
            (tenant(), conv_id, author, question, json.dumps(parsed), time.time()))
        return cur.lastrowid

    def set_task(self, turn_id: int, task_id: str) -> None:
        self.db.execute("UPDATE chat_turns SET task_id=? WHERE turn_id=? AND tenant_id=?",
                        (task_id, turn_id, tenant()))

    def turn(self, turn_id: int) -> dict | None:
        rows = self._rows("turn_id=?", (turn_id,))
        return rows[0] if rows else None

    def turns(self, conv_id: str) -> list[dict]:
        return self._rows("conv_id=?", (conv_id,))

    def conversations(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT conv_id, COUNT(*), MIN(question), MAX(at) FROM chat_turns WHERE tenant_id=?"
            " GROUP BY conv_id ORDER BY MAX(at) DESC", (tenant(),)).fetchall()
        return [{"conv_id": c, "turns": n, "first": q, "at": at} for c, n, q, at in rows]

    def forget(self, conv_id: str) -> int:
        return self.db.execute("DELETE FROM chat_turns WHERE tenant_id=? AND conv_id=?",
                               (tenant(), conv_id)).rowcount

    def last_targets(self, conv_id: str, before_turn: int | None = None) -> dict | None:
        """The latest finished find_targets result in this conversation."""
        for t in reversed(self.turns(conv_id)):
            if before_turn is not None and t["turn_id"] >= before_turn:
                continue
            if t["parsed"].get("type") == "find_targets" and t["task_id"]:
                task = get_task(self.ledger, t["task_id"])
                if task and task["status"] == "done":
                    return task["result"]
        return None

    def _rows(self, where: str, args: tuple) -> list[dict]:
        rows = self.db.execute(
            "SELECT turn_id, conv_id, author, question, parsed, task_id, at FROM chat_turns"
            f" WHERE tenant_id=? AND {where} ORDER BY turn_id", (tenant(), *args)).fetchall()
        return [{"turn_id": i, "conv_id": c, "author": a, "question": q,
                 "parsed": json.loads(p), "task_id": t, "at": at}
                for i, c, a, q, p, t, at in rows]
