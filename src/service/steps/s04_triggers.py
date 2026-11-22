"""Step 4: check triggers. OWNER: D (rules are the hard cases D tests).

Rules (config triggers.*):
  * trigger with no date                        -> CannotVerify for that trigger
  * date before request_date - window_days      -> rejected
  * date after request_date                     -> rejected (bad data)
  * same type within dedupe_days                -> one event, keep both sources
  * types in ignore_types (layoffs, ...)        -> ignored for prospects
  * no valid trigger left                       -> drop (or cannot_verify if only undated)
TODO(D): "sources disagree" rule -> CannotVerify showing both sources.
"""
from __future__ import annotations

from datetime import timedelta

from ..models import CannotVerify, Prospect, Status, Trigger
from . import Ctx, active


def _dedupe(triggers: list[Trigger], days: int) -> list[Trigger]:
    out: list[Trigger] = []
    for t in sorted(triggers, key=lambda t: t.event_date):
        dup = next((o for o in out if o.type == t.type
                    and abs((o.event_date - t.event_date).days) <= days), None)
        if dup:
            dup.extra_sources.extend(t.fact.sources)
        else:
            out.append(t)
    return out


def run(ctx: Ctx, prospects: list[Prospect]) -> list[Prospect]:
    cfg = ctx.cfg["triggers"]
    req = ctx.job.request_date
    since = req - timedelta(days=cfg["window_days"])
    for p in active(prospects):
        found = ctx.sources.triggers(p.company, since)
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
        p.triggers = _dedupe(valid, cfg["dedupe_days"])
        if not p.triggers:
            if undated:
                p.status, p.reason = Status.CANNOT_VERIFY, "only undated triggers found"
            else:
                p.drop(f"no trigger within {cfg['window_days']} days of {req}"
                       + (f" ({len(old)} older/out-of-window rejected)" if old else ""))
    return prospects
