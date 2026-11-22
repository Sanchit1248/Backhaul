"""Per-job time and cost limits. Checked before every paid call."""
from __future__ import annotations

import time

from .config import settings, usd_to_inr
from .ledger import Ledger


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, ledger: Ledger, run_id: str, max_cost_inr: float | None = None):
        lim = settings()["limits"]
        self.ledger, self.run_id = ledger, run_id
        self.max_cost_inr = float(max_cost_inr or lim["max_cost_inr"])
        self.max_seconds = float(lim["max_seconds"])
        self.max_llm_calls = int(lim["max_llm_calls"])
        self.started = time.monotonic()
        self.llm_calls = 0

    @property
    def spent_inr(self) -> float:
        return usd_to_inr(self.ledger.run_cost(self.run_id))

    def check(self, what: str = "") -> None:
        if self.spent_inr >= self.max_cost_inr:
            raise BudgetExceeded(
                f"cost limit Rs {self.max_cost_inr} reached (Rs {self.spent_inr:.2f}) {what}")
        if time.monotonic() - self.started >= self.max_seconds:
            raise BudgetExceeded(f"time limit {self.max_seconds}s reached {what}")
        if self.llm_calls >= self.max_llm_calls:
            raise BudgetExceeded(f"LLM call limit {self.max_llm_calls} reached {what}")

    @property
    def remaining_inr(self) -> float:
        return max(0.0, self.max_cost_inr - self.spent_inr)
