#!/usr/bin/env python3
"""store.py — the ONLY persistence layer of the durable agent-org (REBUILD-PLAN A1).

The arch-review verdict this fixes: "the actual AI org engine sits in a demo folder with zero
production callers" and "agents are stateless subprocess invocations with no identity, memory,
or tenure". This module gives the orchestra its DURABLE substrate: every actor is a Postgres row
(identity, tenure, assignment, memory, result), the org chart is the supervisor_id tree, every
inter-actor event is a persisted, SKIP-LOCKED-claimable row, and a run (a tenant's vision) has a
crash-surviving lifecycle. The in-memory reactor in bus.py/actor.py/supervisor.py keeps its
semantics — this is where its state LIVES so a process death loses nothing.

Schema: postgres/initdb/50-orchestra.sql (single source of truth; ensure() applies it
idempotently at runtime, exactly like the other modules' _ensure pattern).

API (all reads/writes tenant-scoped; every mutation is one transaction — crash-safe):
  runs     start_run(tenant_id, vision, org_id=None) / run(run_id) / finish_run(run_id, status, result)
  actors   spawn_actor(run_id, tenant_id, name, role, kind, supervisor_id, assignment, memory)
           update_actor(actor_id, status=/assignment=/memory=/result=/supervisor_id=)   # memory MERGES
           actor(actor_id) / actors(run_id) / heartbeat(actor_id) -> fresh last_active
  org tree org_tree(run_id) -> the NESTED tree built from supervisor_id links (roots -> "reports")
  events   emit(run_id, tenant_id, frm, to_actor, kind, payload, corr_id)
           claim_events(actor_id, ...)   # FOR UPDATE SKIP LOCKED, lease-reclaimable on crash
           complete_event(event_id) / events(run_id, corr_id=None)

Gates: hiring (spawn_actor), new runs and new events refuse while killswitch says HALTED — the
factory kill-switch reaches every durable-org mutation that creates new work.

    scripts/orchestra/store.py selftest      # OFFLINE (no LLM), REAL local Postgres, cleans up after itself
    scripts/orchestra/store.py tree <run_id>
Run with the agent-os venv python. Data/logic module only — binds no server.
"""
from __future__ import annotations

import json
import os
import random
import secrets
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent.parent          # scripts/
REPO = SCRIPTS.parent                                     # agent-os/
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bus as _bus            # noqa: E402  — reuse the ONE event vocabulary (bus.KINDS)
from dbpool import connection, tenant_connection  # noqa: E402

try:                          # the factory kill-switch gates creation of new durable work
    import killswitch         # noqa: E402
except Exception:             # pragma: no cover — store must not brick if gates are absent
    killswitch = None

from aoscfg import ENV, DB

MIGRATION = REPO / "postgres" / "initdb" / "50-orchestra.sql"

KINDS = set(_bus.KINDS)                                        # task|done|next|blocked|finding|...
ACTOR_KINDS = ("worker", "supervisor", "controller")
ACTOR_STATUSES = ("idle", "working", "blocked", "parked", "done", "dead")
RUN_END_STATUSES = ("done", "failed", "halted")
CLAIM_LEASE_S = 900          # a claim whose holder died is reclaimable after this many seconds
# The heartbeat runs at one third of this interval. Thirty seconds gives every live worker two missed-heartbeat
# opportunities while bounding crash recovery to 30 seconds instead of making a replacement controller sit
# idle for 90 seconds after a dead browser/fixer process. Operators can still raise it for unusually latent DBs.
TOOL_LEASE_S = max(30, int(os.environ.get("AOS_TOOL_JOB_LEASE_S", "30")))

_ensured = False
_ensure_lock = threading.Lock()


def _conn(tenant_id=None):
    return tenant_connection(tenant_id) if tenant_id else connection()


def _positive_env_ms(name, default):
    """A safe transaction timeout even when an operator supplied a bad value."""
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return int(default)


def _set_step_timeouts(cur):
    """Bound hot orchestra transactions without leaking settings through the connection pool.

    ``set_config(..., true)`` is PostgreSQL's parameter-safe equivalent of ``SET LOCAL``. Both values reset
    when the transaction ends, including on rollback, so one tenant's tuning never contaminates another pool
    borrower.
    """
    lock_ms = _positive_env_ms("AOS_ORCHESTRA_DB_LOCK_TIMEOUT_MS", 500)
    statement_ms = _positive_env_ms("AOS_ORCHESTRA_DB_STATEMENT_TIMEOUT_MS", 2000)
    cur.execute("SELECT set_config('lock_timeout', %s, true)", (f"{lock_ms}ms",))
    cur.execute("SELECT set_config('statement_timeout', %s, true)", (f"{statement_ms}ms",))


_STEP_TRANSIENT_ERRORS = (
    psycopg.errors.DeadlockDetected,
    psycopg.errors.SerializationFailure,
    psycopg.errors.LockNotAvailable,
    psycopg.errors.QueryCanceled,
)


def _step_attempts():
    try:
        return min(8, max(1, int(os.environ.get("AOS_ORCHESTRA_DB_RETRIES", "3"))))
    except (TypeError, ValueError):
        return 3


def _retry_pause(attempt):
    time.sleep((0.02 * (2 ** attempt)) + random.random() * 0.02)


# ---------------------------------------------------------------------------- schema + helpers
def ensure():
    """Apply the migration file idempotently (CREATE ... IF NOT EXISTS throughout). The SQL file
    is the single source of truth — no DDL is duplicated here, so schema can never drift."""
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        with _conn() as c, c.cursor() as cur:
            cur.execute(MIGRATION.read_text())
            c.commit()
        _ensured = True


def _halted():
    """The factory kill-switch, best-effort: is the orchestra (or everything) halted?"""
    if killswitch is None:
        return None
    try:
        h = killswitch.is_halted("orchestra")
        return h if h.get("halted") else None
    except Exception:
        return None


def _iso(ts):
    return ts.isoformat() if ts is not None else None


_RUN_COLS = "run_id, tenant_id, org_id, vision, status, result, created_at, finished_at"


def _run_dict(r):
    return {"run_id": r[0], "tenant_id": r[1], "org_id": r[2], "vision": r[3], "status": r[4],
            "result": r[5], "created_at": _iso(r[6]), "finished_at": _iso(r[7])}


_ACTOR_COLS = ("actor_id, run_id, tenant_id, org_id, name, role, kind, supervisor_id, status, "
               "assignment, memory, result, hired_at, last_active, hire_key")


def _actor_dict(r):
    return {"actor_id": r[0], "run_id": r[1], "tenant_id": r[2], "org_id": r[3], "name": r[4],
            "role": r[5], "kind": r[6], "supervisor_id": r[7], "status": r[8], "assignment": r[9],
            "memory": r[10], "result": r[11], "hired_at": _iso(r[12]), "last_active": _iso(r[13]),
            "hire_key": r[14]}


_EVENT_COLS = ("id, run_id, tenant_id, frm, to_actor, kind, payload, corr_id, ts, "
               "claimed_at, claimed_by, processed_at")


def _event_dict(r):
    return {"id": r[0], "run_id": r[1], "tenant_id": r[2], "frm": r[3], "to_actor": r[4],
            "kind": r[5], "payload": r[6], "corr_id": r[7], "ts": _iso(r[8]),
            "claimed_at": _iso(r[9]), "claimed_by": r[10], "processed_at": _iso(r[11])}


# ---------------------------------------------------------------------------- run lifecycle
def start_run(tenant_id, vision, org_id=None):
    """Open a run: one tenant vision entering the org. Refused while the kill-switch is down."""
    ensure()
    h = _halted()
    if h:
        return {"error": f"halted: {h.get('reason') or 'kill-switch engaged'}"}
    if not tenant_id or not vision:
        return {"error": "tenant_id and vision are required"}
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute(f"""INSERT INTO orchestra_runs (tenant_id, org_id, vision)
                        VALUES (%s,%s,%s) RETURNING {_RUN_COLS}""", (tenant_id, org_id, vision))
        row = cur.fetchone(); c.commit()
    return _run_dict(row)


def abandon_stale_runs(stale_h=None):
    """Resilience sweep: a run whose orchestrator crashed stays status='running' FOREVER — inflating the
    'running' count and misleading dashboards/routing (found 16 such, all >2h old). Mark a running run
    'abandoned' when it is older than the threshold AND no actor within it has shown a sign of life
    (last_active) inside the window. A LIVE build — any actor active recently (e.g. an in-flight prodr
    build) — is NEVER touched, so this is safe to run on a cadence. Returns the count. Fail-open."""
    ensure()
    import os
    secs = (stale_h if stale_h is not None else float(os.environ.get("AOS_RUN_STALE_H", "2"))) * 3600
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""UPDATE orchestra_runs r SET status='abandoned', finished_at=now()
                           WHERE r.status='running'
                             AND r.created_at < now() - make_interval(secs => %s)
                             AND NOT EXISTS (SELECT 1 FROM orchestra_actors a
                                             WHERE a.run_id = r.run_id
                                               AND a.last_active > now() - make_interval(secs => %s))""",
                        (secs, secs))
            n = cur.rowcount
            c.commit()
            return n
    except Exception:
        return 0


def run(run_id, tenant_id=None):
    """Fetch one run (tenant-scoped when tenant_id given). None if absent/not yours."""
    ensure()
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute(f"""SELECT {_RUN_COLS} FROM orchestra_runs
                        WHERE run_id=%s AND (%s::text IS NULL OR tenant_id=%s)""",
                    (run_id, tenant_id, tenant_id))
        row = cur.fetchone()
    return _run_dict(row) if row else None


def finish_run(run_id, status="done", result=None, tenant_id=None):
    """Terminal transition for a run (done|failed|halted) + finished_at stamp."""
    ensure()
    if status not in RUN_END_STATUSES:
        return {"error": f"status must be one of {RUN_END_STATUSES}"}
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute(f"""UPDATE orchestra_runs SET status=%s, result=%s, finished_at=now()
                        WHERE run_id=%s AND (%s::text IS NULL OR tenant_id=%s)
                        RETURNING {_RUN_COLS}""",
                    (status, json.dumps(result) if result is not None else None,
                     run_id, tenant_id, tenant_id))
        row = cur.fetchone(); c.commit()
    return _run_dict(row) if row else {"error": "no such run"}


def resume_run(run_id, tenant_id=None):
    """Re-open an explicitly halted run so its durable blocked actors can resume from checkpoints."""
    ensure()
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute(f"""UPDATE orchestra_runs SET status='running', result=NULL, finished_at=NULL
                        WHERE run_id=%s AND status='halted'
                          AND (%s::text IS NULL OR tenant_id=%s)
                        RETURNING {_RUN_COLS}""", (run_id, tenant_id, tenant_id))
        row = cur.fetchone(); c.commit()
    return _run_dict(row) if row else {"error": "no resumable halted run"}


# ---------------------------------------------------------------------------- actors (the org)
def spawn_actor(run_id, tenant_id, name, role, kind="worker", supervisor_id=None,
                assignment=None, memory=None, org_id=None, hire_key=None):
    """HIRE an AI employee into a run's org: a durable row with identity (name/role/kind), a
    place in the tree (supervisor_id), tenure (hired_at) and its own memory. The supervisor must
    be a live actor of the SAME run+tenant — the tree cannot cross tenants. Kill-switch-gated."""
    ensure()
    h = _halted()
    if h:
        return {"error": f"halted: {h.get('reason') or 'kill-switch engaged'}"}
    if kind not in ACTOR_KINDS:
        return {"error": f"kind must be one of {ACTOR_KINDS}"}
    hire_key = str(hire_key).strip() if hire_key is not None else None
    if hire_key == "":
        return {"error": "hire_key cannot be empty"}
    with _conn(tenant_id) as c, c.cursor() as cur:
        if hire_key:
            lock_key = f"orchestra-hire:{tenant_id}:{run_id}:{hire_key}"
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))
            cur.execute(f"""SELECT {_ACTOR_COLS} FROM orchestra_actors
                            WHERE run_id=%s AND tenant_id=%s AND hire_key=%s""",
                        (run_id, tenant_id, hire_key))
            existing = cur.fetchone()
            if existing:
                c.commit()
                return _actor_dict(existing)
        cur.execute("SELECT status FROM orchestra_runs WHERE run_id=%s AND tenant_id=%s",
                    (run_id, tenant_id))
        r = cur.fetchone()
        if not r:
            return {"error": "no such run for this tenant"}
        if r[0] != "running":
            return {"error": f"run is {r[0]}, not running"}
        if supervisor_id is not None:
            cur.execute("""SELECT 1 FROM orchestra_actors
                           WHERE actor_id=%s AND run_id=%s AND tenant_id=%s AND status <> 'dead'""",
                        (supervisor_id, run_id, tenant_id))
            if not cur.fetchone():
                return {"error": "supervisor_id is not a live actor of this run/tenant"}
        cur.execute(f"""INSERT INTO orchestra_actors
                        (run_id, tenant_id, org_id, name, role, kind, supervisor_id, assignment, memory,hire_key)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_ACTOR_COLS}""",
                    (run_id, tenant_id, org_id, name, role, kind, supervisor_id, assignment,
                     json.dumps(memory or {}), hire_key))
        row = cur.fetchone(); c.commit()
    return _actor_dict(row)


def spawn_actor_once(run_id, tenant_id, hire_key, name, role, kind="worker",
                     supervisor_id=None, assignment=None, memory=None, org_id=None):
    """Return the same durable employee when a decide-step is replayed."""
    if not str(hire_key or "").strip():
        return {"error": "hire_key is required"}
    return spawn_actor(run_id, tenant_id, name, role, kind=kind,
                       supervisor_id=supervisor_id, assignment=assignment,
                       memory=memory, org_id=org_id, hire_key=hire_key)


_UNSET = object()


def update_actor(actor_id, tenant_id=None, status=_UNSET, assignment=_UNSET, memory=_UNSET,
                 result=_UNSET, supervisor_id=_UNSET):
    """Mutate an actor in ONE transaction. memory is a shallow JSONB MERGE (it accumulates —
    an employee's memory is never wholesale overwritten); result/assignment/supervisor replace;
    status is validated. Always bumps last_active (any update is a sign of life)."""
    ensure()
    sets, args = ["last_active=now()"], []
    if status is not _UNSET:
        if status not in ACTOR_STATUSES:
            return {"error": f"status must be one of {ACTOR_STATUSES}"}
        sets.append("status=%s"); args.append(status)
    if assignment is not _UNSET:
        sets.append("assignment=%s"); args.append(assignment)
    if memory is not _UNSET:
        sets.append("memory = COALESCE(memory,'{}'::jsonb) || %s::jsonb")
        args.append(json.dumps(memory or {}))
    if result is not _UNSET:
        sets.append("result=%s"); args.append(json.dumps(result) if result is not None else None)
    if supervisor_id is not _UNSET:
        sets.append("supervisor_id=%s"); args.append(supervisor_id)
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute(f"""UPDATE orchestra_actors SET {', '.join(sets)}
                        WHERE actor_id=%s AND (%s::text IS NULL OR tenant_id=%s)
                        RETURNING {_ACTOR_COLS}""", (*args, actor_id, tenant_id, tenant_id))
        row = cur.fetchone(); c.commit()
    return _actor_dict(row) if row else {"error": "no such actor"}


def actor(actor_id, tenant_id=None):
    """Fetch one actor (tenant-scoped when tenant_id given). None if absent/not yours."""
    ensure()
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute(f"""SELECT {_ACTOR_COLS} FROM orchestra_actors
                        WHERE actor_id=%s AND (%s::text IS NULL OR tenant_id=%s)""",
                    (actor_id, tenant_id, tenant_id))
        row = cur.fetchone()
    return _actor_dict(row) if row else None


def actors(run_id, tenant_id=None):
    """All actors of a run, hire order."""
    ensure()
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute(f"""SELECT {_ACTOR_COLS} FROM orchestra_actors
                        WHERE run_id=%s AND (%s::text IS NULL OR tenant_id=%s)
                        ORDER BY actor_id""", (run_id, tenant_id, tenant_id))
        rows = cur.fetchall()
    return [_actor_dict(r) for r in rows]


def actors_with_pending_events(run_id, tenant_id=None):
    """Return only actors whose durable inbox contains unprocessed work.

    The runtime used to load every historical actor in a long-lived run, then acquire/release an actor-step
    lease and query each empty inbox. After ~160 completed QA actors, delivery of one finished tool result
    could spend more than a minute cycling through irrelevant rows. This single indexed join keeps dispatch
    proportional to runnable actors while still including terminal recipients so stale mail can be drained.
    """
    ensure()
    qualified = ", ".join(f"a.{column.strip()}" for column in _ACTOR_COLS.split(","))
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute(f"""SELECT {qualified} FROM orchestra_actors a
                        WHERE a.run_id=%s AND (%s::text IS NULL OR a.tenant_id=%s)
                          AND EXISTS (
                              SELECT 1 FROM orchestra_events e
                              WHERE e.to_actor=a.actor_id AND e.run_id=a.run_id
                                AND e.tenant_id=a.tenant_id AND e.processed_at IS NULL)
                        ORDER BY a.actor_id""", (run_id, tenant_id, tenant_id))
        rows = cur.fetchall()
    return [_actor_dict(r) for r in rows]


def heartbeat(actor_id, tenant_id=None):
    """Sign of life: stamp last_active=now(). Returns the fresh last_active (or an error).
    This is what liveness watchdogs read — a stale last_active means a silently-dead employee."""
    ensure()
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""UPDATE orchestra_actors SET last_active=now()
                       WHERE actor_id=%s AND (%s::text IS NULL OR tenant_id=%s)
                       RETURNING last_active""", (actor_id, tenant_id, tenant_id))
        row = cur.fetchone(); c.commit()
    return {"actor_id": actor_id, "last_active": _iso(row[0])} if row else {"error": "no such actor"}


def claim_actor_step(actor_id, tenant_id=None, claimed_by=None, lease_s=CLAIM_LEASE_S):
    """Cross-process single-flight for an actor's decide-step. Event SKIP LOCKED prevents duplicate
    delivery of one event, but an actor can have multiple different inbox events; without this lease two
    runtime processes can update the same actor memory concurrently. Returns True only when this caller owns
    the actor's step lease. A crashed owner is reclaimable after lease_s."""
    ensure()
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""UPDATE orchestra_actors
                          SET step_claimed_at=now(), step_claimed_by=%s, last_active=now()
                        WHERE actor_id=%s
                          AND (%s::text IS NULL OR tenant_id=%s)
                          AND (step_claimed_at IS NULL
                               OR step_claimed_at < now() - make_interval(secs => %s))
                        RETURNING actor_id""",
                    (claimed_by or f"actor:{actor_id}", actor_id, tenant_id, tenant_id, lease_s))
        ok = cur.fetchone() is not None
        c.commit()
    return bool(ok)


def release_actor_step(actor_id, tenant_id=None, claimed_by=None):
    """Release an actor step lease after the decide-step commits. If claimed_by is provided, only the owner
    can release it. Idempotent so cleanup paths can call it freely."""
    ensure()
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""UPDATE orchestra_actors
                          SET step_claimed_at=NULL, step_claimed_by=NULL
                        WHERE actor_id=%s
                          AND (%s::text IS NULL OR tenant_id=%s)
                          AND (%s::text IS NULL OR step_claimed_by=%s)
                        RETURNING actor_id""",
                    (actor_id, tenant_id, tenant_id, claimed_by, claimed_by))
        released = cur.fetchone() is not None
        c.commit()
    return {"actor_id": actor_id, "released": released}


def org_tree(run_id, tenant_id=None):
    """THE ORG CHART: the run's actors as a NESTED tree built from supervisor_id links.
    Roots are actors with no supervisor (normally the controller); every node carries its
    full identity row plus a 'reports' list of its direct reports (recursively nested),
    and computed TENURE fields for rendering (tenure_s since hire, last_active_age_s for
    liveness). An actor whose supervisor row is missing is surfaced as a root, never dropped."""
    from datetime import datetime, timezone
    ensure()
    rows = actors(run_id, tenant_id)
    now = datetime.now(timezone.utc)
    by_id = {}
    for a in rows:
        a = dict(a)
        a["reports"] = []
        try:
            a["tenure_s"] = max(0, int((now - datetime.fromisoformat(a["hired_at"])).total_seconds()))
            a["last_active_age_s"] = max(0, int((now - datetime.fromisoformat(a["last_active"])).total_seconds()))
        except Exception:
            a["tenure_s"] = a["last_active_age_s"] = None
        by_id[a["actor_id"]] = a
    roots = []
    for a in by_id.values():
        sup = a["supervisor_id"]
        if sup is not None and sup in by_id:
            by_id[sup]["reports"].append(a)
        else:
            roots.append(a)
    return {"run_id": run_id, "actors": len(rows), "tree": roots}


def runs_for(tenant_id, limit=5):
    """A tenant's most recent org runs, newest first — the orgview seam: each of these runs'
    org_tree() is REAL spawn data (hired agents with identity/status/tenure), not a static chart."""
    ensure()
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute(f"""SELECT {_RUN_COLS} FROM orchestra_runs WHERE tenant_id=%s
                        ORDER BY run_id DESC LIMIT %s""", (tenant_id, int(limit)))
        rows = cur.fetchall()
    return [_run_dict(r) for r in rows]


def stale_working(stale_min=10):
    """LIVENESS SWEEP (the sentinel seam): actors of still-RUNNING runs that claim to be
    'working' but whose heartbeat (last_active) went silent for > stale_min minutes — the
    signature of a silently-dead agent holding an assignment. Every live code path beats
    heartbeat()/update_actor() while it works, so silence here is itself the failure signal."""
    ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT a.actor_id, a.run_id, a.tenant_id, a.name, a.role,
                              EXTRACT(EPOCH FROM (now()-a.last_active))/60.0
                       FROM orchestra_actors a JOIN orchestra_runs r ON r.run_id=a.run_id
                       WHERE a.status='working' AND r.status='running'
                         AND a.last_active < now() - make_interval(mins => %s)
                       ORDER BY a.last_active""", (int(stale_min),))
        rows = cur.fetchall()
    return [{"actor_id": i, "run_id": rn, "tenant_id": t, "name": n, "role": ro,
             "stale_min": round(float(m), 1)} for i, rn, t, n, ro, m in rows]


def stale_step_claims(stale_min=15):
    """Actors with a step lease older than the expected lease window. This is the monitoring signal for a
    crashed/hung runtime worker holding an actor single-flight claim; reclaim still happens in claim_actor_step
    by lease age, but the sentinel can alert before a run looks mysteriously idle."""
    ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT a.actor_id, a.run_id, a.tenant_id, a.name, a.role, a.step_claimed_by,
                              EXTRACT(EPOCH FROM (now()-a.step_claimed_at))/60.0
                       FROM orchestra_actors a
                       JOIN orchestra_runs r ON r.run_id=a.run_id
                       WHERE a.step_claimed_at IS NOT NULL
                         AND a.step_claimed_at < now() - make_interval(mins => %s)
                         AND r.status='running'
                         AND a.status NOT IN ('done','dead')
                       ORDER BY a.step_claimed_at""", (int(stale_min),))
        rows = cur.fetchall()
    return [{"actor_id": i, "run_id": rn, "tenant_id": t, "name": n, "role": ro,
             "claimed_by": by, "stale_min": round(float(m), 1)} for i, rn, t, n, ro, by, m in rows]


def release_terminal_step_claims():
    """Clear actor single-flight leases that can never be resumed.

    A controller cancellation used to halt a run and mark its actors dead while leaving their lease fields
    populated. Those inert rows then looked like live, blocked work forever and generated a watchdog storm.
    Only terminal actors or non-running runs are touched; every live actor in a running run is excluded.
    """
    ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE orchestra_actors a
                          SET step_claimed_at=NULL, step_claimed_by=NULL
                         FROM orchestra_runs r
                        WHERE r.run_id=a.run_id
                          AND a.step_claimed_at IS NOT NULL
                          AND (r.status <> 'running' OR a.status IN ('done','dead'))""")
        n = cur.rowcount
        c.commit()
    return n


# ---------------------------------------------------------------------------- the persisted bus
def emit(run_id, tenant_id, frm, to_actor, kind, payload=None, corr_id=None):
    """Persist one event onto the durable bus (an actor's inbox row). frm=None means the system/
    human injected it. kind must be in the shared bus vocabulary. Kill-switch-gated (a halted
    org accepts no NEW work; claiming/completing in-flight events stays allowed for drain)."""
    ensure()
    h = _halted()
    if h:
        return {"error": f"halted: {h.get('reason') or 'kill-switch engaged'}"}
    if kind not in KINDS:
        return {"error": f"unknown kind {kind!r}; must be one of {sorted(KINDS)}"}
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute(f"""INSERT INTO orchestra_events (run_id, tenant_id, frm, to_actor, kind, payload, corr_id)
                        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING {_EVENT_COLS}""",
                    (run_id, tenant_id, frm, to_actor, kind, json.dumps(payload or {}), corr_id))
        row = cur.fetchone(); c.commit()
    return _event_dict(row)


def emit_once(run_id, tenant_id, frm, to_actor, kind, payload=None, corr_id=None):
    """Idempotently persist one externally-produced event.

    A tool can finish successfully, commit its result event, and then lose the database acknowledgement.  A
    blind retry through :func:`emit` creates a second ``tool_result`` and can repeat browser/fixer work.  Tool
    jobs already carry a stable correlation id, so serialize that id with a transaction advisory lock and return
    the existing event when present.  The lock closes the select/insert race without imposing generic uniqueness
    on conversation ``corr_id`` values (ordinary conversations legitimately contain several same-kind events).
    """
    ensure()
    h = _halted()
    if h:
        return {"error": f"halted: {h.get('reason') or 'kill-switch engaged'}"}
    if kind not in KINDS:
        return {"error": f"unknown kind {kind!r}; must be one of {sorted(KINDS)}"}
    if not corr_id:
        return {"error": "corr_id is required for emit_once"}
    key = f"orchestra-event:{tenant_id}:{run_id}:{to_actor}:{kind}:{corr_id}"
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
        cur.execute(f"""SELECT {_EVENT_COLS} FROM orchestra_events
                        WHERE run_id=%s AND tenant_id=%s AND to_actor=%s AND kind=%s AND corr_id=%s
                        ORDER BY id LIMIT 1""", (run_id, tenant_id, to_actor, kind, corr_id))
        row = cur.fetchone()
        if row is None:
            cur.execute(f"""INSERT INTO orchestra_events
                            (run_id, tenant_id, frm, to_actor, kind, payload, corr_id)
                            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING {_EVENT_COLS}""",
                        (run_id, tenant_id, frm, to_actor, kind, json.dumps(payload or {}), corr_id))
            row = cur.fetchone()
        c.commit()
    return _event_dict(row)


@contextmanager
def tool_job_lock(run_id, tenant_id, actor_id, tool, resource_key=None):
    """Cross-process, reconnect-safe fenced lease for one durable tool-worker.

    ``jobrunner._JOBS`` only protects threads in one Python process. Duplicate controller/resume processes used
    to see the same blocked actor and both launch Chromium.  A session advisory lock is insufficient: a transient
    connection loss releases it while the old process can keep producing side effects.  This durable row lease
    survives reconnects and increments ``fence_token`` on every takeover.  The job runner heartbeats through
    fresh pooled connections and passes the token into the worker's cancellation/action boundary.

    Yields a lease dict when acquired, otherwise ``False``.  Release is owner+token conditional, so an obsolete
    process can never clear its successor's lease.
    """
    # Repository mutations must serialize across runs/processes, not merely per actor. QA browsers remain scoped
    # to their durable attempt/actor and may safely run in parallel up to admission capacity.
    key = (f"orchestra-tool-resource:{tool}:{resource_key}" if resource_key else
           f"orchestra-tool:{tenant_id}:{run_id}:{actor_id}:{tool}")
    owner = f"{os.getpid()}-{threading.get_ident()}-{secrets.token_hex(12)}"
    lease = claim_tool_job_lease(
        key, run_id, tenant_id, actor_id, tool, owner,
        # A physical repository path can outlive an old campaign tenant. Its mutation fence must therefore be
        # claimed through the trusted platform coordination connection; ordinary actor/tool leases remain under
        # tenant RLS. Once claimed, the row is reassigned to the current owner tenant and renew/release stay RLS.
        shared_resource=bool(resource_key))
    try:
        yield lease or False
    finally:
        if lease:
            release_tool_job_lease(key, owner, lease["fence_token"], tenant_id)


def claim_tool_job_lease(lease_key, run_id, tenant_id, actor_id, tool, owner_id,
                         lease_s=TOOL_LEASE_S, shared_resource=False):
    """Claim an absent/expired tool lease and return its new fencing generation; never steal a live lease."""
    ensure()
    # Shared physical resources intentionally cross tenant boundaries, so their tiny coordination row cannot
    # be read/taken over through a tenant RLS session. This owner connection is limited to the lease table and
    # returns no tenant data; all ordinary tool leases keep the tenant-scoped path below.
    with _conn(None if shared_resource else tenant_id) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO orchestra_tool_leases
                       (lease_key,tenant_id,run_id,actor_id,tool,owner_id,fence_token,lease_until,heartbeat_at)
                       VALUES (%s,%s,%s,%s,%s,%s,1,now()+make_interval(secs=>%s),now())
                       ON CONFLICT (lease_key) DO UPDATE SET
                           tenant_id=EXCLUDED.tenant_id, run_id=EXCLUDED.run_id,
                           actor_id=EXCLUDED.actor_id, tool=EXCLUDED.tool,
                           owner_id=EXCLUDED.owner_id,
                           fence_token=orchestra_tool_leases.fence_token+1,
                           lease_until=EXCLUDED.lease_until, heartbeat_at=now()
                       WHERE orchestra_tool_leases.lease_until <= now()
                       RETURNING fence_token, lease_until""",
                    (lease_key, tenant_id, run_id, actor_id, tool, owner_id, int(lease_s)))
        row = cur.fetchone(); c.commit()
    if not row:
        return None
    return {"lease_key": lease_key, "owner_id": owner_id, "fence_token": int(row[0]),
            "lease_until": _iso(row[1]), "lease_s": int(lease_s)}


def renew_tool_job_lease(lease_key, owner_id, fence_token, tenant_id=None,
                         lease_s=TOOL_LEASE_S):
    """Renew only the current, still-live fencing generation.  An expired generation cannot resurrect."""
    ensure()
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""UPDATE orchestra_tool_leases
                       SET lease_until=now()+make_interval(secs=>%s), heartbeat_at=now()
                       WHERE lease_key=%s AND owner_id=%s AND fence_token=%s
                         AND lease_until > now()
                       RETURNING lease_until""",
                    (int(lease_s), lease_key, owner_id, int(fence_token)))
        row = cur.fetchone(); c.commit()
    return {"valid": bool(row), "lease_until": _iso(row[0]) if row else None}


def tool_job_lease_valid(lease_key, owner_id, fence_token, tenant_id=None):
    """Fail-closed ownership check used immediately before a side-effecting worker action."""
    ensure()
    for attempt in range(2):
        try:
            with _conn(tenant_id) as c, c.cursor() as cur:
                cur.execute("""SELECT EXISTS (SELECT 1 FROM orchestra_tool_leases
                                              WHERE lease_key=%s AND owner_id=%s AND fence_token=%s
                                                AND lease_until > now())""",
                            (lease_key, owner_id, int(fence_token)))
                return bool(cur.fetchone()[0])
        except Exception:
            # A pooled socket can die independently of the lease row.  Retry on a fresh checkout once; if
            # ownership still cannot be proven, the side-effect boundary remains fail-closed.
            if attempt == 0:
                time.sleep(0.05)
    return False


def release_tool_job_lease(lease_key, owner_id, fence_token, tenant_id=None):
    """Expire our generation without deleting its monotonically increasing fencing counter."""
    ensure()
    try:
        with _conn(tenant_id) as c, c.cursor() as cur:
            cur.execute("""UPDATE orchestra_tool_leases SET lease_until=now(), heartbeat_at=now()
                           WHERE lease_key=%s AND owner_id=%s AND fence_token=%s""",
                        (lease_key, owner_id, int(fence_token)))
            released = cur.rowcount == 1; c.commit()
        return released
    except Exception:
        return False


def claim_events(actor_id, tenant_id=None, limit=16, claimed_by=None, lease_s=CLAIM_LEASE_S):
    """Atomically claim up to `limit` of an actor's oldest unprocessed events — FOR UPDATE SKIP
    LOCKED, so any number of concurrent claimers never double-claim (the tasks-queue pattern).
    CRASH-SAFE: an event claimed but never completed becomes claimable again once its claim is
    older than `lease_s` — a dead worker strands nothing. Returns the claimed event dicts."""
    ensure()
    # LOCK ORDER INVARIANT: every transaction touching both tables locks actor first, then events.
    # persist_step() already uses that order. The former event-first claim path deadlocked against it under
    # the QA pool. A failed transient transaction is safe to retry: PostgreSQL rolled it back and SKIP LOCKED
    # still prevents double delivery.
    attempts = _step_attempts()
    for attempt in range(attempts):
        try:
            with _conn(tenant_id) as c, c.cursor() as cur:
                _set_step_timeouts(cur)
                cur.execute("""SELECT actor_id FROM orchestra_actors
                               WHERE actor_id=%s AND (%s::text IS NULL OR tenant_id=%s)
                               FOR UPDATE""", (actor_id, tenant_id, tenant_id))
                if not cur.fetchone():
                    c.commit()
                    return []
                cur.execute("""SELECT id FROM orchestra_events
                               WHERE to_actor=%s AND processed_at IS NULL
                                 AND (claimed_at IS NULL OR claimed_at < now() - make_interval(secs => %s))
                                 AND (%s::text IS NULL OR tenant_id=%s)
                               ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s""",
                            (actor_id, lease_s, tenant_id, tenant_id, limit))
                ids = [r[0] for r in cur.fetchall()]
                if not ids:
                    c.commit()
                    return []
                cur.execute(f"""UPDATE orchestra_events SET claimed_at=now(), claimed_by=%s
                                WHERE id = ANY(%s) RETURNING {_EVENT_COLS}""",
                            (claimed_by or f"actor:{actor_id}", ids))
                rows = cur.fetchall()
                c.commit()
            return sorted((_event_dict(r) for r in rows), key=lambda e: e["id"])
        except _STEP_TRANSIENT_ERRORS:
            if attempt == attempts - 1:
                raise
            _retry_pause(attempt)


def release_event_claims(event_ids, tenant_id=None, claimed_by=None):
    """Immediately re-open an abandoned decide-step's inbox rows.

    Runtime exceptions used to leave otherwise healthy events invisible for the full 15-minute crash lease.
    Owner-conditional release makes this safe against a newer claimant: an obsolete worker cannot release a
    successor's generation. This is best-effort cleanup; the ordinary lease remains the crash backstop.
    """
    ids = [int(event_id) for event_id in (event_ids or [])]
    if not ids:
        return 0
    ensure()
    with _conn(tenant_id) as c, c.cursor() as cur:
        _set_step_timeouts(cur)
        cur.execute("""UPDATE orchestra_events SET claimed_at=NULL, claimed_by=NULL
                       WHERE id = ANY(%s) AND processed_at IS NULL
                         AND (%s::text IS NULL OR tenant_id=%s)
                         AND (%s::text IS NULL OR claimed_by=%s)""",
                    (ids, tenant_id, tenant_id, claimed_by, claimed_by))
        released = cur.rowcount
        c.commit()
    return released


def release_stale_claims(run_id, tenant_id=None, older_than_s=5):
    """Re-open work abandoned by a prior local runtime process.

    Event claims older than ``older_than_s`` are always reopened; the actor-step fence still prevents a live
    predecessor from executing concurrently.  That fence itself used to survive a dead controller for the full
    900-second lease, however, so a replacement runtime would reopen the inbox and then spin unable to claim its
    actor.  Runtime worker owners begin with their OS pid (``<pid>-...:orgw-N``).  For that exact, parseable local
    format, also clear an old actor-step claim only when the owning pid no longer exists.  Unknown owner formats
    and live pids remain untouched, preserving the fence for bounded-shutdown survivors and non-runtime callers.

    The return value remains the number of event claims reopened for API compatibility; actor-step recovery is
    deliberately an additional side effect of this startup reconciliation.
    """
    ensure()
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""UPDATE orchestra_events SET claimed_at=NULL, claimed_by=NULL
                       WHERE run_id=%s AND processed_at IS NULL AND claimed_at IS NOT NULL
                         AND claimed_at < now() - make_interval(secs => %s)
                         AND (%s::text IS NULL OR tenant_id=%s)""",
                    (run_id, older_than_s, tenant_id, tenant_id))
        n = cur.rowcount
        cur.execute("""SELECT actor_id,step_claimed_by FROM orchestra_actors
                       WHERE run_id=%s AND step_claimed_at IS NOT NULL
                         AND step_claimed_at < now() - make_interval(secs => %s)
                         AND (%s::text IS NULL OR tenant_id=%s)""",
                    (run_id, older_than_s, tenant_id, tenant_id))
        dead_owners = []
        for actor_id, owner in cur.fetchall():
            prefix = str(owner or "").split("-", 1)[0]
            if not prefix.isdigit() or ":orgw-" not in str(owner or ""):
                continue
            pid = int(prefix)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                dead_owners.append((int(actor_id), str(owner)))
            except (PermissionError, OSError):
                continue                         # existence cannot be disproved: keep the fence
        for actor_id, owner in dead_owners:
            cur.execute("""UPDATE orchestra_actors SET step_claimed_at=NULL,step_claimed_by=NULL
                           WHERE actor_id=%s AND step_claimed_by=%s
                             AND step_claimed_at < now() - make_interval(secs => %s)
                             AND (%s::text IS NULL OR tenant_id=%s)""",
                        (actor_id, owner, older_than_s, tenant_id, tenant_id))
        c.commit()
    return n


def complete_event(event_id, tenant_id=None):
    """Mark one claimed event handled (processed_at=now()). Idempotent — completing twice is a
    no-op that reports already_processed."""
    ensure()
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""UPDATE orchestra_events SET processed_at=now()
                       WHERE id=%s AND processed_at IS NULL
                         AND (%s::text IS NULL OR tenant_id=%s)
                       RETURNING id""", (event_id, tenant_id, tenant_id))
        done = cur.fetchone() is not None
        c.commit()
    return {"id": event_id, "processed": done, "already_processed": not done}


def persist_step(run_id, tenant_id, actor_id, status=None, assignment=None,
                 memory=None, result=None, emits=(), complete_ids=(), claimed_by=None,
                 finish_status=None, stop_requested=None):
    """ATOMIC durable decide-step: apply an actor's status/assignment/memory/result change, all its
    outbound emits, and completion of the events it just handled — in ONE transaction. Either the whole
    step lands or none of it does.

    Why this exists: doing these as separate `update_actor`/`emit`/`complete_event` calls left a fatal
    window — a crash after an actor committed status='done' but before its 'done' emit reached its
    supervisor stranded the parent FOREVER (it never reached its all-terminal aggregate). It also created
    an 'emitted but not completed' gap that re-delivered and double-spawned on replay. One transaction
    removes both: a crash before commit persists NOTHING (the claimed events simply reappear after their
    lease and the step re-runs cleanly); a commit persists EVERYTHING at once.

    None means 'leave unchanged' for status/assignment/result; memory is a shallow JSONB MERGE (skipped
    when falsy). A kill-switch or terminal run refuses the complete unit, leaving its input replayable; status
    and event-kind validation likewise happen before mutation. Returns a summary dict."""
    ensure()
    if status is not None and status not in ACTOR_STATUSES:
        return {"error": f"status must be one of {ACTOR_STATUSES}"}
    if finish_status is not None and finish_status not in RUN_END_STATUSES:
        return {"error": f"finish_status must be one of {RUN_END_STATUSES}"}
    bad = [k for (_f, _t, k, _p, _c) in emits if k not in KINDS]
    if bad:
        return {"error": f"unknown kind(s) {sorted(set(bad))}; must be in {sorted(KINDS)}"}
    halted = _halted()
    # A halt racing a decide-step must not create a half-commit. In particular, marking a worker done and
    # completing its input while suppressing its required `done` emit strands the supervisor forever. Leave the
    # entire unit untouched so it is safely retried after resume.
    if halted:
        return {"error": f"halted: {halted.get('reason') or 'kill-switch engaged'}", "halted": True}
    sets, args = ["last_active=now()"], []
    if status is not None:
        sets.append("status=%s"); args.append(status)
    if assignment is not None:
        sets.append("assignment=%s"); args.append(assignment)
    if memory:
        sets.append("memory = COALESCE(memory,'{}'::jsonb) || %s::jsonb"); args.append(json.dumps(memory))
    if result is not None:
        sets.append("result=%s"); args.append(json.dumps(result))
    attempts = _step_attempts()
    for attempt in range(attempts):
        if callable(stop_requested) and stop_requested():
            return {"error": "runtime stop requested before durable commit", "checkpoint": True,
                    "retryable": True, "attempts": attempt}
        try:
            with _conn(tenant_id) as c, c.cursor() as cur:
                _set_step_timeouts(cur)
                # RUN -> ACTOR -> EVENTS is the global lock order for a decide commit. Holding the run row
                # closes the check/use race with finish_run(): either this entire step lands before the halt,
                # or the terminal transition wins and this entire step is refused.
                cur.execute("""SELECT status FROM orchestra_runs
                               WHERE run_id=%s AND (%s::text IS NULL OR tenant_id=%s)
                               FOR UPDATE""", (run_id, tenant_id, tenant_id))
                run_row = cur.fetchone()
                if not run_row:
                    c.rollback()
                    return {"error": "no such run for this tenant"}
                if run_row[0] != "running":
                    c.rollback()
                    return {"error": f"run is {run_row[0]}, not running",
                            "halted": run_row[0] == "halted", "run_status": run_row[0]}
                cur.execute(f"""UPDATE orchestra_actors SET {', '.join(sets)}
                                WHERE actor_id=%s AND run_id=%s
                                  AND (%s::text IS NULL OR tenant_id=%s)
                                  AND (%s::text IS NULL OR step_claimed_by=%s)
                                RETURNING {_ACTOR_COLS}""",
                            (*args, actor_id, run_id, tenant_id, tenant_id, claimed_by, claimed_by))
                arow = cur.fetchone()
                if arow is None:
                    c.rollback()
                    return {"error": ("actor step lease is no longer owned" if claimed_by
                                      else "no such actor for this run/tenant")}
                emitted = 0
                for frm, to, kind, payload, corr in emits:
                    cur.execute("""INSERT INTO orchestra_events
                                   (run_id, tenant_id, frm, to_actor, kind, payload, corr_id)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                                (run_id, tenant_id, frm, to, kind, json.dumps(payload or {}), corr))
                    emitted += 1
                completed = 0
                for eid in complete_ids:
                    cur.execute("""UPDATE orchestra_events SET processed_at=now()
                                   WHERE id=%s AND run_id=%s AND to_actor=%s AND processed_at IS NULL
                                     AND (%s::text IS NULL OR tenant_id=%s)""",
                                (eid, run_id, actor_id, tenant_id, tenant_id))
                    completed += cur.rowcount
                if finish_status is not None:
                    cur.execute("""UPDATE orchestra_runs
                                      SET status=%s, result=%s, finished_at=now()
                                    WHERE run_id=%s AND status='running'
                                      AND (%s::text IS NULL OR tenant_id=%s)""",
                                (finish_status, json.dumps(result) if result is not None else None,
                                 run_id, tenant_id, tenant_id))
                    if cur.rowcount != 1:  # defensive: the locked status row should make this unreachable
                        raise psycopg.errors.SerializationFailure("run terminal fence changed")
                if callable(stop_requested) and stop_requested():
                    c.rollback()
                    return {"error": "runtime stop requested before durable commit", "checkpoint": True,
                            "retryable": True, "attempts": attempt + 1}
                c.commit()
            return {"ok": True, "actor": _actor_dict(arow), "emitted": emitted,
                    "completed": completed, "halted": False, "run_status": finish_status or "running"}
        except _STEP_TRANSIENT_ERRORS as exc:
            if callable(stop_requested) and stop_requested():
                return {"error": "runtime stop requested during database retry", "checkpoint": True,
                        "retryable": True, "attempts": attempt + 1,
                        "sqlstate": getattr(exc, "sqlstate", None)}
            if attempt == attempts - 1:
                return {"error": f"database contention: {type(exc).__name__}", "retryable": True,
                        "attempts": attempts, "sqlstate": getattr(exc, "sqlstate", None)}
            _retry_pause(attempt)


def events(run_id, tenant_id=None, corr_id=None):
    """Audit read-back: a run's full event stream (optionally one corr_id conversation)."""
    ensure()
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute(f"""SELECT {_EVENT_COLS} FROM orchestra_events
                        WHERE run_id=%s AND (%s::text IS NULL OR tenant_id=%s)
                          AND (%s::text IS NULL OR corr_id=%s)
                        ORDER BY id""", (run_id, tenant_id, tenant_id, corr_id, corr_id))
        rows = cur.fetchall()
    return [_event_dict(r) for r in rows]


def pending_count(actor_id, tenant_id=None):
    """How many unprocessed events sit in an actor's durable inbox."""
    ensure()
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""SELECT count(*) FROM orchestra_events
                       WHERE to_actor=%s AND processed_at IS NULL
                         AND (%s::text IS NULL OR tenant_id=%s)""",
                    (actor_id, tenant_id, tenant_id))
        n = cur.fetchone()[0]
    return int(n)


# ============================================================================================
# OFFLINE SELFTEST — no LLM calls; a REAL local Postgres (like the other modules' selftests).
# Spawns a 3-actor tree, emits/claims/completes events, proves SKIP-LOCKED exclusivity under
# real thread concurrency + lease reclaim after a simulated crash + tenant scoping + tenure.
# Touches ONLY its own throwaway tenant's rows and deletes them at the end. Kills nothing.
# ============================================================================================
def _selftest():
    import time
    import uuid
    from datetime import datetime

    tid = f"orchestra-store-selftest-{uuid.uuid4().hex[:8]}"
    ensure(); ensure()                                     # idempotent double-apply
    r = start_run(tid, "build a payments product")
    try:
        run_ok = r.get("run_id") and r["status"] == "running" and r["finished_at"] is None
        rid = r["run_id"]

        # --- the 3-actor tree: controller -> supervisor -> worker (identity + tenure rows) ---
        ctrl = spawn_actor(rid, tid, "Controller", "controller", kind="controller")
        sup = spawn_actor(rid, tid, "Head of Pay", "team-lead", kind="supervisor",
                          supervisor_id=ctrl["actor_id"], assignment="own the payments team")
        dev = spawn_actor(rid, tid, "dev-1", "backend-dev", kind="worker",
                          supervisor_id=sup["actor_id"], assignment="build the charges endpoint",
                          memory={"stack": "python"})
        spawn_ok = (ctrl.get("actor_id") and sup.get("supervisor_id") == ctrl["actor_id"]
                    and dev.get("supervisor_id") == sup["actor_id"]
                    and dev["status"] == "idle" and dev["hired_at"] and dev["memory"] == {"stack": "python"})
        # hiring guards: bad kind, dead run/tenant, foreign supervisor
        guard_ok = ("error" in spawn_actor(rid, tid, "x", "r", kind="boss")
                    and "error" in spawn_actor(rid, "not-my-tenant", "x", "r")
                    and "error" in spawn_actor(rid, tid, "x", "r", supervisor_id=999999999))

        # --- org_tree: the NESTED structure from supervisor_id links -------------------------
        t = org_tree(rid, tid)
        tree_ok = (t["actors"] == 3 and len(t["tree"]) == 1
                   and t["tree"][0]["actor_id"] == ctrl["actor_id"]
                   and t["tree"][0]["reports"][0]["actor_id"] == sup["actor_id"]
                   and t["tree"][0]["reports"][0]["reports"][0]["actor_id"] == dev["actor_id"]
                   and t["tree"][0]["reports"][0]["reports"][0]["reports"] == [])

        # --- emit -> claim -> complete on the persisted bus ----------------------------------
        ev = emit(rid, tid, sup["actor_id"], dev["actor_id"], "task",
                  {"do": "build /charges"}, corr_id="corr-1")
        bad = emit(rid, tid, sup["actor_id"], dev["actor_id"], "not-a-kind")
        got = claim_events(dev["actor_id"], tid)
        claim_ok = (len(got) == 1 and got[0]["id"] == ev["id"] and got[0]["kind"] == "task"
                    and got[0]["payload"]["do"] == "build /charges"
                    and got[0]["claimed_at"] and got[0]["processed_at"] is None
                    and "error" in bad)
        reclaim_blocked = claim_events(dev["actor_id"], tid) == []          # leased -> not re-claimable
        comp = complete_event(ev["id"], tid)
        comp_ok = comp["processed"] and complete_event(ev["id"], tid)["already_processed"]
        drained = pending_count(dev["actor_id"], tid) == 0

        # --- crash-safety: a claim whose holder died is lease-reclaimed ----------------------
        ev2 = emit(rid, tid, dev["actor_id"], sup["actor_id"], "blocked",
                   {"reason": "no API creds"}, corr_id="corr-2")
        first = claim_events(sup["actor_id"], tid, claimed_by="worker-that-crashes")
        # ...the holder dies without completing; with the lease expired the event comes back:
        re2 = claim_events(sup["actor_id"], tid, claimed_by="recovery-worker", lease_s=0)
        crash_ok = (len(first) == 1 and first[0]["id"] == ev2["id"]
                    and len(re2) == 1 and re2[0]["id"] == ev2["id"]
                    and re2[0]["claimed_by"] == "recovery-worker")
        complete_event(ev2["id"], tid)

        # --- actor step lease: cross-process single-flight over the actor's memory/result ----
        step_claim_1 = claim_actor_step(dev["actor_id"], tid, claimed_by="proc-a", lease_s=60)
        step_claim_2 = claim_actor_step(dev["actor_id"], tid, claimed_by="proc-b", lease_s=60)
        step_release = release_actor_step(dev["actor_id"], tid, claimed_by="proc-a")["released"]
        step_claim_3 = claim_actor_step(dev["actor_id"], tid, claimed_by="proc-b", lease_s=60)
        with _conn() as c, c.cursor() as cur:
            cur.execute("UPDATE orchestra_actors SET step_claimed_at=now()-interval '2 hours' "
                        "WHERE actor_id=%s", (dev["actor_id"],))
            c.commit()
        step_claim_4 = claim_actor_step(dev["actor_id"], tid, claimed_by="proc-c", lease_s=0)
        with _conn() as c, c.cursor() as cur:
            cur.execute("UPDATE orchestra_actors SET step_claimed_at=now()-interval '30 minutes' "
                        "WHERE actor_id=%s", (dev["actor_id"],))
            c.commit()
        stale_step = [a for a in stale_step_claims(10) if a["actor_id"] == dev["actor_id"]]
        release_actor_step(dev["actor_id"], tid, claimed_by="proc-c")
        actor_step_ok = (step_claim_1 and not step_claim_2 and step_release and step_claim_3
                         and step_claim_4 and stale_step and stale_step[0]["claimed_by"] == "proc-c")

        # --- SKIP LOCKED exclusivity under REAL concurrency ----------------------------------
        n_events = 12
        for i in range(n_events):
            emit(rid, tid, sup["actor_id"], dev["actor_id"], "task", {"n": i}, corr_id="corr-race")
        claimed, lock = [], threading.Lock()

        def _racer(name):
            while True:
                batch = claim_events(dev["actor_id"], tid, limit=3, claimed_by=name)
                if not batch:
                    return
                with lock:
                    claimed.extend(e["id"] for e in batch)
                for e in batch:
                    complete_event(e["id"], tid)

        threads = [threading.Thread(target=_racer, args=(f"racer-{i}",)) for i in range(3)]
        [th.start() for th in threads]
        [th.join(timeout=30) for th in threads]
        race_ok = (len(claimed) == n_events and len(set(claimed)) == n_events
                   and pending_count(dev["actor_id"], tid) == 0)

        # --- actor lifecycle: status/assignment/result + memory MERGE + tenure heartbeat -----
        u = update_actor(dev["actor_id"], tid, status="working")
        u2 = update_actor(dev["actor_id"], tid, memory={"lesson": "creds live in vault"},
                          status="done", result={"built": "/charges"})
        upd_ok = (u["status"] == "working" and u2["status"] == "done"
                  and u2["memory"] == {"stack": "python", "lesson": "creds live in vault"}  # MERGED
                  and u2["result"] == {"built": "/charges"}
                  and "error" in update_actor(dev["actor_id"], tid, status="retired"))
        before = datetime.fromisoformat(u2["last_active"])
        time.sleep(0.05)
        hb = heartbeat(dev["actor_id"], tid)
        hb_ok = datetime.fromisoformat(hb["last_active"]) > before

        # --- liveness sweep: a 'working' actor whose heartbeat went silent is flagged --------
        update_actor(sup["actor_id"], tid, status="working")
        with _conn() as c, c.cursor() as cur:   # simulate silence: backdate the beat
            cur.execute("UPDATE orchestra_actors SET last_active=now()-interval '30 minutes' "
                        "WHERE actor_id=%s", (sup["actor_id"],))
            c.commit()
        flagged = [a for a in stale_working(10) if a["actor_id"] == sup["actor_id"]]
        heartbeat(sup["actor_id"], tid)                     # sign of life -> no longer stale
        cleared = not [a for a in stale_working(10) if a["actor_id"] == sup["actor_id"]]
        update_actor(sup["actor_id"], tid, status="done")
        stale_ok = (len(flagged) == 1 and flagged[0]["stale_min"] >= 10
                    and flagged[0]["name"] == "Head of Pay" and cleared)

        # --- runs_for + org_tree tenure fields (the orgview render seam) ---------------------
        rf = runs_for(tid)
        node = org_tree(rid, tid)["tree"][0]
        view_ok = (len(rf) == 1 and rf[0]["run_id"] == rid
                   and isinstance(node["tenure_s"], int) and node["tenure_s"] >= 0
                   and isinstance(node["last_active_age_s"], int))

        # --- tenant scoping: another tenant sees NOTHING of this org -------------------------
        scope_ok = (actor(dev["actor_id"], "someone-else") is None
                    and run(rid, "someone-else") is None
                    and claim_events(dev["actor_id"], "someone-else") == []
                    and org_tree(rid, "someone-else")["actors"] == 0
                    and "error" in update_actor(dev["actor_id"], "someone-else", status="dead"))

        # --- audit read-back + run lifecycle close -------------------------------------------
        stream = events(rid, tid)
        audit_ok = (len(stream) == 2 + n_events
                    and len(events(rid, tid, corr_id="corr-race")) == n_events)
        fin = finish_run(rid, "done", {"summary": "payments product shipped"}, tenant_id=tid)
        fin_ok = (fin["status"] == "done" and fin["finished_at"]
                  and "error" in finish_run(rid, "sideways", tenant_id=tid))

        # --- stale-run sweep: a crashed 'running' run (no fresh actor) is abandoned; a run with a
        #     recently-active actor (a LIVE build) is KEPT. Both backdated past the window to isolate the
        #     'actor sign-of-life' signal from age.
        stale_run = start_run(tid, "x")["run_id"]
        fresh_run = start_run(tid, "x")["run_id"]
        fresh_actor = spawn_actor(fresh_run, tid, "live", "backend-engineer", kind="worker")["actor_id"]
        with _conn() as c, c.cursor() as cur:
            cur.execute("UPDATE orchestra_runs SET created_at=now()-interval '3 h' WHERE run_id IN (%s,%s)",
                        (stale_run, fresh_run))
            cur.execute("UPDATE orchestra_actors SET last_active=now() WHERE actor_id=%s", (fresh_actor,))
            c.commit()
        abandon_stale_runs(2)
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT status FROM orchestra_runs WHERE run_id=%s", (stale_run,)); ss = cur.fetchone()[0]
            cur.execute("SELECT status FROM orchestra_runs WHERE run_id=%s", (fresh_run,)); fs = cur.fetchone()[0]
        abandon_ok = ss == "abandoned" and fs == "running"   # crashed run swept; live-actor build untouched

        ok = all([run_ok, spawn_ok, guard_ok, tree_ok, claim_ok, reclaim_blocked, comp_ok,
                  drained, crash_ok, actor_step_ok, race_ok, upd_ok, hb_ok, stale_ok, view_ok, scope_ok,
                  audit_ok, fin_ok, abandon_ok])
        print(f"run={run_ok} spawn3={spawn_ok} hire_guards={guard_ok} org_tree_nested={tree_ok} "
              f"emit/claim={claim_ok} lease_holds={reclaim_blocked} complete={comp_ok} "
              f"drained={drained} crash_reclaim={crash_ok} skip_locked_race(3x{n_events})={race_ok} "
              f"actor_step_singleflight+monitor={actor_step_ok} update+memory_merge={upd_ok} "
              f"heartbeat_tenure={hb_ok} stale_working_sweep={stale_ok} "
              f"runs_for+tenure_view={view_ok} tenant_scope={scope_ok} audit={audit_ok} finish={fin_ok} "
              f"stale_run_abandon={abandon_ok}")
        print("PASS: durable org — actors are Postgres rows with identity/tenure/memory, the org "
              "tree nests from supervisor links, events are SKIP-LOCKED claimable and crash-"
              "reclaimable, everything tenant-scoped ✅" if ok else "FAIL")
    finally:
        with _conn() as c, c.cursor() as cur:   # remove ONLY this run's throwaway rows
            cur.execute("DELETE FROM orchestra_events WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM orchestra_actors WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM orchestra_runs WHERE tenant_id=%s", (tid,))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "ensure":
        ensure(); print("schema ok")
    elif a[0] == "tree" and len(a) > 1:
        print(json.dumps(org_tree(int(a[1]), a[2] if len(a) > 2 else None), indent=2))
    elif a[0] == "run" and len(a) > 1:
        print(json.dumps(run(int(a[1]), a[2] if len(a) > 2 else None), indent=2))
    elif a[0] == "actors" and len(a) > 1:
        print(json.dumps(actors(int(a[1]), a[2] if len(a) > 2 else None), indent=2))
    elif a[0] == "events" and len(a) > 1:
        print(json.dumps(events(int(a[1]), a[2] if len(a) > 2 else None), indent=2))
    else:
        print("usage: store.py selftest | ensure | tree <run_id> [tenant] | run <run_id> [tenant] | "
              "actors <run_id> [tenant] | events <run_id> [tenant]")


__all__ = ["ensure", "start_run", "run", "finish_run", "spawn_actor", "spawn_actor_once",
           "update_actor", "actor",
           "actors", "actors_with_pending_events", "heartbeat", "org_tree", "runs_for", "stale_working", "stale_step_claims",
           "release_terminal_step_claims", "emit", "emit_once", "claim_events",
           "claim_actor_step", "release_actor_step", "complete_event", "events", "pending_count",
           "tool_job_lock", "claim_tool_job_lease", "renew_tool_job_lease",
           "tool_job_lease_valid", "release_tool_job_lease",
           "KINDS", "ACTOR_KINDS", "ACTOR_STATUSES", "DB"]


if __name__ == "__main__":
    _main(sys.argv[1:])
