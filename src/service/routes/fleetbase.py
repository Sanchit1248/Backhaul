"""FleetbaseAdapter: lanes, trucks, loads and clients from a Fleetbase instance (REST API v1).

Fleetbase has no "lane" object, so LaneLead uses these fields (docs/FLEETBASE.md):
  * vehicle  meta.lane = "mumbai-pune" (sorted city pair), meta.trips_per_week = 4,
             payload_capacity = truck payload
  * order    payload.pickup.city -> payload.dropoff.city = the lane,
             scheduled_at = the week it uses a truck, customer = the client,
             meta.weight_kg = load weight
  * contact  type = customer -> a client; meta.city = its main city
Every value carries a Source pointing at the API record it came from.
Config: routes.fleetbase.base_url; key in FLEETBASE_API_KEY (.env), never in config.
"""
from __future__ import annotations

from datetime import date

from ..budget import Budget
from ..config import env, settings
from ..ledger import Ledger
from ..models import Source
from ..sources.base import SourceUnavailable, fetch_json
from .adapter import RouteAdapter, lane_key
from .files import _day, _int


def _items(data) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "results", "items"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def _city(place) -> str:
    return (place or {}).get("city", "") if isinstance(place, dict) else ""


class FleetbaseAdapter(RouteAdapter):
    name = "Fleetbase"

    def __init__(self, ledger: Ledger, budget: Budget, base_url: str | None = None,
                 api_key: str | None = None):
        super().__init__()
        cfg = settings().get("routes", {}).get("fleetbase", {})
        self.ledger, self.budget = ledger, budget
        self.base = (base_url or env("FLEETBASE_API_URL") or cfg.get("base_url")
                     or "http://localhost:8000").rstrip("/")
        self.key = api_key if api_key is not None else env("FLEETBASE_API_KEY")
        self.limit = int(cfg.get("page_limit", 500))

    def _get(self, path: str, params: dict | None = None) -> tuple[list[dict], str, date]:
        if not self.key:
            raise SourceUnavailable("FLEETBASE_API_KEY is not set (.env)")
        res = fetch_json(self.ledger, self.budget, provider="fleetbase", op=path.strip("/"),
                         method="GET", url=f"{self.base}/v1/{path.strip('/')}",
                         headers={"Authorization": f"Bearer {self.key}",
                                  "Accept": "application/json"},
                         params={"limit": self.limit, **(params or {})}, use_cache=False)
        day = _day(res.get("retrieved_at")) or date.today()
        return _items(res["data"]), res.get("url", f"{self.base}/v1/{path}"), day

    def _src(self, url: str, rid: str, excerpt: str, field_path: str, day) -> Source:
        return Source(url=f"{url.split('?')[0]}/{rid}" if rid else url, kind="api_response",
                      excerpt=excerpt, field_path=field_path, record_date=day)

    def _load(self) -> dict:
        out: dict[str, list] = {"routes": [], "vehicles": [], "clients": [], "orders": []}
        vehicles, vurl, vday = self._get("vehicles")
        for v in vehicles:
            meta = v.get("meta") or {}
            lane = str(meta.get("lane") or "")
            if "-" in lane:
                lane = lane_key(*lane.split("-", 1))
            rid = v.get("id") or v.get("public_id") or ""
            trips = _int(meta.get("trips_per_week"))
            out["vehicles"].append({
                "id": rid, "lane": lane or None, "trips_per_week": trips,
                "payload_capacity_kg": _int(v.get("payload_capacity")),
                "source": self._src(vurl, rid, f"vehicle {rid} ({v.get('name') or ''}): "
                                    f"meta.lane = {lane or '-'}, meta.trips_per_week = {trips}",
                                    "vehicle.meta", vday)})
        contacts, curl, cday = self._get("contacts", {"type": "customer"})
        for c in contacts:
            if str(c.get("type", "customer")) != "customer":
                continue
            rid = c.get("id") or c.get("public_id") or ""
            out["clients"].append({
                "key": rid, "name": c.get("name") or rid,
                "city": str((c.get("meta") or {}).get("city") or "").lower() or None,
                "source": self._src(curl, rid, f"contact {rid}: name = {c.get('name')}, "
                                    "type = customer", "contact.name", cday)})
        orders, ourl, oday = self._get("orders")
        for o in orders:
            payload = o.get("payload") or {}
            a, b = _city(payload.get("pickup")).lower(), _city(payload.get("dropoff")).lower()
            cust = o.get("customer") or {}
            ckey = cust.get("id") or cust.get("public_id") if isinstance(cust, dict) else cust
            rid = o.get("id") or o.get("public_id") or ""
            when = o.get("scheduled_at") or o.get("created_at")
            d = _day(when)
            out["orders"].append({
                "id": rid, "origin": a, "destination": b, "day": d,
                "customer_key": ckey or None,
                "customer_name": cust.get("name") if isinstance(cust, dict) else None,
                "weight_kg": _int((o.get("meta") or {}).get("weight_kg")),
                "status": o.get("status", ""),
                "source": self._src(ourl, rid, f"order {rid}: payload.pickup.city = {a}, "
                                    f"payload.dropoff.city = {b}, scheduled_at = {when}, "
                                    f"status = {o.get('status')}", "order.payload", d or oday)})
        return out
