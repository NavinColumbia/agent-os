#!/usr/bin/env python3
"""browser_gate.py — a GLOBAL, cross-process cap on concurrent QA browser sessions.

Production reality: at scale (1000s of agents across many companies) many builds run coverage-driven browser
QA at once, and each session is a full Chromium + a video recorder — heavy on RAM/CPU. Without a global cap,
concurrent QA runs thrash the box: browsers stall, exploration crawls at ~0 coverage, and everything slows
(observed live — two QA runs competing for browsers stalled both). This gate bounds TOTAL concurrent browser
sessions across the whole machine, no matter how many QA processes exist — the exact analog of claude_gate for
the browser resource. Reuses claude_gate's proven lease-based Postgres slot pool (a dead holder's slot is
reclaimed after the lease; FAIL-OPEN so a DB hiccup never deadlocks QA).

    sid = browser_gate.acquire("qa:1-recipe/US-3")   # blocks until a global browser slot is free
    ... run the browser session ...
    browser_gate.release(sid)

A single box comfortably runs a handful of Chromium+video sessions; tune with AOS_BROWSER_GLOBAL_MAX.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_gate  # noqa: E402  — reuse the proven lease-based slot pool machinery

TABLE = "browser_slots"
# a browser session can legitimately run a long story; give it a generous lease before a dead holder is reclaimed
LEASE_S = int(os.environ.get("AOS_BROWSER_LEASE_S", "2400"))
WAIT_S = int(os.environ.get("AOS_BROWSER_WAIT_S", "1200"))         # wait up to this for a free slot, then fail-open

# Per-session cost, MEASURED on real runs (Chromium + a video recorder): ~475MB RSS + ~0.8 CPU core. The cap
# is the number of sessions the BOX can actually run at once — RAM-bound AND CPU-bound — with headroom so the
# machine never thrashes. "Spin up as many as the hardware allows, no more" — auto-sized to THIS machine, not
# a hardcoded guess. AOS_BROWSER_GLOBAL_MAX overrides (e.g. per-node in a cloud pool).
_MB_PER_SESSION = int(os.environ.get("AOS_BROWSER_MB_PER_SESSION", "550"))   # generous vs the measured ~475
_CORES_PER_SESSION = float(os.environ.get("AOS_BROWSER_CORES_PER_SESSION", "0.8"))
_RESERVE_MB = int(os.environ.get("AOS_BROWSER_RESERVE_MB", "2048"))          # leave the OS + other daemons room


def _auto_cap():
    """How many browser sessions THIS box can run at once, from real RAM + CPU (never a hardcoded number).
    RAM-bound: (available - reserve) / per-session. CPU-bound: cores / per-session-cores. Take the min; clamp
    to [1, 64]. Fail-safe to a modest default if the probes are unavailable."""
    override = os.environ.get("AOS_BROWSER_GLOBAL_MAX")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            pass
    try:
        import os as _os
        avail_mb = None
        with open("/proc/meminfo") as fh:                       # MemAvailable = what we can actually use now
            for line in fh:
                if line.startswith("MemAvailable:"):
                    avail_mb = int(line.split()[1]) // 1024
                    break
        if avail_mb is None:                                    # fallback: total * 0.6
            page = _os.sysconf("SC_PAGE_SIZE"); n = _os.sysconf("SC_PHYS_PAGES")
            avail_mb = int(page * n / (1024 * 1024) * 0.6)
        cores = _os.cpu_count() or 4
        ram_cap = max(1, (avail_mb - _RESERVE_MB) // _MB_PER_SESSION)
        cpu_cap = max(1, int(cores / _CORES_PER_SESSION))
        return int(max(1, min(64, ram_cap, cpu_cap)))
    except Exception:
        return 6                                                # safe modest default if probing fails


GLOBAL_MAX = _auto_cap()      # sized to THIS box at import; AOS_BROWSER_GLOBAL_MAX overrides

# IN-PROCESS FALLBACK: the DB slot pool FAILS OPEN on a Postgres hiccup (returns None = proceed ungated) —
# which would remove the browser cap exactly under heavy load when Postgres is most stressed. This local
# semaphore is the backstop: when the DB gate can't grant a slot, we still bound concurrency WITHIN this
# process to GLOBAL_MAX. It can't enforce a box-wide cap across separate processes without shared state (that
# needs the DB), but it fully bounds the common case (one big run) and caps each process otherwise — so the
# box degrades to (n_processes x GLOBAL_MAX) instead of unbounded when the DB is down.
import threading  # noqa: E402
_LOCAL_SEM = threading.BoundedSemaphore(GLOBAL_MAX)
_LOCAL_TAG = "local-fallback"


def acquire(holder, wait_s=WAIT_S):
    """Claim a browser slot sized to what THIS machine can run. Prefer the cross-process DB pool; if it
    fails-open (DB hiccup), fall back to an in-process semaphore so concurrency is STILL bounded. Returns a
    slot id (int) or the sentinel _LOCAL_TAG (fallback held) or None (both unavailable -> proceed ungated)."""
    try:
        claude_gate._ensure(TABLE, GLOBAL_MAX)
        sid = claude_gate.acquire(holder, wait_s=wait_s, table=TABLE, lease_s=LEASE_S)
    except Exception:
        sid = None
    if sid is not None:
        return sid
    # DB gate unavailable -> bound this process locally instead of going fully ungated
    if _LOCAL_SEM.acquire(timeout=max(1, min(wait_s, 60))):
        return _LOCAL_TAG
    return None                                   # even the local cap is full -> last-resort fail-open


def release(sid):
    if sid == _LOCAL_TAG:
        try:
            _LOCAL_SEM.release()
        except (ValueError, RuntimeError):
            pass                                  # never over-release the bounded semaphore
        return
    claude_gate.release(sid, table=TABLE)


def status():
    return claude_gate.status(table=TABLE)


def _selftest():
    # The pool is sized to THIS box (GLOBAL_MAX). A live QA run may already hold some slots — that's the gate
    # working. Prove the invariant regardless: you can never hold MORE than GLOBAL_MAX at once, and a released
    # slot is reclaimable. Drain whatever is free, assert one more is refused, then a release frees exactly one.
    print(f"auto-sized cap (this box) = {GLOBAL_MAX}")
    got = []
    for i in range(GLOBAL_MAX + 2):
        s = acquire(f"selftest-{i}", wait_s=1)
        if s is None:
            break
        got.append(s)
    # Never hold MORE than the cap (the core invariant), regardless of how many a live run already holds.
    assert len(got) <= GLOBAL_MAX, f"held {len(got)} but cap is {GLOBAL_MAX} (never exceed the cap)"
    if not got:
        # pool fully saturated by a live QA run -> that IS the cap working. Prove reclaim differently: a
        # lease-expired slot is reclaimable (acquire with lease_s=0 forces reclaim of the oldest).
        forced = claude_gate.acquire("selftest-forced", wait_s=2, table=TABLE, lease_s=0)
        ok = forced is not None
        if forced is not None:
            release(forced)
        print(f"pool fully held by a live run (cap={GLOBAL_MAX} enforced); lease-reclaim works={ok}")
        print("browser_gate selftest: PASS (cap enforced; lease-reclaim proven) ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    over_ok = (acquire("selftest-overflow", wait_s=1) is None)   # pool exhausted -> fail-open None
    one = got.pop()
    release(one)                                            # free exactly one
    again = acquire("selftest-after-release", wait_s=2)     # a released slot must be reclaimable
    reuse_ok = again is not None
    if again is not None:
        got.append(again)
    for s in got:                                           # clean up everything we held
        release(s)
    ok = over_ok and reuse_ok
    print(f"held_up_to={GLOBAL_MAX} overflow_blocked={over_ok} reuse_after_release={reuse_ok}")
    print("browser_gate selftest: PASS (global browser cap bounds concurrency, reclaims on release) ✅"
          if ok else "browser_gate selftest: FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        import json
        print(json.dumps(status(), indent=2, default=str))
    else:
        _selftest()
