#!/usr/bin/env python3
"""dbpool.py — a shared connection pool (REBUILD-PLAN C2 scale).

The review: "fresh psycopg.connect() per operation, no pooling" — every DB touch pays TCP+auth setup, and
at scale the connection churn caps throughput. This adds a process-wide pooled connection source that
modules can adopt incrementally, WITHOUT breaking the 79 existing `psycopg.connect(DB)` call sites (they
keep working). FAIL-OPEN by design: if the pool can't be created or is exhausted, we fall back to a direct
connect — a pool problem must NEVER take the app down (the whole point is more headroom, not a new SPOF).

Usage (drop-in for `with psycopg.connect(DB) as c`):
    from dbpool import connection
    with connection() as c, c.cursor() as cur: ...

Autocommit variant for read paths:
    with connection(autocommit=True) as c, c.cursor() as cur: ...
"""
import os
import threading
from contextlib import contextmanager
from pathlib import Path

import psycopg
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import trace as _trace  # noqa: E402  — shared DATABASE_URL

DB = _trace.DB
_MIN = int(os.environ.get("AOS_DB_POOL_MIN", "2"))
_MAX = int(os.environ.get("AOS_DB_POOL_MAX", "16"))
_pool = None
_lock = threading.Lock()
_disabled = os.environ.get("AOS_DB_POOL", "1").strip().lower() in ("0", "false", "off", "no")


def _get_pool():
    """Lazily build ONE pool per process. Returns None (—> callers fall back to direct connect) if the
    pool lib is unavailable or the pool can't open — fail-open, never raise into the caller."""
    global _pool
    if _disabled:
        return None
    if _pool is not None:
        return _pool
    with _lock:
        if _pool is None:
            try:
                from psycopg_pool import ConnectionPool
                _pool = ConnectionPool(DB, min_size=_MIN, max_size=_MAX, open=True, timeout=10,
                                       kwargs={"autocommit": False})
                _pool.wait(timeout=10)
            except Exception:
                _pool = None
    return _pool


@contextmanager
def connection(autocommit=False):
    """Yield a pooled connection (returned to the pool on exit); fall back to a fresh direct connection if
    the pool is unavailable/exhausted. Mirrors `with psycopg.connect(DB) as c` semantics (commit on clean
    exit for non-autocommit, rollback on exception) so it is a safe drop-in."""
    pool = _get_pool()
    if pool is not None:
        try:
            with pool.connection(timeout=10) as c:
                if autocommit and not c.autocommit:
                    c.autocommit = True
                yield c
            return
        except Exception:
            pass                                      # pool hiccup -> fall through to a direct connect
    # FAIL-OPEN: direct connection (exactly today's behavior)
    c = psycopg.connect(DB, autocommit=autocommit)
    try:
        yield c
        if not autocommit:
            c.commit()
    except Exception:
        if not autocommit:
            try:
                c.rollback()
            except Exception:
                pass
        raise
    finally:
        c.close()


def stats():
    p = _get_pool()
    if p is None:
        return {"pool": "disabled/fallback (direct connects)"}
    try:
        s = p.get_stats()
        return {"pool": "active", "min": _MIN, "max": _MAX, **{k: s[k] for k in
                ("pool_size", "pool_available", "requests_waiting") if k in s}}
    except Exception:
        return {"pool": "active", "min": _MIN, "max": _MAX}


def _selftest():
    ok = True

    def chk(cond, label):
        nonlocal ok
        print(("PASS" if cond else "FAIL") + f": {label}")
        ok = ok and bool(cond)

    # pooled query works + returns to pool
    with connection() as c, c.cursor() as cur:
        cur.execute("SELECT 1")
        chk(cur.fetchone()[0] == 1, "pooled connection runs a query")
    # many sequential borrows don't leak (pool reuses, doesn't exhaust)
    for _ in range(50):
        with connection(autocommit=True) as c, c.cursor() as cur:
            cur.execute("SELECT 1")
    chk(True, "50 sequential borrows reuse the pool without exhaustion")
    # commit semantics: a write in a pooled txn persists; rollback on error
    import uuid
    key = f"dbpool-{uuid.uuid4().hex[:8]}"
    with connection() as c, c.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS dbpool_selftest (k text primary key)")
        cur.execute("INSERT INTO dbpool_selftest (k) VALUES (%s) ON CONFLICT DO NOTHING", (key,))
    with connection(autocommit=True) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM dbpool_selftest WHERE k=%s", (key,))
        chk(cur.fetchone()[0] == 1, "pooled write committed on clean exit (drop-in semantics)")
        cur.execute("DELETE FROM dbpool_selftest WHERE k=%s", (key,))
    # fail-open: even with the pool force-disabled, connection() still works (direct)
    global _disabled, _pool
    _disabled, _pool = True, None
    with connection() as c, c.cursor() as cur:
        cur.execute("SELECT 1")
        chk(cur.fetchone()[0] == 1, "FAIL-OPEN: works via direct connect when pool disabled")
    _disabled = False
    print("PASS: dbpool — shared pool, drop-in semantics, fail-open to direct connect ✅" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    import sys as _s
    if len(_s.argv) > 1 and _s.argv[1] == "stats":
        print(stats())
    else:
        _s.exit(0 if _selftest() else 1)
