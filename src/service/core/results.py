"""Shared result rule: every result carries kept | held | cannot_verify + reason + evidence IDs."""
from __future__ import annotations

from typing import Literal

from ..models import Status

ResultStatus = Literal["kept", "held", "cannot_verify"]

_KEPT = {Status.ACTIVE, Status.AWAITING_APPROVAL, Status.SENT}


def result_status(status: Status | str) -> ResultStatus:
    s = Status(status)
    if s == Status.CANNOT_VERIFY:
        return "cannot_verify"
    return "kept" if s in _KEPT else "held"
