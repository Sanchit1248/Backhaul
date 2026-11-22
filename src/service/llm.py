"""Thin wrapper over the Anthropic SDK that records the cost of every call.

The model must never be the *source* of a fact. Use it to plan, extract fields
from fetched records, match names, and write explanations - then attach the
record (Source) the fact came from.
"""
from __future__ import annotations

import time

from .budget import Budget
from .config import settings
from .ledger import Ledger

_client = None


def client():
    global _client
    if _client is None:
        import anthropic  # lazy: tests run without the SDK/key

        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    return _client


def price_usd(model: str, usage) -> float:
    p = settings()["llm"]["pricing"].get(model)
    if p is None:
        raise KeyError(f"No pricing for {model} in config/service.toml - add it")
    cache_read_rate = p.get("cache_read", p["input"] * 0.1)
    cache_write_rate = p["input"] * 1.25
    return (
        usage.input_tokens * p["input"]
        + usage.output_tokens * p["output"]
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * cache_read_rate
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * cache_write_rate
    ) / 1_000_000


def ask(ledger: Ledger, budget: Budget, *, messages: list[dict], system: str | None = None,
        model: str | None = None, max_tokens: int = 16000, op: str = "llm",
        **kwargs):
    """Call Claude once, log cost, enforce budget. Returns the SDK Message."""
    cfg = settings()["llm"]
    model = model or cfg["model"]
    budget.check(f"before {op}")
    budget.llm_calls += 1

    t0 = time.monotonic()
    params = dict(model=model, max_tokens=max_tokens, messages=messages,
                  output_config={"effort": cfg.get("effort", "medium")}, **kwargs)
    if system:
        params["system"] = system
    resp = client().messages.create(**params)
    ms = int((time.monotonic() - t0) * 1000)

    served_by = resp.model or model
    u = resp.usage
    ledger.cost(
        budget.run_id, "anthropic", op, price_usd(served_by, u),
        units={"model": served_by, "in": u.input_tokens, "out": u.output_tokens,
               "cache_read": getattr(u, "cache_read_input_tokens", 0)},
        ms=ms,
    )
    if resp.stop_reason == "refusal":
        ledger.log(budget.run_id, "warn", f"{op}: model refused", {"model": served_by})
    return resp


def text_of(resp) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
