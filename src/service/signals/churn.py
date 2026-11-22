"""check_churn(clients, window) -> per client: risk level, signals, evidence IDs.

Signal today: order volume from the route adapter, last `window_weeks` vs the
`window_weeks` before (config churn.*). Each weekly count is a fact whose sources
are the orders themselves (or "0 orders" with the adapter's records as the source).
Less history than churn.min_history_weeks -> cannot_verify, never a guess.
"""
from __future__ import annotations

from datetime import date, timedelta

from ..budget import Budget
from ..config import settings
from ..core.evidence import EvidenceStore
from ..core.tasks import task
from ..ledger import Ledger
from ..models import Fact, Source
from ..routes.adapter import RouteAdapter, week_start
from ..routes.factory import get_adapter


def risk_level(drop: float) -> str:
    bands = settings()["churn"]["bands"]
    for level in ("high", "medium"):
        if drop >= float(bands[level]):
            return level
    return "low"


def _week_fact(adapter: RouteAdapter, client_name: str, w: dict) -> Fact:
    srcs = w["sources"] or [Source(
        url=f"{adapter.name}:orders", kind="client_record",
        excerpt=f"no order for {client_name} scheduled in week {w['week']}",
        field_path="orders", record_date=date.fromisoformat(w["week_start"]))]
    return Fact(claim=f"{client_name} had {w['orders']} orders in week {w['week']}",
                value=w["orders"], sources=srcs)


def churn_for(adapter: RouteAdapter, store: EvidenceStore, client, until: date,
              weeks: int) -> dict:
    cfg = settings()["churn"]
    first_needed = week_start(until) - timedelta(weeks=int(cfg["min_history_weeks"]))
    base = {"client": client.name, "client_key": client.key, "city": client.city}
    if not adapter.has_volume_history(client.key, first_needed):
        return {**base, "status": "cannot_verify", "risk": None, "signals": [],
                "evidence_ids": [],
                "reason": f"cannot verify: fewer than {cfg['min_history_weeks']} weeks of orders"}
    series = adapter.client_volume(client.key, weeks * 2, until)
    before, recent = series[:weeks], series[weeks:]
    b = sum(w["orders"] for w in before)
    r = sum(w["orders"] for w in recent)
    if b == 0:
        return {**base, "status": "cannot_verify", "risk": None, "signals": [],
                "evidence_ids": [], "reason": "cannot verify: no orders in the earlier window"}
    drop = max(0.0, (b - r) / b)
    ids = [store.save_fact(_week_fact(adapter, client.name, w)) for w in series]
    level = risk_level(drop)
    signal = {"type": "volume_drop", "value": round(drop, 3),
              "summary": (f"{r} orders in the last {weeks} weeks vs {b} in the {weeks} "
                          f"weeks before ({drop:.0%} drop)" if r < b else
                          f"{r} orders in the last {weeks} weeks vs {b} before (no drop)"),
              "evidence_ids": ids}
    return {**base, "status": "kept", "risk": level, "score": round(
        drop * float(cfg["weights"].get("volume_drop", 1.0)), 3), "signals": [signal],
        "weekly": [{"week": w["week"], "orders": w["orders"]} for w in series],
        "evidence_ids": ids, "reason": signal["summary"]}


@task("check_churn")
def check_churn(ledger: Ledger, params: dict) -> dict:
    """params: clients (names or keys; empty = all), window_weeks, until (ISO date)."""
    notes: list[str] = []
    adapter = get_adapter(ledger, Budget(ledger, params["task_id"]), notes)
    until = date.fromisoformat(params["until"]) if params.get("until") else date.today()
    weeks = int(params.get("window_weeks") or settings()["churn"]["window_weeks"])
    want = [w.strip().lower() for w in params.get("clients") or []]
    clients = [c for c in adapter.list_clients()
               if not want or c.key.lower() in want
               or any(w in c.name.lower() for w in want)]
    store = EvidenceStore(ledger)
    out = [churn_for(adapter, store, c, until, weeks) for c in clients]
    rank = {"high": 0, "medium": 1, "low": 2, None: 3}
    out.sort(key=lambda c: (rank[c["risk"]], -c.get("score", 0)))
    missing = [w for w in want if not any(w in c["client"].lower() or w == c["client_key"].lower()
                                          for c in out)]
    return {"adapter": adapter.name, "synced_at": adapter.synced_at, "until": until.isoformat(),
            "window_weeks": weeks, "clients": out, "notes": notes
            + [f"no client matching {m!r} in {adapter.name}" for m in missing]}
