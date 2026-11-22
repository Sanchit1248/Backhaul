"""Chat flow: ask -> parsed task shown -> (confirm) -> task runs -> answer with sources.

Free, fast tasks (churn, capacity, show a draft) run as soon as they are parsed;
find_targets uses paid data and waits for the person to click Run.
"""
from __future__ import annotations

from ..budget import Budget
from ..config import settings
from ..core.evidence import EvidenceStore
from ..core.tasks import get_task, start_task
from ..ledger import Ledger
from .answer import note, write_answer
from .memory import ConversationMemory
from .parser import parse


def ask(ledger: Ledger, conv_id: str, author: str, question: str) -> int:
    question = " ".join(question.split())[:500]
    parsed = parse(question, ledger, Budget(ledger, f"chat-{conv_id}"))
    mem = ConversationMemory(ledger)
    turn_id = mem.add_turn(conv_id, author, question, parsed.as_dict())
    if parsed.type in ("check_churn", "route_capacity") and not parsed.needs_confirm:
        run(ledger, turn_id)
    return turn_id


def run(ledger: Ledger, turn_id: int) -> str | None:
    mem = ConversationMemory(ledger)
    t = mem.turn(turn_id)
    if t is None or t["parsed"].get("type") in (None, "show_draft"):
        return None
    if t["task_id"]:
        return t["task_id"]
    tid = start_task(ledger, t["parsed"]["type"], t["parsed"]["params"])
    mem.set_task(turn_id, tid)
    return tid


def resolve_draft(ledger: Ledger, conv_id: str, turn_id: int, index: int) -> dict:
    res = ConversationMemory(ledger).last_targets(conv_id, before_turn=turn_id)
    if res is None:
        return {"error": "There is no finished target list in this conversation yet; ask for "
                         "targets first (e.g. \"find 5 targets on Pune to Mumbai\")."}
    kept = [t for t in res["targets"] if t["status"] == "kept" and t["outbox_key"]]
    if not kept:
        return {"error": "The last target list has no target with a draft."}
    if index == 0 or abs(index) > len(kept) or index < -1:
        return {"error": f"The last list has {len(kept)} target(s) with a draft; pick 1 to "
                         f"{len(kept)}."}
    t = kept[index - 1] if index > 0 else kept[-1]
    a = ledger.get_action(t["outbox_key"])
    if a is None:
        return {"error": f"No draft found for {t['company']}."}
    return {"company": t["company"], "key": a["key"], "action_status": a["status"],
            "draft": a["payload"]["draft"]}


def render(ledger: Ledger, turn: dict) -> dict:
    """{state: parsed | running | done | failed | not_understood, lines: [...]}"""
    p = turn["parsed"]
    store = EvidenceStore(ledger)
    limit = int(settings().get("chat", {}).get("max_targets_shown", 10))
    if not p.get("type"):
        return {"state": "not_understood", "lines": [note(
            (p.get("error") or "I could not match that to a task.")
            + " Try: \"find 5 targets on Pune to Mumbai\", \"what clients might be leaving?\", "
              "\"free capacity on Nashik to Mumbai\", \"draft an email to the second one\".")]}
    if p["type"] == "show_draft":
        r = resolve_draft(ledger, turn["conv_id"], turn["turn_id"], int(p["params"]["index"]))
        return {"state": "done", "lines": write_answer("show_draft", r, store)}
    if not turn["task_id"]:
        return {"state": "parsed", "lines": []}
    task = get_task(ledger, turn["task_id"])
    if task is None or task["status"] == "running":
        return {"state": "running", "lines": []}
    if task["status"] == "failed":
        return {"state": "failed", "lines": [note(f"The task failed: {task['error']}")]}
    return {"state": "done", "lines": write_answer(p["type"], task["result"], store, limit)}
