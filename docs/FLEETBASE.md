# Fleetbase fields LaneLead uses

Fleetbase (FleetOps API v1) has no "lane" or "weekly capacity" object, so LaneLead reads
these fields. Checked against the running instance's v1 resources and create-request
rules on 22 Nov 2026.

| What LaneLead needs | Fleetbase record | Field |
|---|---|---|
| Lane a truck runs | vehicle | `meta.lane` = sorted city pair, e.g. `mumbai-pune` |
| Truck trips per week on that lane | vehicle | `meta.trips_per_week` |
| Truck payload | vehicle | `payload_capacity` |
| A load on a lane | order | `payload.pickup.city` → `payload.dropoff.city` |
| Week the load uses a truck | order | `scheduled_at` (falls back to `created_at`) |
| Load cancelled | order | `status` in `canceled` / `cancelled` / `void` |
| Load weight | order | `meta.weight_kg` |
| Which client | order | `customer` (a contact) |
| Client list | contact | `type = customer`; `meta.city` = main city |

**Free capacity** for a lane and ISO week = sum of `meta.trips_per_week` over vehicles
on the lane, minus the orders on the lane (either direction) scheduled that week and
not cancelled. If no vehicle on the lane has `meta.trips_per_week`, capacity is
**unknown**. LaneLead says so; it never guesses a number.

## Setup
1. Fleetbase running locally (`~/fleetbase`, API on `http://localhost:8000`).
2. Console → Developers → API keys → create a key; put it in `.env` as
   `FLEETBASE_API_KEY=flb_...`. Never put the key in config.
3. Load data: `python scripts/seed_fleetbase.py` (the sample) or
   `python scripts/seed_fleetbase.py kickoff_routes.json`. Use `--dry-run` first.
   You can re-run it: created ids are kept in `data/fleetbase_seed.json`.
4. `config/service.yaml` → `routes.adapter: fleetbase`. If Fleetbase is down or the key
   is missing, LaneLead falls back to `routes.file` and says so in the result
   (`routes.fallback_to_file`).
5. `python -m service.routes.sync --every 3600` keeps the map's snapshot fresh (hourly).

Not verified against a live key yet: the seed script's create calls and the
`?type=customer` filter on contacts. The adapter's read mapping is tested against
Fleetbase-shaped responses (`tests/test_lanelead.py`).
