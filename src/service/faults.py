"""Crash injection for kill tests and drills. OWNER: B.

Set SERVICE_CRASH_AT to a crash point name and the process hard-exits there
(os._exit: no cleanup, no flush, like `taskkill /F` or a judge pulling the plug).
Unset in normal runs, so it costs nothing.

Crash points:
  after:<step>      right after a step is checkpointed, e.g. after:04_triggers
  before_send       outbox row is 'started', email not written yet
  after_send        email + invite written, outbox row not yet 'done'
"""
from __future__ import annotations

import os

EXIT_CODE = 137  # what a SIGKILL'd process reports on Linux; easy to assert


def maybe_crash(point: str) -> None:
    if os.environ.get("SERVICE_CRASH_AT") == point:
        print(f"[fault] crashing at {point}", flush=True)
        os._exit(EXIT_CODE)
