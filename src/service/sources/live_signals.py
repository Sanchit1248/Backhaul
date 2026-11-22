"""Live trigger signal mapping for Crustdata + published web pages. OWNER: A (a/signals).

Implements `triggers(client, company, since)` over the shared Crustdata client
interface:
  - `client.call(operation, *, body=None, params=None)` -> `{data, url, retrieved_at}`
  - `cite(response, field_path, value, record_date=None)` -> `models.Source`

Signal channels:
  1. Hiring (`job_search`):
     - Primary query by `company.basic_info.crustdata_company_id` when known.
     - Fallback company-name lookup when an ID-filtered query misses (or no ID is
       known), keeping ONLY rows corroborated against the target company (exact
       company ID, matching domain/website/job URL host, or exact normalized
       employer name + sourced location city match). Name-only employer matches
       and non-freight keyword hits (e.g. visa/travel admin) are rejected.
     - Cites both the original job URL and the API posting-date field
       (`metadata.date_added`), preserving old and undated listings for Person D's
       `s04_triggers.py`.
  2. Funding (`company_enrich`):
     - Maps the dated funding fields (`funding.last_fundraise_date`,
       `funding.last_round_type`, `funding.last_round_amount_usd`), cross-checking
       `funding.milestones` when present so an older round's amount is not
       attached to an undisclosed latest round.
     - Preserves old and undated funding signals for Person D's rules.
  3. Expansion (`web_search` + `web_fetch`):
     - Never emits an expansion trigger from a search snippet alone.
     - Requires a fetched published page containing an exact verbatim passage
       supporting a physical facility/logistics expansion for the target company.
     - Extracts publication dates from HTML metadata (`article:published_time`,
       `iso-8601-publish-date`, JSON-LD `datePublished`, `<time datetime>`),
       keeping publication date strictly distinct from fetch retrieval `timestamp`.
  4. New-leader signals (`person_search` / F3):
     - Shared `models.TriggerType` only permits `"hiring" | "expansion" | "funding"`.
     - New-leader signals are parsed and flagged separately via `NewLeaderSignal`
       and `NEW_LEADER_CONTRACT_GAP` rather than coerced into an existing type.

Error handling:
  - `BudgetExceeded` is never caught and always propagates.
  - Provider failures (`SourceUnavailable` or malformed/failed provider payloads)
    are kept distinct from successful empty results (`[]`).
"""
from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from ..budget import BudgetExceeded
from ..models import CompanyRef, Fact, Source, Trigger
from .base import SourceUnavailable
from .crustdata import cite

HIRING_TITLE_KEYWORDS: tuple[str, ...] = (
    "warehouse",
    "warehousing",
    "logistics",
    "supply chain",
    "3PL",
)

JOB_SEARCH_FIELDS: list[str] = [
    "job_details.title",
    "job_details.url",
    "job_details.source",
    "company.basic_info.name",
    "company.basic_info.crustdata_company_id",
    "company.basic_info.primary_domain",
    "company.basic_info.website",
    "location.raw",
    "location.city",
    "location.state",
    "location.country",
    "metadata.date_added",
    "metadata.date_updated",
]

COMPANY_ENRICH_FIELDS: list[str] = [
    "basic_info",
    "funding",
    "news",
    "metadata",
]

NEW_LEADER_TITLES: tuple[str, ...] = (
    "head of supply chain",
    "supply chain head",
    "chief supply chain officer",
    "CSCO",
    "director of supply chain",
    "supply chain director",
    "director supply chain",
    "VP supply chain",
    "vice president supply chain",
    "head of logistics",
    "logistics head",
    "logistics director",
    "director of logistics",
)

NEW_LEADER_CONTRACT_GAP = (
    "Shared models.TriggerType only allows 'hiring', 'expansion', and 'funding'; "
    "new-leader signals are flagged separately until the team agrees on a shared-contract update."
)

# Legal suffixes stripped when normalizing company names for corroboration.
_LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:private\s+limited|pvt\.?\s*ltd\.?|limited|ltd\.?|llp|inc\.?|corp\.?|"
    r"corporation|llc|plc|co\.?|company)\b",
    re.IGNORECASE,
)

# Common Indian city aliases for location corroboration.
_CITY_ALIASES: dict[str, str] = {
    "bangalore": "bengaluru",
    "bengaluru": "bengaluru",
    "bombay": "mumbai",
    "mumbai": "mumbai",
    "gurgaon": "gurugram",
    "gurugram": "gurugram",
    "madras": "chennai",
    "chennai": "chennai",
    "calcutta": "kolkata",
    "kolkata": "kolkata",
    "poona": "pune",
    "pune": "pune",
    "new delhi": "delhi",
    "delhi": "delhi",
    "secunderabad": "hyderabad",
    "hyderabad": "hyderabad",
}

# Positive freight / warehousing / supply-chain title patterns.
_POSITIVE_JOB_ROLE_RE = re.compile(
    r"\b(?:warehouse|warehousing|supply\s+chain|3pl|freight|fleet|dispatch|"
    r"fulfill?ment|distribution|cold\s+chain|inventory|transport(?:ation)?|cargo|"
    r"shipping|logistics)\b",
    re.IGNORECASE,
)

# Strong facility/freight signals that survive generic office words.
_STRONG_FREIGHT_ROLE_RE = re.compile(
    r"\b(?:warehouse|warehousing|supply\s+chain|3pl|freight|fleet|dispatch|"
    r"fulfill?ment|cold\s+chain|cargo)\b",
    re.IGNORECASE,
)

# Non-freight false-positive job title qualifiers (e.g. F1 "Admin Executive - Visa & Travel- Logistics").
_NON_FREIGHT_TITLE_RE = re.compile(
    r"\b(?:visa|travel|immigration|ticketing|tour|hotel|hospitality|wedding|"
    r"event|events|reception|front\s+desk|admin\s+executive|office\s+admin|"
    r"human\s+resources|recruiter|talent\s+acquisition|cab|flight)\b",
    re.IGNORECASE,
)

# Physical facility / logistics expansion nouns (excludes "margin expanding" / "retail outlets").
_EXPANSION_FACILITY_RE = re.compile(
    r"\b(?:"
    r"warehouses?|warehousing|"
    r"fulfill?ment\s+(?:cent(?:er|re)s?|hubs?|facilit(?:y|ies))|"
    r"distribution\s+(?:cent(?:er|re)s?|hubs?|facilit(?:y|ies)|depots?)|"
    r"logistics\s+(?:hubs?|parks?|cent(?:er|re)s?|facilit(?:y|ies))|"
    r"cold\s+(?:storage|chain\s+facilit(?:y|ies))|"
    r"depots?|"
    r"(?:manufacturing|production|assembly|bottling|packaging|processing|industrial|"
    r"greenfield|brownfield|new|second|third|fourth|upcoming)\s+"
    r"(?:plants?|units?|facilit(?:y|ies)|factor(?:y|ies))|"
    r"factor(?:y|ies)"
    r")\b",
    re.IGNORECASE,
)

_EXPANSION_ACTION_RE = re.compile(
    r"\b(?:"
    r"open(?:s|ed|ing)?|"
    r"launch(?:es|ed|ing)?|"
    r"commission(?:s|ed|ing)?|"
    r"inaugurat(?:e|es|ed|ing)|"
    r"set(?:s|ting)?\s+up|"
    r"establish(?:es|ed|ing)?|"
    r"build(?:s|ing)?|built|"
    r"construct(?:s|ed|ing|ion)?|"
    r"expand(?:s|ed|ing)?|expansion|"
    r"invest(?:s|ed|ing|ment)?|"
    r"leas(?:e|es|ed|ing)|"
    r"acquir(?:e|es|ed|ing)|"
    r"add(?:s|ed|ing)?|"
    r"plan(?:s|ned|ning)?|"
    r"announc(?:e|es|ed|ing)|"
    r"new|second|third|greenfield|upcoming|"
    r"sq\.?\s*ft\.?|sqft|square\s+feet|acres?|capacity"
    r")\b",
    re.IGNORECASE,
)

_CONTRACTION_OR_INCIDENT_RE = re.compile(
    r"\b(?:"
    r"shut(?:s|ting)?\s+down|shutter(?:s|ed|ing)?|"
    r"clos(?:e|es|ed|ing)\s+(?:its|the|a)\s+(?:plant|factory|warehouse|unit|facility)|"
    r"layoffs?|downsizing|divest(?:s|ed|ing)?|sold\s+off|fire\s+at|strike\s+at"
    r")\b",
    re.IGNORECASE,
)

_ISO_DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:$|[T\s])")

_PUBLISH_META_KEYS: tuple[str, ...] = (
    "article:published_time",
    "og:article:published_time",
    "iso-8601-publish-date",
    "datepublished",
    "publishdate",
    "pubdate",
    "publish_date",
    "article.published",
    "parsely-pub-date",
    "sailthru.date",
    "dc.date.issued",
    "dcterms.created",
    "dc.date",
)

_SKIP_HTML_TAGS = frozenset({"script", "style", "noscript", "svg", "template"})
_BLOCK_HTML_TAGS = frozenset({"p", "li", "h1", "h2", "h3", "h4", "blockquote"})


# ------------------------------------------------------------------ models ---


class ProviderFailure(BaseModel):
    """Records a provider failure separately from an empty result."""

    operation: str
    error: str


class NewLeaderSignal(BaseModel):
    """Flagged separately because shared `models.TriggerType` lacks a new-leader value."""

    person_id: str | None = None
    company_name: str
    title: str
    start_date: date | None = None
    fact: Fact
    contract_gap: str = NEW_LEADER_CONTRACT_GAP


class PublishedPage(BaseModel):
    """Parsed representation of a fetched published HTML page."""

    url: str
    title: str | None = None
    publication_date: date | None = None
    date_source: str | None = None
    paragraphs: list[str] = Field(default_factory=list)
    sentences: list[str] = Field(default_factory=list)


class SignalCollection(BaseModel):
    """Full trigger collection output including per-channel failures and contract notes."""

    triggers: list[Trigger] = Field(default_factory=list)
    failures: list[ProviderFailure] = Field(default_factory=list)
    new_leader_signals: list[NewLeaderSignal] = Field(default_factory=list)
    contract_notes: list[str] = Field(default_factory=list)


# ------------------------------------------------------------ basic helpers ---


def _leaf(field: str, operator: str, value: Any) -> dict[str, Any]:
    return {"field": field, "type": operator, "value": value}


def _group(operator: str, conditions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"op": operator, "conditions": conditions}


def _require_envelope(resp: Any, operation: str) -> dict[str, Any]:
    if not isinstance(resp, dict) or "data" not in resp or "url" not in resp or "retrieved_at" not in resp:
        raise SourceUnavailable(f"{operation}: malformed client response envelope")
    return resp


def _coerce_company_id(company_id: str) -> int | str:
    stripped = str(company_id).strip()
    return int(stripped) if stripped.isdigit() else stripped


def _normalize_domain(raw: str | None) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    candidate = raw.strip().lower()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    host = urlparse(candidate).hostname
    if not host:
        return None
    host = host.strip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _domains_match(candidate: str | None, target: str | None) -> bool:
    cand = _normalize_domain(candidate)
    targ = _normalize_domain(target)
    if not cand or not targ:
        return False
    return cand == targ or cand.endswith(f".{targ}")


def _normalize_company_name(name: str | None) -> str:
    if not name or not isinstance(name, str):
        return ""
    cleaned = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    cleaned = _LEGAL_SUFFIX_RE.sub(" ", cleaned)
    return " ".join(cleaned.split())


def _normalize_city(city: str | None) -> str:
    if not city or not isinstance(city, str):
        return ""
    cleaned = " ".join(re.sub(r"[^a-z\s]", " ", city.lower()).split())
    return _CITY_ALIASES.get(cleaned, cleaned)


def parse_date(value: Any) -> date | None:
    """Parse a record/event date into `datetime.date`. Never fabricates a date."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    match = _ISO_DATE_PREFIX_RE.match(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            return parsed.date()
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _date_str(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


def _is_http_url(url: Any) -> bool:
    return isinstance(url, str) and url.startswith(("https://", "http://"))


def _format_round_type(raw: str | None) -> str:
    if not raw or not isinstance(raw, str):
        return "funding"
    words = [w for w in re.split(r"[_\-\s]+", raw.strip()) if w]
    formatted: list[str] = []
    for word in words:
        upper = word.upper()
        if upper in {"IPO", "PE", "VC", "M&A"} or (len(word) == 1 and word.isalpha()):
            formatted.append(upper)
        else:
            formatted.append(word.capitalize())
    return " ".join(formatted) if formatted else "funding"


# ----------------------------------------------------------- hiring (T4/F1) ---


# Job functions that are not a freight need even when the title says "warehouse"
# ("HR Generalist - Warehouse HR", "Warehouse Accountant"). Config
# triggers.hiring_exclude_title_words overrides this list.
HIRING_EXCLUDE_TITLE_WORDS = [
    "hr", "human resources", "recruiter", "recruitment", "talent acquisition", "payroll",
    "accountant", "accounts", "finance", "audit", "tax", "software", "developer",
    "it support", "marketing", "sales", "business development", "legal",
]


def _hiring_exclude_words() -> list[str]:
    try:
        from ..config import settings

        words = settings().get("triggers", {}).get("hiring_exclude_title_words")
    except Exception:  # noqa: BLE001 - config unavailable in isolated tests
        words = None
    return HIRING_EXCLUDE_TITLE_WORDS if words is None else list(words)


def _names_function(text: str, words: list[str]) -> bool:
    norm = " " + " ".join(re.findall(r"[a-z0-9&]+", text.lower())) + " "
    return any(f" {' '.join(re.findall(r'[a-z0-9&]+', w.lower()))} " in norm
               for w in words if w.strip())


def is_relevant_logistics_job_title(title: str | None) -> bool:
    """Reject non-freight keyword hits such as visa/travel admin roles, and roles whose
    function is not freight (HR, accounts, sales...) even inside a warehouse."""
    if not title or not isinstance(title, str):
        return False
    text = title.strip()
    if not text or not _POSITIVE_JOB_ROLE_RE.search(text):
        return False
    if _NON_FREIGHT_TITLE_RE.search(text) and not _STRONG_FREIGHT_ROLE_RE.search(text):
        return False
    if _names_function(text, _hiring_exclude_words()):
        return False
    return True


def _name_lookup_query(name: str) -> str:
    """Strip trailing legal suffixes for substring queries when a distinctive core remains."""
    cleaned = re.sub(r"[.,]+$", "", name.strip())
    core = _LEGAL_SUFFIX_RE.sub("", cleaned)
    core = " ".join(re.sub(r"[.,]+$", "", core).split())
    return core if len(core) >= 3 else name.strip()


def _location_corroborates(row: dict[str, Any], company: CompanyRef) -> bool:
    if not company.locations:
        return False
    loc = row.get("location") or {}
    if not isinstance(loc, dict):
        return False
    raw_city = loc.get("city")
    raw_text = loc.get("raw") or ""
    row_city_norm = _normalize_city(raw_city) if isinstance(raw_city, str) else ""
    raw_lower = raw_text.lower() if isinstance(raw_text, str) else ""

    for known in company.locations:
        target_city = _normalize_city(known.city)
        if not target_city:
            continue
        if row_city_norm and row_city_norm == target_city:
            return True
        if raw_lower:
            # Check both canonical city name and any alias mapping to it.
            candidates = {target_city} | {
                alias for alias, canonical in _CITY_ALIASES.items() if canonical == target_city
            }
            if any(re.search(rf"\b{re.escape(c)}\b", raw_lower) for c in candidates):
                return True
    return False


def corroborates_job_company(row: dict[str, Any], company: CompanyRef) -> tuple[bool, str]:
    """Determine whether a job listing row is corroborated as belonging to `company`.

    A name-only employer match is explicitly insufficient when the Crustdata
    company ID is missing/null (as seen in F1 Indeed jobs).
    """
    basic = ((row.get("company") or {}).get("basic_info") or {}) if isinstance(row.get("company"), dict) else {}
    job_details = row.get("job_details") if isinstance(row.get("job_details"), dict) else {}

    row_id = basic.get("crustdata_company_id")
    if row_id is not None:
        if company.crustdata_id is not None:
            if str(row_id).strip() == str(company.crustdata_id).strip():
                return True, "company_id"
            return False, "company_id_mismatch"

    # Domain corroboration (primary_domain, website, or job hosted on company domain).
    if company.domain:
        for candidate in (basic.get("primary_domain"), basic.get("website"), job_details.get("url")):
            if _domains_match(candidate, company.domain):
                return True, "domain"

    # Null-ID fallback: require exact normalized employer name AND sourced location corroboration.
    row_name_norm = _normalize_company_name(basic.get("name"))
    target_name_norm = _normalize_company_name(company.name)
    if row_name_norm and target_name_norm and row_name_norm == target_name_norm:
        if _location_corroborates(row, company):
            return True, "name_and_location"
        return False, "name_only_uncorroborated"

    return False, "unmatched"


def _can_corroborate_name_lookup(company: CompanyRef) -> bool:
    """A name-only job lookup can only succeed if we have domain or sourced locations."""
    return bool(company.name and company.name.strip() and (company.domain or company.locations))


def _job_search_body_by_id(company: CompanyRef, since: date, *, limit: int = 10) -> dict[str, Any]:
    return {
        "filters": _group(
            "and",
            [
                _leaf(
                    "company.basic_info.crustdata_company_id",
                    "in",
                    [_coerce_company_id(company.crustdata_id or "")],
                ),
                _group(
                    "or",
                    [_leaf("job_details.title", "[.]", kw) for kw in HIRING_TITLE_KEYWORDS],
                ),
                _leaf("metadata.date_added", "=>", since.isoformat()),
            ],
        ),
        "fields": JOB_SEARCH_FIELDS,
        "sorts": [{"field": "metadata.date_added", "order": "desc"}],
        "limit": limit,
    }


def _job_search_body_by_name(company: CompanyRef, since: date, *, limit: int = 10) -> dict[str, Any]:
    return {
        "filters": _group(
            "and",
            [
                _leaf("company.basic_info.name", "[.]", _name_lookup_query(company.name)),
                _group(
                    "or",
                    [_leaf("job_details.title", "[.]", kw) for kw in HIRING_TITLE_KEYWORDS],
                ),
                _leaf("metadata.date_added", "=>", since.isoformat()),
            ],
        ),
        "fields": JOB_SEARCH_FIELDS,
        "sorts": [{"field": "metadata.date_added", "order": "desc"}],
        "limit": limit,
    }


def _extract_job_posting_date(row: dict[str, Any]) -> tuple[date | None, str | None, Any]:
    """Return `(event_date, field_subpath, raw_value)` for a job's original posting date.

    Uses `metadata.date_added` (or `job_details.date_posted` if ever supplied).
    Never substitutes `metadata.date_updated` or API `retrieved_at`.
    """
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    job_details = row.get("job_details") if isinstance(row.get("job_details"), dict) else {}

    if metadata.get("date_added") is not None:
        raw = metadata["date_added"]
        return parse_date(raw), "metadata.date_added", raw
    if job_details.get("date_posted") is not None:
        raw = job_details["date_posted"]
        return parse_date(raw), "job_details.date_posted", raw
    if metadata.get("date_posted") is not None:
        raw = metadata["date_posted"]
        return parse_date(raw), "metadata.date_posted", raw
    return None, None, None


def map_job_listings(resp: dict[str, Any], company: CompanyRef,
                     resolved_names: dict[str, dict] | None = None) -> list[Trigger]:
    """Map a Crustdata `job_search` response envelope into hiring `Trigger`s.

    Preserves old and undated listings returned by the source so Person D's
    `s04_triggers.py` applies the freshness window and CannotVerify rules.
    """
    envelope = _require_envelope(resp, "job_search")
    data = envelope["data"]
    if not isinstance(data, dict) or not isinstance(data.get("job_listings"), list):
        raise SourceUnavailable("job_search: unexpected payload shape (missing job_listings list)")

    out: list[Trigger] = []
    seen_keys: set[str] = set()

    for idx, row in enumerate(data["job_listings"]):
        if not isinstance(row, dict):
            continue
        job_details = row.get("job_details") if isinstance(row.get("job_details"), dict) else {}
        title = job_details.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        title = title.strip()
        if not is_relevant_logistics_job_title(title):
            continue

        matched, _reason = corroborates_job_company(row, company)
        identity_src: Source | None = None
        if not matched and resolved_names:
            # Post without a company id whose employer NAME was resolved to this company
            # by company identify during discovery: the identify citation goes with it.
            basic = ((row.get("company") or {}).get("basic_info") or {}) \
                if isinstance(row.get("company"), dict) else {}
            hit = resolved_names.get(_normalize_company_name(basic.get("name")))
            if basic.get("crustdata_company_id") is None and hit and company.crustdata_id \
                    and str(hit.get("crustdata_id")) == str(company.crustdata_id):
                matched = True
                identity_src = Source.model_validate(hit["source"])
        if not matched:
            continue

        job_url = job_details.get("url")
        event_date, date_subpath, raw_date = _extract_job_posting_date(row)
        loc = row.get("location") if isinstance(row.get("location"), dict) else {}
        loc_raw = (loc.get("raw") or loc.get("city") or "").strip() if isinstance(loc.get("raw") or loc.get("city") or "", str) else ""

        dedupe_key = (job_url.strip().lower() if _is_http_url(job_url)
                      else f"{title.lower()}|{loc_raw.lower()}|{_date_str(event_date)}")
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        sources: list[Source] = []
        date_path = f"job_listings[{idx}].{date_subpath}" if date_subpath else None

        if _is_http_url(job_url):
            excerpt_parts = [f"job_details.title = {title!r}"]
            if loc_raw:
                excerpt_parts.append(f"location.raw = {loc_raw!r}")
            if date_subpath is not None:
                excerpt_parts.append(f"{date_subpath} = {raw_date!r}")
            sources.append(
                Source(
                    url=job_url.strip(),
                    kind="api_response",
                    excerpt="; ".join(excerpt_parts),
                    field_path=f"job_listings[{idx}].job_details.url",
                    record_date=event_date,
                    retrieved_at=envelope["retrieved_at"],
                )
            )

        if identity_src is not None:
            sources.append(identity_src)
        if date_path is not None:
            sources.append(cite(envelope, date_path, raw_date, record_date=_date_str(event_date)))
        else:
            sources.append(
                cite(envelope, f"job_listings[{idx}].job_details.title", title, record_date=None)
            )

        employer_name = (
            ((row.get("company") or {}).get("basic_info") or {}).get("name")
            if isinstance(row.get("company"), dict)
            else None
        ) or company.name
        summary = f"{title} job posted" + (f" in {loc_raw}" if loc_raw else "")
        claim = f"{employer_name} posted a {title} job"
        if loc_raw:
            claim += f" in {loc_raw}"
        if event_date is not None:
            claim += f" on {event_date.isoformat()}"

        out.append(
            Trigger(
                type="hiring",
                summary=summary,
                event_date=event_date,
                fact=Fact(claim=claim, value=title, sources=sources),
            )
        )

    return out


def hiring_triggers(
    client: Any,
    company: CompanyRef,
    since: date,
    *,
    limit: int = 10,
) -> list[Trigger]:
    """Fetch hiring triggers via `job_search`, with corroborated name fallback on ID miss."""
    if company.crustdata_id:
        id_resp = client.call(
            "job_search",
            body=_job_search_body_by_id(company, since, limit=limit),
        )
        envelope = _require_envelope(id_resp, "job_search")
        data = envelope["data"]
        if not isinstance(data, dict) or not isinstance(data.get("job_listings"), list):
            raise SourceUnavailable("job_search: unexpected payload shape (missing job_listings list)")
        if data["job_listings"]:
            return map_job_listings(envelope, company)

        # ID-filtered query returned 0 listings: use company-name lookup only if
        # the target company carries attributes that can corroborate a null-ID row.
        if not _can_corroborate_name_lookup(company):
            return []
        name_resp = client.call(
            "job_search",
            body=_job_search_body_by_name(company, since, limit=limit),
        )
        return map_job_listings(name_resp, company)

    if not _can_corroborate_name_lookup(company):
        return []
    name_resp = client.call(
        "job_search",
        body=_job_search_body_by_name(company, since, limit=limit),
    )
    return map_job_listings(name_resp, company)


# ---------------------------------------------------------- funding (F2/T2) ---


def _company_enrich_body(company: CompanyRef) -> dict[str, Any] | None:
    if company.crustdata_id:
        return {
            "crustdata_company_ids": [_coerce_company_id(company.crustdata_id)],
            "fields": COMPANY_ENRICH_FIELDS,
        }
    if company.domain:
        return {
            "company_domains": [company.domain.strip()],
            "fields": COMPANY_ENRICH_FIELDS,
        }
    return None


def _iter_enriched_company_records(data: Any) -> list[tuple[str, dict[str, Any]]]:
    """Yield `(json_prefix, company_data)` pairs from `/company/enrich` or F2 shapes."""
    records: list[tuple[str, dict[str, Any]]] = []
    if isinstance(data, list):
        for i, entry in enumerate(data):
            if not isinstance(entry, dict):
                continue
            matches = entry.get("matches")
            if isinstance(matches, list):
                for j, match in enumerate(matches):
                    if isinstance(match, dict) and isinstance(match.get("company_data"), dict):
                        records.append((f"[{i}].matches[{j}].company_data", match["company_data"]))
            elif isinstance(entry.get("company_data"), dict):
                records.append((f"[{i}].company_data", entry["company_data"]))
            elif "funding" in entry or "basic_info" in entry:
                records.append((f"[{i}]", entry))
        return records

    if isinstance(data, dict):
        if isinstance(data.get("companies"), list):
            for i, item in enumerate(data["companies"]):
                if isinstance(item, dict):
                    records.append((f"companies[{i}]", item))
            return records
        if isinstance(data.get("company_data"), dict):
            return [("company_data", data["company_data"])]
        if "funding" in data or "basic_info" in data:
            return [("", data)]

    raise SourceUnavailable("company_enrich: unexpected payload shape")


def _record_matches_company(company_data: dict[str, Any], company: CompanyRef) -> bool:
    basic = company_data.get("basic_info") if isinstance(company_data.get("basic_info"), dict) else {}
    rec_id = company_data.get("crustdata_company_id")
    if rec_id is None:
        rec_id = basic.get("crustdata_company_id")

    if company.crustdata_id is not None and rec_id is not None:
        return str(rec_id).strip() == str(company.crustdata_id).strip()

    if company.domain:
        domains: list[Any] = [basic.get("primary_domain"), basic.get("website")]
        if isinstance(basic.get("all_domains"), list):
            domains.extend(basic["all_domains"])
        if any(_domains_match(d, company.domain) for d in domains):
            return True
        if any(isinstance(d, str) and d.strip() for d in domains):
            return False

    rec_name = _normalize_company_name(basic.get("name"))
    target_name = _normalize_company_name(company.name)
    return bool(rec_name and target_name and rec_name == target_name)


def _milestone_amount_conflicts(funding: dict[str, Any], event_date: date | None) -> bool:
    """Detect boAt-style T2 case where latest milestone on `last_fundraise_date` has null amount."""
    if event_date is None:
        return False
    milestones = funding.get("milestones")
    if not isinstance(milestones, list) or not milestones:
        return False
    same_date = [
        m for m in milestones
        if isinstance(m, dict)
        and parse_date(m.get("funding_date") or m.get("date")) == event_date
    ]
    if not same_date:
        return False
    return all(m.get("amount_usd") is None for m in same_date)


def map_funding_triggers(resp: dict[str, Any], company: CompanyRef) -> list[Trigger]:
    """Map dated funding fields (`last_fundraise_date`, `last_round_type`, `last_round_amount_usd`)."""
    envelope = _require_envelope(resp, "company_enrich")
    records = _iter_enriched_company_records(envelope["data"])

    triggers_out: list[Trigger] = []
    for prefix, company_data in records:
        if not _record_matches_company(company_data, company):
            continue
        funding = company_data.get("funding")
        if not isinstance(funding, dict):
            continue

        raw_date = funding.get("last_fundraise_date")
        round_type = funding.get("last_round_type")
        amount_usd = funding.get("last_round_amount_usd")

        has_signal = any(v is not None and v != "" for v in (raw_date, round_type, amount_usd))
        if not has_signal:
            continue

        event_date = parse_date(raw_date)
        funding_prefix = f"{prefix}.funding" if prefix else "funding"
        sources: list[Source] = []

        if raw_date is not None and raw_date != "":
            sources.append(
                cite(
                    envelope,
                    f"{funding_prefix}.last_fundraise_date",
                    raw_date,
                    record_date=_date_str(event_date),
                )
            )
        if round_type is not None and round_type != "":
            sources.append(
                cite(
                    envelope,
                    f"{funding_prefix}.last_round_type",
                    round_type,
                    record_date=_date_str(event_date),
                )
            )

        include_amount = (
            isinstance(amount_usd, (int, float))
            and not isinstance(amount_usd, bool)
            and amount_usd > 0
            and not _milestone_amount_conflicts(funding, event_date)
        )
        if include_amount:
            sources.append(
                cite(
                    envelope,
                    f"{funding_prefix}.last_round_amount_usd",
                    amount_usd,
                    record_date=_date_str(event_date),
                )
            )

        if not sources:
            continue

        basic = company_data.get("basic_info") if isinstance(company_data.get("basic_info"), dict) else {}
        company_name = basic.get("name") or company.name
        round_label = _format_round_type(round_type)

        summary = f"{round_label} funding"
        if include_amount:
            summary += f" (US${amount_usd:,.0f})"

        claim = f"{company_name} raised {round_label} funding"
        if include_amount:
            claim += f" of US${amount_usd:,.0f}"
        if event_date is not None:
            claim += f" on {event_date.isoformat()}"

        triggers_out.append(
            Trigger(
                type="funding",
                summary=summary,
                event_date=event_date,
                fact=Fact(
                    claim=claim,
                    value=amount_usd if include_amount else round_label,
                    sources=sources,
                ),
            )
        )
        break  # one matched company record per company

    return triggers_out


def _extract_enrich_expansion_urls(resp: dict[str, Any], company: CompanyRef) -> list[str]:
    """Return news article URLs from `company_enrich` whose title suggests physical expansion."""
    envelope = _require_envelope(resp, "company_enrich")
    records = _iter_enriched_company_records(envelope["data"])
    urls: list[str] = []
    for _prefix, company_data in records:
        if not _record_matches_company(company_data, company):
            continue
        news = company_data.get("news")
        if not isinstance(news, list):
            break
        for item in news:
            if not isinstance(item, dict):
                continue
            url = item.get("article_url")
            title = item.get("article_title") or ""
            if (
                _is_http_url(url)
                and isinstance(title, str)
                and _EXPANSION_FACILITY_RE.search(title)
                and not _CONTRACTION_OR_INCIDENT_RE.search(title)
            ):
                urls.append(url.strip())
        break
    return urls


def funding_triggers(
    client: Any,
    company: CompanyRef,
    since: date,
    *,
    enrich_resp: dict[str, Any] | None = None,
) -> list[Trigger]:
    """Fetch funding triggers from `company_enrich`."""
    _ = since  # Person D's s04_triggers applies the freshness window; preserve dated/undated rows.
    if enrich_resp is not None:
        return map_funding_triggers(enrich_resp, company)
    body = _company_enrich_body(company)
    if body is None:
        return []
    resp = client.call("company_enrich", body=body)
    return map_funding_triggers(resp, company)


# -------------------------------------------- expansion (web_search/fetch/F4) ---


class _ArticleHTMLParser(HTMLParser):
    """Stdlib HTML parser extracting meta tags, JSON-LD blocks, `<time>`, title, and blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_dates: dict[str, str] = {}
        self.jsonld_blocks: list[str] = []
        self.time_datetimes: list[tuple[bool, str]] = []  # (is_published_prop, value)
        self.title: str = ""
        self.paragraphs: list[str] = []

        self._skip_depth: int = 0
        self._in_jsonld: bool = False
        self._jsonld_buf: list[str] = []
        self._in_title: bool = False
        self._title_buf: list[str] = []
        self._block_stack: list[str] = []
        self._block_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}

        if tag_l == "meta":
            key = (attr_map.get("property") or attr_map.get("name") or attr_map.get("itemprop") or "").strip().lower()
            content = attr_map.get("content", "").strip()
            if key and content and key not in self.meta_dates:
                self.meta_dates[key] = content
            return

        if tag_l == "script":
            script_type = attr_map.get("type", "").strip().lower()
            if "ld+json" in script_type:
                self._in_jsonld = True
                self._jsonld_buf = []
            self._skip_depth += 1
            return

        if tag_l in _SKIP_HTML_TAGS:
            self._skip_depth += 1
            return

        if tag_l == "title" and self._skip_depth == 0:
            self._in_title = True
            self._title_buf = []
            return

        if tag_l == "time" and self._skip_depth == 0:
            dt = attr_map.get("datetime", "").strip()
            if dt:
                is_pub = "datepublished" in attr_map.get("itemprop", "").lower()
                self.time_datetimes.append((is_pub, dt))

        if tag_l in _BLOCK_HTML_TAGS and self._skip_depth == 0:
            if not self._block_stack:
                self._block_buf = []
            self._block_stack.append(tag_l)

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l == "script":
            if self._in_jsonld:
                raw = "".join(self._jsonld_buf).strip()
                if raw:
                    self.jsonld_blocks.append(raw)
                self._in_jsonld = False
                self._jsonld_buf = []
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return

        if tag_l in _SKIP_HTML_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return

        if tag_l == "title" and self._in_title:
            self._in_title = False
            if not self.title:
                self.title = " ".join("".join(self._title_buf).split())
            return

        if tag_l in _BLOCK_HTML_TAGS and self._block_stack and self._block_stack[-1] == tag_l:
            self._block_stack.pop()
            if not self._block_stack:
                text = " ".join("".join(self._block_buf).split())
                if text:
                    self.paragraphs.append(text)
                self._block_buf = []

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._jsonld_buf.append(data)
            return
        if self._skip_depth > 0:
            return
        if self._in_title:
            self._title_buf.append(data)
        if self._block_stack:
            self._block_buf.append(data)


def _walk_jsonld_for_date(obj: Any) -> list[tuple[int, str, date]]:
    """Collect `(priority, source_label, parsed_date)` from JSON-LD objects."""
    found: list[tuple[int, str, date]] = []
    if isinstance(obj, list):
        for item in obj:
            found.extend(_walk_jsonld_for_date(item))
        return found
    if not isinstance(obj, dict):
        return found

    raw_type = obj.get("@type")
    types = raw_type if isinstance(raw_type, list) else ([raw_type] if isinstance(raw_type, str) else [])
    type_names = [t for t in types if isinstance(t, str)]
    primary_type = type_names[0] if type_names else "Thing"

    if "datePublished" in obj:
        parsed = parse_date(obj.get("datePublished"))
        if parsed is not None:
            prio = 0 if any(t in {"NewsArticle", "Article", "PressRelease", "Report", "BlogPosting"} for t in type_names) else 1
            found.append((prio, f"jsonld.{primary_type}.datePublished", parsed))

    for nested_key in ("@graph", "mainEntity", "hasPart"):
        if nested_key in obj:
            found.extend(_walk_jsonld_for_date(obj[nested_key]))
    return found


def _split_sentences(paragraph: str) -> list[str]:
    """Split a paragraph into clean sentences without breaking on Rs., Ltd., Pvt., or decimals."""
    text = " ".join(paragraph.split())
    if not text:
        return []
    # Protect common abbreviations before splitting on sentence-ending punctuation.
    protected = re.sub(
        r"\b(Rs|Mr|Mrs|Ms|Dr|St|Pvt|Ltd|Inc|Corp|Co|No|sq|ft)\.",
        r"\1<DOT>",
        text,
        flags=re.IGNORECASE,
    )
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9₹\"'(])", protected)
    return [part.replace("<DOT>", ".").strip() for part in parts if part.strip()]


def extract_published_page(html: str, *, url: str) -> PublishedPage:
    """Extract publication date (never retrieval time), title, and verbatim text from HTML."""
    parser = _ArticleHTMLParser()
    parser.feed(html or "")

    pub_date: date | None = None
    date_source: str | None = None

    # 1. Explicit publication meta tags (e.g. F4 article:published_time, T7 iso-8601-publish-date).
    for key in _PUBLISH_META_KEYS:
        if key in parser.meta_dates:
            parsed = parse_date(parser.meta_dates[key])
            if parsed is not None:
                pub_date = parsed
                date_source = f"meta[{key}]"
                break

    # 2. JSON-LD `datePublished` (e.g. NewsArticle.datePublished).
    if pub_date is None:
        ld_candidates: list[tuple[int, str, date]] = []
        for block in parser.jsonld_blocks:
            try:
                decoded = json.loads(block)
            except (ValueError, TypeError):
                continue
            ld_candidates.extend(_walk_jsonld_for_date(decoded))
        if ld_candidates:
            ld_candidates.sort(key=lambda item: item[0])
            _, date_source, pub_date = ld_candidates[0]

    # 3. `<time datetime="...">` fallback (preferring itemprop="datePublished").
    if pub_date is None and parser.time_datetimes:
        for is_pub, dt in sorted(parser.time_datetimes, key=lambda pair: not pair[0]):
            parsed = parse_date(dt)
            if parsed is not None:
                pub_date = parsed
                date_source = "time[itemprop=datePublished]" if is_pub else "time[datetime]"
                break

    # Deduplicate paragraphs while preserving document order.
    deduped_paragraphs: list[str] = []
    seen_p: set[str] = set()
    for p in parser.paragraphs:
        if p not in seen_p:
            seen_p.add(p)
            deduped_paragraphs.append(p)

    sentences: list[str] = []
    seen_s: set[str] = set()
    for p in deduped_paragraphs:
        for s in _split_sentences(p):
            if s not in seen_s:
                seen_s.add(s)
                sentences.append(s)

    return PublishedPage(
        url=url,
        title=parser.title or None,
        publication_date=pub_date,
        date_source=date_source,
        paragraphs=deduped_paragraphs,
        sentences=sentences,
    )


def _company_mention_patterns(company: CompanyRef) -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    norm_name = _normalize_company_name(company.name)
    if norm_name and len(norm_name) >= 3:
        patterns.append(re.compile(rf"\b{re.escape(norm_name)}\b", re.IGNORECASE))
        # Also allow the leading distinctive brand token if multi-word (e.g. "Honasa" from "Honasa Consumer").
        first_token = norm_name.split()[0]
        if len(first_token) >= 4 and first_token not in {
            "india", "indian", "national", "global", "united", "shree", "shri",
            "bharat", "hindustan", "new", "royal", "modern", "general", "super",
        }:
            patterns.append(re.compile(rf"\b{re.escape(first_token)}\b", re.IGNORECASE))
    domain = _normalize_domain(company.domain)
    if domain:
        patterns.append(re.compile(rf"\b{re.escape(domain)}\b", re.IGNORECASE))
        stem = domain.split(".")[0]
        if len(stem) >= 4:
            patterns.append(re.compile(rf"\b{re.escape(stem)}\b", re.IGNORECASE))
    return patterns


def _mentions_company(text: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.search(text) for p in patterns)


def find_expansion_passage(page: PublishedPage, company: CompanyRef) -> str | None:
    """Return an exact verbatim sentence from `page` directly supporting an expansion event."""
    company_patterns = _company_mention_patterns(company)
    if not company_patterns:
        return None

    title_mentions = bool(page.title and _mentions_company(page.title, company_patterns))

    # First pass: prefer a single verbatim sentence that mentions BOTH the company and the facility expansion.
    fallback_passage: str | None = None
    for sentence in page.sentences:
        if len(sentence) < 25:
            continue
        if _CONTRACTION_OR_INCIDENT_RE.search(sentence):
            continue
        if not (_EXPANSION_FACILITY_RE.search(sentence) and _EXPANSION_ACTION_RE.search(sentence)):
            continue
        if _mentions_company(sentence, company_patterns):
            return sentence
        if title_mentions and fallback_passage is None:
            fallback_passage = sentence

    return fallback_passage


def map_expansion_fetch(resp: dict[str, Any], company: CompanyRef) -> list[Trigger]:
    """Map a `web_fetch` (`/web/enrich/live`) response envelope into expansion `Trigger`s.

    Raises `SourceUnavailable` when the fetch response reports failure for all items.
    Returns `[]` when pages fetched successfully but contain no supporting expansion passage
    (such as the F4/T7 Honasa earnings parser controls).
    """
    envelope = _require_envelope(resp, "web_fetch")
    data = envelope["data"]
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        items = data["results"]
    elif isinstance(data, list):
        items = data
    else:
        raise SourceUnavailable("web_fetch: unexpected payload shape")

    if not items:
        return []

    triggers_out: list[Trigger] = []
    succeeded = 0
    failed_urls: list[str] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        ok = item.get("success")
        http_status = item.get("http_status")
        content = item.get("content")
        if ok is False or (isinstance(http_status, int) and http_status >= 400) or not isinstance(content, str) or not content.strip():
            failed_urls.append(str(url or "<unknown>"))
            continue
        succeeded += 1
        if not _is_http_url(url):
            continue

        page = extract_published_page(content, url=url.strip())
        passage = find_expansion_passage(page, company)
        if not passage:
            continue

        field_path = f"html.body; date:{page.date_source}" if page.date_source else "html.body"
        source = Source(
            url=page.url,
            kind="web_page",
            excerpt=passage,
            field_path=field_path,
            record_date=page.publication_date,
            retrieved_at=envelope["retrieved_at"],
        )
        summary = page.title or f"{company.name} facility expansion"
        claim = passage
        triggers_out.append(
            Trigger(
                type="expansion",
                summary=summary,
                event_date=page.publication_date,
                fact=Fact(claim=claim, value=page.url, sources=[source]),
            )
        )

    if succeeded == 0 and failed_urls:
        raise SourceUnavailable(f"web_fetch failed for URLs: {', '.join(failed_urls)}")

    return triggers_out


def _web_search_body(company: CompanyRef, since: date) -> dict[str, Any]:
    start_ts = int(datetime(since.year, since.month, since.day, tzinfo=UTC).timestamp())
    query_name = _name_lookup_query(company.name)
    return {
        "query": f'"{query_name}" (warehouse OR plant OR "distribution center" OR "manufacturing facility")',
        "sources": ["news"],
        "location": "IN",
        "page": 1,
        "start_date": start_ts,
    }


def _rank_search_candidates(results: list[dict[str, Any]], company: CompanyRef) -> list[str]:
    patterns = _company_mention_patterns(company)
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for row in results:
        if not isinstance(row, dict):
            continue
        url = row.get("url")
        if not _is_http_url(url):
            continue
        clean_url = url.strip()
        if clean_url in seen:
            continue
        seen.add(clean_url)
        text = f"{row.get('title', '')} {row.get('snippet', '')}"
        if _CONTRACTION_OR_INCIDENT_RE.search(text):
            continue
        has_facility = bool(_EXPANSION_FACILITY_RE.search(text))
        has_action = bool(_EXPANSION_ACTION_RE.search(text))
        has_company = _mentions_company(text, patterns) if patterns else False
        if not has_facility:
            continue
        score = (4 if has_company else 0) + (2 if has_facility else 0) + (1 if has_action else 0)
        scored.append((score, clean_url))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [url for _score, url in scored]


def expansion_triggers(
    client: Any,
    company: CompanyRef,
    since: date,
    *,
    extra_candidate_urls: list[str] | None = None,
    max_fetches: int = 2,
) -> list[Trigger]:
    """Find expansion triggers via `web_search` + `web_fetch` with exact passage verification."""
    if not company.name or not company.name.strip():
        return []

    search_resp = _require_envelope(
        client.call("web_search", body=_web_search_body(company, since)),
        "web_search",
    )
    data = search_resp["data"]
    if not isinstance(data, dict):
        raise SourceUnavailable("web_search: unexpected payload shape")
    if data.get("success") is False:
        raise SourceUnavailable("web_search: provider reported success=false")

    raw_results = data.get("results")
    if raw_results is None:
        raw_results = []
    if not isinstance(raw_results, list):
        raise SourceUnavailable("web_search: unexpected results field")

    candidate_urls = _rank_search_candidates(raw_results, company)
    for url in extra_candidate_urls or []:
        if _is_http_url(url) and url.strip() not in candidate_urls:
            candidate_urls.append(url.strip())

    if not candidate_urls:
        return []

    to_fetch = candidate_urls[: max(1, max_fetches)]
    triggers_out: list[Trigger] = []
    fetch_failures: list[str] = []
    fetch_successes = 0

    for url in to_fetch:
        try:
            fetch_resp = client.call("web_fetch", body={"urls": [url]})
            found = map_expansion_fetch(fetch_resp, company)
            fetch_successes += 1
            triggers_out.extend(found)
        except BudgetExceeded:
            raise
        except SourceUnavailable as exc:
            fetch_failures.append(str(exc))

    if fetch_successes == 0 and fetch_failures:
        raise SourceUnavailable("; ".join(fetch_failures))

    return triggers_out


# ---------------------------------------------- new-leader signals (F3 gap) ---


def map_new_leader_signals(
    resp: dict[str, Any],
    company: CompanyRef | None = None,
) -> list[NewLeaderSignal]:
    """Parse `person_search` new-leader records separately from `Trigger`.

    Because shared `models.TriggerType` has no `"new_leader"` value, these
    records are returned as `NewLeaderSignal` with `NEW_LEADER_CONTRACT_GAP`
    and never coerced into hiring/expansion/funding triggers.
    """
    envelope = _require_envelope(resp, "person_search")
    data = envelope["data"]
    if not isinstance(data, dict):
        raise SourceUnavailable("person_search: unexpected payload shape")
    profiles = data.get("profiles")
    if profiles is None:
        profiles = data.get("people", [])
    if not isinstance(profiles, list):
        raise SourceUnavailable("person_search: missing profiles list")

    out: list[NewLeaderSignal] = []
    for p_idx, profile in enumerate(profiles):
        if not isinstance(profile, dict):
            continue
        person_id = profile.get("crustdata_person_id")
        exp = profile.get("experience") if isinstance(profile.get("experience"), dict) else {}
        emp = exp.get("employment_details") if isinstance(exp.get("employment_details"), dict) else {}
        current_roles = emp.get("current") if isinstance(emp.get("current"), list) else []

        for r_idx, role in enumerate(current_roles):
            if not isinstance(role, dict):
                continue
            role_company_id = role.get("company_id")
            role_company_name = role.get("name") or ""
            role_title = role.get("title") or ""
            if not isinstance(role_title, str) or not role_title.strip():
                continue

            if company is not None:
                if company.crustdata_id is not None and role_company_id is not None:
                    if str(role_company_id).strip() != str(company.crustdata_id).strip():
                        continue
                else:
                    if _normalize_company_name(role_company_name) != _normalize_company_name(company.name):
                        continue

            raw_start = role.get("start_date")
            start_dt = parse_date(raw_start)
            base_path = f"profiles[{p_idx}].experience.employment_details.current[{r_idx}]"
            sources = [
                cite(envelope, f"{base_path}.title", role_title.strip(), record_date=_date_str(start_dt)),
            ]
            if raw_start is not None:
                sources.append(
                    cite(envelope, f"{base_path}.start_date", raw_start, record_date=_date_str(start_dt))
                )

            claim = f"New supply-chain leader ({role_title.strip()}) joined {role_company_name or 'company'}"
            if start_dt is not None:
                claim += f" on {start_dt.isoformat()}"

            out.append(
                NewLeaderSignal(
                    person_id=str(person_id) if person_id is not None else None,
                    company_name=str(role_company_name),
                    title=role_title.strip(),
                    start_date=start_dt,
                    fact=Fact(claim=claim, value=role_title.strip(), sources=sources),
                )
            )

    return out


def new_leader_signals(
    client: Any,
    company: CompanyRef,
    since: date,
    *,
    limit: int = 5,
) -> list[NewLeaderSignal]:
    """Opt-in helper to query `person_search` for recent supply-chain leaders (flagged gap)."""
    role = "experience.employment_details.current"
    conditions: list[dict[str, Any]] = [
        _leaf(f"{role}.start_date", "=>", since.isoformat()),
        _group("or", [_leaf(f"{role}.title", "[.]", t) for t in NEW_LEADER_TITLES]),
    ]
    if company.crustdata_id:
        conditions.insert(0, _leaf(f"{role}.company_id", "=", _coerce_company_id(company.crustdata_id)))
    elif company.name and company.name.strip():
        conditions.insert(0, _leaf(f"{role}.name", "[.]", company.name.strip()))
    else:
        return []

    resp = client.call(
        "person_search",
        body={
            "filters": _group("and", conditions),
            "fields": [
                "basic_profile.current_title",
                "basic_profile.location.country",
                f"{role}.company_id",
                f"{role}.name",
                f"{role}.title",
                f"{role}.start_date",
                "metadata.updated_at",
            ],
            "limit": limit,
        },
    )
    return map_new_leader_signals(resp, company=company)


# ----------------------------------------------------------- top-level API ---


def collect_triggers(
    client: Any,
    company: CompanyRef,
    since: date,
) -> SignalCollection:
    """Collect hiring, funding, and expansion triggers while tracking provider failures.

    Preserves `BudgetExceeded` immediately. Captures `SourceUnavailable` per
    channel so callers can distinguish provider outages from empty results.
    """
    collected: list[Trigger] = []
    failures: list[ProviderFailure] = []
    enrich_resp: dict[str, Any] | None = None
    enrich_expansion_urls: list[str] = []

    # 1. Hiring (`job_search`)
    try:
        collected.extend(hiring_triggers(client, company, since))
    except BudgetExceeded:
        raise
    except SourceUnavailable as exc:
        failures.append(ProviderFailure(operation="job_search", error=str(exc)))

    # 2. Funding (`company_enrich`) — single enrich call also surfaces candidate news URLs.
    enrich_body = _company_enrich_body(company)
    if enrich_body is not None:
        try:
            enrich_resp = _require_envelope(
                client.call("company_enrich", body=enrich_body),
                "company_enrich",
            )
            collected.extend(map_funding_triggers(enrich_resp, company))
            enrich_expansion_urls = _extract_enrich_expansion_urls(enrich_resp, company)
        except BudgetExceeded:
            raise
        except SourceUnavailable as exc:
            failures.append(ProviderFailure(operation="company_enrich", error=str(exc)))

    # 3. Expansion (`web_search` + `web_fetch`)
    try:
        collected.extend(
            expansion_triggers(
                client,
                company,
                since,
                extra_candidate_urls=enrich_expansion_urls,
            )
        )
    except BudgetExceeded:
        raise
    except SourceUnavailable as exc:
        failures.append(ProviderFailure(operation="web_search/web_fetch", error=str(exc)))

    return SignalCollection(
        triggers=collected,
        failures=failures,
        contract_notes=[NEW_LEADER_CONTRACT_GAP],
    )


def triggers(client: Any, company: CompanyRef, since: date) -> list[Trigger]:
    """DataSources.triggers implementation for live Crustdata + web signals.

    - Returns all sourced hiring, funding, and expansion `Trigger` objects found,
      preserving old and undated items for Person D's `s04_triggers.py`.
    - Returns `[]` when providers succeed with no matching signals.
    - Raises `SourceUnavailable` when provider failures prevent obtaining any
      triggers, distinguishing an outage from a successful empty search.
    - Always propagates `BudgetExceeded`.
    """
    result = collect_triggers(client, company, since)
    if result.triggers:
        return result.triggers
    if result.failures:
        summary = "; ".join(f"{f.operation}: {f.error}" for f in result.failures)
        raise SourceUnavailable(f"trigger providers failed with no signals collected: {summary}")
    return []
