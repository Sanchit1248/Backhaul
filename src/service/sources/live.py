"""LiveSources: the real DataSources. OWNER: A (wired by B on the user's request).

Each method delegates to A's mapping module, all sharing one CrustdataClient per job
(one cache, one budget, serialized calls):
  discover           live_companies.discover_hiring_first (logistics job posts in the route
                     cities inside the window -> company search by id with India + industry
                     + headcount); falls back to live_companies.discover (company index by
                     HQ city) when that finds nothing. Config discovery.strategy.
  identify           live_companies.identify   (company identify, domain first, then name)
  triggers           live_signals.triggers     (job search, company enrich funding/news,
                                                web search + fetch for expansion passages)
  locations          live_companies.locations  (company enrich HQ + office addresses)
  people             live_buyers.people        (person search at the employer id)
  verify_employment  live_buyers.verify_employment (person enrich, current role dates)

Rules (see the modules): every HTTP call goes through sources.base.fetch_json, every
value carries a models.Source, SourceUnavailable means "cannot verify", and
BudgetExceeded always propagates to the pipeline.
"""
from __future__ import annotations

import json
from datetime import date

from ..config import settings
from ..models import Buyer, ClientRecords, CompanyRef, IdentityMatch, JobRequest, Location, Trigger
from . import live_buyers, live_companies, live_signals
from .base import SourceUnavailable
from .crustdata import CrustdataClient


class LiveSources:
    def __init__(self, ledger, budget, client: CrustdataClient | None = None):
        self.ledger, self.budget = ledger, budget
        self.client = client or CrustdataClient(ledger, budget)
        self.notes: list[str] = []

    def _note(self, msg: str) -> None:
        self.notes.append(msg)
        self.ledger.log(self.budget.run_id, "info", msg)

    def discover(self, job: JobRequest, client: ClientRecords) -> list[CompanyRef]:
        notes: list[str] = []
        cfg = settings()
        found: list[CompanyRef] = []
        if cfg.get("discovery", {}).get("strategy", "hiring_first") == "hiring_first":
            posts: list[dict] = []
            found = live_companies.discover_hiring_first(self.client, job, client, cfg=cfg,
                                                         notes=notes, posts_out=posts)
            if posts:
                self._save_posts(posts[0])
            if not found:
                notes.append("discovery: no hiring-first candidates; falling back to the "
                             "company index (largest companies headquartered on the route)")
        if not found:
            found = live_companies.discover(self.client, job, client, cfg=cfg, notes=notes)
        for n in notes:
            self._note(n)
        return found

    def identify(self, company: CompanyRef) -> list[IdentityMatch]:
        return live_companies.identify(self.client, company)

    # Discovery's job posts, kept in the ledger (survives a restart, costs nothing again).
    POSTS_STEP = "02_discover_posts"

    def _save_posts(self, resp: dict) -> None:
        self._posts = resp
        self.ledger.step(self.budget.run_id, self.POSTS_STEP, lambda: resp)

    def _discovery_posts(self) -> dict | None:
        if getattr(self, "_posts", None) is None:
            row = self.ledger.db.execute(
                "SELECT output FROM steps WHERE run_id=? AND step=?",
                (self.budget.run_id, self.POSTS_STEP)).fetchone()
            self._posts = json.loads(row[0]) if row else {}
        return self._posts or None

    def triggers(self, company: CompanyRef, since: date) -> list[Trigger]:
        """Company-wide triggers, with this company's posts from discovery FIRST: those are
        the ones located on the route, and the company-wide job search (newest 10) can
        miss them for multinationals hiring worldwide."""
        local: list[Trigger] = []
        posts = self._discovery_posts()
        if posts:
            try:
                local = live_signals.map_job_listings(posts, company)
            except SourceUnavailable:
                local = []
        try:
            found = live_signals.triggers(self.client, company, since)
        except SourceUnavailable:
            if local:
                return local
            raise
        seen = {_post_key(t) for t in local}
        return local + [t for t in found if _post_key(t) not in seen]

    def locations(self, company: CompanyRef) -> list[Location]:
        return live_companies.locations(self.client, company)

    def people(self, company: CompanyRef, titles: list[str]) -> list[Buyer]:
        return live_buyers.people(self.client, company, titles)

    def verify_employment(self, buyer: Buyer) -> Buyer:
        return live_buyers.verify_employment(self.client, buyer)


def _post_key(t: Trigger) -> str:
    """Identity of one job post: its own URL (the job_details.url source), else
    type + summary + date. Never the API endpoint URL, which every trigger shares."""
    for src in t.fact.sources:
        if (src.field_path or "").endswith("job_details.url"):
            return src.url.strip().lower()
    return f"{t.type}|{t.summary.lower()}|{t.event_date}"
