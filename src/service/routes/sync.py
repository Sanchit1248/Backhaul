"""Route sync: snapshot lanes + this week's capacity + clients into the ledger.

The map and chat read the latest snapshot (with its sync time) instead of calling
Fleetbase on every page load. Run hourly:  python -m service.routes.sync --every 3600
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import date

from ..budget import Budget
from ..ledger import Ledger
from .adapter import RouteAdapter
from .factory import get_adapter

SCHEMA = """
CREATE TABLE IF NOT EXISTS route_snapshots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    adapter   TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    data      TEXT NOT NULL
);
"""


def snapshot(adapter: RouteAdapter, today: date | None = None) -> dict:
    today = today or date.today()
    adapter.refresh()
    lanes = [adapter.capacity(lane, today).as_dict() for lane in adapter.list_routes()]
    clients = [{"key": c.key, "name": c.name, "city": c.city} for c in adapter.list_clients()]
    return {"adapter": adapter.name, "synced_at": adapter.synced_at, "week_of": today.isoformat(),
            "lanes": lanes, "clients": clients}


def sync(ledger: Ledger, today: date | None = None) -> dict:
    ledger.db.executescript(SCHEMA)
    notes: list[str] = []
    budget = Budget(ledger, "route-sync")
    snap = snapshot(get_adapter(ledger, budget, notes), today)
    snap["notes"] = notes
    ledger.db.execute("INSERT INTO route_snapshots (adapter, synced_at, data) VALUES (?,?,?)",
                      (snap["adapter"], snap["synced_at"], json.dumps(snap, default=str)))
    return snap


def latest(ledger: Ledger) -> dict | None:
    ledger.db.executescript(SCHEMA)
    row = ledger.db.execute("SELECT data FROM route_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None


def main() -> None:
    ap = argparse.ArgumentParser(description="Snapshot lanes and capacity for the map")
    ap.add_argument("--every", type=int, default=0, help="repeat every N seconds (0 = once)")
    a = ap.parse_args()
    led = Ledger()
    while True:
        snap = sync(led)
        print(f"synced {len(snap['lanes'])} lanes from {snap['adapter']} at {snap['synced_at']}"
              + "".join(f"\n  note: {n}" for n in snap["notes"]), flush=True)
        if not a.every:
            return
        time.sleep(a.every)


if __name__ == "__main__":
    main()
