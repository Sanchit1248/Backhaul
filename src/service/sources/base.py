"""HTTP helper with retries, timeouts, cost logging and a disk cache."""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx

from ..budget import Budget, BudgetExceeded
from ..config import DATA_DIR
from ..ledger import Ledger

CACHE_DIR = DATA_DIR / "http_cache"
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
MAX_RETRY_DELAY = 60
# Rejected requests have no charge when the provider sends no credit header.
UNCHARGED_STATUSES = {400, 401, 402, 403, 404, 422, 429}


class SourceUnavailable(RuntimeError):
    pass


class SourcePermissionDenied(SourceUnavailable):
    pass


@dataclass(frozen=True)
class CreditEstimate:
    maximum: Decimal
    inr_per_credit: float
    inr_per_usd: float

    def check(self, budget: Budget, operation: str) -> None:
        if not self.maximum:
            return
        if not math.isfinite(self.inr_per_credit) or self.inr_per_credit <= 0:
            raise BudgetExceeded(f"{operation} needs a positive costs.crustdata_inr_per_credit")
        if not math.isfinite(self.inr_per_usd) or self.inr_per_usd <= 0:
            raise BudgetExceeded(f"{operation} needs a positive costs.usd_to_inr")
        maximum_inr = float(self.maximum) * self.inr_per_credit
        remaining = budget.remaining_inr
        if maximum_inr > remaining and not math.isclose(
            maximum_inr, remaining, rel_tol=0, abs_tol=1e-9
        ):
            raise BudgetExceeded(
                f"{operation} may cost Rs {maximum_inr:.2f} ({self.maximum} credits), "
                f"with Rs {remaining:.2f} remaining"
            )

    def cost_usd(self, credits: Decimal) -> float:
        if not credits:
            return 0.0
        return float(credits) * self.inr_per_credit / self.inr_per_usd


def _cache_path(method: str, url: str, body) -> Path:
    h = hashlib.sha256(json.dumps([method, url, body], sort_keys=True).encode()).hexdigest()
    return CACHE_DIR / f"{h[:40]}.json"


def _write_cache(path: Path, envelope: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            json.dump(envelope, tmp)
            tmp.close()
            tmp_path.replace(path)
        finally:
            tmp_path.unlink(missing_ok=True)


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    fallback = min(2 ** attempt, 8)
    if response is None:
        return fallback
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            try:
                end = parsedate_to_datetime(retry_after)
                if end.tzinfo is None:
                    end = end.replace(tzinfo=UTC)
                delay = (end - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = math.nan
        if math.isfinite(delay):
            return max(0, delay)
    if response.status_code == 429:
        # The smoke runner observed a gateway 429 without Retry-After.
        try:
            reset = float(response.headers.get("X-RateLimit-Reset", "60"))
        except ValueError:
            return MAX_RETRY_DELAY
        return max(0, reset) if math.isfinite(reset) else MAX_RETRY_DELAY
    return fallback


def _record_credits(ledger: Ledger, budget: Budget, *, provider: str, op: str, method: str,
                    url: str, estimate: CreditEstimate, response: httpx.Response | None,
                    attempt: int, started_at: str, ms: int) -> None:
    status = response.status_code if response is not None else None
    headers = response.headers if response is not None else {}
    reported = None
    raw_credits = headers.get("X-Credits-Used")
    if raw_credits is not None:
        try:
            parsed = Decimal(raw_credits)
            if parsed.is_finite() and parsed >= 0:
                reported = parsed
        except InvalidOperation:
            pass
    budgeted = reported
    if budgeted is None:
        budgeted = Decimal(0) if status in UNCHARGED_STATUSES else estimate.maximum
    ledger.cost(
        budget.run_id, provider, op, estimate.cost_usd(budgeted), ms=ms,
        units={"status": status, "attempt": attempt, "method": method,
               "path": httpx.URL(url).path, "started_at": started_at,
               "request_id": headers.get("X-Request-Id"),
               "estimated_max_credits": float(estimate.maximum),
               "reported_credits": float(reported) if reported is not None else None,
               "budgeted_credits": float(budgeted)},
    )


def fetch_json(ledger: Ledger, budget: Budget, *, provider: str, op: str, method: str,
               url: str, headers: dict | None = None, params: dict | None = None,
               json_body: dict | None = None, cost_usd: float = 0.0,
               retries: int = 3, timeout: float = 20.0, use_cache: bool = True,
               credits: CreditEstimate | None = None) -> dict:
    """Return {data, url, retrieved_at}. Credit estimates are applied per HTTP attempt."""
    operation = f"{provider}.{op}"
    key_body = {"params": params, "json": json_body}
    if headers:
        key_body["headers"] = sorted(httpx.Headers(headers).multi_items())
    cpath = _cache_path(method, url, key_body)
    if use_cache:
        try:
            return json.loads(cpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    for attempt in range(retries + 1):
        budget.check(f"before {operation}")
        if credits is not None:
            credits.check(budget, operation)
        started_at = datetime.now(UTC).isoformat() if credits is not None else ""
        t0 = time.monotonic()
        response = None
        try:
            response = httpx.request(method, url, headers=headers, params=params,
                                     json=json_body, timeout=timeout)
        except httpx.TransportError as error:
            failure = type(error).__name__
        ms = int((time.monotonic() - t0) * 1000)
        if credits is not None:
            _record_credits(ledger, budget, provider=provider, op=op, method=method, url=url,
                            estimate=credits, response=response, attempt=attempt,
                            started_at=started_at, ms=ms)

        if response is not None:
            status = response.status_code
            if response.is_success:
                if credits is None:
                    ledger.cost(budget.run_id, provider, op, cost_usd,
                                units={"status": status, "attempt": attempt}, ms=ms)
                try:
                    data = response.json()
                except ValueError as error:
                    raise SourceUnavailable(f"{operation}: invalid JSON response") from error
                out = {"data": data, "url": str(response.url),
                       "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                if use_cache:
                    try:
                        _write_cache(cpath, out)
                    except OSError:
                        ledger.log(budget.run_id, "warn", f"{operation}: could not write cache")
                return out

            failure = f"HTTP {status}"
            if status in (401, 403):
                raise SourcePermissionDenied(f"{operation}: {failure} (access denied)")
            if status not in RETRYABLE:
                raise SourceUnavailable(f"{operation}: {failure}")

        ledger.log(budget.run_id, "warn", f"{operation} attempt {attempt + 1} failed: {failure}")
        if attempt == retries:
            break
        delay = _retry_delay(response, attempt)
        if delay > MAX_RETRY_DELAY:
            break
        time.sleep(delay)

    raise SourceUnavailable(f"{operation} failed after {attempt + 1} attempt(s): {failure}")
