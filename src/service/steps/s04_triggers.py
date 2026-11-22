"""Step 4: check triggers. OWNER: D (rules are the hard cases D tests).

Rules (config triggers.*):
  * source fails after retries                  -> cannot verify: source unavailable
  * trigger with no date                        -> CannotVerify for that trigger
  * date before request_date - window_days      -> rejected
  * date after request_date                     -> rejected (bad data)
  * types in ignore_types (layoffs, ...)        -> ignored for prospects
  * same type within dedupe_days                -> one event, keep every source
  * ...and for conflict_types, those sources state different values
                                                -> sources disagree: CannotVerify quoting
                                                   each source; the event is not used
  * no valid trigger left                       -> drop, or cannot_verify if the only
                                                   triggers were undated or disputed
"""
from __future__ import annotations

from datetime import timedelta

from ..models import CannotVerify, Prospect, Status, Trigger
from ..sources.base import SourceUnavailable
from . import Ctx, active, norm


def _events(triggers: list[Trigger], days: int) -> list[list[Trigger]]:
    """Group dated triggers into events: same type, within `days` of the event's first report."""
    events: list[list[Trigger]] = []
    for t in sorted(triggers, key=lambda t: t.event_date):
        ev = next((e for e in events if e[0].type == t.type
                   and abs((e[0].event_date - t.event_date).days) <= days), None)
        if ev:
            ev.append(t)
        else:
            events.append([t])
    return events


def _disagree(event: list[Trigger], conflict_types: list[str]) -> bool:
    """True if reports of one event state different values (e.g. Series B Rs 120 cr vs 80 cr)."""
    if event[0].type not in conflict_types:
        return False
    values = {norm(str(t.fact.value)) for t in event if t.fact.value is not None}
    return len(values) > 1


def _quote_all(event: list[Trigger]) -> str:
    return "; ".join(f'"{s.excerpt}" ({s.url}, dated {s.record_date or "undated"})'
                     for t in event for s in t.fact.sources)


def run(ctx: Ctx, prospects: list[Prospect]) -> list[Prospect]:
    cfg = ctx.cfg["triggers"]
    conflict_types = cfg.get("conflict_types", ["funding", "expansion"])
    req = ctx.job.request_date
    since = req - timedelta(days=cfg["window_days"])
    for p in active(prospects):
        try:
            found = ctx.sources.triggers(p.company, since)
        except SourceUnavailable as e:
            p.unverifiable(CannotVerify(
                question=f"Has {p.company.name} shown a trigger since {since}?",
                missing=f"source unavailable: {e}", tried=["triggers sources"]), stop=True)
            p.reason = "cannot verify: source unavailable"
            ctx.log(f"triggers: source unavailable for {p.company.name}", error=str(e))
            continue

        valid, undated, old = [], [], []
        for t in found:
            if t.type in cfg.get("ignore_types", []):
                continue
            if t.event_date is None:
                undated.append(t)
                p.unverifiable(CannotVerify(question=f"When did this happen: {t.summary}?",
                                            missing="source gives no date for the trigger",
                                            tried=[s.url for s in t.fact.sources]))
            elif since <= t.event_date <= req:
                valid.append(t)
            else:
                old.append(t)

        kept, disputed = [], []
        for ev in _events(valid, cfg["dedupe_days"]):
            if _disagree(ev, conflict_types):
                disputed.append(ev)
                p.unverifiable(CannotVerify(
                    question=(f"Sources disagree on the {ev[0].type} event around "
                              f"{ev[0].event_date}: which is right?"),
                    missing="a record that settles it. Sources say: " + _quote_all(ev),
                    tried=[s.url for t in ev for s in t.fact.sources]))
                continue
            first = ev[0]
            for dup in ev[1:]:
                first.extra_sources.extend(dup.fact.sources)
            kept.append(first)
        p.triggers = kept

        if not p.triggers:
            if disputed:
                p.status = Status.CANNOT_VERIFY
                p.reason = "sources disagree: " + " | ".join(ev[0].summary for ev in disputed)
            elif undated:
                p.status, p.reason = Status.CANNOT_VERIFY, "only undated triggers found"
            else:
                p.drop(f"no trigger within {cfg['window_days']} days of {req}"
                       + (f" ({len(old)} older/out-of-window rejected)" if old else ""))
    return prospects
