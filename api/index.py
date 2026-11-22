"""Vercel entry point: the Backhaul web app as a limited demo.

On Vercel the app runs in demo mode (BACKHAUL_DEMO=1): test data only (no Crustdata key is
deployed, nothing is emailed), each job runs to the end inside the request, and data lives in
/tmp, so it resets when the platform recycles the instance. The full service (live data,
persistent ledger, kill/restart guarantees) runs on a server: see README.md.

Environment variables to set in the Vercel project:
  APPROVER_PASSWORD        login password (choose a strong one)
  APPROVER_SESSION_SECRET  long random string, so sessions survive across instances
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("BACKHAUL_DEMO", "1")
os.environ.setdefault("SERVICE_DATA_DIR", "/tmp/backhaul")
sys.path.insert(0, str(ROOT / "src"))

from service.web.app import create_app  # noqa: E402

app = create_app()
