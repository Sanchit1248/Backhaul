"""The ten steps. Each step is one file with one owner (see docs/WORKSTREAMS.md).

STEP CONTRACT: `run(ctx: Ctx, prospects: list[Prospect]) -> list[Prospect]`
  * work only on prospects where `p.active` is True; leave the others untouched
  * record every decision with p.hold / p.drop / p.exclude / p.unverifiable + a reason
  * never return a fact without a Source; never guess
  * get data only via ctx.sources (DataSources), LLM only via service.llm.ask
  * steps 2-8 are checkpointed by the runner; steps 9-10 re-run every time and must be
    idempotent through the ledger (approval queue / outbox)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..budget import Budget
from ..ledger import Ledger
from ..models import ClientRecords, JobRequest, Prospect
from ..sources import DataSources


@dataclass
class Ctx:
    job: JobRequest
    client: ClientRecords
    ledger: Ledger
    budget: Budget
    sources: DataSources
    cfg: dict
    notes: list[str] = field(default_factory=list)

    def log(self, msg: str, **data) -> None:
        self.ledger.log(self.job.job_id, "info", msg, data or None)


def active(prospects: list[Prospect]) -> list[Prospect]:
    return [p for p in prospects if p.active]


def norm(s: str | None) -> str:
    return (s or "").strip().lower()
