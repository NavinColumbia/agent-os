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
BROWSER_STALE_S = int(os.environ.get("AOS_BROWSER_STALE_MIN", "60")) * 60  # 60 min — far beyond any live QA run
BUILD_ABANDON_H = int(os.environ.get("AOS_BUILD_ABANDON_H", "12"))         # no-outcome build after 12h = dead (sentinel WARNs at 3h)


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


def _reap_browser_decision(ppid, etimes, max_s=BROWSER_STALE_S):
    """Pure decision (unit-tested): reap a playwright browser only if ORPHANED (parent dead) or very OLD.
    A live QA run's browser is young + parented -> kept."""
    return ppid == 1 or etimes > max_s


def _browser_procs():
    """(pid, ppid, etimes) for QA-leaked PROCESSES: playwright browser ROOTS (chrome-headless-shell AND the
    full 'chromium'/'chrome' channels — a leaked run left 42 of the latter that the old headless-only match
    missed) plus the per-session FFMPEG video recorders (each QA session = a Chromium + an ffmpeg; nothing
    reaped ffmpeg, so they leaked forever -> slow OOM). Renderer/gpu children (--type=) are skipped: they die
    with their root. Only ROOTS + recorders are returned; the ORPHANED/very-OLD safety in the caller ensures a
    live QA run is never touched."""
    out = subprocess.run(["ps", "-eo", "pid,ppid,etimes,args"], capture_output=True, text=True).stdout
    procs = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, etimes, args = parts
        low = args.lower()
        is_browser_root = (("chrome-headless-shell" in low or "/chromium" in low or "/chrome " in low
                            or low.endswith("/chrome") or "headless_shell" in low)
                           and "--type=" not in args)
        # playwright records video via an ffmpeg child; a leaked session leaves the recorder writing forever
        is_ffmpeg_recorder = ("ffmpeg" in low and ("agent-os-qa-evidence" in low or "image2pipe" in low
                              or ".webm" in low))
        if is_browser_root or is_ffmpeg_recorder:
            try:
                procs.append((int(pid), int(ppid), int(etimes)))
            except ValueError:
                pass
    return procs


def _sweep_browsers(dry=False):
    """Kill ORPHANED (PPID 1) or very-OLD (> BROWSER_STALE_S) playwright browsers leaked by crashed/finished
    QA explorer runs. CONSERVATIVE by design: a live QA run's browser is young (<~15m) with a live parent, so
    it is never touched — only genuine leaks are reaped. Returns the count killed."""
    killed = []
    for pid, ppid, etimes in _browser_procs():
        if _reap_browser_decision(ppid, etimes):
            killed.append(pid)
            if not dry:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
    return len(killed)


def _sweep_stuck_builds(dry=False):
    """Auto-RESOLVE (not just detect) a build stuck with NO terminal outcome for BUILD_ABANDON_H+ hours: mark
    it ABANDONED (a recoverable terminal state) so the loop can stop, the sentinel stuck-build flag clears,
    and the projects view shows the truth instead of an eternal 'building'. VERY conservative — 12h, no real
    build runs that long, and sentinel already WARN-observes at 3h. Returns the count abandoned. Fail-open."""
    try:
        import psycopg
        with psycopg.connect(DB) as c, c.cursor() as cur:
            # Scoped to BUILD runs (run_id 'build-%', the same key factory's stuck-run query uses). A
            # standalone QA run traces under 'qa-<product>-<ts>'; without this scope a long QA sweep would
            # keep max(ts) fresh on a product whose first build trace is >12h old and get it marked
            # ABANDONED mid-verification. A build genuinely stuck IN its QA stage still traces under
            # 'build-%' and is still caught.
            cur.execute("""SELECT t.product FROM traces t
                           WHERE t.kind='agent' AND t.run_id LIKE 'build-%%'
                           GROUP BY t.product
                           HAVING max(t.ts) > now() - interval '20 minutes'
                              AND min(t.ts) < now() - make_interval(hours => %s)
                              AND NOT EXISTS (SELECT 1 FROM audit_log a
                                              WHERE a.resource=t.product AND a.action='ProductComplete')
                           LIMIT 20""", (BUILD_ABANDON_H,))
            stuck = [r[0] for r in cur.fetchall()]
        if dry or not stuck:
            return len(stuck)
        for prod in stuck:
            audit.append(actor="reap", action="ProductComplete", resource=prod, decision="ABANDONED",
                         payload={"reason": f"stuck build: agents for >{BUILD_ABANDON_H}h with no terminal outcome"})
        return len(stuck)
    except Exception:
        return 0


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
    stale_browsers = _sweep_browsers(dry)             # reap leaked playwright browsers (orphaned/very old)
    stale_runs = _sweep_stale_runs(dry)               # abandon crashed 'running' orchestra runs (inflate counts)
    stuck_builds = _sweep_stuck_builds(dry)           # auto-resolve builds looping with no outcome (>12h)
    if killed and not dry:
        audit.append(actor="reap", action="ReapAgents", resource="orphans", decision="killed",
                     payload={"count": len(killed), "pids": [k["pid"] for k in killed][:10]})
    return {"reaped": killed, "scratch_cleaned": cleaned, "stale_directory_released": stale_dir,
            "stale_browsers_reaped": stale_browsers, "stale_runs_abandoned": stale_runs,
            "stuck_builds_abandoned": stuck_builds}


def _sweep_stale_runs(dry=False):
    """Abandon orchestra runs stuck 'running' after an orchestrator crash (no actor sign-of-life in the
    window) — they inflate the running count + mislead dashboards. Delegates to store.abandon_stale_runs,
    which NEVER touches a run with a recently-active actor. Fail-open; dry-run mutates nothing."""
    if dry:
        return 0
    try:
        sys.path.insert(0, str(SCRIPTS / "orchestra"))
        import store
        return store.abandon_stale_runs()
    except Exception:
        return 0


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
    # browser sweep is wired + conservative: a YOUNG parented browser is kept; an orphan/very-old one is reaped
    browser_wired = isinstance(_sweep_browsers(dry=True), int) and "stale_browsers_reaped" in reap(dry=True)
    browser_safe = _reap_browser_decision(ppid=12345, etimes=60) is False \
        and _reap_browser_decision(ppid=1, etimes=60) is True \
        and _reap_browser_decision(ppid=999, etimes=BROWSER_STALE_S + 1) is True
    # stale-run + stuck-build sweeps are wired into reap() (crashed runs; builds looping w/o outcome)
    runs_wired = "stale_runs_abandoned" in reap(dry=True)
    builds_wired = "stuck_builds_abandoned" in reap(dry=True) and isinstance(_sweep_stuck_builds(dry=True), int)
    ok = (keep and orphan and stuck and interactive_excluded and dir_ok and browser_wired
          and browser_safe and runs_wired and builds_wired)
    print(f"keep-healthy={keep} reap-orphan={orphan} reap-stuck={stuck} session-excluded={interactive_excluded} "
          f"directory-sweep={dir_ok} browser-sweep={browser_wired} browser-safe={browser_safe} "
          f"stale-run-sweep={runs_wired} stuck-build-sweep={builds_wired}")
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
