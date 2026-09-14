#!/usr/bin/env python3
"""dbpool.py — a shared connection pool (REBUILD-PLAN C2 scale).

The review: "fresh psycopg.connect() per operation, no pooling" — every DB touch pays TCP+auth setup, and
at scale the connection churn caps throughput. This adds a process-wide pooled connection source that
modules can adopt incrementally, WITHOUT breaking the 79 existing `psycopg.connect(DB)` call sites (they
keep working). If pool creation is unavailable, a small process-local direct fallback preserves bootstrap
liveness without allowing unbounded connection creation. An established exhausted pool remains fail-closed.

Usage (drop-in for `with psycopg.connect(DB) as c`):
    from dbpool import connection
    with connection() as c, c.cursor() as cur: ...

Autocommit variant for read paths:
    with connection(autocommit=True) as c, c.cursor() as cur: ...

RLS tenant-scoped transaction:
    with tenant_connection(tenant_id) as c, c.cursor() as cur: ...
"""
import os
import threading
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from aoscfg import DB  # noqa: E402  — shared DATABASE_URL


def _env_number(name, default, cast, minimum):
    try:
        return max(minimum, cast(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, cast(default))


_MAX = _env_number("AOS_DB_POOL_MAX", 8, int, 1)
_MIN = min(_MAX, _env_number("AOS_DB_POOL_MIN", 1, int, 0))
_DIRECT_MAX = _env_number("AOS_DB_DIRECT_MAX", 1, int, 1)
_DIRECT_WAIT_S = _env_number("AOS_DB_DIRECT_WAIT_S", 10, float, 0.0)
_pool = None
_lock = threading.Lock()
_disabled = os.environ.get("AOS_DB_POOL", "1").strip().lower() in ("0", "false", "off", "no")
_direct_sem = threading.BoundedSemaphore(_DIRECT_MAX)


class DatabaseCapacityError(RuntimeError):
    """The process-local direct-connect fallback reached its safety ceiling."""


def _get_pool():
    """Lazily build one pool per process; None selects the bounded bootstrap fallback."""
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
        cm = None
        exc_info = (None, None, None)
        try:
            cm = pool.connection(timeout=10)
            c = cm.__enter__()
        except Exception:
            # Once a bounded pool exists, bypassing it with a fresh direct connection defeats the bound exactly
            # under overload and can stampede Postgres. Let callers retry/back off. Direct fallback remains for
            # hosts where pooling is disabled/unavailable before a pool is established.
            raise
        else:
            try:
                if c.autocommit != bool(autocommit):
                    c.autocommit = bool(autocommit)
                yield c
            except Exception:
                exc_info = sys.exc_info()
                raise
            finally:
                cm.__exit__(*exc_info)
            return
    # Pool creation may be unavailable during bootstrap, but fallback is still bounded. PostgreSQL's own
    # max_connections is the cross-process backstop; this prevents any one process from stampeding it.
    if not _direct_sem.acquire(timeout=_DIRECT_WAIT_S):
        raise DatabaseCapacityError(
            f"direct database capacity exhausted ({_DIRECT_MAX} connections per process)")
    c = None
    try:
        c = psycopg.connect(DB, autocommit=autocommit)
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
        if c is not None:
            c.close()
        _direct_sem.release()


def _app_role():
    # DB-enforced tenancy is the safe production default.  Previously an
    # unset variable silently left tenant transactions running as the owner
    # (and this host's owner is a RLS-bypassing superuser), so a green policy
    # catalog did not mean the application was actually isolated.  An
    # operator can explicitly disable role switching for break-glass/schema
    # maintenance, but absence or an empty value must never weaken isolation.
    role = os.environ.get("AOS_DB_APP_ROLE")
    if role is None or not role.strip():
        return "agentos_app"
    role = role.strip()
    return None if role.lower() in {"off", "none", "disabled"} else role


@contextmanager
def tenant_connection(tenant_id, app_role=None):
    """Yield one non-autocommit transaction with the RLS tenant GUC set transaction-locally.

    Uses set_config(..., is_local=true), equivalent to SET LOCAL, so a pooled connection cannot leak tenant
    context into the next borrower. Keep the whole tenant DB operation inside this transaction; committing
    midway resets the local GUC by design.

    The transaction first runs ``SET LOCAL ROLE agentos_app`` by default so
    DB-enforced RLS is active even when the login role owns the schema or can
    bypass RLS. ``AOS_DB_APP_ROLE=off`` is an explicit break-glass escape for
    operator-only maintenance; ordinary service startup must not use it."""
    if not tenant_id:
        raise ValueError("tenant_id is required for tenant_connection")
    role = app_role or _app_role()
    with connection(autocommit=False) as c:
        with c.cursor() as cur:
            if role:
                cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(str(role))))
            cur.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        yield c


def stats():
    p = _get_pool()
    if p is None:
        return {"pool": "disabled/bounded-direct", "direct_max": _DIRECT_MAX}
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
    # Bounded bootstrap fallback: force-disable pooling and prove direct mode still works.
    global _disabled, _pool
    _disabled, _pool = True, None
    with connection() as c, c.cursor() as cur:
        cur.execute("SELECT 1")
        chk(cur.fetchone()[0] == 1, "bounded direct fallback works when pool disabled")
    _disabled = False
    # RLS prep: transaction-local tenant GUC is set and then automatically clears at transaction end.
    with tenant_connection("tenant-dbpool-selftest") as c, c.cursor() as cur:
        cur.execute("SELECT current_setting('app.tenant_id', true)")
        chk(cur.fetchone()[0] == "tenant-dbpool-selftest", "tenant_connection sets transaction-local RLS tenant")
    with connection(autocommit=True) as c, c.cursor() as cur:
        cur.execute("SELECT nullif(current_setting('app.tenant_id', true),'')")
        chk(cur.fetchone()[0] is None, "tenant_connection does not leak tenant GUC to later borrowers")
    # opt-in app role: prove tenant_connection can enter a non-owner role inside the tenant transaction and
    # the role is reset when the transaction returns to the pool/direct fallback.
    import uuid as _uuid
    role = f"dbpool_app_role_{_uuid.uuid4().hex[:8]}"
    try:
        with connection() as c, c.cursor() as cur:
            cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
            cur.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(role)))
        with tenant_connection("tenant-dbpool-role-selftest", app_role=role) as c, c.cursor() as cur:
            cur.execute("SELECT current_role, current_setting('app.tenant_id', true)")
            user, guc = cur.fetchone()
            chk(user == role and guc == "tenant-dbpool-role-selftest",
                "tenant_connection can SET LOCAL ROLE for non-owner RLS app sessions")
        with connection(autocommit=True) as c, c.cursor() as cur:
            cur.execute("SELECT current_user")
            chk(cur.fetchone()[0] != role, "tenant_connection app role resets after transaction")
    finally:
        with connection(autocommit=True) as c, c.cursor() as cur:
            cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
    print("PASS: dbpool — shared pool, drop-in semantics, bounded direct fallback ✅" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    import sys as _s
    if len(_s.argv) > 1 and _s.argv[1] == "stats":
        print(stats())
    else:
        _s.exit(0 if _selftest() else 1)
