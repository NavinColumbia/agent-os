#!/usr/bin/env python3
"""tasksweep.py — queue resiliency: reclaim STUCK tasks + surface the dead-letter queue.

The dispatcher claims a task by setting it 'active' + stamping locked_at (a lease). If that dispatcher
crashes mid-task, the row would sit 'active' forever (orphaned work). This sweep — the queue analogue of
reap.py for processes — resets any task whose lease has expired back to 'pending' (counting it as a failed
attempt, so it still dead-letters eventually rather than looping), and reports dead-letter depth so the
watchdog/dashboard can page on a growing DLQ. Idempotent, safe on a cadence (wired as a scheduler job).

    tasksweep.py run       # reclaim expired-lease tasks + report dlq
    tasksweep.py status    # dry-run: what WOULD be reclaimed + current dlq depth
    tasksweep.py selftest
Run with the agent-os venv python.
"""
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit   # noqa: E402
from dbpool import connection  # noqa: E402

LEASE_S = int(os.environ.get("AOS_TASK_LEASE_S", "1800"))   # 30 min — beyond any real single task
SWEEP_LIMIT = max(1, min(1000, int(os.environ.get("AOS_TASK_SWEEP_LIMIT", "100"))))


def _depths():
    """(reclaimable_active, dead_count) — what's stuck past lease and what's dead-lettered."""
    with connection() as c, c.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout='1s'")
        cur.execute("SET LOCAL statement_timeout='5s'")
        cur.execute("""SELECT count(*) FROM tasks WHERE status='active' AND locked_at IS NOT NULL
                       AND locked_at < now() - (%s || ' seconds')::interval""", (LEASE_S,))
        stuck = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM tasks WHERE status='dead'")
        dead = cur.fetchone()[0]
    return stuck, dead


def sweep(dry=False, limit=SWEEP_LIMIT):
    """Reclaim expired-lease 'active' tasks to 'pending' (as a failed attempt). Returns a summary dict."""
    stuck, dead = _depths()
    reclaimed = 0
    if stuck and not dry:
        with connection() as c, c.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout='1s'")
            cur.execute("SET LOCAL statement_timeout='5s'")
            # Reclaim one bounded lease page. SKIP LOCKED lets a live dispatcher finish its row and prevents
            # a large historical backlog from producing one unbounded transaction/RETURNING result.
            cur.execute("""WITH candidates AS (
                             SELECT id FROM tasks
                              WHERE status='active' AND locked_at IS NOT NULL
                                AND locked_at < now() - (%s || ' seconds')::interval
                              ORDER BY locked_at, id
                              FOR UPDATE SKIP LOCKED
                              LIMIT %s
                           )
                           UPDATE tasks t
                              SET status='pending', attempts=COALESCE(t.attempts,0)+1, locked_at=NULL,
                                  last_error='lease expired (dispatcher crash/stall) — reclaimed',
                                  not_before=now()
                             FROM candidates c
                            WHERE t.id=c.id
                           RETURNING t.id""", (LEASE_S, max(1, int(limit or 1))))
            reclaimed = len(cur.fetchall())
            c.commit()
        audit.append(actor="tasksweep", action="ReclaimStuckTasks", resource="queue", decision="reclaimed",
                     payload={"reclaimed": reclaimed, "lease_s": LEASE_S})
    return {"reclaimed": reclaimed, "reclaimable": stuck, "remaining_reclaimable": max(0, stuck-reclaimed),
            "dead_letter_depth": dead, "batch_limit": max(1, int(limit or 1))}


def _selftest():
    """Insert a task whose lease is already expired; prove sweep reclaims exactly it. No agent spend."""
    suf = os.urandom(3).hex()
    ag = f"sweeptest@{suf}"
    with connection() as c, c.cursor() as cur:
        # expired-lease active task (locked_at far in the past) + a fresh active task that must NOT move
        cur.execute("""INSERT INTO tasks (tenant_id,assignee,title,status,locked_at)
                       VALUES ('_platform',%s,%s,'active',now()-interval '999 hours') RETURNING id""",
                    (ag, f"stuck {suf}"))
        stuck_id = cur.fetchone()[0]
        cur.execute("""INSERT INTO tasks (tenant_id,assignee,title,status,locked_at)
                       VALUES ('_platform',%s,%s,'active',now()) RETURNING id""", (ag, f"fresh {suf}"))
        fresh_id = cur.fetchone()[0]
        c.commit()
    sweep(dry=False)
    with connection() as c, c.cursor() as cur:
        cur.execute("SELECT status, attempts FROM tasks WHERE id=%s", (stuck_id,))
        s_status, s_attempts = cur.fetchone()
        cur.execute("SELECT status FROM tasks WHERE id=%s", (fresh_id,))
        f_status = cur.fetchone()[0]
        cur.execute("DELETE FROM tasks WHERE assignee=%s", (ag,)); c.commit()
    ok = (s_status == "pending" and s_attempts == 1 and f_status == "active")
    print(f"stuck->{s_status}(attempts={s_attempts}) fresh-stays->{f_status}")
    print("PASS: tasksweep reclaims expired-lease tasks, leaves fresh ones ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "run":
        print(json.dumps(sweep(dry=False), indent=2))
    elif a[0] == "status":
        print(json.dumps(sweep(dry=True), indent=2))
    else:
        sys.exit("usage: tasksweep.py run | status | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
