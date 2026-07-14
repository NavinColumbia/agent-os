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
import psycopg  # noqa: E402
import trace as _trace  # noqa: E402  — shared .env.local DATABASE_URL

DB = _trace.DB
SETTLE_S = int(os.environ.get("AOS_JOBD_SETTLE_S", "5"))       # a thread must be runnable this long before we drive it
MAX_DRIVE = int(os.environ.get("AOS_JOBD_MAX_DRIVE", "16"))    # cap threads kicked per tick (fan-out is capped downstream)


def runnable_threads():
    """Threads that are READY to dispatch their next phase: no gate held (awaiting IS NULL), not terminal, and
    SETTLED for a few seconds so we never race the completion path's own advance() during its awaiting=NULL window."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT thread_id FROM controller_state
                       WHERE awaiting IS NULL AND phase <> 'DELIVER'
                         AND updated_at < now() - (%s || ' seconds')::interval
                       ORDER BY updated_at ASC LIMIT %s""", (str(SETTLE_S), MAX_DRIVE))
        return [r[0] for r in cur.fetchall()]


def tick():
    """One central-driver tick. Returns a small summary. Best-effort: a hiccup in one part never blocks the rest."""
    import loopcontroller as lc
    recovered = driven = reaped = 0
    # F12: reap orphaned/hung `claude` agent calls every tick. When a driver dies (G1) its claude child is
    # orphaned and runs forever, holding subscription capacity so new calls throttle+hang. This central,
    # always-on kill of stale headless calls is what stops the pile-up (no more manual killing).
    try:
        import clauded
        reaped = clauded.reap().get("reaped", 0)
        if reaped:
            _log(f"reaped {reaped} hung/orphaned claude agent call(s)")
    except Exception as e:
        _log(f"clauded.reap error: {e}")
    try:
        lc.resume_stalled()
        recovered = 1
    except Exception as e:
        _log(f"resume_stalled error: {e}")
    for tid in runnable_threads():
        try:
            # Single-owner-per-build: the SAME per-thread advisory lock resume_stalled's advances take, so a
            # sweeper and this loop (or two drivers) can never advance one thread at once. (lc owns the key.)
            with lc.thread_drive_lock(tid) as owned:
                if not owned:
                    continue
                lc.advance(int(tid))
                driven += 1
        except Exception as e:
            _log(f"advance({tid}) error: {e}")
    return {"recovered": recovered, "driven": driven, "reaped": reaped}


def _log(msg):
    print(f"[jobd] {msg}", flush=True)


def serve(interval=15):
    import governance
    governance.assert_control_plane()   # fail LOUD at boot if policy enforcement is missing (not mid-build)
    _log(f"central controller execution daemon up (tick={interval}s, settle={SETTLE_S}s)")
    while True:
        try:
            r = tick()
            if r["driven"]:
                _log(f"tick: drove {r['driven']} runnable thread(s)")
        except Exception as e:
            _log(f"tick error: {e}")
        time.sleep(interval)


def _selftest():
    import types
    import loopcontroller as lc

    # 1) runnable_threads() finds a settled, gate-free, non-terminal thread and EXCLUDES gated/terminal ones.
    lc._ensure()
    import uuid
    tids = []
    with psycopg.connect(DB) as c, c.cursor() as cur:
        base = f"jobd-{uuid.uuid4().hex[:6]}"
        rows = [(f"{base}-run", "PROTOTYPE", None),      # runnable (awaiting NULL, not DELIVER)
                (f"{base}-gate", "OPTIONS", "user_approval"),  # gated -> excluded
                (f"{base}-done", "DELIVER", None)]        # terminal -> excluded
        for i, (tk, phase, awaiting) in enumerate(rows):
            cur.execute("""INSERT INTO controller_state (thread_id, tenant_id, org_id, phase, awaiting, updated_at)
                           VALUES (%s,%s,1,%s,%s, now() - interval '30 seconds')
                           ON CONFLICT (thread_id) DO NOTHING""",
                        (900000 + i, tk, phase, awaiting))
            tids.append(900000 + i)
        c.commit()
    try:
        runnable = set(runnable_threads())
        assert 900000 in runnable, "a settled gate-free non-terminal thread must be runnable"
        assert 900001 not in runnable, "a gated thread must NOT be driven"
        assert 900002 not in runnable, "a DELIVER (terminal) thread must NOT be driven"

        # 2) tick() calls resume_stalled AND advance()s each runnable thread — stub both to record calls.
        calls = {"resume": 0, "advance": []}
        _real_resume, _real_advance = lc.resume_stalled, lc.advance
        lc.resume_stalled = lambda: calls.__setitem__("resume", calls["resume"] + 1)
        lc.advance = lambda t, **k: calls["advance"].append(int(t))
        try:
            r = tick()
        finally:
            lc.resume_stalled, lc.advance = _real_resume, _real_advance
        assert calls["resume"] == 1, "tick must run recovery once"
        assert 900000 in calls["advance"], f"tick must drive the runnable thread: {calls['advance']}"
        assert 900001 not in calls["advance"] and 900002 not in calls["advance"], "must not drive gated/terminal"
        assert r["driven"] >= 1, r
        print("jobd selftest: PASS (runnable detection excludes gated/terminal; tick recovers + drives runnable "
              "threads in a long-lived process — the central execution fix)")
        return 0
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_state WHERE thread_id = ANY(%s)", (tids,))
            c.commit()


if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0] if a else "serve"
    if cmd == "selftest":
        sys.exit(_selftest())
    elif cmd == "tick":
        print(tick())
    else:
        serve(int(a[1]) if len(a) > 1 else 15)
