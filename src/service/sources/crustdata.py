"""Versioned Crustdata client for the endpoints covered by Person A's smoke run."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from threading import Lock

from ..budget import Budget
from ..config import env, settings
from ..ledger import Ledger
from ..models import Source
from .base import CreditEstimate, SourceUnavailable, fetch_json

BASE_URL = env("CRUSTDATA_BASE_URL", "https://api.crustdata.com")
API_VERSION = "2025-11-01"

# Prices in hundredths of a credit, from the smoke account's endpoint inventory.
_ENDPOINTS = {
    "company_search": ("/company/search", 3),
    "company_identify": ("/company/identify", 20),
    "company_enrich": ("/company/enrich", 200),
    "job_search": ("/job/search", 3),
    "person_search": ("/person/search", 3),
    "person_enrich": ("/person/enrich", 100),
    "web_search": ("/web/search/live", 100),
    "web_fetch": ("/web/enrich/live", 100),
}
_COMPANY_SEARCH_FIELDS = {
    "taxonomy": 10, "followers": 20, "headcount": 20, "roles": 20, "funding": 20,
    "hiring": 20, "software_reviews": 20, "maps": 20,
}
_COMPANY_SEARCH_FILTERS = {**_COMPANY_SEARCH_FIELDS, "technographics": 100}
_PERSON_SEARCH_FILTERS = {
    "experience": 10, "education": 10, "honors": 20, "skills": 10, "certifications": 20,
    "professional_network.connections": 20, "professional_network.followers": 20,
    "professional_network.open_to_cards": 20, "professional_network.languages": 20,
    "dev_platform_profiles": 80, "dev_platform_profiles.repos": 250, "research_profiles": 20,
}
_PERSON_SEARCH_FIELDS = {**_PERSON_SEARCH_FILTERS, "professional_network.summary": 20}
_COMPANY_ENRICH_FIELDS = {"technographics": 200, "social_posts": 500}
_PERSON_ENRICH_FIELDS = {
    "contact.business_emails": 100, "contact.personal_emails": 200, "contact.phone_numbers": 200,
    "dev_platform_profiles": 100, "social_posts": 500, "research_profiles": 200,
}
_COMPANY_IDENTIFIERS = (
    "names", "domains", "crustdata_company_ids", "professional_network_profile_urls",
)


def _filter_fields(node: dict | None):
    if node is None:
        return
    if "field" in node:
        yield node["field"]
    for child in node.get("conditions", []):
        yield from _filter_fields(child)


def _premium_cost(prices: dict[str, int], fields) -> int:
    groups = {}
    for field in fields:
        for path, price in prices.items():
            if (field in ("*", path) or field.startswith(path + ".")
                    or path.startswith(field + ".")):
                # The repository tier replaces the base developer profile tier.
                group = path.removesuffix(".repos")
                groups[group] = max(price, groups.get(group, 0))
    return sum(groups.values())


def _list_length(payload: dict, key: str) -> int:
    values = payload.get(key)
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError(f"Crustdata requires a nonempty {key} array")
    return len(values)


def _search_limit(operation: str, payload: dict) -> int:
    # All three searches default to 20. Person search also accepts `count`.
    limits = [payload[key] for key in ("limit", "count") if key in payload
              and (key == "limit" or operation == "person_search")]
    minimum = 0 if operation == "job_search" else 1
    if any(type(limit) is not int or not minimum <= limit <= 1000 for limit in limits):
        raise ValueError(f"{operation} requires an integer limit between {minimum} and 1000")
    return max(limits, default=20)


def _estimated_credits(operation: str, base: int, payload: dict) -> Decimal:
    fields = payload.get("fields")
    if operation in ("company_search", "person_search", "job_search"):
        count = _search_limit(operation, payload)
        if operation == "company_search":
            base += _premium_cost(_COMPANY_SEARCH_FILTERS, _filter_fields(payload.get("filters")))
            base += _premium_cost(_COMPANY_SEARCH_FIELDS, fields or ["*"])
        if operation == "person_search":
            base += _premium_cost(_PERSON_SEARCH_FILTERS, _filter_fields(payload.get("filters")))
            default_fields = [path for path in _PERSON_SEARCH_FIELDS
                              if not path.startswith(("dev_platform_profiles", "research_profiles"))]
            base += _premium_cost(_PERSON_SEARCH_FIELDS, fields or default_fields)
    elif operation in ("company_identify", "company_enrich"):
        supplied = [key for key in _COMPANY_IDENTIFIERS if payload.get(key) is not None]
        if len(supplied) != 1:
            raise ValueError(f"{operation} requires exactly one company identifier array")
        key = supplied[0]
        count = _list_length(payload, key)
        if operation == "company_enrich":
            base += _premium_cost(_COMPANY_ENRICH_FIELDS, fields or [])
            if key != "crustdata_company_ids":
                # Ambiguous identifiers can enrich multiple companies (up to 25 per request).
                count = max(count, 25)
    elif operation == "person_enrich":
        count = _list_length(payload, "professional_network_profile_urls")
        base += _premium_cost(_PERSON_ENRICH_FIELDS, fields or [])
        if payload.get("preview") is True:
            base = 0
    elif operation == "web_search":
        pages = payload.get("page", 1)
        if type(pages) is not int or not 1 <= pages <= 15:
            raise ValueError("web_search requires an integer page count between 1 and 15")
        sources = payload.get("sources")
        count = pages * (len(sources) if sources else 7)  # Omission searches every source.
    else:
        count = _list_length(payload, "urls")
    return Decimal(base * count) / 100


def _headers() -> dict:
    token = env("CRUSTDATA_API_KEY")
    if not token:
        raise SourceUnavailable("CRUSTDATA_API_KEY not set (see .env.example)")
    return {"Authorization": f"Bearer {token}", "x-api-version": API_VERSION,
            "Accept": "application/json"}


class CrustdataClient:
    def __init__(self, ledger: Ledger, budget: Budget):
        self.ledger, self.budget = ledger, budget
        self._lock = Lock()

    def call(self, operation: str, *, body: dict | None = None, params: dict | None = None) -> dict:
        """Send a versioned JSON POST and return the original fetch_json envelope."""
        if operation not in _ENDPOINTS:
            raise ValueError(f"unknown Crustdata operation {operation!r}")
        path, base = _ENDPOINTS[operation]
        maximum = _estimated_credits(operation, base, body or {})
        costs = settings()["costs"]
        credits = CreditEstimate(maximum, float(costs["crustdata_inr_per_credit"]),
                                 float(costs["usd_to_inr"]))
        # One client per job keeps its cache misses and budget checks serialized.
        with self._lock:
            return fetch_json(
                self.ledger, self.budget, provider="crustdata", op=operation, method="POST",
                url=f"{BASE_URL.rstrip('/')}{path}", headers=_headers(), params=params,
                json_body=body, retries=2, credits=credits,
            )


def company(ledger: Ledger, budget: Budget, *, domain: str) -> dict:
    """Resolve an exact company domain via the versioned client."""
    return CrustdataClient(ledger, budget).call(
        "company_identify", body={"domains": [domain], "exact_match": True}
    )


def cite(response: dict, field_path: str, value, record_date: date | str | None = None) -> Source:
    """Build a Source pointing at one field of an API response."""
    if isinstance(record_date, str) and len(record_date) > 10:
        record_date = datetime.fromisoformat(record_date).date()
    if isinstance(record_date, datetime):
        record_date = record_date.date()
    return Source(url=response["url"], kind="api_response", excerpt=f"{field_path} = {value!r}",
                  field_path=field_path, record_date=record_date,
                  retrieved_at=response["retrieved_at"])
