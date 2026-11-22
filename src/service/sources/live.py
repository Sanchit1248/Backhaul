"""LiveSources: the real DataSources. OWNER: A.

Map each method to Crustdata (primary) and the fallbacks from the plan:
  discover           Crustdata company search / job search (posting date, location)
  identify           Crustdata company identify (ranked matches); fallback MCA/GST legal name
  triggers           job search (hiring), web search + fetch for exact news passage
                     (expansion), company enrichment (funding)
  locations          company enrichment; fallback GST principal place, website locations page
  people             person search + enrichment (titles from config buyer.titles)
  verify_employment  current employer + role start; work-history verification

Rules for every method:
  * every HTTP call through sources.base.fetch_json (retry, cache, cost ledger, budget)
  * every value returned carries a models.Source with the exact field/passage + record date
  * on SourceUnavailable: return what you have; the step records CannotVerify
  * work facts only for people: name, title, employer, role dates, business email
"""
from __future__ import annotations

from datetime import date

from ..models import Buyer, ClientRecords, CompanyRef, IdentityMatch, JobRequest, Location, Trigger


class LiveSources:
    def __init__(self, ledger, budget):
        self.ledger, self.budget = ledger, budget

    def discover(self, job: JobRequest, client: ClientRecords) -> list[CompanyRef]:
        raise NotImplementedError("A: Crustdata discovery")

    def identify(self, company: CompanyRef) -> list[IdentityMatch]:
        raise NotImplementedError("A: Crustdata company identify")

    def triggers(self, company: CompanyRef, since: date) -> list[Trigger]:
        raise NotImplementedError("A: job posts + news + funding")

    def locations(self, company: CompanyRef) -> list[Location]:
        raise NotImplementedError("A: enrichment / GST / website")

    def people(self, company: CompanyRef, titles: list[str]) -> list[Buyer]:
        raise NotImplementedError("A: person search")

    def verify_employment(self, buyer: Buyer) -> Buyer:
        raise NotImplementedError("A: employment check")
