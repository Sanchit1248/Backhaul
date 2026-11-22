"""SHARED DATA CONTRACT: every workstream builds against these shapes.

Change this file only by agreement (tell the team in chat, then one small PR).
All models are pydantic so they round-trip through the step log as JSON.

Flow: JobRequest + ClientRecords -> list[Prospect] -> each step mutates the
prospects it is given -> JobResult.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


# ------------------------------------------------------------ evidence ---

SourceKind = Literal["api_response", "web_page", "filing", "registry", "client_record"]


class Source(BaseModel):
    """Where a fact came from. The excerpt must STATE the fact itself."""
    url: str                                   # page URL, or API endpoint + query
    kind: SourceKind
    excerpt: str                               # exact passage, or "field.path = value"
    field_path: str | None = None              # e.g. "company.legal_name"
    record_date: date | None = None            # date of the record (post date, article date)
    retrieved_at: datetime = Field(default_factory=_now)

    @field_validator("excerpt")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Source.excerpt must quote the supporting passage/field")
        return v


class Fact(BaseModel):
    """A claim we output. Cannot be built without at least one Source."""
    claim: str                                 # "Acme posted a Logistics Manager job in Pune"
    value: str | int | float | bool | None = None
    sources: list[Source] = Field(min_length=1)


class CannotVerify(BaseModel):
    question: str                              # "Is the expansion news dated?"
    missing: str                               # what record would settle it
    tried: list[str] = []                      # sources consulted
    label: Literal["cannot verify"] = "cannot verify"


# ------------------------------------------------------------- client ---

class Route(BaseModel):
    origin: str                                # city, lower-case normalised by intake
    destination: str
    spare_trucks_per_week: int = 0


class ClientRecords(BaseModel):
    """Loaded from the kickoff kit. Format will change at kickoff: adapt loader, not this."""
    name: str
    routes: list[Route] = []
    current_clients: list[str] = []            # company names or domains
    rivals: list[str] = []
    do_not_contact: list[str] = []             # company names, domains or emails


class JobRequest(BaseModel):
    job_id: str
    text: str                                  # the raw request
    route_filter: list[str] = []               # cities, e.g. ["pune", "mumbai"]
    meetings_wanted: int = 5
    spare_trucks_per_week: int | None = None
    request_date: date
    budget_inr: float | None = None            # overrides config limit if given


# ----------------------------------------------------------- prospect ---

class Location(BaseModel):
    city: str
    kind: Literal["plant", "warehouse", "office", "hq", "unknown"] = "unknown"
    source: Source


class CompanyRef(BaseModel):
    name: str
    domain: str | None = None
    crustdata_id: str | None = None            # set by resolve-identity
    industry: str | None = None
    headcount: int | None = None
    locations: list[Location] = []


class IdentityMatch(BaseModel):
    company: CompanyRef
    score: float                               # 0..1 from the identify endpoint


TriggerType = Literal["hiring", "expansion", "funding"]


class Trigger(BaseModel):
    type: TriggerType
    summary: str
    event_date: date | None                    # None -> cannot verify the trigger
    fact: Fact
    extra_sources: list[Source] = []           # same event seen elsewhere (dedupe keeps both)


class RouteFit(BaseModel):
    level: Literal["full", "partial", "none"]
    score: float                               # rule score, labelled "estimate" in output
    reason: str                                # "plant in Nashik; spare capacity on Nashik to Mumbai"
    route: Route | None = None
    facts: list[Fact] = []


class Buyer(BaseModel):
    name: str
    title: str
    profile_url: str | None = None
    person_id: str | None = None
    employer_id: str | None = None             # must equal the prospect's crustdata_id
    employer_name: str | None = None
    role_start: date | None = None
    role_end: date | None = None               # set -> has left
    business_email: str | None = None          # work email only; never personal
    facts: list[Fact] = []


class EmailDraft(BaseModel):
    to: str
    subject: str
    body: str
    claims: list[Fact] = []                    # every claim in the body links here
    ics: str | None = None                     # calendar invite text
    edited_by: str | None = None


class Status(StrEnum):
    ACTIVE = "active"                          # still moving through the steps
    DROPPED = "dropped"                        # e.g. no trigger inside the window
    EXCLUDED = "excluded"                      # client / rival / do-not-contact
    HELD = "held"                              # lookalike, no verified buyer, ...
    CANNOT_VERIFY = "cannot_verify"
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"
    SENT = "sent"


class Prospect(BaseModel):
    prospect_id: str                           # stable: hash of job_id + company identity
    company: CompanyRef
    identity_candidates: list[IdentityMatch] = []
    triggers: list[Trigger] = []
    route_fit: RouteFit | None = None
    buyer: Buyer | None = None
    buyers_rejected: list[dict] = []           # [{"name":..., "reason":...}]
    draft: EmailDraft | None = None
    status: Status = Status.ACTIVE
    reason: str | None = None                  # why held/dropped/excluded
    cannot_verify: list[CannotVerify] = []
    outbox: list[dict] = []                    # action results from the ledger

    # helpers so every step records decisions the same way
    def hold(self, reason: str) -> None:
        self.status, self.reason = Status.HELD, reason

    def drop(self, reason: str) -> None:
        self.status, self.reason = Status.DROPPED, reason

    def exclude(self, reason: str) -> None:
        self.status, self.reason = Status.EXCLUDED, reason

    def unverifiable(self, cv: CannotVerify, *, stop: bool = False) -> None:
        self.cannot_verify.append(cv)
        if stop:
            self.status, self.reason = Status.CANNOT_VERIFY, cv.missing

    @property
    def active(self) -> bool:
        return self.status == Status.ACTIVE


class JobResult(BaseModel):
    job_id: str
    status: Literal["done", "awaiting_approval", "partial", "stopped_limit"]
    meetings_wanted: int
    prospects: list[Prospect]
    cost_usd: float
    cost_inr: float
    notes: list[str] = []
    generated_at: datetime = Field(default_factory=_now)
