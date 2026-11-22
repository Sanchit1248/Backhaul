"""Every fact in the output must link to the record that states it.

Rule from the problem statement: the link shows the exact passage or field that
supports the fact, and the date of that record. Facts from model memory are not
permitted. If no source settles a question, say "cannot verify" and name what is
missing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal

from .config import settings

SourceKind = Literal["web_page", "api_response", "filing", "registry", "client_record"]


@dataclass(frozen=True)
class Source:
    url: str                      # where the record lives (or API endpoint + params)
    kind: SourceKind
    excerpt: str                  # exact passage or field value that states the fact
    field_path: str | None = None # e.g. "company.legal_name" for API/JSON sources
    record_date: str | None = None  # date of the record itself, if known (ISO)
    retrieved_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )


@dataclass
class Fact:
    claim: str                    # e.g. "Payee is Swiggy Limited"
    value: object
    sources: list[Source]

    def __post_init__(self) -> None:
        if not self.sources:
            raise ValueError(f"Fact without a source is not allowed: {self.claim!r}")
        for s in self.sources:
            if not s.excerpt.strip():
                raise ValueError(f"Source for {self.claim!r} has no supporting excerpt")


@dataclass
class CannotVerify:
    question: str
    missing: str                  # what record would settle it
    tried: list[str] = field(default_factory=list)  # sources consulted
    label: str = field(default_factory=lambda: settings()["output"]["cannot_verify_label"])


def to_dict(obj) -> dict:
    return asdict(obj)
