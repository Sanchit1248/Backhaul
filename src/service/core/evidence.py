"""Evidence store: save_fact() -> evidence ID; get_fact(id) -> the Fact with its sources.

The ID is a hash of the claim + its sources, so saving the same fact twice (e.g. after a
restart) returns the same ID. Facts live in the ledger database next to the outbox.
"""
from __future__ import annotations

import json
import time

from ..config import settings
from ..ledger import Ledger, idem_key
from ..models import Fact

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    evidence_id TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    claim       TEXT NOT NULL,
    fact        TEXT NOT NULL,          -- Fact JSON (claim, value, sources)
    record_date TEXT,                   -- newest source record date
    saved_at    REAL NOT NULL
);
"""


def tenant() -> str:
    return str(settings().get("tenant_id", "default"))


class EvidenceStore:
    def __init__(self, ledger: Ledger):
        self.db = ledger.db
        self.db.executescript(SCHEMA)

    def save_fact(self, fact: Fact) -> str:
        data = fact.model_dump(mode="json")
        key_sources = [{k: s.get(k) for k in ("url", "excerpt", "field_path", "record_date")}
                       for s in data["sources"]]
        eid = "ev_" + idem_key(tenant(), data["claim"], data.get("value"), key_sources)[:20]
        dates = [s["record_date"] for s in data["sources"] if s.get("record_date")]
        self.db.execute("INSERT OR IGNORE INTO facts VALUES (?,?,?,?,?,?)",
                        (eid, tenant(), data["claim"], json.dumps(data),
                         max(dates) if dates else None, time.time()))
        return eid

    def get_fact(self, evidence_id: str) -> Fact | None:
        row = self.db.execute("SELECT fact FROM facts WHERE evidence_id=? AND tenant_id=?",
                              (evidence_id, tenant())).fetchone()
        return Fact.model_validate(json.loads(row[0])) if row else None

    def get_many(self, ids: list[str]) -> dict[str, Fact]:
        return {i: f for i in dict.fromkeys(ids) if (f := self.get_fact(i)) is not None}
