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
GLOBAL_MAX = int(os.environ.get("AOS_BROWSER_GLOBAL_MAX", "6"))     # total concurrent QA browsers across the box
# a browser session can legitimately run a long story; give it a generous lease before a dead holder is reclaimed
LEASE_S = int(os.environ.get("AOS_BROWSER_LEASE_S", "2400"))
WAIT_S = int(os.environ.get("AOS_BROWSER_WAIT_S", "1200"))         # wait up to this for a free slot, then fail-open


def acquire(holder, wait_s=WAIT_S):
    """Claim a global browser slot (or a lease-expired one). Returns slot_id, or None on fail-open."""
    claude_gate._ensure(TABLE, GLOBAL_MAX)
    return claude_gate.acquire(holder, wait_s=wait_s, table=TABLE, lease_s=LEASE_S)


def release(sid):
    claude_gate.release(sid, table=TABLE)


def status():
    return claude_gate.status(table=TABLE)


def _selftest():
    # prove the pool bounds concurrency: acquire GLOBAL_MAX, the next acquire (tiny wait) fails-open to None.
    got = [acquire(f"selftest-{i}", wait_s=2) for i in range(GLOBAL_MAX)]
    assert all(s is not None for s in got), f"should grant {GLOBAL_MAX} slots, got {got}"
    extra = acquire("selftest-overflow", wait_s=1)          # pool full -> waits briefly -> fail-open None
    over_ok = extra is None
    for s in got:
        release(s)
    # after releasing, a new acquire succeeds again
    again = acquire("selftest-after-release", wait_s=2)
    reuse_ok = again is not None
    if again is not None:
        release(again)
    ok = over_ok and reuse_ok
    print(f"granted={len([s for s in got if s])}/{GLOBAL_MAX} overflow_blocked={over_ok} reuse_after_release={reuse_ok}")
    print("browser_gate selftest: PASS (global browser cap bounds concurrency, reclaims on release) ✅"
          if ok else "browser_gate selftest: FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        import json
        print(json.dumps(status(), indent=2, default=str))
    else:
        _selftest()
