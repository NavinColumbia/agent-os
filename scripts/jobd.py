#!/usr/bin/env python3
"""jobd.py — the central CONTROLLER EXECUTION DAEMON (fixes root-cause G1; see docs/E2E-FINDINGS-AND-FIXES.md).

The problem it fixes: loopcontroller runs heavy phase work (research fleet, prototype, build, QA) in an
in-process daemon thread of WHOEVER called advance()/say(). That thread dies the instant its caller exits — and
the "recovery" was a scheduler running `loopcontroller.py resume` as a SHORT-LIVED subprocess every 10 min, which
re-dispatches into a process that also immediately exits, killing the thread again. So a build could silently
stall at awaiting='fleet' with nothing driving it.

jobd is the missing piece: ONE long-lived daemon that OWNS driving controller threads. Because it stays alive,
the fleet work it dispatches lives in ITS process and cascades to completion. Coordinators/controllers just leave
a thread in a runnable state (or a completion leaves the next phase ready) and jobd drives it — a central place
that runs the fleet, instead of every caller spawning threads that die. Each tick:

  1. resume_stalled()  — recover any orphaned/stalled job a dead worker left behind (reconcile research against
                         its real run; re-surface failures) — now IN a long-lived process, so re-dispatched work
                         survives instead of dying with a 10-min subprocess.
  2. drive runnable    — any controller thread with awaiting IS NULL and phase != DELIVER (and settled for a few
                         seconds, to avoid the completion-transition race) gets advance()'d, dispatching its next
                         phase here in jobd.

    jobd.py serve [interval_s]     run the daemon (default 15s tick)
    jobd.py tick                   one tick (ops/debug)
    jobd.py selftest               offline
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dbpool import connection  # noqa: E402

SETTLE_S = int(os.environ.get("AOS_JOBD_SETTLE_S", "5"))       # a thread must be runnable this long before we drive it
MAX_DRIVE = int(os.environ.get("AOS_JOBD_MAX_DRIVE", "2"))
MAX_ACTIVE_JOBS = int(os.environ.get("AOS_JOBD_MAX_ACTIVE_JOBS", "2"))


def _dispatch_limit(active_jobs, max_active=MAX_ACTIVE_JOBS, max_drive=MAX_DRIVE):
    """Bound new phase dispatches by both this tick and all work already in flight."""
    return max(0, min(int(max_drive), int(max_active) - int(active_jobs)))


def _scope(value):
    if value not in {"production", "test"}:
        raise ValueError("execution scope must be 'production' or 'test'")
    return value


def runnable_threads(execution_scope="production"):
    """Threads that are READY to dispatch their next phase: no gate held (awaiting IS NULL), not terminal, and
    SETTLED for a few seconds so we never race the completion path's own advance() during its awaiting=NULL window.

    There is deliberately NO maximum age here.  A runnable can wait behind the global active-job capacity cap
    for an arbitrary amount of time; age is not evidence that the durable transition is corrupt, and converting
    an old queue item into a human gate makes ordinary backpressure look like a CEO decision.
    """
    with connection() as c, c.cursor() as cur:
        execution_scope = _scope(execution_scope)
        cur.execute("""SELECT count(*) FROM controller_jobs
                       WHERE status IN ('running','pending') AND execution_scope=%s""",
                    (execution_scope,))
        limit = _dispatch_limit(cur.fetchone()[0])
        if limit <= 0:
            return []
        cur.execute("""SELECT thread_id FROM controller_state
                       WHERE awaiting IS NULL AND phase <> 'DELIVER' AND execution_scope=%s
                         AND updated_at < now() - (%s || ' seconds')::interval
                       ORDER BY updated_at ASC LIMIT %s""",
                    (execution_scope, str(SETTLE_S), limit))
        return [r[0] for r in cur.fetchall()]


def park_stale_runnables():
    """Compatibility shim: age alone must never turn autonomous work into a human gate.

    A durable, ungated phase is the controller's queue record across process/host restarts.  Genuine corruption
    needs an explicit invariant failure plus an owned operator incident; it cannot be inferred from wall time.
    """
    return []


def queued_runnables(limit=50, execution_scope="production"):
    """Observable backlog of settled, ungated controller transitions, oldest first.

    This includes items waiting for a global execution slot.  Keeping the queue visible in every tick result
    makes capacity pressure distinguishable from a human decision without mutating the workstream.
    """
    with connection() as c, c.cursor() as cur:
        execution_scope = _scope(execution_scope)
        cur.execute("""SELECT thread_id FROM controller_state
                       WHERE awaiting IS NULL AND phase <> 'DELIVER' AND execution_scope=%s
                         AND updated_at < now() - (%s || ' seconds')::interval
                       ORDER BY updated_at ASC LIMIT %s""",
                    (execution_scope, str(SETTLE_S), max(1, int(limit))))
        return [r[0] for r in cur.fetchall()]


def tick(execution_scope="production"):
    """One central-driver tick. Returns a small summary. Best-effort: a hiccup in one part never blocks the rest."""
    import loopcontroller as lc
    execution_scope = _scope(execution_scope)
    recovered = driven = reaped = 0
    parked = []
    # F12: reap orphaned/hung `claude` agent calls every tick. When a driver dies (G1) its claude child is
    # orphaned and runs forever, holding subscription capacity so new calls throttle+hang. This central,
    # always-on kill of stale headless calls is what stops the pile-up (no more manual killing).
    if execution_scope == "production":
        try:
            import clauded
            reaped = clauded.reap().get("reaped", 0)
            if reaped:
                _log(f"reaped {reaped} hung/orphaned claude agent call(s)")
        except Exception as e:
            _log(f"clauded.reap error: {e}")
    try:
        lc.resume_stalled(execution_scope=execution_scope)
        recovered = 1
    except Exception as e:
        _log(f"resume_stalled error: {e}")
    for tid in runnable_threads(execution_scope):
        try:
            # Single-owner-per-build: the SAME per-thread advisory lock resume_stalled's advances take, so a
            # sweeper and this loop (or two drivers) can never advance one thread at once. (lc owns the key.)
            with lc.thread_drive_lock(tid) as owned:
                if not owned:
                    continue
                result = lc.advance(int(tid))
                if isinstance(result, dict) and result.get("error"):
                    _log(f"advance({tid}) refused: {result['error']}")
                    continue
                driven += 1
        except Exception as e:
            _log(f"advance({tid}) error: {e}")
    try:
        queued = queued_runnables(execution_scope=execution_scope)
    except Exception as e:
        _log(f"queued_runnables error: {e}")
        queued = []
    return {"recovered": recovered, "driven": driven, "reaped": reaped,
            "parked_stale": parked, "queued_runnable": queued, "queued_count": len(queued)}


def _log(msg):
    print(f"[jobd] {msg}", flush=True)


def serve(interval=15):
    import singleton_exec
    if not singleton_exec.require("jobd"):
        _log("another identity-locked jobd owns the singleton; exiting")
        return
    legacy = singleton_exec.older_matching(singleton_exec._argv(os.getpid()), Path.cwd())
    if legacy:
        _log(f"legacy pre-lock jobd still owns execution (pid {legacy[0]}); exiting until it is replaced")
        return
    import governance
    governance.assert_control_plane()   # fail LOUD at boot if policy enforcement is missing (not mid-build)
    _log(f"central controller execution daemon up (tick={interval}s, settle={SETTLE_S}s, "
         f"max_drive={MAX_DRIVE}, max_active_jobs={MAX_ACTIVE_JOBS})")
    while True:
        try:
            r = tick()
            singleton_exec.mark_ready("jobd", {"tick": "completed"})
            if r["driven"]:
                _log(f"tick: drove {r['driven']} runnable thread(s)")
        except Exception as e:
            _log(f"tick error: {e}")
        time.sleep(interval)


def _selftest():
    import types
    import loopcontroller as lc

    assert _dispatch_limit(0, 2, 2) == 2
    assert _dispatch_limit(1, 2, 2) == 1
    assert _dispatch_limit(2, 2, 2) == 0
    assert _dispatch_limit(99, 2, 2) == 0
    # 1) runnable_threads() finds a settled, gate-free, non-terminal thread and EXCLUDES gated/terminal ones.
    lc._ensure()
    import uuid
    tids = []
    with connection() as c, c.cursor() as cur:
        base = f"jobd-{uuid.uuid4().hex[:6]}"
        rows = [(f"{base}-run", "PROTOTYPE", None),      # runnable (awaiting NULL, not DELIVER)
                (f"{base}-gate", "OPTIONS", "user_approval"),  # gated -> excluded
                (f"{base}-done", "DELIVER", None),        # terminal -> excluded
                (f"{base}-stale", "OPTIONS", None)]        # old ungated -> remains durable + runnable
        for i, (tk, phase, awaiting) in enumerate(rows):
            cur.execute("""INSERT INTO controller_state
                              (thread_id, tenant_id, org_id, phase, awaiting, updated_at, execution_scope)
                           VALUES (%s,%s,1,%s,%s,now()-interval '30 seconds','test')
                           ON CONFLICT (thread_id) DO NOTHING""",
                        (900000 + i, tk, phase, awaiting))
            tids.append(900000 + i)
        cur.execute("UPDATE controller_state SET updated_at=now()-interval '1 day' WHERE thread_id=900003")
    try:
        runnable = set(runnable_threads("test"))
        assert 900000 in runnable, "a settled gate-free non-terminal thread must be runnable"
        assert 900001 not in runnable, "a gated thread must NOT be driven"
        assert 900002 not in runnable, "a DELIVER (terminal) thread must NOT be driven"
        assert 900003 in runnable, "an old ungated thread must remain runnable across restarts/backpressure"

        # 2) tick() calls resume_stalled AND advance()s each runnable thread — stub both to record calls.
        calls = {"resume": 0, "advance": []}
        _real_resume, _real_advance = lc.resume_stalled, lc.advance
        lc.resume_stalled = lambda **_k: calls.__setitem__("resume", calls["resume"] + 1)
        lc.advance = lambda t, **k: calls["advance"].append(int(t))
        try:
            r = tick("test")
        finally:
            lc.resume_stalled, lc.advance = _real_resume, _real_advance
        assert calls["resume"] == 1, "tick must run recovery once"
        assert 900000 in calls["advance"], f"tick must drive the runnable thread: {calls['advance']}"
        assert 900001 not in calls["advance"] and 900002 not in calls["advance"], "must not drive gated/terminal"
        assert 900003 in calls["advance"], "must eventually drive old durable work, not turn it into a CEO gate"
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT awaiting FROM controller_state WHERE thread_id=900003")
            assert cur.fetchone()[0] is None
        assert r["driven"] >= 1, r
        print("jobd selftest: PASS (runnable detection excludes gated/terminal; tick recovers + drives runnable "
              "threads in a long-lived process — the central execution fix)")
        return 0
    finally:
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_state WHERE thread_id = ANY(%s)", (tids,))


def _main(argv=None):
    """CLI dispatcher. Unknown diagnostic words must fail closed, never start a daemon by accident."""
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a[0] if a else "serve"
    if cmd == "selftest":
        return _selftest()
    if cmd == "tick":
        print(tick())
        return 0
    if cmd == "serve":
        serve(int(a[1]) if len(a) > 1 else 15)
        return 0
    print("usage: jobd.py [serve [interval_s] | tick | selftest]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main())
