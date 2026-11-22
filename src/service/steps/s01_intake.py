"""Step 1: intake. OWNER: B.

Turn the raw request into a JobRequest and load the client records.
TODO(B, kickoff): adapt both parsers to the kickoff-kit formats. If the request
is free text ("Book 5 meetings for the Pune to Mumbai route; spare capacity 12
trucks a week"), parse with regex first; use llm.ask (cheap model) only as fallback.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from ..config import ROOT
from ..ledger import idem_key
from ..models import ClientRecords, JobRequest


def parse_request(raw: dict) -> JobRequest:
    text = raw.get("text") or raw.get("instruction") or ""
    route = [c.lower() for c in raw.get("route", [])]
    if not route:
        m = re.search(r"for the (\w+) to (\w+) route", text, re.IGNORECASE)
        if m:
            route = [m.group(1).lower(), m.group(2).lower()]
    meetings = raw.get("meetings")
    if meetings is None:
        m = re.search(r"book (\d+) meetings?", text, re.IGNORECASE)
        meetings = int(m.group(1)) if m else 5
    trucks = raw.get("spare_trucks_per_week")
    if trucks is None:
        m = re.search(r"(\d+) trucks? a week", text, re.IGNORECASE)
        trucks = int(m.group(1)) if m else None
    req_date = date.fromisoformat(raw["request_date"]) if raw.get("request_date") else date.today()
    job_id = raw.get("job_id") or idem_key("job", text, route, meetings, req_date.isoformat())
    return JobRequest(job_id=job_id, text=text, route_filter=route, meetings_wanted=meetings,
                      spare_trucks_per_week=trucks, request_date=req_date,
                      budget_inr=raw.get("budget_inr"))


def load_client_records(path: str | Path) -> ClientRecords:
    p = Path(path)
    data = json.loads((p if p.is_absolute() else ROOT / p).read_text(encoding="utf-8"))
    data.pop("_note", None)
    rec = ClientRecords(**data)
    for r in rec.routes:
        r.origin, r.destination = r.origin.lower(), r.destination.lower()
    return rec
