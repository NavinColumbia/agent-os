#!/usr/bin/env python3
"""claude_gate.py — a CROSS-PROCESS concurrency gate for `claude` CLI calls (the deeper F12 fix).

The problem the per-process semaphore couldn't solve: factory's `_AGENT_SEM` caps concurrent agent calls to 8
WITHIN one python process — but agent-os runs many processes at once (build driver, QA run, jobd, the console
server, ad-hoc scripts), each with its OWN semaphore. So the true concurrency against the single Claude
subscription is 8 × (number of processes) — dozens of simultaneous calls, which throttles the subscription and
makes calls slow/hang. There is no shared limiter.

This is that shared limiter: ONE global pool of N slots in Postgres. Every `claude` invocation (factory._run_once)
must hold a slot for its duration, so TOTAL concurrent claude calls across the whole box is capped at N, no
matter how many processes are running. Lease-based: a slot whose holder died (crash / the G1 orphan problem) is
reclaimed after LEASE_S, so a dead process can't permanently hold a slot. FAIL-OPEN: any DB hiccup lets the call
proceed ungated — the gate throttles, it must never deadlock the fleet.

    with claude_gate.slot("build:1234"):     # blocks until a global slot is free (or wait budget elapses)
        subprocess.run(["claude", ...])
    claude_gate.py status | selftest
"""
import contextlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import psycopg  # noqa: E402
import trace as _trace  # noqa: E402

DB = _trace.DB


def _auto_global_max():
    """Total concurrent `claude -p` calls allowed across the box. Env override wins; else AUTO-SIZE — a
    hardcoded 6 throttled the whole fleet (QA fanned out 15 browsers but their per-step model calls queued
    6-wide, so deep QA crawled). A `claude -p` process is an API CLIENT — the heavy compute is server-side, so
    it's cheap locally (a little RAM); the real ceiling is the provider's concurrency, not this box. Size to
    the box generously (cores*3) with a floor of 12 and a sane cap, and let AOS_CLAUDE_GLOBAL_MAX pin it for
    ops (raise for throughput, lower if a plan hits provider rate limits)."""
    env = os.environ.get("AOS_CLAUDE_GLOBAL_MAX")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    # Empirical: a burst of ~10 concurrent calls is fine, but SUSTAINED heavy calls (QA's long browser-
    # exploration prompts, 15 workers for hours) at high concurrency hit the SUBSCRIPTION's rate limit →
    # transient overloads → mass Codex failover → degraded QA. A subscription sustains roughly ~8 heavy
    # concurrent calls. Keep the default conservative (8) so runs stay on Claude and don't thrash into
    # failover; raise AOS_CLAUDE_GLOBAL_MAX only with an API key / higher-tier plan that tolerates more.
    try:
        cores = os.cpu_count() or 4
    except Exception:
        cores = 4
    return max(6, min(8, cores))


GLOBAL_MAX = _auto_global_max()                                    # total concurrent claude calls across the box
LEASE_S = int(os.environ.get("AOS_CLAUDE_LEASE_S", "1200"))        # a slot held longer than this = crashed holder
WAIT_S = int(os.environ.get("AOS_CLAUDE_WAIT_S", "900"))           # how long to wait for a free slot before fail-open


def _ensure(table="claude_slots", n=GLOBAL_MAX):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
            slot_id INT PRIMARY KEY, holder TEXT, acquired_at TIMESTAMPTZ)""")
        # Seed slots 1..n ONLY when the pool is empty. Re-callers (acquire) mustn't grow a pool they don't own —
        # otherwise a caller using the default N would silently expand a pool another caller sized deliberately.
        cur.execute(f"SELECT count(*) FROM {table}")
        if cur.fetchone()[0] == 0:
            cur.execute(f"INSERT INTO {table} (slot_id) SELECT g FROM generate_series(1,%s) g "
                        f"ON CONFLICT (slot_id) DO NOTHING", (n,))
        c.commit()


def acquire(holder, wait_s=WAIT_S, table="claude_slots", lease_s=LEASE_S):
    """Claim a free (or lease-expired) global slot. Returns slot_id, or None if none freed within wait_s
    (caller then proceeds FAIL-OPEN — throttling, not a hard gate). Never raises on a DB hiccup."""
    try:
        _ensure(table)
    except Exception:
        return None
    deadline = time.time() + max(0, wait_s)
    backoff = 0.5
    while True:
        try:
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute(f"""
                    UPDATE {table} SET holder=%s, acquired_at=now()
                    WHERE slot_id = (
                        SELECT slot_id FROM {table}
                        WHERE holder IS NULL OR acquired_at < now() - make_interval(secs => %s)
                        ORDER BY slot_id LIMIT 1 FOR UPDATE SKIP LOCKED)
                    RETURNING slot_id""", (str(holder)[:80], lease_s))
                row = cur.fetchone(); c.commit()
                if row:
                    return row[0]
        except Exception:
            return None                          # DB trouble -> fail-open (proceed ungated)
        if time.time() >= deadline:
            return None                          # waited long enough -> fail-open rather than deadlock
        time.sleep(min(backoff, 5.0)); backoff *= 1.5


def release(slot_id, table="claude_slots"):
    if slot_id is None:
        return
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute(f"UPDATE {table} SET holder=NULL, acquired_at=NULL WHERE slot_id=%s", (slot_id,))
            c.commit()
    except Exception:
        pass                                     # a leaked slot is reclaimed by the lease — never block on release


@contextlib.contextmanager
def slot(holder, wait_s=WAIT_S, table="claude_slots"):
    """Hold a global claude slot for the duration of the block. Fail-open: if no slot can be acquired (busy or
    DB hiccup) it yields None and the call still runs — better a brief over-subscription than a deadlocked fleet."""
    sid = acquire(holder, wait_s=wait_s, table=table)
    try:
        yield sid
    finally:
        release(sid, table=table)


def status(table="claude_slots"):
    _ensure(table)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute(f"SELECT count(*), count(holder), "
                    f"count(*) FILTER (WHERE holder IS NOT NULL AND acquired_at > now()-make_interval(secs=>%s)) "
                    f"FROM {table}", (LEASE_S,))
        total, held, live = cur.fetchone()
    return {"slots": total, "held": held, "live_held": live, "free": total - live}


def _selftest():
    import uuid
    t = f"claude_slots_test_{uuid.uuid4().hex[:8]}"
    try:
        _ensure(table=t, n=2)                     # a tiny 2-slot pool
        a = acquire("h1", wait_s=2, table=t)
        b = acquire("h2", wait_s=2, table=t)
        assert a and b and a != b, f"two distinct slots: {a},{b}"
        c3 = acquire("h3", wait_s=1, table=t)     # pool exhausted -> None within the short wait (fail-open path)
        assert c3 is None, f"3rd acquire on a full 2-pool must time out -> None, got {c3}"
        release(a, table=t)
        d = acquire("h4", wait_s=2, table=t)      # a slot freed -> acquirable again
        assert d == a, f"released slot is reusable: {d} vs {a}"
        # lease reclaim: force b's slot stale -> it becomes acquirable even though 'held'
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute(f"UPDATE {t} SET acquired_at = now() - interval '999 hours' WHERE slot_id=%s", (b,)); c.commit()
        e = acquire("h5", wait_s=2, table=t, lease_s=60)
        assert e == b, f"a lease-expired (crashed-holder) slot is reclaimed: {e} vs {b}"
        release(d, table=t); release(e, table=t)   # free the pool before the ctx-mgr check
        # context manager acquires then releases on exit
        with slot("h6", wait_s=2, table=t) as sid:
            assert sid is not None, "ctx-mgr should acquire a freed slot"
        assert status(table=t)["live_held"] == 0, "ctx-mgr must release on exit"
        st = status(table=t)
        print(f"claude_gate selftest: PASS (global N-slot cap across processes; released + lease-expired slots "
              f"reclaimed; fail-open when full; ctx-mgr releases). final={st}")
        return 0
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {t}"); c.commit()


if __name__ == "__main__":
    import json
    a = sys.argv[1:]
    if not a or a[0] == "selftest":
        sys.exit(_selftest())
    elif a[0] == "status":
        print(json.dumps(status(), indent=2))
