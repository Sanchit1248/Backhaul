"""Privacy controls from config `privacy.*`. OWNER: B.

* mask(): emails and phone-like numbers in log lines and log data are masked when
  `privacy.mask_in_logs` is true (Ledger.log calls it for every event + console line).
* Retention (`privacy.retention_days`) is enforced by Ledger.purge_person_data and
  `svc purge` (also run automatically before every `svc run`).
"""
from __future__ import annotations

import re
from typing import Any

from .config import settings

EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
# +91 98765 43210, 098765-43210, (020) 2345 6789 ... : 10+ digits with separators.
PHONE_RE = re.compile(r"(?<![\w/=.-])\+?\d(?:[\s().-]*\d){9,13}(?![\w/-])")
_SENSITIVE_KEYS = {"email", "business_email", "personal_email", "phone", "phone_number", "to"}


def masking_on() -> bool:
    try:
        return bool(settings().get("privacy", {}).get("mask_in_logs", True))
    except Exception:  # noqa: BLE001 - no config: mask (safe default)
        return True


def mask_text(s: str) -> str:
    """'anita.rao@kaveri.in' -> 'a***@kaveri.in'; phone-like numbers -> '[phone]'."""
    s = EMAIL_RE.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", s)
    return PHONE_RE.sub("[phone]", s)


def mask(value: Any) -> Any:
    """Mask strings anywhere inside dicts/lists. Values under email/phone keys are
    masked even when they do not look like an address."""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(v, str) and str(k).lower() in _SENSITIVE_KEYS and "@" not in v and v:
                out[k] = "[masked]"
            else:
                out[k] = mask(v)
        return out
    if isinstance(value, (list, tuple)):
        return [mask(v) for v in value]
    return value


def retention_days() -> int:
    return int(settings().get("privacy", {}).get("retention_days", 30))
