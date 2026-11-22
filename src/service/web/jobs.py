"""Background jobs for the browser: each job is the same `svc run` the CLI uses, started
as its own process, so kill/restart behaves exactly as on the command line (finished
steps resume from checkpoints; each email is sent once by step 10).

This module never imports the pipeline, the mailer or step 10: it only starts
`python -m service.cli run <request.json>` and reads the ledger/result files.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from ..config import ROOT

FINAL = {"done", "awaiting_approval", "partial", "stopped_limit"}


def new_job_id(route: list[str]) -> str:
    slug = "-".join(re.sub(r"[^a-z0-9]+", "", c.lower()) for c in route if c) or "job"
    return f"web-{slug}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


class JobRunner:
    def __init__(self, data_dir: Path, python: str = sys.executable):
        self.data_dir, self.python = Path(data_dir), python
        self.procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    # ---- files -------------------------------------------------------------
    def request_path(self, job_id: str) -> Path:
        """data/requests/<job_id>.json; for requests written by hand for the CLI (any file
        name), the file whose "job_id" field is job_id."""
        direct = self.data_dir / "requests" / f"{job_id}.json"
        if direct.exists():
            return direct
        return self._index().get(job_id, direct)

    def _index(self) -> dict[str, Path]:
        out: dict[str, Path] = {}
        d = self.data_dir / "requests"
        for f in sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime) if d.exists() else []:
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            jid = (raw.get("request") or raw).get("job_id") if isinstance(raw, dict) else None
            out[jid or f.stem] = f
        return out

    def log_path(self, job_id: str) -> Path:
        return self.data_dir / "jobs" / f"{job_id}.log"

    def meta_path(self, job_id: str) -> Path:
        return self.data_dir / "jobs" / f"{job_id}.meta.json"

    def result_path(self, job_id: str) -> Path:
        return self.data_dir / "results" / f"{job_id}.json"

    def meta(self, job_id: str) -> dict:
        p = self.meta_path(job_id)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def request(self, job_id: str) -> dict:
        p = self.request_path(job_id)
        raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        return raw.get("request", raw) if isinstance(raw, dict) else {}

    def result(self, job_id: str) -> dict | None:
        p = self.result_path(job_id)
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        except json.JSONDecodeError:
            return None          # being written right now

    def log_tail(self, job_id: str, lines: int = 40) -> str:
        p = self.log_path(job_id)
        if not p.exists():
            return ""
        return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])

    # ---- processes ---------------------------------------------------------
    def is_running(self, job_id: str) -> bool:
        p = self.procs.get(job_id)
        return p is not None and p.poll() is None

    def running_job(self) -> str | None:
        return next((j for j in self.procs if self.is_running(j)), None)

    def start(self, job_id: str, raw: dict | None = None, mode: str | None = None,
              by: str | None = None, reason: str = "run") -> None:
        """Start (or resume) a job. One job at a time: the Crustdata key allows 5
        requests a minute, and two jobs would share it."""
        with self._lock:
            busy = self.running_job()
            if busy:
                raise RuntimeError(f"job {busy} is still running; wait for it or stop it")
            if raw is not None:
                self.request_path(job_id).parent.mkdir(parents=True, exist_ok=True)
                self.request_path(job_id).write_text(json.dumps(raw, indent=2), encoding="utf-8")
            if not self.request_path(job_id).exists():
                raise FileNotFoundError(f"no request saved for job {job_id}")
            meta = self.meta(job_id)
            if mode is not None:
                meta["mode"] = mode
            meta.setdefault("created", time.time())
            meta.setdefault("by", by)
            meta.setdefault("runs", []).append({"at": time.time(), "by": by, "reason": reason})
            self.meta_path(job_id).parent.mkdir(parents=True, exist_ok=True)
            self.meta_path(job_id).write_text(json.dumps(meta, indent=2), encoding="utf-8")

            cmd = [self.python, "-m", "service.cli", "run", str(self.request_path(job_id))]
            if meta.get("mode"):
                cmd += ["--mode", meta["mode"]]
            env = {**os.environ, "PYTHONIOENCODING": "utf-8",
                   "SERVICE_DATA_DIR": str(self.data_dir),
                   "PYTHONPATH": os.pathsep.join(
                       [str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]).strip(os.pathsep)}
            env.pop("SERVICE_CRASH_AT", None)
            log = open(self.log_path(job_id), "ab")  # noqa: SIM115 - handed to the child
            log.write(f"\n===== {reason} {datetime.now():%Y-%m-%d %H:%M:%S} by {by} =====\n"
                      .encode())
            log.flush()
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            self.procs[job_id] = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log,
                                                  stderr=subprocess.STDOUT,
                                                  creationflags=flags)
            log.close()

    def stop(self, job_id: str) -> bool:
        """Hard-kill the job process (like taskkill /F). Resume re-runs the same request."""
        p = self.procs.get(job_id)
        if p is None or p.poll() is not None:
            return False
        p.kill()
        p.wait(timeout=10)
        return True

    def wait(self, job_id: str, timeout: float = 120) -> int | None:
        p = self.procs.get(job_id)
        return p.wait(timeout=timeout) if p else None

    # ---- listing -----------------------------------------------------------
    def job_ids(self) -> list[str]:
        """Newest first; a hand-written request file is listed under its job_id."""
        idx = self._index()
        return sorted(idx, key=lambda j: idx[j].stat().st_mtime, reverse=True)

    def status(self, job_id: str, ledger_run: dict | None) -> str:
        """running | done | awaiting_approval | partial | stopped_limit |
        interrupted (killed mid-run: resume) | not started"""
        if self.is_running(job_id):
            return "running"
        st = (ledger_run or {}).get("status")
        if st in FINAL:
            return st
        if st == "running":
            return "interrupted"
        return "not started" if not self.log_path(job_id).exists() else "failed"
