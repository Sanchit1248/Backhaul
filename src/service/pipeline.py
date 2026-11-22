"""Request -> result pipeline. Track-agnostic skeleton: fill in the steps once
the track is chosen.

    intake   parse/normalise the request (client records from kickoff kit)
    gather   call live sources; every record kept with its Source
    decide   match/score/judge -> Facts or CannotVerify, never guesses
    act      propose side effects; irreversible ones wait for approval
    report   assemble the output in the kickoff-kit format

Each step is checkpointed (Ledger.step) so a kill + restart resumes where it
stopped, and each side effect goes through Ledger.action so it happens once.
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any

from .budget import Budget, BudgetExceeded
from .config import settings
from .evidence import CannotVerify
from .ledger import Ledger, idem_key


def run_id_for(request: dict) -> str:
    """Same request -> same run id -> restart resumes instead of duplicating."""
    return request.get("request_id") or idem_key("run", request)


def _plain(x: Any) -> Any:
    if is_dataclass(x):
        return asdict(x)
    if isinstance(x, list):
        return [_plain(i) for i in x]
    return x


# ---------------------------------------------------------------- steps ---
# Replace the bodies below with the real job. Keep signatures: each returns
# JSON-serialisable data (it is checkpointed).

def intake(request: dict) -> dict:
    return {"items": request.get("items", []), "instruction": request.get("instruction", "")}


def gather(ledger: Ledger, budget: Budget, parsed: dict) -> list[dict]:
    # TODO: call sources (sources/crustdata.py, registries, web) per item.
    # On SourceUnavailable -> record it; decide() turns it into CannotVerify.
    return [{"item": it, "records": []} for it in parsed["items"]]


def decide(ledger: Ledger, budget: Budget, gathered: list[dict]) -> list[dict]:
    out = []
    for g in gathered:
        if not g["records"]:
            out.append({"item": g["item"], "result": _plain(CannotVerify(
                question=f"resolve {g['item']}", missing="no source record found",
                tried=[]))})
            continue
        # TODO: build Fact(...) objects with Source(...) from g["records"].
    return out


def propose_actions(decisions: list[dict]) -> list[dict]:
    """Return [{"kind": ..., "identity": [...], "payload": {...}}]. identity must be the
    business identity of the action (invoice no + payee + amount), never a timestamp."""
    return []


def perform(action: dict) -> dict:
    """The real side effect (Razorpay/Stripe TEST mode, email send, ...).
    Must pass action['idempotency_key'] to the provider."""
    raise NotImplementedError(action["kind"])


# ------------------------------------------------------------- runner ---

def run(request: dict, ledger: Ledger | None = None) -> dict:
    ledger = ledger or Ledger()
    rid = run_id_for(request)
    fresh = ledger.start_run(rid, request)
    ledger.log(rid, "info", "run started" if fresh else "run resumed")
    budget = Budget(ledger, rid)
    irreversible = set(settings()["approval"]["irreversible"])

    try:
        parsed = ledger.step(rid, "intake", lambda: intake(request))
        gathered = ledger.step(rid, "gather", lambda: gather(ledger, budget, parsed))
        decisions = ledger.step(rid, "decide", lambda: decide(ledger, budget, gathered))

        action_results = []
        for a in propose_actions(decisions):
            key = idem_key(a["kind"], *a["identity"])
            res = ledger.action(rid, a["kind"], key, a["payload"], perform,
                                needs_approval=a["kind"] in irreversible)
            action_results.append({"kind": a["kind"], **res})

        waiting = any(r["status"] == "pending_approval" for r in action_results)
        result = {
            "run_id": rid,
            "status": "waiting_approval" if waiting else "done",
            "decisions": decisions,
            "actions": action_results,
            "cost_usd": round(ledger.run_cost(rid), 6),
        }
        missing = [f for f in settings()["output"]["required_fields"]
                   if f not in result and f != "facts"]
        if missing:
            ledger.log(rid, "error", f"output missing required fields: {missing}")
        ledger.set_run(rid, result["status"], result)
        return result
    except BudgetExceeded as e:
        ledger.log(rid, "error", str(e))
        result = {"run_id": rid, "status": "stopped_limit", "reason": str(e),
                  "cost_usd": round(ledger.run_cost(rid), 6)}
        ledger.set_run(rid, "failed", result)
        return result


if __name__ == "__main__":
    import sys

    with open(sys.argv[1], encoding="utf-8") as f:
        print(json.dumps(run(json.load(f)), indent=2))
