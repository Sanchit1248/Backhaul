"""Approval web app (OWNER: C). Run: `python -m service.web` (needs APPROVER_PASSWORD in .env).

Approve / edit / reject only change the ledger. The app NEVER sends: step 10 sends
what was approved on the next pipeline run.
"""
from .app import create_app

__all__ = ["create_app"]
