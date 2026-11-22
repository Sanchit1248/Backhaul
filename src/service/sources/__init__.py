"""Data-source interface. Steps call ONLY these methods, never an API directly.

Two implementations:
  * FixtureSources (sources/fixtures.py): reads tests/fixtures/*.json. Lets
    B, C, D build and test without Crustdata credits or network.
  * LiveSources (sources/live.py, owner A): Crustdata + web search/fetch +
    registries, with retries, cache and cost logging via sources/base.py.

Pick with config `sources.mode`. Every returned fact must carry a Source.
"""
from __future__ import annotations

from datetime import date
from typing import Protocol

from ..models import Buyer, ClientRecords, CompanyRef, IdentityMatch, JobRequest, Location, Trigger


class DataSources(Protocol):
    def discover(self, job: JobRequest, client: ClientRecords) -> list[CompanyRef]:
        """Candidate companies: target industries + headcount band, near the route
        end points, with logistics job posts or expansion news since job date - window."""

    def identify(self, company: CompanyRef) -> list[IdentityMatch]:
        """Ranked identity matches (Crustdata company identify). Best first."""

    def triggers(self, company: CompanyRef, since: date) -> list[Trigger]:
        """Hiring posts, expansion news and funding events. Return everything found,
        including undated/old ones: the triggers step applies the rules."""

    def locations(self, company: CompanyRef) -> list[Location]:
        """Plant / warehouse / office cities, each with its Source."""

    def people(self, company: CompanyRef, titles: list[str]) -> list[Buyer]:
        """Supply-chain / logistics / operations heads at the company, best first."""

    def verify_employment(self, buyer: Buyer) -> Buyer:
        """Fresh check of current employer + role dates. Returns an updated Buyer."""


def get_sources(ledger, budget) -> DataSources:
    from ..config import settings

    mode = settings()["sources"]["mode"]
    if mode == "fixtures":
        from .fixtures import FixtureSources

        return FixtureSources(settings()["sources"]["fixtures_file"])
    if mode == "live":
        from .live import LiveSources

        return LiveSources(ledger, budget)
    raise ValueError(f"unknown sources.mode {mode!r}")
