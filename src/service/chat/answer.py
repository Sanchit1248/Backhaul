"""Answer writer: turns a task result into lines; a line that states a fact must carry
evidence IDs that exist in the evidence store, otherwise it is replaced by a
"cannot verify" line. Notes (our own decisions: held, skipped, counts) need none.

Line = {"kind": "fact" | "note" | "heading" | "link", "text": str, "evidence_ids": [...],
        "href"?: str}
"""
from __future__ import annotations

from ..core.evidence import EvidenceStore
from ..models import Fact, Source


def fact(text: str, ids: list[str]) -> dict:
    return {"kind": "fact", "text": text, "evidence_ids": list(ids)}


def note(text: str) -> dict:
    return {"kind": "note", "text": text, "evidence_ids": []}


def heading(text: str) -> dict:
    return {"kind": "heading", "text": text, "evidence_ids": []}


def link(text: str, href: str) -> dict:
    return {"kind": "link", "text": text, "evidence_ids": [], "href": href}


def check(lines: list[dict], store: EvidenceStore) -> list[dict]:
    """Every fact line must point at stored evidence; otherwise it becomes cannot verify."""
    out = []
    for ln in lines:
        if ln["kind"] == "fact":
            ids = ln["evidence_ids"]
            if not ids or any(store.get_fact(i) is None for i in ids):
                out.append(note(f"cannot verify: no stored evidence for \"{ln['text']}\""))
                continue
        out.append(ln)
    return out


def _targets(r: dict, limit: int) -> list[dict]:
    lanes = ", ".join(lane["label"] for lane in r["lanes"]) or "no lane"
    lines = [heading(f"{r['kept']} target(s) kept on {lanes} (window {r['window_days']} days, "
                     f"cost Rs {r['cost_inr']:.2f}, routes from {r['adapter']})")]
    lines += [note(n) for n in r.get("notes", [])]
    for lane in r["lanes"]:
        if lane.get("skipped"):
            lines.append(note(f"{lane['label']}: {lane['skipped']}"))
        else:
            cap = lane["capacity"]
            lines.append(note(f"{lane['label']}: {cap['free']} free trucks in {cap['week']}; "
                              f"job {lane['job_status']}"))
    kept = [t for t in r["targets"] if t["status"] == "kept"]
    for i, t in enumerate(kept[:limit], 1):
        lines.append(heading(f"{i}. {t['company']}"))
        for trig in t["triggers"][:1]:
            when = f" ({trig['event_date']})" if trig["event_date"] else ""
            lines.append(fact(f"Trigger: {trig['summary']}{when}", [trig["evidence_id"]]))
        if t["route_fit"]:
            lines.append(fact(f"Route fit ({t['route_fit']['level']}, score "
                              f"{t['route_fit']['score']:.2f} estimate): "
                              f"{t['route_fit']['reason']}", t["route_fit"]["evidence_ids"]))
        if t["buyer"]:
            lines.append(fact(f"Buyer: {t['buyer']['name']}, {t['buyer']['title']}",
                              t["buyer"]["evidence_ids"]))
        if t["outbox_key"]:
            lines.append(link("Draft is in the approval queue", f"/#a-{t['outbox_key']}"))
    others = [t for t in r["targets"] if t["status"] != "kept"]
    if others:
        lines.append(heading(f"Held or cannot verify ({len(others)})"))
        for t in others:
            last = t.get("last_contact")
            extra = f"; contacted on {last['day']} ({last['action']})" if last else ""
            lines.append(note(f"{t['company']}: {t['status'].replace('_', ' ')}: "
                              f"{t['reason']}{extra}"))
    return lines


def _churn(r: dict) -> list[dict]:
    lines = [heading(f"Churn watch: {len(r['clients'])} client(s), last {r['window_weeks']} "
                     f"weeks of orders vs the {r['window_weeks']} before (from {r['adapter']})")]
    lines += [note(n) for n in r.get("notes", [])]
    for c in r["clients"]:
        if c["status"] == "cannot_verify":
            lines.append(note(f"{c['client']}: {c['reason']}"))
        else:
            lines.append(fact(f"{c['client']}: {c['risk']} risk. {c['reason']}",
                              c["evidence_ids"]))
    return lines


def _capacity(r: dict, store: EvidenceStore) -> list[dict]:
    lines = [heading(f"Free truck capacity (from {r['adapter']}, synced {r['synced_at']})")]
    lines += [note(n) for n in r.get("notes", [])]
    for cap in r["lanes"]:
        label = f"{cap['origin'].title()} to {cap['destination'].title()}"
        if not cap["known"]:
            lines.append(note(f"{label}: {cap['reason']}"))
            continue
        srcs = [Source.model_validate(s) for s in cap["sources"]]
        eid = store.save_fact(Fact(claim=f"{label}: {cap['reason']}", value=cap["free"],
                                   sources=srcs))
        lines.append(fact(f"{label}: {cap['free']} free of {cap['trucks_per_week']} truck "
                          f"trips in {cap['week']} ({cap['booked']} booked)", [eid]))
    return lines


def _draft(r: dict, store: EvidenceStore) -> list[dict]:
    if r.get("error"):
        return [note(r["error"])]
    d = r["draft"]
    ids = [store.save_fact(Fact.model_validate(c)) for c in d.get("claims", [])]
    lines = [heading(f"Draft to {r['company']} ({r['action_status'].replace('_', ' ')})"),
             note(f"To: {d['to']}"), note(f"Subject: {d['subject']}"),
             {"kind": "pre", "text": d["body"], "evidence_ids": []}]
    lines += [fact(f"[{i}] {c['claim']}", [eid])
              for i, (c, eid) in enumerate(zip(d.get("claims", []), ids, strict=True), 1)]
    if r["action_status"] == "pending_approval":
        lines.append(link("Approve, edit or reject it", f"/#a-{r['key']}"))
    return lines


def write_answer(task_type: str, result: dict, store: EvidenceStore, limit: int = 10) -> list[dict]:
    lines = {"find_targets": lambda: _targets(result, limit),
             "check_churn": lambda: _churn(result),
             "route_capacity": lambda: _capacity(result, store),
             "show_draft": lambda: _draft(result, store)}[task_type]()
    return check(lines, store)
