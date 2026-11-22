"""HTTP helper with retries, timeouts, cost logging and a disk cache.

"Prove that it keeps working when sources fail": every source call goes
through `fetch_json`, which retries transient errors with backoff and raises
SourceUnavailable when it gives up - callers turn that into "cannot verify",
never into a guess.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import httpx

from ..budget import Budget
from ..config import DATA_DIR
from ..ledger import Ledger

CACHE_DIR = DATA_DIR / "http_cache"
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


class SourceUnavailable(RuntimeError):
    pass


def _cache_path(method: str, url: str, body) -> Path:
    h = hashlib.sha256(json.dumps([method, url, body], sort_keys=True).encode()).hexdigest()
    return CACHE_DIR / f"{h[:40]}.json"


def fetch_json(ledger: Ledger, budget: Budget, *, provider: str, op: str, method: str,
               url: str, headers: dict | None = None, params: dict | None = None,
               json_body: dict | None = None, cost_usd: float = 0.0,
               retries: int = 3, timeout: float = 20.0, use_cache: bool = True) -> dict:
    """Returns {"data": ..., "url": ..., "retrieved_at": ...}."""
    key_body = {"params": params, "json": json_body}
    cpath = _cache_path(method, url, key_body)
    if use_cache and cpath.exists():
        return json.loads(cpath.read_text(encoding="utf-8"))

    budget.check(f"before {provider}.{op}")
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        t0 = time.monotonic()
        try:
            r = httpx.request(method, url, headers=headers, params=params,
                              json=json_body, timeout=timeout)
            ms = int((time.monotonic() - t0) * 1000)
            if r.status_code in RETRYABLE:
                raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            ledger.cost(budget.run_id, provider, op, cost_usd,
                        units={"status": r.status_code, "attempt": attempt}, ms=ms)
            out = {"data": r.json(), "url": str(r.url),
                   "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            if use_cache:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cpath.write_text(json.dumps(out), encoding="utf-8")
            return out
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            last_err = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status and status not in RETRYABLE:
                break
            ledger.log(budget.run_id, "warn", f"{provider}.{op} attempt {attempt} failed: {e}")
            time.sleep(min(2 ** attempt, 8))
    raise SourceUnavailable(f"{provider}.{op} failed after {retries + 1} attempts: {last_err}")
