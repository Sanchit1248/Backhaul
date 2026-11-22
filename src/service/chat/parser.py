"""Turn a question into one task. Rules first; Claude tool calling only if rules find none.

Tasks (one tool each): find_targets, check_churn, route_capacity, show_draft.
The parser never answers a question itself: it only picks a task and its parameters,
and the UI shows that parsed task before it runs.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from ..config import env, settings
from ..routes.cities import CITIES

ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
            "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "last": -1}


@dataclass
class ParsedTask:
    type: str | None                    # None -> not understood
    params: dict = field(default_factory=dict)
    summary: str = ""
    parser: str = "rules"
    needs_confirm: bool = True          # paid / slow tasks wait for a click
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


_CITY_RX = "|".join(sorted((re.escape(c) for c in CITIES), key=len, reverse=True))
_LANE = re.compile(rf"\b(?:from\s+)?({_CITY_RX})\s*(?:to|-|->|–|→)\s*({_CITY_RX})\b", re.IGNORECASE)
_COUNT = re.compile(r"\b(\d{1,3})\s+(?:meetings?|targets?|companies|prospects|leads|buyers)\b",
                    re.IGNORECASE)
_WINDOW = re.compile(r"\b(?:last|past|within|in the last)\s+(\d{1,3})\s+days?\b", re.IGNORECASE)
_ORDINAL = re.compile(r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
                      r"last|(\d{1,2})(?:st|nd|rd|th)|#\s?(\d{1,2})|number\s+(\d{1,2}))\b", re.IGNORECASE)
_WHY_RISK = re.compile(r"\bwhy\s+(?:is|are)\s+(.+?)\s+(?:at\s+risk|leaving|churning)\b", re.IGNORECASE)
_CLIENT_RISK = re.compile(r"\b(?:is|are)\s+(.+?)\s+(?:at\s+risk|leaving|churning)\b", re.IGNORECASE)


def _lane(q: str) -> tuple[str, str] | None:
    m = _LANE.search(q)
    return (m.group(1).lower(), m.group(2).lower()) if m else None


def _ordinal(q: str) -> int | None:
    m = _ORDINAL.search(q)
    if not m:
        return None
    if m.group(2) or m.group(3) or m.group(4):
        return int(m.group(2) or m.group(3) or m.group(4))
    return ORDINALS[m.group(1).lower()]


def parse_rules(q: str) -> ParsedTask:
    ql = q.lower().strip()
    lane = _lane(q)
    if re.search(r"\b(draft|write|email|mail|show)\b", ql) and _ordinal(ql) and \
            re.search(r"\b(one|target|company|prospect|it)\b|#", ql):
        n = _ordinal(ql)
        return ParsedTask("show_draft", {"index": n}, f"Show the draft email for target "
                          f"{'last' if n == -1 else n} from the last target list",
                          needs_confirm=False)
    if re.search(r"\b(leav\w*|churn\w*|at risk|risk)\b", ql):
        m = _WHY_RISK.search(q) or _CLIENT_RISK.search(q)
        clients = [m.group(1).strip(" ?.")] if m and m.group(1).lower() not in (
            "anyone", "anybody", "who", "which clients", "any client", "any clients") else []
        p = {"clients": clients}
        return ParsedTask("check_churn", p, "Check churn risk for "
                          + (clients[0] if clients else "every client")
                          + " from order volume", needs_confirm=False)
    if re.search(r"\b(capacity|free trucks?|spare trucks?|space on|room on)\b", ql):
        p = {"origin": lane[0], "destination": lane[1]} if lane else {}
        return ParsedTask("route_capacity", p, "Show free truck capacity this week on "
                          + (f"{lane[0].title()} to {lane[1].title()}" if lane else "every lane"),
                          needs_confirm=False)
    if re.search(r"\b(find|book|who|which|targets?|prospects?|companies|leads|meetings?|"
                 r"buyers?|expanding|hiring)\b", ql):
        p: dict = {}
        if lane:
            p["routes"] = [list(lane)]
        if m := _COUNT.search(q):
            p["count"] = int(m.group(1))
        if m := _WINDOW.search(q):
            p["window_days"] = int(m.group(1))
        where = f"{lane[0].title()} to {lane[1].title()}" if lane else \
            "every lane with free capacity"
        return ParsedTask("find_targets", p,
                          f"Find up to {p.get('count', 5)} targets on {where}, triggers in the "
                          f"last {p.get('window_days', settings()['triggers']['window_days'])} "
                          "days (uses paid data; drafts go to the approval queue)")
    return ParsedTask(None, {}, "", error="I could not match that to a task.")


TOOLS = [
    {"name": "find_targets", "description": "Find companies with a fresh expansion trigger "
     "near a lane with free truck capacity, with a verified buyer.",
     "input_schema": {"type": "object", "properties": {
         "routes": {"type": "array", "items": {"type": "array", "items": {"type": "string"},
                                               "minItems": 2, "maxItems": 2},
                    "description": "lanes as [origin_city, destination_city], lower case"},
         "count": {"type": "integer", "minimum": 1, "maximum": 50},
         "window_days": {"type": "integer", "minimum": 1, "maximum": 365}}}},
    {"name": "check_churn", "description": "Rate which existing clients might be leaving, "
     "from their order volume trend.",
     "input_schema": {"type": "object", "properties": {
         "clients": {"type": "array", "items": {"type": "string"},
                     "description": "client names; empty for all"}}}},
    {"name": "route_capacity", "description": "Free truck capacity this week per lane.",
     "input_schema": {"type": "object", "properties": {
         "origin": {"type": "string"}, "destination": {"type": "string"}}}},
    {"name": "show_draft", "description": "Show the draft email for the n-th target of the "
     "previous target list (1 = first, -1 = last).",
     "input_schema": {"type": "object", "properties": {"index": {"type": "integer"}},
                      "required": ["index"]}},
]


def parse_llm(q: str, ledger, budget) -> ParsedTask:
    from .. import llm

    resp = llm.ask(ledger, budget, model=settings()["llm"]["cheap_model"], max_tokens=1024,
                   op="chat.parse", tools=TOOLS, tool_choice={"type": "any"},
                   system=("You route a logistics sales team's question to exactly one tool. "
                           "Never answer the question yourself. Cities are Indian cities in "
                           "lower case."),
                   messages=[{"role": "user", "content": q}])
    use = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
    if use is None:
        return ParsedTask(None, error="The model did not pick a task.", parser="llm")
    p = dict(use.input or {})
    summary = {"find_targets": "Find targets (uses paid data; drafts go to approval)",
               "check_churn": "Check client churn risk from order volume",
               "route_capacity": "Show free truck capacity",
               "show_draft": "Show a draft email from the last target list"}[use.name]
    return ParsedTask(use.name, p, f"{summary}: {p}", parser="llm",
                      needs_confirm=use.name == "find_targets")


def parse(q: str, ledger=None, budget=None) -> ParsedTask:
    t = parse_rules(q)
    if t.type or not settings().get("chat", {}).get("use_llm_parser", True) \
            or not env("ANTHROPIC_API_KEY") or ledger is None:
        return t
    try:
        return parse_llm(q, ledger, budget)
    except Exception as e:  # noqa: BLE001 - chat must answer even if the model is down
        return ParsedTask(None, error=f"I could not match that to a task (model: "
                                      f"{type(e).__name__}).", parser="llm")
