"""Crustdata API client (people, company, jobs, web search/fetch, watchers).

Docs: https://docs.crustdata.com  - VERIFY endpoint paths and auth header
against the docs at kickoff; the ones below are placeholders to fill in.
Each Crustdata record carries the date of its last refresh -> put it in
Source.record_date.
"""
from __future__ import annotations

from ..budget import Budget
from ..config import env
from ..ledger import Ledger
from ..models import Source
from .base import fetch_json

BASE_URL = env("CRUSTDATA_BASE_URL", "https://api.crustdata.com")
COST_PER_CALL_USD = 0.0  # set from the credit price once known


def _headers() -> dict:
    token = env("CRUSTDATA_API_KEY")
    if not token:
        raise RuntimeError("CRUSTDATA_API_KEY not set (see .env.example)")
    return {"Authorization": f"Token {token}", "Accept": "application/json"}


def company(ledger: Ledger, budget: Budget, *, domain: str) -> dict:
    """TODO(kickoff): confirm path/params in docs.crustdata.com."""
    return fetch_json(ledger, budget, provider="crustdata", op="company", method="GET",
                      url=f"{BASE_URL}/screener/company", headers=_headers(),
                      params={"company_domain": domain}, cost_usd=COST_PER_CALL_USD)


def cite(resp: dict, field_path: str, value, record_date: str | None = None) -> Source:
    """Build a Source pointing at one field of an API response."""
    return Source(url=resp["url"], kind="api_response", excerpt=f"{field_path} = {value!r}",
                  field_path=field_path, record_date=record_date,
                  retrieved_at=resp["retrieved_at"])
