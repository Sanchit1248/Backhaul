"""Live company mapping: discovery shortlist, identity matches and sourced locations. OWNER: A.

Each function takes the shared CrustdataClient first, then the arguments of the
matching DataSources method. `client.call(operation, *, body=None, params=None)`
returns the fetch_json envelope {"data", "url", "retrieved_at"}.

Outcomes are kept apart on purpose:
  * a successful call with no rows  -> an empty list
  * a provider failure or a response of the wrong shape -> SourceUnavailable
  * BudgetExceeded from the client  -> propagates untouched (never caught here)

Request shapes follow the saved smoke run (API version 2025-11-01): filter
operators are `=>`/`=<`, `basic_info.industries` is filterable but projected as
`taxonomy.professional_network_industries`, and `locations.city/state/country`
are the headquarters fields of the company index.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from ..models import ClientRecords, CompanyRef, IdentityMatch, JobRequest, Location, Source
from .base import SourceUnavailable
from .crustdata import cite

# Config tokens (discovery.industries) -> values seen in the Crustdata index in the
# saved evidence (T3 autocomplete and T1/T3 records). Tokens without a verified value
# are left out of the filter and reported by `industry_filter`, never guessed.
# `discovery.index_industries` in config overrides this table (B owns config).
INDEX_INDUSTRIES: dict[str, list[str]] = {
    "manufacturing": ["Manufacturing"],
    "fmcg": ["Consumer Goods"],
    "consumer goods": ["Consumer Goods"],
    "retail": ["Retail"],
    "chemicals": ["Chemical Manufacturing"],
    "automotive": ["Motor Vehicle Manufacturing", "Transportation Equipment Manufacturing"],
}

SEARCH_FIELDS = [
    "crustdata_company_id",
    "basic_info.name",
    "basic_info.primary_domain",
    "taxonomy.professional_network_industries",
    "headcount.total",
    "locations.country",
    "locations.state",
    "locations.city",
    "metadata.updated_at",
]

# One field set for every company_enrich call, so the locations lookup and the
# funding/news triggers send the same body and share the client cache.
ENRICH_FIELDS = ["basic_info", "headcount", "funding", "locations", "news", "metadata"]


# ------------------------------------------------------------------ helpers ---

def _data(resp: dict, kind: type, op: str) -> Any:
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, dict) and "error" in data:
        raise SourceUnavailable(f"crustdata.{op} returned an error: {data['error']}")
    if not isinstance(data, kind):
        raise SourceUnavailable(
            f"crustdata.{op} returned {type(data).__name__}, expected {kind.__name__}")
    return data


def _day(ts: str | None) -> str | None:
    """Record date (YYYY-MM-DD) from an index timestamp; retrieval time is separate."""
    return ts[:10] if isinstance(ts, str) and len(ts) >= 10 else None


def _fold(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _domain(d: str | None) -> str:
    d = (d or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0]
    return d[4:] if d.startswith("www.") else d


def _id(v) -> str | None:
    return str(v) if v not in (None, "") else None


def _industry(values) -> str | None:
    return values[0] if isinstance(values, list) and values else None


def industry_filter(tokens: list[str], table: dict[str, list[str]] | None = None
                    ) -> tuple[list[str], list[str]]:
    """Map config industry tokens to index values. Returns (values, unmapped tokens)."""
    table = {k.lower(): v for k, v in (table or INDEX_INDUSTRIES).items()}
    values: list[str] = []
    unmapped: list[str] = []
    for t in tokens:
        mapped = table.get(t.strip().lower())
        if not mapped:
            unmapped.append(t)
        for v in mapped or []:
            if v not in values:
                values.append(v)
    return values, unmapped


def route_cities(job: JobRequest, records: ClientRecords) -> list[str]:
    """Route end points the job can serve: the job's route filter when it matches a
    route with spare trucks, else every end point with spare trucks (mirrors s05)."""
    routes = [r for r in records.routes if r.spare_trucks_per_week > 0]
    if job.route_filter:
        wanted = {c.lower() for c in job.route_filter}
        routes = [r for r in routes if {r.origin, r.destination} & wanted] or routes
    cities: list[str] = []
    for r in routes:
        for c in (r.origin, r.destination):
            if c and c.lower() not in cities:
                cities.append(c.lower())
    return cities


# ---------------------------------------------------------------- discover ---

def search_body(job: JobRequest, records: ClientRecords, cfg: dict) -> tuple[dict, list[str]]:
    """Company search request built from runtime config. Returns (body, notes)."""
    disc = cfg.get("discovery", {})
    limit = int(cfg.get("limits", {}).get("max_candidates", 40))
    notes: list[str] = []
    conds: list[dict] = [{"field": "locations.country", "type": "=", "value": "India"}]

    values, unmapped = industry_filter(disc.get("industries", []), disc.get("index_industries"))
    if values:
        conds.append({"field": "basic_info.industries", "type": "in", "value": values})
    if unmapped:
        notes.append("discovery: no verified index value for industries "
                     + ", ".join(unmapped) + " (not filtered on)")
    if disc.get("headcount_min") is not None:
        conds.append({"field": "headcount.total", "type": "=>",
                      "value": int(disc["headcount_min"])})
    if disc.get("headcount_max") is not None:
        conds.append({"field": "headcount.total", "type": "=<",
                      "value": int(disc["headcount_max"])})
    cities = route_cities(job, records)
    if cities:
        # The index filters on the headquarters city only; plants elsewhere are missed.
        conds.append({"field": "locations.city", "type": "in",
                      "value": [c.title() for c in cities]})
        notes.append("discovery: route filter applies to headquarters city ("
                     + ", ".join(cities) + "); plants or warehouses elsewhere are not indexed")
    body = {"filters": {"op": "and", "conditions": conds}, "fields": SEARCH_FIELDS,
            "limit": limit, "sorts": [{"field": "headcount.total", "order": "desc"}]}
    return body, notes


def discover(client, job: JobRequest, records: ClientRecords, *, cfg: dict | None = None,
             notes: list[str] | None = None) -> list[CompanyRef]:
    """Bounded shortlist from the indexed company search. Hiring evidence is joined
    later (A3/A5); this only applies what the index can filter on."""
    if cfg is None:
        from ..config import settings

        cfg = settings()
    body, extra = search_body(job, records, cfg)
    if notes is not None:
        notes.extend(extra)
    resp = client.call("company_search", body=body)
    data = _data(resp, dict, "company_search")
    rows = data.get("companies")
    if not isinstance(rows, list):
        raise SourceUnavailable("crustdata.company_search response has no companies list")

    out: list[CompanyRef] = []
    seen: set[str] = set()
    for row in rows[: body["limit"]]:
        info = row.get("basic_info") or {}
        cid = _id(row.get("crustdata_company_id") or info.get("crustdata_company_id"))
        name = info.get("name")
        if not name:
            continue
        key = cid or _domain(info.get("primary_domain")) or _fold(name)
        if key in seen:
            continue
        seen.add(key)
        out.append(CompanyRef(
            name=name,
            domain=_domain(info.get("primary_domain")) or None,
            crustdata_id=cid,
            industry=_industry((row.get("taxonomy") or {}).get("professional_network_industries")),
            headcount=(row.get("headcount") or {}).get("total"),
        ))
    return out


# ---------------------------------------------------------------- identify ---

def _corroboration(company: CompanyRef, info: dict, cid: str | None) -> int:
    """2 = discovered id or primary domain matches the input, 1 = input domain is one
    of the record's domains, 0 = name only. Breaks score ties, never changes scores."""
    want = _domain(company.domain)
    if company.crustdata_id and cid == str(company.crustdata_id):
        return 2
    if want and _domain(info.get("primary_domain")) == want:
        return 2
    if want and want in {_domain(d) for d in info.get("all_domains") or []}:
        return 1
    return 0


def _matches(company: CompanyRef, resp: dict, op: str, key: str) -> list[IdentityMatch]:
    entries = _data(resp, list, op)
    ranked: list[tuple[float, int, int, IdentityMatch]] = []
    for e in entries:
        if not isinstance(e, dict):
            raise SourceUnavailable(f"crustdata.{op} entry is not an object")
        if e.get("matched_on") is not None and _fold(str(e["matched_on"])) != _fold(key):
            continue
        for j, m in enumerate(e.get("matches") or []):
            cd = m.get("company_data") or {}
            info = cd.get("basic_info") or {}
            cid = _id(cd.get("crustdata_company_id") or info.get("crustdata_company_id"))
            if not info.get("name") or m.get("confidence_score") is None:
                continue
            ref = CompanyRef(name=info["name"], domain=_domain(info.get("primary_domain")) or None,
                             crustdata_id=cid, industry=_industry(info.get("industries")))
            score = float(m["confidence_score"])
            ranked.append((score, _corroboration(company, info, cid), -j,
                           IdentityMatch(company=ref, score=score)))
    ranked.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    return [t[3] for t in ranked]


def identify(client, company: CompanyRef) -> list[IdentityMatch]:
    """Ranked identity matches, best first, with every match kept for the hold rules.

    A known domain is looked up exactly first; only when that lookup is empty does
    a name lookup run, so a domain mismatch reaches the step's mismatch rule.
    Equal scores are ordered by corroboration (id, primary domain, any domain)."""
    if company.domain:
        d = _domain(company.domain)
        resp = client.call("company_identify", body={"domains": [d], "exact_match": True})
        found = _matches(company, resp, "company_identify", d)
        if found:
            return found
    if not company.name:
        return []
    resp = client.call("company_identify", body={"names": [company.name]})
    return _matches(company, resp, "company_identify", company.name)


# --------------------------------------------------------------- locations ---

_COUNTRY_TAIL = re.compile(r"^(in|ind|india)$", re.I)


def _office_city(address: str) -> str | None:
    """City from an index office address shaped "street, city, [state,] [pin,] IN".
    Returns None when the address does not have that shape."""
    parts = [p.strip() for p in address.split(",")]
    if len(parts) < 3 or not _COUNTRY_TAIL.match(parts[-1]):
        return None
    city = parts[1]
    if not city or not re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", city):
        return None
    return city


def _with_excerpt(src: Source, excerpt: str) -> Source:
    return src.model_copy(update={"excerpt": excerpt})


def locations(client, company: CompanyRef) -> list[Location]:
    """Locations stated by company enrichment, each citing its exact field.

    * `locations.city` is labelled "hq" only when the same record's
      `locations.headquarters` text names that city; otherwise "unknown".
    * each `locations.all_office_addresses[i]` with a parsable city is an "office".
    * nothing here is ever labelled plant or warehouse: an address alone says
      nothing about what the site is used for.
    """
    if not company.crustdata_id:
        raise ValueError("locations() needs company.crustdata_id; resolve identity first")
    cid = str(company.crustdata_id)
    body = {"crustdata_company_ids": [int(cid) if cid.isdigit() else cid],
            "fields": ENRICH_FIELDS}
    resp = client.call("company_enrich", body=body)
    entries = _data(resp, list, "company_enrich")

    out: list[Location] = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise SourceUnavailable("crustdata.company_enrich entry is not an object")
        for j, m in enumerate(e.get("matches") or []):
            cd = m.get("company_data") or {}
            if _id(cd.get("crustdata_company_id")) != cid:
                continue
            loc = cd.get("locations") or {}
            rec = _day((cd.get("metadata") or {}).get("updated_at"))
            base = f"[{i}].matches[{j}].company_data.locations"
            city, hq = loc.get("city"), loc.get("headquarters")
            if isinstance(city, str) and city.strip():
                src = cite(resp, f"{base}.city", city, rec)
                if isinstance(hq, str) and _fold(city) and _fold(city) in _fold(hq):
                    src = _with_excerpt(src, f"{src.excerpt}; {base}.headquarters = {hq!r}")
                    kind = "hq"
                else:
                    kind = "unknown"
                out.append(Location(city=city.strip(), kind=kind, source=src))
            for k, addr in enumerate(loc.get("all_office_addresses") or []):
                c = _office_city(addr) if isinstance(addr, str) else None
                if c:
                    out.append(Location(city=c, kind="office",
                                        source=cite(resp, f"{base}.all_office_addresses[{k}]",
                                                    addr, rec)))
    return out
