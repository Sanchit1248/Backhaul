"""Pick the route adapter from config routes.adapter (fleetbase | file)."""
from __future__ import annotations

from ..budget import Budget
from ..config import settings
from ..ledger import Ledger
from ..sources.base import SourceUnavailable
from .adapter import RouteAdapter
from .files import FileAdapter
from .fleetbase import FleetbaseAdapter


def get_adapter(ledger: Ledger, budget: Budget, notes: list[str] | None = None) -> RouteAdapter:
    cfg = settings().get("routes", {})
    kind = cfg.get("adapter", "file")
    file_path = cfg.get("file") or settings()["client"]["records_file"]
    if kind != "fleetbase":
        return FileAdapter(file_path)
    fb = FleetbaseAdapter(ledger, budget)
    try:
        fb.data()
        return fb
    except SourceUnavailable as e:
        if not cfg.get("fallback_to_file", True):
            raise
        if notes is not None:
            notes.append(f"Fleetbase unavailable ({e}); using the route file {file_path}")
        return FileAdapter(file_path)
