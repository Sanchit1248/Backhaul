"""Load route data (lanes, trucks, clients, orders) into a Fleetbase instance.

    python scripts/seed_fleetbase.py                       # tests/fixtures/routes_sample.json
    python scripts/seed_fleetbase.py path/to/kickoff.json  # same shape (see routes/adapter.py)
    python scripts/seed_fleetbase.py --dry-run             # print what would be created

Needs FLEETBASE_API_KEY (Fleetbase console -> Developers -> API keys) in .env and
routes.fleetbase.base_url (default http://localhost:8000). Uses the field convention in
docs/FLEETBASE.md. Re-runnable: ids already created are kept in data/fleetbase_seed.json
and skipped, so a crash halfway does not create duplicates.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service.budget import Budget
from service.config import DATA_DIR, env, settings
from service.ledger import Ledger
from service.routes.cities import coords
from service.sources.base import fetch_json

STATE = DATA_DIR / "fleetbase_seed.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?", default=str(ROOT / settings()["routes"]["file"]))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    data = json.loads(Path(a.file).read_text(encoding="utf-8"))
    base = (env("FLEETBASE_API_URL") or settings()["routes"]["fleetbase"]["base_url"]).rstrip("/")
    key = env("FLEETBASE_API_KEY")
    if not key and not a.dry_run:
        print("FLEETBASE_API_KEY is not set in .env", file=sys.stderr)
        return 2
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    led = Ledger()
    budget = Budget(led, "fleetbase-seed")

    def post(kind: str, our_id: str, body: dict) -> str:
        sk = f"{base}|{kind}|{our_id}"
        if sk in state:
            return state[sk]
        if a.dry_run:
            print(f"would create {kind} {our_id}: {json.dumps(body)[:160]}")
            return f"dry-{our_id}"
        res = fetch_json(led, budget, provider="fleetbase", op=f"seed.{kind}", method="POST",
                         url=f"{base}/v1/{kind}", json_body=body, use_cache=False, retries=1,
                         headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
        rid = res["data"].get("id") or res["data"].get("public_id")
        state[sk] = rid
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1))
        return rid

    cities = sorted({c for o in data.get("orders", []) for c in (o["origin"], o["destination"])}
                    | {c["city"] for c in data.get("clients", []) if c.get("city")})
    places = {}
    for city in cities:
        ll = coords(city)
        body = {"name": f"{city.title()} hub", "street1": f"{city.title()} hub",
                "city": city.title(), "country": "IN"}
        if ll:
            body.update(latitude=ll[0], longitude=ll[1])
        places[city] = post("places", f"place:{city}", body)
    contacts = {c["id"]: post("contacts", f"client:{c['id']}", {
        "name": c["name"], "type": "customer", "meta": {"city": c.get("city"), "lanelead_id": c["id"]}})
        for c in data.get("clients", [])}
    for v in data.get("vehicles", []):
        post("vehicles", f"vehicle:{v['id']}", {
            "name": v["id"], "plate_number": v["id"],
            "payload_capacity": v.get("payload_capacity_kg"),
            "meta": {"lane": v["lane"], "trips_per_week": v["trips_per_week"]}})
    for o in data.get("orders", []):
        post("orders", f"order:{o['id']}", {
            "pickup": places[o["origin"]], "dropoff": places[o["destination"]],
            "customer": contacts.get(o["customer"]), "scheduled_at": o["scheduled_at"],
            "type": "transport", "meta": {"weight_kg": o.get("weight_kg"), "lanelead_id": o["id"]}})
    print(f"{'planned' if a.dry_run else 'seeded'}: {len(places)} places, {len(contacts)} "
          f"clients, {len(data.get('vehicles', []))} vehicles, {len(data.get('orders', []))} "
          f"orders -> {base}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
