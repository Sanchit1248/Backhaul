"""Keep this clone in step with origin/main automatically.

Runs as a Claude Code hook before every prompt (and at session start), so each
teammate's Claude always works on the latest main. Also has a watch mode for
when nobody is prompting.

    python scripts/sync.py --hook        # used by .claude/settings.json (JSON output)
    python scripts/sync.py               # one sync, human output
    python scripts/sync.py --watch 60    # sync every 60 s (only merges when tree is clean)

What it does when origin/main has new commits:
  * on main:     git pull --rebase --autostash origin main
  * on a branch: git merge --autostash --no-edit origin/main
  * uncommitted work is stashed and re-applied automatically
  * on a conflict it aborts cleanly (your work is untouched) and says which files
  * if pyproject.toml changed, reinstalls the package into .venv
  * if a shared contract file changed, tells Claude to re-read it
Stdlib only, never raises: a failed sync must not block anyone's prompt.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ["src/service/models.py", "src/service/sources/__init__.py",
          "src/service/steps/__init__.py", "config/service.yaml", "CLAUDE.md"]


def git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          timeout=timeout, check=False)


def in_progress() -> str | None:
    gitdir = Path(git("rev-parse", "--absolute-git-dir").stdout.strip() or ROOT / ".git")
    for name, what in (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"),
                       ("rebase-apply", "rebase")):
        if (gitdir / name).exists():
            return what
    return None


def venv_python() -> Path | None:
    for p in (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python"):
        if p.exists():
            return p
    return None


def sync(only_if_clean: bool = False) -> dict:
    """Returns {"level": "none|info|warn", "user": str, "claude": str}."""
    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        return {"level": "none"}
    if (op := in_progress()):
        return {"level": "warn", "user": f"sync skipped: a {op} is in progress",
                "claude": f"A git {op} is in progress in this repo; finish or abort it before "
                          "editing. Auto-sync is paused until then."}
    try:
        f = git("fetch", "-q", "origin", "main", timeout=20)
    except subprocess.TimeoutExpired:
        return {"level": "none"}  # offline / slow network: stay quiet, try next prompt
    if f.returncode != 0:
        return {"level": "none"}

    behind = int(git("rev-list", "--count", "HEAD..origin/main").stdout.strip() or 0)
    if behind == 0:
        return {"level": "none"}
    changed = git("diff", "--name-only", "HEAD...origin/main").stdout.split()
    log = git("log", "--format=%h %an: %s", "HEAD..origin/main", "-n", "8").stdout.strip()
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    dirty = bool(git("status", "--porcelain").stdout.strip())

    if only_if_clean and dirty:
        return {"level": "warn",
                "user": f"origin/main has {behind} new commit(s); not merged yet because you have "
                        "uncommitted changes (it will sync on your next Claude prompt)",
                "claude": ""}

    if branch == "main":
        r = git("pull", "--rebase", "--autostash", "-q", "origin", "main", timeout=60)
    else:
        r = git("merge", "--autostash", "--no-edit", "-q", "origin/main", timeout=60)

    if r.returncode != 0:
        conflicts = git("diff", "--name-only", "--diff-filter=U").stdout.split()
        op = in_progress()
        if op == "rebase":
            git("rebase", "--abort")
        elif op == "merge":
            git("merge", "--abort")
        files = ", ".join(conflicts) or "see git output"
        return {"level": "warn",
                "user": f"AUTO-SYNC CONFLICT with main in: {files}. Nothing was changed. "
                        "Ask Claude to resolve it.",
                "claude": (f"Auto-sync tried to bring in {behind} new commit(s) from origin/main "
                           f"and hit a conflict in: {files}. It aborted, so the working tree is "
                           "as it was. Before other work, tell the user, then resolve: commit "
                           "or stash local changes, run `git pull --rebase origin main` (or "
                           "`git merge origin/main` on a branch), fix the conflicts keeping "
                           "both sides' intent, run pytest + evals.\n"
                           f"git said: {(r.stderr or r.stdout).strip()[-600:]}")}

    notes = []
    if "pyproject.toml" in changed and (py := venv_python()):
        p = subprocess.run([str(py), "-m", "pip", "install", "-q", "-e", ".[dev]"], cwd=ROOT,
                           capture_output=True, text=True, timeout=180, check=False)
        notes.append("dependencies reinstalled" if p.returncode == 0
                     else "pyproject changed but pip install failed: run scripts/setup.ps1")
    shared = [s for s in SHARED if s in changed]
    user = f"synced {behind} new commit(s) from main" + (
        f"; SHARED FILES CHANGED: {', '.join(shared)}" if shared else "") + (
        f"; {'; '.join(notes)}" if notes else "")
    claude = (f"Auto-sync just merged {behind} new commit(s) from origin/main into this clone:\n"
              f"{log}\nFiles changed upstream: {', '.join(changed[:40])}"
              + (f"\nSHARED CONTRACT FILES CHANGED: {', '.join(shared)}. Re-read them before "
                 "editing code that depends on them." if shared else "")
              + ("\nAny file you read earlier may be stale; re-read before editing it."))
    return {"level": "info", "user": user, "claude": claude}


def main(argv: list[str]) -> int:
    if "--watch" in argv:
        i = argv.index("--watch")
        every = int(argv[i + 1]) if len(argv) > i + 1 and argv[i + 1].isdigit() else 60
        print(f"auto-sync: checking origin/main every {every}s (Ctrl+C to stop)")
        while True:
            try:
                res = sync(only_if_clean=True)
            except Exception as e:  # noqa: BLE001 - never die
                res = {"level": "warn", "user": f"sync error: {e}"}
            if res["level"] != "none":
                print(f"\a[{time.strftime('%H:%M:%S')}] {res['user']}", flush=True)
            time.sleep(every)

    event = "UserPromptSubmit"
    if "--hook" in argv:
        try:
            event = json.loads(sys.stdin.read() or "{}").get("hook_event_name", event)
        except (ValueError, OSError):
            pass
    try:
        res = sync()
    except Exception as e:  # noqa: BLE001 - a failed sync must not block a prompt
        res = {"level": "warn", "user": f"auto-sync error: {e}", "claude": ""}
    if res["level"] == "none":
        return 0
    if "--hook" in argv:
        out = {"systemMessage": res["user"]}
        if res.get("claude"):
            out["hookSpecificOutput"] = {"hookEventName": event,
                                         "additionalContext": res["claude"]}
        print(json.dumps(out))
    else:
        print(res["user"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
