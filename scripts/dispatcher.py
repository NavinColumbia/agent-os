#!/usr/bin/env python3
"""dispatcher.py — the activation loop. THIS is what wakes idle agents.

Agents are ephemeral — they run, act, exit. Without a poller, a task routed to an agent would sit in
its queue forever. The dispatcher polls the priority task queue, and for each pending task it INVOKES
the assigned agent to actually do it, then closes the loop by replying to whoever asked. Bounded per
tick (cost-safe) and skips work for paused apps. Drive it from a short loop (dispatcher.sh) the same
way the ticker/watchdog run.

    dispatcher.py tick           # process up to MAX_PER_TICK pending tasks
    dispatcher.py selftest
Run with the agent-os venv python.
"""
import os
import sys
import threading
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import appguard   # noqa: E402  — per-app circuit-breaker (pause state); gate dispatch so paused apps don't spend
import audit      # noqa: E402
import directory  # noqa: E402
import factory    # noqa: E402
import notify      # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)
INBOX_WORKSPACE = factory.PRODUCTS / "_inbox"
MAX_PER_TICK = int(os.environ.get("DISPATCH_MAX_PER_TICK", "2"))   # cost guard
BACKOFF_S = int(os.environ.get("AOS_TASK_BACKOFF_S", "120"))       # base retry backoff (×attempts)
# Lease heartbeat: while a worker holds a claimed task it re-stamps locked_at on this cadence so a task
# that legitimately runs longer than tasksweep's AOS_TASK_LEASE_S (default 1800s) is NOT reaped as if its
# worker were dead. MUST stay comfortably below that lease so several beats land within one lease window.
HEARTBEAT_S = int(os.environ.get("AOS_TASK_HEARTBEAT_S", "300"))
# Circuit-breaker gate: how long to defer (not fail) a claimed task whose app appguard has PAUSED, before
# re-checking. Cheap DB re-poll only — the agent is never invoked while paused, so no money is spent.
PAUSE_DEFER_S = int(os.environ.get("AOS_PAUSE_DEFER_S", "600"))


def _pull(limit):
    """Atomically claim up to `limit` runnable highest-priority tasks (concurrent-dispatcher-safe).
    Runnable = pending AND past its backoff (`not_before`). Stamps `locked_at` so a crashed dispatcher's
    task can be lease-reclaimed by tasksweep instead of being orphaned in 'active' forever. The holding
    worker then refreshes locked_at on a heartbeat (see _Heartbeat) so only DEAD workers get reclaimed."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id, assignee, requester, title, priority,
                              COALESCE(attempts,0), COALESCE(max_retry,3)
                       FROM tasks WHERE status='pending' AND (not_before IS NULL OR not_before <= now())
                       ORDER BY priority, id FOR UPDATE SKIP LOCKED LIMIT %s""", (limit,))
        rows = cur.fetchall()
        if rows:
            cur.execute("UPDATE tasks SET status='active', locked_at=now() WHERE id = ANY(%s)",
                        ([r[0] for r in rows],))
        c.commit()
    return rows


def _touch(tid):
    """Refresh the lease on a still-running claimed task: re-stamp locked_at=now() so tasksweep does not
    reclaim a LIVE worker. Scoped to status='active' so we never resurrect a row that some other path has
    already moved to done/dead/pending (avoids a heartbeat racing a concurrent completion)."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE tasks SET locked_at=now() WHERE id=%s AND status='active'", (tid,))
        c.commit()


class _Heartbeat:
    """Background lease-refresher held for the duration of a (possibly long) agent invocation.

    #25: _pull() stamps locked_at ONCE at claim. tasksweep reaps any 'active' row whose locked_at aged past
    LEASE_S — which, without refresh, reaps tasks whose worker is alive but merely running longer than the
    lease, double-running them. While we hold the task we beat every HEARTBEAT_S to keep the lease fresh, so
    tasksweep only reclaims genuinely dead/stalled workers. Use as a context manager around factory.agent()."""

    def __init__(self, tid):
        self.tid = tid
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, name=f"lease-{tid}", daemon=True)

    def _run(self):
        # wait-then-touch: sleep one interval, refresh, repeat — until __exit__ sets the stop event.
        while not self._stop.wait(HEARTBEAT_S):
            try:
                _touch(self.tid)
            except Exception:
                # A transient DB hiccup must not kill the worker; the next beat (or, worst case, tasksweep's
                # full lease window) covers a single missed refresh, so swallow and keep beating.
                pass

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=5)
        return False


def _done(tid):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE tasks SET status='done', locked_at=NULL WHERE id=%s", (tid,))
        c.commit()


def _retry_or_dead(tid, attempts, max_retry, err):
    """A failed task is NOT dropped: requeue with linear backoff until max_retry, then dead-letter it
    (visible in the 'dead' state + paged) so a human can act. Returns 'retry' or 'dead'."""
    attempts += 1
    err = (err or "")[:500]
    with psycopg.connect(DB) as c, c.cursor() as cur:
        if attempts >= max_retry:
            cur.execute("UPDATE tasks SET status='dead', attempts=%s, last_error=%s, locked_at=NULL WHERE id=%s",
                        (attempts, err, tid))
            c.commit()
            audit.append(actor="dispatcher", action="TaskDeadLettered", resource=str(tid),
                         decision="dead", payload={"attempts": attempts, "error": err[:160]})
            notify.send(f"Task #{tid} dead-lettered after {attempts} attempts: {err[:120]}",
                        title="agent-os queue", priority="high", tags="warning")
            return "dead"
        cur.execute("""UPDATE tasks SET status='pending', attempts=%s, last_error=%s, locked_at=NULL,
                       not_before=now() + (%s || ' seconds')::interval WHERE id=%s""",
                    (attempts, err, BACKOFF_S * attempts, tid))
        c.commit()
        audit.append(actor="dispatcher", action="TaskRetry", resource=str(tid), decision="requeued",
                     payload={"attempts": attempts, "backoff_s": BACKOFF_S * attempts, "error": err[:160]})
        return "retry"


def _app_of(assignee):
    """The app/product a task belongs to, for the pause gate. Agent ids are '{role}@{product}' (factory.py
    aid = f'{role}@{product}'), so the product is the part after '@'. Returns None when there is no product
    component (e.g. a global/agentless assignee) — then we can't attribute it to an app, so dispatch proceeds."""
    return assignee.split("@", 1)[1] if assignee and "@" in assignee else None


def _paused_apps():
    """Set of apps the circuit-breaker (appguard) has PAUSED. FAIL-OPEN by design: a transient
    appguard/DB hiccup must NEVER brick the whole activation loop, so on any lookup error we return an
    empty set and dispatch normally (factory's per-product budget governor still backstops spend). Only a
    CONFIRMED 'paused' status withholds work — fail-closed here would strand every tenant's liveness."""
    try:
        return {p["app"] for p in appguard.paused_apps()}
    except Exception as e:                            # lookup failure -> open the gate, but leave a trail
        audit.append(actor="dispatcher", action="PauseLookupFailed", resource="appguard",
                     decision="fail-open", payload={"error": str(e)[:160]})
        return set()


def _defer_paused(tid, app):
    """Release a claimed task for a PAUSED app back to 'pending' with a re-check delay instead of running
    it — waking its agent would spend the very money the circuit-breaker is halting. attempts/max_retry are
    left UNTOUCHED so a long pause never dead-letters legitimate work; it simply waits for the human resume."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""UPDATE tasks SET status='pending', locked_at=NULL,
                       not_before=now() + (%s || ' seconds')::interval
                       WHERE id=%s AND status='active'""", (PAUSE_DEFER_S, tid))
        c.commit()
    audit.append(actor="dispatcher", action="SkipPausedApp", resource=app, decision="deferred",
                 payload={"task_id": tid, "recheck_s": PAUSE_DEFER_S})


def process(task, paused=None):
    tid, assignee, requester, title, priority, attempts, max_retry = task
    app = _app_of(assignee)
    if paused is None:                                # standalone call (not via tick): resolve pauses now
        paused = _paused_apps()
    if app and app in paused:                         # circuit-breaker open for this app -> do NOT spend
        _defer_paused(tid, app)
        return {"task_id": tid, "assignee": assignee, "ok": False, "disposition": "paused-skip", "app": app}
    role = assignee.split("@", 1)[0]                  # agent_id 'legal-...@inst' -> role
    workspace = INBOX_WORKSPACE / assignee.replace("@", "_at_").replace("/", "_")
    workspace.mkdir(parents=True, exist_ok=True)
    audit.append(actor="dispatcher", action="WakeAgent", resource=assignee, decision="invoked",
                 payload={"task_id": tid, "priority": priority, "attempt": attempts + 1})
    try:
        with _Heartbeat(tid):                            # keep the lease fresh while this live worker runs
            r = factory.agent(role, str(workspace), title)   # INVOKE the idle agent to actually do the task
    except Exception as e:                               # a crash is a failure, not a silent drop
        r = {"rc": 1, "out": "", "blocker": f"agent raised: {e}"}
    if r.get("rc") == 0:
        _done(tid)
        if requester:                                 # close the loop: reply to whoever asked
            directory.contact(assignee, requester, "reply", (r.get("out") or "")[:800])
        return {"task_id": tid, "assignee": assignee, "ok": True, "disposition": "done"}
    disp = _retry_or_dead(tid, attempts, max_retry, r.get("blocker") or (r.get("out") or "")[:200])
    if requester and disp == "dead":
        directory.contact(assignee, requester, "reply", f"FAILED (dead-lettered): {title[:200]}")
    return {"task_id": tid, "assignee": assignee, "ok": False, "disposition": disp}


def tick():
    INBOX_WORKSPACE.mkdir(parents=True, exist_ok=True)
    rows = _pull(MAX_PER_TICK)
    paused = _paused_apps()                           # one lookup per tick; gates every claimed task below
    results = [process(t, paused) for t in rows]
    return {"processed": len(results), "tasks": results}


def _main(a):
    import json
    if not a or a[0] == "tick":
        print(json.dumps(tick(), indent=2))
    elif a[0] == "selftest":
        # offline: prove the claim path (pull-and-claim semantics) without spending on an agent call
        import orchestrate
        suf = os.urandom(3).hex()
        ag = f"technical-writer@disp-{suf}"
        # self-heal: purge any orphaned selftest rows left by a PRIOR interrupted run (they would
        # otherwise age past the lease and get run as bogus real work). Unmistakable test-only pattern.
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE assignee LIKE 'technical-writer@disp-%%' "
                        "AND title LIKE 'selftest task %%'"); c.commit()
        try:
            orchestrate.enqueue(ag, f"selftest task {suf}", priority=5, requester=f"controller@{suf}")
            claimed = _pull(5)
            got = [r for r in claimed if r[1] == ag]
            ok = len(got) == 1 and got[0][3].startswith("selftest task")
        finally:                                          # ALWAYS release our own row, even on error
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("DELETE FROM tasks WHERE assignee=%s", (ag,)); c.commit()
        print(f"claimed pending task for idle agent: {ok} (assignee={ag})")
        # #25: prove the lease heartbeat refreshes a LIVE worker's locked_at so tasksweep won't reap it.
        # Insert an 'active' row with a stale (long-expired) lease, run one _touch, confirm it moved forward
        # past the tasksweep lease horizon. Done WITHOUT spending on an agent call.
        hb_ag = f"hbtest@disp-{suf}"
        try:
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("""INSERT INTO tasks (assignee,title,status,locked_at)
                               VALUES (%s,%s,'active', now() - interval '999 hours') RETURNING id""",
                            (hb_ag, f"selftest heartbeat {suf}"))
                hb_id = cur.fetchone()[0]; c.commit()
            _touch(hb_id)                                 # the heartbeat's core lease-refresh step
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("SELECT now() - locked_at < interval '1 minute' FROM tasks WHERE id=%s", (hb_id,))
                hb_ok = bool(cur.fetchone()[0])
        finally:
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("DELETE FROM tasks WHERE assignee=%s", (hb_ag,)); c.commit()
        print(f"heartbeat refreshed live lease: {hb_ok}")
        ok = ok and hb_ok
        # circuit-breaker gate: a claimed task whose app appguard PAUSED must be skipped (agent NOT invoked,
        # so $0 spent) and released back to 'pending' for later — NOT failed/dead-lettered. Proven offline.
        papp = f"paused-app-{suf}"
        pag = f"technical-writer@{papp}"
        try:
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("""INSERT INTO app_policies (app,status,reason) VALUES (%s,'paused','selftest')
                               ON CONFLICT (app) DO UPDATE SET status='paused', reason='selftest'""", (papp,))
                c.commit()
            orchestrate.enqueue(pag, f"selftest paused {suf}", priority=5)
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("""UPDATE tasks SET status='active', locked_at=now() WHERE assignee=%s
                               RETURNING id, assignee, requester, title, priority,
                                         COALESCE(attempts,0), COALESCE(max_retry,3)""", (pag,))
                prow = cur.fetchone(); c.commit()
            disp = process(prow, _paused_apps()) if prow else {}     # gate; does NOT call factory.agent
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("SELECT status FROM tasks WHERE assignee=%s", (pag,))
                st = cur.fetchone()
            pause_ok = bool(prow) and disp.get("disposition") == "paused-skip" and bool(st) and st[0] == "pending"
        finally:                                          # ALWAYS clean up our fixture rows
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("DELETE FROM tasks WHERE assignee=%s", (pag,))
                cur.execute("DELETE FROM app_policies WHERE app=%s", (papp,)); c.commit()
        print(f"paused-app task skipped (no spend) + requeued, not failed: {pause_ok}")
        ok = ok and pause_ok
        print("PASS: dispatcher claims + would invoke idle agents + heartbeats live leases + skips paused apps ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
