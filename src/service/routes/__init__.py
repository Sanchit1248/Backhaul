"""Routes layer (the 'B' layer of docs/LANELEAD.md): the 3PL's lanes, trucks and loads.

  adapter.py    RouteAdapter interface + the capacity calculator both adapters share
  files.py      FileAdapter (JSON file or a folder of CSVs): the backup
  fleetbase.py  FleetbaseAdapter (Fleetbase REST API v1)
  fit.py        route_fit(company_sites) -> matched lane, score, reason, or "partial"
  sync.py       snapshot of lanes + capacity (hourly job; the map reads it)
  cities.py     fixed table of Indian city coordinates for the map
"""
from .adapter import ClientInfo, Lane, LaneCapacity, RouteAdapter, week_start  # noqa: F401
from .factory import get_adapter  # noqa: F401
