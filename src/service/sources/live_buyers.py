"""Live buyer lookup: Crustdata person search + person enrichment. OWNER: A.

    people(client, company, titles)    -> employer-specific current roles, best first
    verify_employment(client, buyer)   -> fresh recheck of that role by profile URL

What the current key can and cannot tell us (smoke T5/T6):
  * person search and person enrichment return INDEXED employment: current and past
    roles with crustdata_company_id and start/end dates. That is the provider's index,
    not an independent check, so every fact here is labelled "indexed".
  * batch work-history verification (/batch/person/verify) is disabled on this key.
  * business email: person enrichment with `contact.business_emails` works (checked
    live 22 Nov 2026, +1 credit). verify_employment requests it in the same call and
    keeps an email ONLY when the buyer is still current at the employer AND the email
    row's crustdata_company_id equals that employer id. Personal emails and phones
    are never requested. Config buyer.fetch_business_email turns this off.

Matching rules:
  * a company without a crustdata_id is never searched (no name-only matching)
  * only the current role whose crustdata_company_id equals the company's id counts;
    a profile's headline, default role or other concurrent employers are ignored
  * the role title must match a configured buyer title (token match, see title_matches)
  * a past role at the same employer may coexist with a current one there (title
    change); only "no current role at the employer" is a departure

Each finding becomes a models.Fact whose Sources are cite()s of the exact response
fields. Fact.value carries the evidence label (EVIDENCE_*) step 7 reads.
Provider failure (SourceUnavailable) propagates so the step can tell it apart from a
successful empty response. BudgetExceeded always propagates.
"""
from __future__ import annotations

import re
from datetime import date

from ..models import Buyer, CompanyRef, Fact, Source
from .crustdata import cite

# Field sets observed working in smoke T5/T6. Only name, roles, ids, dates and the
# profile URL (the enrichment identifier) are read from them.
SEARCH_FIELDS = [
    "basic_profile",
    "experience",
    "social_handles.professional_network_identifier.profile_url",
    "metadata.updated_at",
]
ENRICH_FIELDS = ["basic_profile", "experience", "metadata"]
EMAIL_FIELDS = ["contact.business_emails"]   # never contact.personal_emails / phones
SEARCH_LIMIT = 5

EVIDENCE_INDEXED = "indexed: current role (person_search)"
EVIDENCE_RECHECKED = "indexed: current role rechecked (person_enrich)"
EVIDENCE_TITLE_CHANGE = "indexed: earlier title at same employer ended (person_enrich)"
EVIDENCE_DEPARTED = "indexed: role at employer ended (person_enrich)"
EVIDENCE_MISMATCH = "indexed: no role at expected employer (person_enrich)"

_ABBREV = {
    "vp": "vice president",
    "svp": "senior vice president",
    "avp": "assistant vice president",
    "sr": "senior",
    "coo": "chief operating officer",
    "csco": "chief supply chain officer",
    "ops": "operations",
    "mgr": "manager",
    "gm": "general manager",
}
_FILLER = {"of", "the", "and", "for", "in", "at"}
_SENIORITY = {"head", "vice", "president", "senior", "assistant", "director", "manager",
              "chief", "officer", "general", "lead", "global", "national"}


# ------------------------------------------------------------- titles ---

def _tokens(title: str | None) -> list[str]:
    out: list[str] = []
    for t in re.findall(r"[a-z0-9]+", (title or "").lower()):
        out.extend(_ABBREV.get(t, t).split())
    return [t for t in out if t not in _FILLER]


def title_rank(title: str | None, titles: list[str]) -> int | None:
    """Index of the first configured title whose tokens all appear in `title`."""
    have = set(_tokens(title))
    for i, want in enumerate(titles):
        need = _tokens(want)
        if need and set(need) <= have:
            return i
    return None


def title_matches(title: str | None, titles: list[str]) -> bool:
    return title_rank(title, titles) is not None


def search_keywords(titles: list[str]) -> list[str]:
    """Functional phrases for the provider's substring filter ("head of supply chain"
    -> "supply chain"). Recall only: rows are re-checked with title_matches."""
    words: list[str] = []
    for want in titles:
        phrase = " ".join(t for t in _tokens(want) if t not in _SENIORITY)
        if phrase and phrase not in words:
            words.append(phrase)
        for abbr, full in _ABBREV.items():
            if full == " ".join(_tokens(want)) and abbr not in words:
                words.append(abbr)
    return words


# -------------------------------------------------------------- helpers ---

def _day(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _iso(value) -> str | None:
    d = _day(value)
    return d.isoformat() if d else None


def _same_id(a, b) -> bool:
    """Employer ids compare as strings; 0/None/'' is a missing id, never a match."""
    return bool(a) and bool(b) and str(a) != "0" and str(a) == str(b)


def _role_sources(resp: dict, path: str, row: dict, record_date: str | None,
                  *, end: bool = False) -> list[Source]:
    srcs = [cite(resp, f"{path}.crustdata_company_id", row.get("crustdata_company_id"), record_date),
            cite(resp, f"{path}.name", row.get("name"), record_date),
            cite(resp, f"{path}.title", row.get("title"), record_date),
            cite(resp, f"{path}.start_date", row.get("start_date"), record_date)]
    if end:
        srcs.append(cite(resp, f"{path}.end_date", row.get("end_date"), record_date))
    return srcs


def _rows(person: dict, kind: str) -> list[tuple[int, dict]]:
    rows = ((person.get("experience") or {}).get("employment_details") or {}).get(kind) or []
    return list(enumerate(rows))


# --------------------------------------------------------------- people ---

def people(client, company: CompanyRef, titles: list[str]) -> list[Buyer]:
    """Current employer-specific buyer roles at `company`, best configured title first."""
    if not company.crustdata_id or str(company.crustdata_id) == "0" or not titles:
        return []
    try:
        employer = int(company.crustdata_id)
    except ValueError:
        return []          # not a Crustdata id; searching would be a name-only match
    body = {
        "filters": {"op": "and", "conditions": [
            {"field": "experience.employment_details.current.company_id", "type": "=",
             "value": employer},
            {"op": "or", "conditions": [
                {"field": "experience.employment_details.current.title", "type": "[.]",
                 "value": kw} for kw in search_keywords(titles)]},
        ]},
        "fields": SEARCH_FIELDS,
        "limit": SEARCH_LIMIT,
    }
    resp = client.call("person_search", body=body)
    found: list[tuple[int, int, Buyer]] = []
    for i, prof in enumerate((resp.get("data") or {}).get("profiles") or []):
        best: tuple[int, int, dict] | None = None
        for j, row in _rows(prof, "current"):
            if row.get("end_date") or not _same_id(row.get("crustdata_company_id"), employer):
                continue
            rank = title_rank(row.get("title"), titles)
            if rank is not None and (best is None or rank < best[0]):
                best = (rank, j, row)
        if best is None:
            continue
        rank, j, row = best
        name = (prof.get("basic_profile") or {}).get("name")
        if not name:
            continue
        updated = _iso((prof.get("metadata") or {}).get("updated_at"))
        path = f"profiles[{i}].experience.employment_details.current[{j}]"
        srcs = [cite(resp, f"profiles[{i}].basic_profile.name", name, updated),
                *_role_sources(resp, path, row, updated, end=True)]
        start = _day(row.get("start_date"))
        url = (((prof.get("social_handles") or {}).get("professional_network_identifier") or {})
               .get("profile_url"))
        pid = prof.get("crustdata_person_id")
        found.append((rank, i, Buyer(
            name=name, title=row["title"], profile_url=url,
            person_id=str(pid) if pid else None,
            employer_id=str(company.crustdata_id), employer_name=row.get("name"),
            role_start=start, role_end=None, business_email=None,
            facts=[Fact(claim=f"Indexed employment (Crustdata person search, not independently "
                              f"verified): {name} holds the current role {row['title']!r} at "
                              f"{row.get('name')} (employer id {employer})"
                              + (f" since {start.isoformat()}" if start else ""),
                        value=EVIDENCE_INDEXED, sources=srcs)])))
    return [b for _, _, b in sorted(found, key=lambda t: (t[0], t[1]))]


# ---------------------------------------------------- verify_employment ---

def _enriched_person(resp: dict, buyer: Buyer) -> tuple[str, dict] | None:
    """(field path prefix, person_data) for the record that is the same person."""
    for i, entry in enumerate(resp.get("data") or []):
        if entry.get("match_status") != "matched":
            continue
        for k, m in enumerate(entry.get("matches") or []):
            person = m.get("person_data") or {}
            pid = person.get("crustdata_person_id")
            if buyer.person_id and str(pid) != str(buyer.person_id):
                continue
            if not buyer.person_id:
                url = (((person.get("social_handles") or {})
                        .get("professional_network_identifier") or {}).get("profile_url"))
                if url != buyer.profile_url:
                    continue
            return f"[{i}].matches[{k}].person_data", person
    return None


def _fetch_email() -> bool:
    try:
        from ..config import settings

        return bool(settings().get("buyer", {}).get("fetch_business_email", True))
    except Exception:  # noqa: BLE001 - config unavailable in isolated tests
        return True


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _employer_email(resp: dict, base: str, person: dict, employer_id: str, name: str,
                    updated: str | None) -> tuple[str, Fact] | None:
    """The business email whose crustdata_company_id is the verified employer, as
    (email, Fact). Prefers a row with a non-null status. None when there is none."""
    rows = ((person.get("contact") or {}).get("business_emails")) or []
    hits = [(k, r) for k, r in enumerate(rows) if isinstance(r, dict)
            and _same_id(r.get("crustdata_company_id"), employer_id)
            and isinstance(r.get("email"), str) and _EMAIL_RE.match(r["email"].strip())]
    if not hits:
        return None
    k, row = sorted(hits, key=lambda t: t[1].get("status") is None)[0]
    email = row["email"].strip()
    path = f"{base}.contact.business_emails[{k}]"
    src = cite(resp, f"{path}.email", email, updated)
    src = src.model_copy(update={"excerpt": f"{src.excerpt}; {path}.crustdata_company_id = "
                                            f"{row.get('crustdata_company_id')!r}; "
                                            f"{path}.status = {row.get('status')!r}"})
    return email, Fact(
        claim=f"Business email for {name} at employer id {employer_id} (Crustdata person "
              f"enrichment; deliverability status {row.get('status')!r})",
        value=email, sources=[src])


def verify_employment(client, buyer: Buyer) -> Buyer:
    """Recheck the buyer's role at buyer.employer_id through person enrichment.

    Returns the buyer with one new fact:
      EVIDENCE_RECHECKED  still current at the employer (title/start refreshed)
      EVIDENCE_DEPARTED   only ended roles at the employer -> role_end set
      EVIDENCE_MISMATCH   no role at the employer -> employer_id set to the current one
    and returns it unchanged (no new fact) when there is no stable identifier or the
    provider returns no record for this person. SourceUnavailable propagates.
    """
    if not buyer.profile_url or not buyer.employer_id:
        return buyer
    resp = client.call("person_enrich", body={
        "professional_network_profile_urls": [buyer.profile_url],
        "fields": ENRICH_FIELDS + (EMAIL_FIELDS if _fetch_email() else [])})
    hit = _enriched_person(resp, buyer)
    if hit is None:
        return buyer
    base, person = hit
    updated = _iso((person.get("metadata") or {}).get("updated_at"))
    emp = buyer.employer_id
    facts = list(buyer.facts)

    current = [(j, r) for j, r in _rows(person, "current")
               if not r.get("end_date") and _same_id(r.get("crustdata_company_id"), emp)]
    past = [(j, r) for j, r in _rows(person, "past")
            if _same_id(r.get("crustdata_company_id"), emp) and r.get("end_date")]

    if current:
        same = [(j, r) for j, r in current if (r.get("title") or "").lower() == buyer.title.lower()]
        j, row = (same or current)[0]
        path = f"{base}.experience.employment_details.current[{j}]"
        start = _day(row.get("start_date"))
        facts.append(Fact(
            claim=f"Indexed employment recheck (Crustdata person enrichment, not independently "
                  f"verified): {buyer.name} still holds {row.get('title')!r} at {row.get('name')} "
                  f"(employer id {emp}), no end date",
            value=EVIDENCE_RECHECKED, sources=_role_sources(resp, path, row, updated, end=True)))
        if not same:
            for pj, prow in past:
                if (prow.get("title") or "").lower() == buyer.title.lower():
                    ppath = f"{base}.experience.employment_details.past[{pj}]"
                    facts.append(Fact(
                        claim=f"Indexed history: {buyer.name}'s earlier title {prow.get('title')!r} "
                              f"at {prow.get('name')} ended on {_iso(prow.get('end_date'))} while "
                              f"a current role there continues (same-company title change)",
                        value=EVIDENCE_TITLE_CHANGE,
                        sources=_role_sources(resp, ppath, prow, updated, end=True)))
                    break
        email = _employer_email(resp, base, person, emp, buyer.name, updated)
        if email:
            facts.append(email[1])
        return buyer.model_copy(update={
            "title": row.get("title") or buyer.title, "employer_name": row.get("name"),
            "role_start": start, "role_end": None, "facts": facts,
            "business_email": email[0] if email else buyer.business_email})

    if past:
        same = [(j, r) for j, r in past if (r.get("title") or "").lower() == buyer.title.lower()]
        j, row = max(same or past, key=lambda t: _day(t[1].get("end_date")) or date.min)
        path = f"{base}.experience.employment_details.past[{j}]"
        end = _day(row.get("end_date"))
        facts.append(Fact(
            claim=f"Indexed departure (Crustdata person enrichment): {buyer.name}'s role "
                  f"{row.get('title')!r} at {row.get('name')} (employer id {emp}) ended on "
                  f"{end.isoformat() if end else 'an undated end'} and no current role there "
                  f"remains",
            value=EVIDENCE_DEPARTED, sources=_role_sources(resp, path, row, updated, end=True)))
        return buyer.model_copy(update={
            "title": row.get("title") or buyer.title, "role_start": _day(row.get("start_date")),
            "role_end": end, "facts": facts})

    rows = _rows(person, "current")
    j, row = next(((j, r) for j, r in rows if r.get("is_default")), rows[0] if rows else (None, None))
    if row is None:
        return buyer       # record has no employment at all: nothing settles it
    other = row.get("crustdata_company_id")
    path = f"{base}.experience.employment_details.current[{j}]"
    facts.append(Fact(
        claim=f"Indexed employment recheck: {buyer.name} has no role at employer id {emp}; "
              f"current employer is {row.get('name')} (employer id {other or 'missing'})",
        value=EVIDENCE_MISMATCH, sources=_role_sources(resp, path, row, updated)))
    return buyer.model_copy(update={
        "employer_id": str(other) if other and str(other) != "0" else None,
        "employer_name": row.get("name"), "title": row.get("title") or buyer.title,
        "role_start": _day(row.get("start_date")), "role_end": None, "facts": facts})
