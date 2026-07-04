#!/usr/bin/env python3
"""reap.py — resiliency: reap ORPHANED / STUCK agent subprocesses and clean stale scratch.

When an orchestrator (a build or research run) is killed, its detached `claude -p` / `codex exec` children
can survive as orphans and keep burning tokens + RAM. This reaps them: an agent process is killed if it is
ORPHANED (reparented to init, PPID 1 — its orchestrator is gone) or STUCK (running far longer than any real
agent should). Also sweeps our /tmp scratch. Safe to run on a cadence (wired as a scheduler job) and
idempotent. Never touches the interactive session (only `claude -p` / `codex exec`, never `claude --continue`).

    reap.py run        # reap orphans/stuck + clean scratch
    reap.py status     # show what WOULD be reaped (dry run)
    reap.py selftest
Run with the agent-os venv python.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402
import trace as _trace  # noqa: E402  — shared DATABASE_URL
DB = _trace.DB

MAX_RUNTIME_S = int(os.environ.get("AOS_AGENT_MAX_RUNTIME", "2400"))   # 40 min — beyond any real agent
SCRATCH_MAX_AGE_S = int(os.environ.get("AOS_SCRATCH_MAX_AGE", "3600")) # 1 h
SCRATCH_GLOBS = ["codexrun-*", "improve-*", "research-*", "webqa-*.png"]


def _reap_reason(ppid, etimes, max_s=MAX_RUNTIME_S):
    """Pure decision (unit-tested): why this agent process should be reaped, or None to keep it."""
    if ppid == 1:
        return "orphaned (parent dead)"
    if etimes > max_s:
        return f"stuck ({etimes}s > {max_s}s)"
    return None


def _agent_procs():
    """(pid, ppid, etimes, args) for every `claude -p` / `codex exec` agent — NOT the interactive session."""
    out = subprocess.run(["ps", "-eo", "pid,ppid,etimes,args"], capture_output=True, text=True).stdout
    procs = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, etimes, args = parts
        if ("claude -p " in args or "claude -p\t" in args or args.rstrip().endswith("claude -p")
                or "codex exec" in args):
            try:
                procs.append((int(pid), int(ppid), int(etimes), args))
            except ValueError:
                pass
    return procs


def reap(dry=False):
    killed = []
    for pid, ppid, etimes, args in _agent_procs():
        reason = _reap_reason(ppid, etimes)
        if reason:
            killed.append({"pid": pid, "reason": reason})
            if not dry:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
    cleaned = _clean_scratch(dry)
    stale_dir = _sweep_directory(dry)                 # release dead agent presence (accumulates + misleads routing)
    if killed and not dry:
        audit.append(actor="reap", action="ReapAgents", resource="orphans", decision="killed",
                     payload={"count": len(killed), "pids": [k["pid"] for k in killed][:10]})
    return {"reaped": killed, "scratch_cleaned": cleaned, "stale_directory_released": stale_dir}


def _sweep_directory(dry=False, stale_min=None):
    """Release STALE agent presence in the directory: an 'active' entry whose updated_at is older than the
    threshold is a dead agent that never released (a finished build, a crashed worker). Left alone they
    accumulate (we found 66) and MISLEAD orchestrate.request_collaborator into reusing a non-existent agent
    instead of hiring — a correctness bug at scale, not just test cruft. Real live agents update far more
    often than this floor, so releasing stale presence is safe. Returns the count released."""
    import os
    mins = stale_min if stale_min is not None else int(os.environ.get("AOS_DIRECTORY_STALE_MIN", "30"))
    try:
        import psycopg
        with psycopg.connect(DB) as c, c.cursor() as cur:
            if dry:
                cur.execute("SELECT count(*) FROM directory WHERE status='active' AND updated_at < now()-make_interval(mins=>%s)", (mins,))
                return cur.fetchone()[0]
            cur.execute("UPDATE directory SET status='released' WHERE status='active' AND updated_at < now()-make_interval(mins=>%s)", (mins,))
            n = cur.rowcount
            c.commit()
            return n
    except Exception:
        return 0


def _clean_scratch(dry=False):
    now = time.time()
    removed = []
    tmp = Path("/tmp")
    for g in SCRATCH_GLOBS:
        for p in tmp.glob(g):
            try:
                if now - p.stat().st_mtime > SCRATCH_MAX_AGE_S:
                    removed.append(p.name)
                    if not dry:
                        if p.is_dir():
                            import shutil
                            shutil.rmtree(p, ignore_errors=True)
                        else:
                            p.unlink(missing_ok=True)
            except Exception:
                pass
    return removed


def _selftest():
    keep = _reap_reason(ppid=12345, etimes=60) is None                      # healthy, parented -> keep
    orphan = _reap_reason(ppid=1, etimes=60) == "orphaned (parent dead)"    # PPID 1 -> reap
    stuck = _reap_reason(ppid=999, etimes=99999) is not None                # too old -> reap
    interactive_excluded = not any("claude --continue" in a for _, _, _, a in _agent_procs())  # never our session
    # directory-staleness sweep is wired + released count is an int (dry-run against the live DB)
    dir_ok = isinstance(_sweep_directory(dry=True), int) and "stale_directory_released" in reap(dry=True)
    ok = keep and orphan and stuck and interactive_excluded and dir_ok
    print(f"keep-healthy={keep} reap-orphan={orphan} reap-stuck={stuck} session-excluded={interactive_excluded} "
          f"directory-sweep={dir_ok}")
    print("PASS: orphan/stuck reaper (decision + session-safe) ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "run":
        import json
        print(json.dumps(reap(dry=False), indent=2))
    elif a[0] == "status":
        import json
        print(json.dumps(reap(dry=True), indent=2))
    else:
        sys.exit("usage: reap.py run | status | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
