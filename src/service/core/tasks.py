"""Task runner: run_task(type, params) -> {task_id, status, result}.

task_id is deterministic (hash of type + params), so asking the same thing twice, or
re-running after a kill, returns / resumes the same task instead of starting another:
  * status done      -> the stored result, nothing re-run
  * status running   -> (process was killed) run again; find_targets resumes from the
                        pipeline's step checkpoints, so finished steps are not redone
Task functions are registered with @task("name") and take (ledger, params) -> dict.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable

from ..ledger import Ledger, idem_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id    TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    params     TEXT NOT NULL,
    status     TEXT NOT NULL,          -- running | done | failed
    result     TEXT,
    error      TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

TaskFn = Callable[[Ledger, dict], dict]
REGISTRY: dict[str, TaskFn] = {}
NO_CACHE: set[str] = set()   # live-state tasks (route capacity): always re-run


def task(name: str, *, cache: bool = True):
    def deco(fn: TaskFn) -> TaskFn:
        REGISTRY[name] = fn
        if not cache:
            NO_CACHE.add(name)
        return fn
    return deco


def _ensure(ledger: Ledger) -> None:
    ledger.db.executescript(SCHEMA)


def task_id(type_: str, params: dict) -> str:
    return "t_" + idem_key("task", type_, params)[:24]


def get_task(ledger: Ledger, tid: str) -> dict | None:
    _ensure(ledger)
    row = ledger.db.execute(
        "SELECT task_id, type, params, status, result, error, created_at, updated_at"
        " FROM tasks WHERE task_id=?", (tid,)).fetchone()
    if not row:
        return None
    t, ty, p, st, res, err, c, u = row
    return {"task_id": t, "type": ty, "params": json.loads(p), "status": st,
            "result": json.loads(res) if res else None, "error": err, "created_at": c,
            "updated_at": u}


def recent_tasks(ledger: Ledger, type_: str | None = None, limit: int = 20) -> list[dict]:
    _ensure(ledger)
    sql, args = "SELECT task_id FROM tasks", []
    if type_:
        sql += " WHERE type=?"
        args.append(type_)
    rows = ledger.db.execute(sql + " ORDER BY updated_at DESC LIMIT ?", (*args, limit))
    return [get_task(ledger, t) for (t,) in rows.fetchall()]


def run_task(ledger: Ledger, type_: str, params: dict, *, rerun: bool = False) -> dict:
    """Run (or resume) a task to completion in this thread."""
    _load_registry()
    if type_ not in REGISTRY:
        raise KeyError(f"unknown task type {type_!r}; known: {sorted(REGISTRY)}")
    _ensure(ledger)
    tid = task_id(type_, params)
    now = time.time()
    existing = get_task(ledger, tid)
    if existing and existing["status"] == "done" and not rerun and type_ not in NO_CACHE:
        return existing
    ledger.db.execute(
        "INSERT INTO tasks VALUES (?,?,?,'running',NULL,NULL,?,?) ON CONFLICT(task_id)"
        " DO UPDATE SET status='running', error=NULL, updated_at=excluded.updated_at",
        (tid, type_, json.dumps(params, sort_keys=True, default=str), now, now))
    try:
        result = REGISTRY[type_](ledger, {**params, "task_id": tid})
    except Exception as e:
        ledger.db.execute("UPDATE tasks SET status='failed', error=?, updated_at=? WHERE task_id=?",
                          (f"{type(e).__name__}: {e}", time.time(), tid))
        raise
    ledger.db.execute("UPDATE tasks SET status='done', result=?, updated_at=? WHERE task_id=?",
                      (json.dumps(result, default=str), time.time(), tid))
    return get_task(ledger, tid)


_bg_lock = threading.Lock()
_running: set[str] = set()


def start_task(ledger: Ledger, type_: str, params: dict) -> str:
    """Start a task in a background thread (web/chat). Returns the task_id at once."""
    _load_registry()
    tid = task_id(type_, params)
    done = get_task(ledger, tid)
    if done and done["status"] == "done" and type_ not in NO_CACHE:
        return tid
    with _bg_lock:
        if tid in _running:
            return tid
        _running.add(tid)

    path = ledger.db.execute("PRAGMA database_list").fetchone()[2]

    def work():
        own = Ledger(path)  # own connection: ledger transactions must not interleave
        try:
            run_task(own, type_, params)
        except Exception:  # noqa: BLE001, S110 - recorded in the tasks table
            pass
        finally:
            own.close()
            with _bg_lock:
                _running.discard(tid)

    threading.Thread(target=work, name=f"task-{tid}", daemon=True).start()
    return tid


def _load_registry() -> None:
    """Import the modules that register task functions (avoids import cycles)."""
    from ..routes import tasks as _routes_tasks  # noqa: F401
    from ..signals import churn as _churn  # noqa: F401
    from ..signals import targets as _targets  # noqa: F401
