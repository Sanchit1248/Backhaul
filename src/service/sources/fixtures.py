"""Fixture-backed DataSources for tests and offline development (mock data is
permitted only for tests; screening and finals must run with sources.mode: live)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ..config import ROOT
from ..models import Buyer, ClientRecords, CompanyRef, IdentityMatch, JobRequest, Location, Trigger


class FixtureSources:
    def __init__(self, path: str | Path):
        p = Path(path)
        self.data = json.loads((p if p.is_absolute() else ROOT / p).read_text(encoding="utf-8"))
        # index each fixture by discovery name + every identified legal name / domain / id
        self.index: dict[str, dict] = {}
        for key, c in self.data["companies"].items():
            self.index[key] = c
            for m in c.get("identify", []):
                for k in ("name", "domain", "crustdata_id"):
                    if m["company"].get(k):
                        self.index.setdefault(m["company"][k], c)

    def _c(self, company: CompanyRef) -> dict:
        for k in (company.crustdata_id, company.domain, company.name):
            if k and k in self.index:
                return self.index[k]
        return {}

    def discover(self, job: JobRequest, client: ClientRecords) -> list[CompanyRef]:
        return [CompanyRef(**c) for c in self.data["discover"]]

    def identify(self, company: CompanyRef) -> list[IdentityMatch]:
        return [IdentityMatch(**m) for m in self._c(company).get("identify", [])]

    def triggers(self, company: CompanyRef, since: date) -> list[Trigger]:
        return [Trigger(**t) for t in self._c(company).get("triggers", [])]

    def locations(self, company: CompanyRef) -> list[Location]:
        return [Location(**loc) for loc in self._c(company).get("locations", [])]

    def people(self, company: CompanyRef, titles: list[str]) -> list[Buyer]:
        return [Buyer(**b) for b in self._c(company).get("people", [])]

    def verify_employment(self, buyer: Buyer) -> Buyer:
        upd = self.data.get("employment_updates", {}).get(buyer.person_id or buyer.name)
        return buyer.model_copy(update=upd) if upd else buyer
