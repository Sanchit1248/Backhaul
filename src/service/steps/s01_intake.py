"""Step 1: intake.

Turn the raw request into a JobRequest and load the client records. Free text is
parsed with patterns; the browser shows what was understood for confirmation before
a job runs. TODO (kickoff kit): adapt both parsers to the organizers' formats.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from ..config import ROOT
from ..ledger import idem_key
from ..models import ClientRecords, JobRequest


# A city is one or two words ("Pune", "Navi Mumbai"); words that only frame the lane are
# stripped ("the Pune to Mumbai route" -> pune, mumbai).
_CITY = r"([a-z][a-z.]+(?:\s+[a-z][a-z.]+)?)"
_FRAME = {"the", "a", "an", "our", "route", "lane", "corridor", "for", "on", "from", "along"}
_ROUTE_PATTERNS = [
    rf"for the {_CITY} to {_CITY} (?:route|lane|corridor)",
    rf"between {_CITY} and {_CITY}",
    rf"from {_CITY} to {_CITY}",
    rf"{_CITY}\s*(?:->|→|–|—|-)\s*{_CITY}",
    rf"{_CITY} to {_CITY}",
]
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20}
_NUM = r"(\d+|" + "|".join(_NUM_WORDS) + r")"


def _num(s: str) -> int:
    return int(s) if s.isdigit() else _NUM_WORDS[s.lower()]


def _clean_city(c: str) -> str:
    words = [w for w in c.lower().strip(" .,;").split() if w not in _FRAME]
    return " ".join(words)


def parse_route(text: str) -> list[str]:
    """Two lane end points from free text, or [] when none is stated."""
    for pat in _ROUTE_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            a, b = _clean_city(m.group(1)), _clean_city(m.group(2))
            if a and b and a != b and not (set(a.split()) & {"meetings", "meeting", "trucks"}):
                return [a, b]
    return []


def parse_request(raw: dict) -> JobRequest:
    """Request dict -> JobRequest. Structured fields win (route, meetings,
    spare_trucks_per_week, request_date, budget_inr); otherwise parsed from the text:
    "Book 5 meetings for the Pune to Mumbai route; spare capacity 12 trucks a week",
    "3 meetings between Nashik and Navi Mumbai, 8 spare trucks weekly", "Pune-Mumbai lane"."""
    text = raw.get("text") or raw.get("instruction") or ""
    route = [c.lower().strip() for c in raw.get("route", []) if c and c.strip()]
    if not route:
        route = parse_route(text)
    meetings = raw.get("meetings")
    if meetings in (None, ""):
        m = re.search(rf"\b{_NUM}\s+(?:\w+\s+)?meetings?\b", text, re.IGNORECASE)
        meetings = _num(m.group(1)) if m else 5
    trucks = raw.get("spare_trucks_per_week")
    if trucks in (None, ""):
        m = (re.search(rf"\b{_NUM}\s+(?:spare\s+|free\s+)?trucks?\b", text, re.IGNORECASE)
             or re.search(rf"(?:spare\s+)?capacity\s+(?:of\s+)?{_NUM}\b", text, re.IGNORECASE))
        trucks = _num(m.group(1)) if m else None
    req_date = date.fromisoformat(raw["request_date"]) if raw.get("request_date") else date.today()
    job_id = raw.get("job_id") or idem_key("job", text, route, meetings, req_date.isoformat())
    budget = raw.get("budget_inr")
    return JobRequest(job_id=job_id, text=text, route_filter=route, meetings_wanted=int(meetings),
                      spare_trucks_per_week=int(trucks) if trucks not in (None, "") else None,
                      request_date=req_date,
                      budget_inr=float(budget) if budget not in (None, "") else None)


def load_client_records(path: str | Path) -> ClientRecords:
    p = Path(path)
    data = json.loads((p if p.is_absolute() else ROOT / p).read_text(encoding="utf-8"))
    data.pop("_note", None)
    rec = ClientRecords(**data)
    for r in rec.routes:
        r.origin, r.destination = r.origin.lower(), r.destination.lower()
    return rec
