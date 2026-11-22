"""FileAdapter: the backup when Fleetbase is down or not set up.

Accepts
  * a JSON file {routes?, vehicles?, clients?, orders?} (see adapter.py for fields), or
    the client-records file (routes with spare_trucks_per_week), or
  * a folder with routes.csv / vehicles.csv / clients.csv / orders.csv (same columns).
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path

from ..config import ROOT
from ..models import Source
from .adapter import RouteAdapter, lane_key


def _day(v) -> date | None:
    if not v:
        return None
    s = str(v)
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return date.fromisoformat(s[:10])


def _int(v):
    return None if v in (None, "") else int(float(v))


class FileAdapter(RouteAdapter):
    name = "route file"

    def __init__(self, path: str | Path):
        super().__init__()
        p = Path(path)
        self.path = p if p.is_absolute() else ROOT / p

    def _src(self, what: str, excerpt: str, day: date | None = None) -> Source:
        return Source(url=f"file://{self.path.as_posix()}#{what}", kind="client_record",
                      excerpt=excerpt, field_path=what, record_date=day)

    def _rows(self) -> dict[str, list[dict]]:
        if self.path.is_dir():
            out = {}
            for name in ("routes", "vehicles", "clients", "orders"):
                f = self.path / f"{name}.csv"
                if f.exists():
                    with f.open(encoding="utf-8", newline="") as fh:
                        out[name] = list(csv.DictReader(fh))
            return out
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.pop("_note", None)
        return data

    def _load(self) -> dict:
        raw = self._rows()
        out: dict[str, list] = {"routes": [], "vehicles": [], "clients": [], "orders": []}
        for i, r in enumerate(raw.get("routes", [])):
            trucks = r.get("trucks_per_week", r.get("spare_trucks_per_week"))
            field = "trucks_per_week" if "trucks_per_week" in r else "spare_trucks_per_week"
            out["routes"].append({
                "origin": r["origin"].lower(), "destination": r["destination"].lower(),
                "trucks_per_week": _int(trucks),
                "source": self._src(f"routes[{i}].{field}",
                                    f"routes[{i}] = {r['origin']} to {r['destination']}, "
                                    f"{field} = {trucks}")})
        for v in raw.get("vehicles", []):
            lane = v.get("lane", "")
            if "-" in lane:
                lane = lane_key(*lane.split("-", 1))
            out["vehicles"].append({
                "id": v["id"], "lane": lane, "trips_per_week": _int(v.get("trips_per_week")),
                "payload_capacity_kg": _int(v.get("payload_capacity_kg")),
                "source": self._src(f"vehicles[{v['id']}]",
                                    f"vehicle {v['id']}: lane = {lane}, trips_per_week = "
                                    f"{v.get('trips_per_week')}")})
        for c in raw.get("clients", []):
            out["clients"].append({
                "key": str(c["id"]), "name": c["name"], "city": (c.get("city") or "").lower()
                or None, "source": self._src(f"clients[{c['id']}]",
                                             f"client {c['id']} = {c['name']}")})
        names = {c["key"]: c["name"] for c in out["clients"]}
        for o in raw.get("orders", []):
            d = _day(o.get("scheduled_at"))
            cust = str(o.get("customer") or "")
            out["orders"].append({
                "id": o["id"], "origin": (o.get("origin") or "").lower(),
                "destination": (o.get("destination") or "").lower(), "day": d,
                "customer_key": cust or None, "customer_name": names.get(cust, cust),
                "weight_kg": _int(o.get("weight_kg")), "status": o.get("status", ""),
                "source": self._src(f"orders[{o['id']}]",
                                    f"order {o['id']}: {o.get('origin')} to "
                                    f"{o.get('destination')}, scheduled_at = "
                                    f"{o.get('scheduled_at')}, customer = {cust}", d)})
        return out
