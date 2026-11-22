"""Route tasks for the task runner: route_capacity, route_sync."""
from __future__ import annotations

from datetime import date

from ..budget import Budget
from ..core.tasks import task
from ..ledger import Ledger
from .adapter import Lane, lane_key
from .factory import get_adapter
from .sync import sync


@task("route_capacity", cache=False)
def route_capacity(ledger: Ledger, params: dict) -> dict:
    """params: origin?, destination?, week_of? (ISO date). No lane -> every lane."""
    notes: list[str] = []
    adapter = get_adapter(ledger, Budget(ledger, params["task_id"]), notes)
    day = date.fromisoformat(params["week_of"]) if params.get("week_of") else date.today()
    lanes = adapter.list_routes()
    if params.get("origin") and params.get("destination"):
        want = lane_key(params["origin"], params["destination"])
        lanes = [lane for lane in lanes if lane.key == want] or \
            [Lane(params["origin"].lower(), params["destination"].lower())]
    return {"adapter": adapter.name, "synced_at": adapter.synced_at, "notes": notes,
            "lanes": [adapter.capacity(lane, day).as_dict() for lane in lanes]}


@task("route_sync", cache=False)
def route_sync(ledger: Ledger, params: dict) -> dict:
    snap = sync(ledger, date.fromisoformat(params["week_of"]) if params.get("week_of") else None)
    return {"adapter": snap["adapter"], "synced_at": snap["synced_at"],
            "lanes": len(snap["lanes"]), "notes": snap["notes"]}
