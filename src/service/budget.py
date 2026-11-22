"""Per-run time and cost limits. Checked before every paid call."""
from __future__ import annotations

import time

from .config import settings
from .ledger import Ledger


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, ledger: Ledger, run_id: str):
        lim = settings()["limits"]
        self.ledger, self.run_id = ledger, run_id
        self.max_cost = float(lim["max_cost_usd"])
        self.max_seconds = float(lim["max_seconds"])
        self.max_llm_calls = int(lim["max_llm_calls"])
        self.started = time.monotonic()
        self.llm_calls = 0

    def check(self, what: str = "") -> None:
        spent = self.ledger.run_cost(self.run_id)
        if spent >= self.max_cost:
            raise BudgetExceeded(f"cost limit ${self.max_cost} reached (${spent:.4f}) {what}")
        if time.monotonic() - self.started >= self.max_seconds:
            raise BudgetExceeded(f"time limit {self.max_seconds}s reached {what}")
        if self.llm_calls >= self.max_llm_calls:
            raise BudgetExceeded(f"LLM call limit {self.max_llm_calls} reached {what}")

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.max_cost - self.ledger.run_cost(self.run_id))
