"""Outbox interface: queue_action() / approve() / reject(). Sends once, only in step 10.

Thin wrapper over the ledger's action table so the chat and web layer never touch
SQL. key = hash(job_id, prospect_id, action), same as step 9.
"""
from __future__ import annotations

from ..ledger import Ledger, idem_key
from .memory import AccountMemory


def action_key(job_id: str, prospect_id: str, action: str = "send_email") -> str:
    return idem_key(job_id, prospect_id, action)


def queue_action(ledger: Ledger, job_id: str, prospect_id: str, payload: dict,
                 action: str = "send_email") -> dict:
    key = action_key(job_id, prospect_id, action)
    status = ledger.request_approval(job_id, action, key, payload)
    return {"key": key, "status": status}


def _decide(ledger: Ledger, key: str, approve: bool, by: str) -> dict:
    a = ledger.get_action(key)
    if a is None:
        raise KeyError(f"no action {key}")
    if a["status"] != "pending_approval":
        raise ValueError(f"action is {a['status']}, not pending approval")
    ledger.decide(key, approve, by=by)
    p = a["payload"]
    company_id = (p.get("buyer") or {}).get("company_id")
    name = p.get("company") or key
    AccountMemory(ledger).record_action(
        str(company_id) if company_id else name.strip().lower(), name,
        "approved" if approve else "rejected", outcome=f"by {by}", job_id=a["run_id"])
    return {"key": key, "status": "approved" if approve else "rejected"}


def approve(ledger: Ledger, key: str, by: str) -> dict:
    return _decide(ledger, key, True, by)


def reject(ledger: Ledger, key: str, by: str) -> dict:
    return _decide(ledger, key, False, by)


def status(ledger: Ledger, key: str) -> str | None:
    a = ledger.get_action(key)
    return a["status"] if a else None
