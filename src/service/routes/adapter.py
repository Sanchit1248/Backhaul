"""RouteAdapter interface and the shared capacity calculator.

Both adapters load the same normalised records, so they pass the same tests:
  vehicles: {id, lane, trips_per_week, payload_capacity_kg, source}
  orders:   {id, origin, destination, day, customer_key, customer_name, weight_kg, status, source}
  clients:  {key, name, city, source}
  routes:   {origin, destination, trucks_per_week, source}   (declared lanes, optional)

Capacity for a lane and ISO week:
  trucks/week = sum(trips_per_week of vehicles on the lane) + declared trucks_per_week
  booked      = orders on the lane (either direction) scheduled that week, not cancelled
  free        = max(0, trucks/week - booked)
No vehicle and no declared lane -> "capacity unknown" (never guessed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from ..models import Source

CANCELLED = {"canceled", "cancelled", "void"}


def lane_key(a: str, b: str) -> str:
    return "-".join(sorted([a.strip().lower(), b.strip().lower()]))


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


@dataclass(frozen=True)
class Lane:
    origin: str
    destination: str

    @property
    def key(self) -> str:
        return lane_key(self.origin, self.destination)

    def label(self) -> str:
        return f"{self.origin.title()} to {self.destination.title()}"


@dataclass
class LaneCapacity:
    lane: Lane
    week: str                       # ISO week, e.g. 2026-W48
    known: bool
    trucks_per_week: int | None
    booked: int
    free: int | None
    reason: str
    sources: list[Source] = field(default_factory=list)
    synced_at: str = ""

    def as_dict(self) -> dict:
        return {"lane": self.lane.key, "origin": self.lane.origin,
                "destination": self.lane.destination, "week": self.week, "known": self.known,
                "trucks_per_week": self.trucks_per_week, "booked": self.booked,
                "free": self.free, "reason": self.reason, "synced_at": self.synced_at,
                "sources": [s.model_dump(mode="json") for s in self.sources]}


@dataclass
class ClientInfo:
    key: str
    name: str
    city: str | None
    source: Source


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class RouteAdapter:
    """Base: subclasses implement _load() -> dict of normalised record lists."""
    name = "base"

    def __init__(self):
        self._data: dict | None = None
        self.synced_at = ""

    # -- subclass hook ---------------------------------------------------
    def _load(self) -> dict:
        raise NotImplementedError

    def data(self) -> dict:
        if self._data is None:
            self._data = self._load()
            self.synced_at = _now()
        return self._data

    def refresh(self) -> None:
        self._data = None

    # -- interface ---------------------------------------------------------
    def list_routes(self) -> list[Lane]:
        d, seen, out = self.data(), set(), []
        # declared lanes and orders keep their direction; vehicle lanes are sorted pairs
        rows = ([(r["origin"], r["destination"]) for r in d.get("routes", [])]
                + [(o["origin"], o["destination"]) for o in d.get("orders", [])
                   if o.get("origin") and o.get("destination")]
                + [tuple(v["lane"].split("-", 1)) for v in d.get("vehicles", []) if v.get("lane")])
        for a, b in rows:
            lane = Lane(a.strip().lower(), b.strip().lower())
            if lane.key not in seen and lane.origin != lane.destination:
                seen.add(lane.key)
                out.append(lane)
        return out

    def loads(self, lane: Lane, week_of: date) -> list[dict]:
        ws = week_start(week_of)
        we = ws + timedelta(days=7)
        return [o for o in self.data().get("orders", [])
                if o.get("origin") and o.get("destination")
                and lane_key(o["origin"], o["destination"]) == lane.key
                and o.get("day") and ws <= o["day"] < we
                and str(o.get("status", "")).lower() not in CANCELLED]

    def capacity(self, lane: Lane, week_of: date) -> LaneCapacity:
        d = self.data()
        vehicles = [v for v in d.get("vehicles", []) if v.get("lane") == lane.key]
        declared = [r for r in d.get("routes", [])
                    if lane_key(r["origin"], r["destination"]) == lane.key
                    and r.get("trucks_per_week") is not None]
        loads = self.loads(lane, week_of)
        week = iso_week(week_of)
        sources = [v["source"] for v in vehicles if v.get("trips_per_week") is not None] \
            + [r["source"] for r in declared]
        if not sources:
            return LaneCapacity(lane, week, False, None, len(loads), None,
                                f"capacity unknown: no vehicle with trips_per_week on "
                                f"{lane.label()} in {self.name}", [], self.synced_at)
        trucks = sum(int(v.get("trips_per_week") or 0) for v in vehicles) \
            + sum(int(r["trucks_per_week"]) for r in declared)
        free = max(0, trucks - len(loads))
        sources += [o["source"] for o in loads]
        return LaneCapacity(
            lane, week, True, trucks, len(loads), free,
            f"{trucks} truck trips/week on {lane.label()} ({len(vehicles)} vehicles"
            + (f", {len(declared)} declared lane" if declared else "")
            + f"), {len(loads)} booked in {week} -> {free} free", sources, self.synced_at)

    def list_clients(self) -> list[ClientInfo]:
        d, out = self.data(), {}
        for c in d.get("clients", []):
            out[c["key"]] = ClientInfo(c["key"], c["name"], c.get("city"), c["source"])
        for o in d.get("orders", []):
            k = o.get("customer_key")
            if k and k not in out:
                out[k] = ClientInfo(k, o.get("customer_name") or k, o.get("origin"), o["source"])
        return list(out.values())

    def client_volume(self, client_key: str, weeks: int, until: date) -> list[dict]:
        """Orders per ISO week for the `weeks` weeks ending with the week before `until`."""
        end = week_start(until)
        starts = [end - timedelta(weeks=i) for i in range(weeks, 0, -1)]
        orders = [o for o in self.data().get("orders", [])
                  if o.get("customer_key") == client_key and o.get("day")
                  and str(o.get("status", "")).lower() not in CANCELLED]
        out = []
        for ws in starts:
            hit = [o for o in orders if ws <= o["day"] < ws + timedelta(days=7)]
            out.append({"week": iso_week(ws), "week_start": ws.isoformat(), "orders": len(hit),
                        "sources": [o["source"] for o in hit]})
        return out

    def has_volume_history(self, client_key: str, since: date) -> bool:
        return any(o.get("customer_key") == client_key and o.get("day") and o["day"] <= since
                   for o in self.data().get("orders", []))
