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
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit      # noqa: E402
import directory  # noqa: E402
import factory    # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)
INBOX_WORKSPACE = factory.PRODUCTS / "_inbox"
MAX_PER_TICK = int(os.environ.get("DISPATCH_MAX_PER_TICK", "2"))   # cost guard


def _pull(limit):
    """Atomically claim up to `limit` highest-priority pending tasks (concurrent-dispatcher-safe)."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id, assignee, requester, title, priority FROM tasks WHERE status='pending'
                       ORDER BY priority, id FOR UPDATE SKIP LOCKED LIMIT %s""", (limit,))
        rows = cur.fetchall()
        if rows:
            cur.execute("UPDATE tasks SET status='active' WHERE id = ANY(%s)", ([r[0] for r in rows],))
        c.commit()
    return rows


def _done(tid):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE tasks SET status='done' WHERE id=%s", (tid,))
        c.commit()


def process(task):
    tid, assignee, requester, title, priority = task
    role = assignee.split("@", 1)[0]                  # agent_id 'legal-...@inst' -> role
    workspace = INBOX_WORKSPACE / assignee.replace("@", "_at_").replace("/", "_")
    workspace.mkdir(parents=True, exist_ok=True)
    audit.append(actor="dispatcher", action="WakeAgent", resource=assignee, decision="invoked",
                 payload={"task_id": tid, "priority": priority})
    r = factory.agent(role, str(workspace), title)    # INVOKE the idle agent to actually do the task
    _done(tid)
    if requester:                                     # close the loop: reply to whoever asked
        directory.contact(assignee, requester, "reply", (r.get("out") or "")[:800])
    return {"task_id": tid, "assignee": assignee, "ok": r.get("rc") == 0}


def tick():
    INBOX_WORKSPACE.mkdir(parents=True, exist_ok=True)
    rows = _pull(MAX_PER_TICK)
    results = [process(t) for t in rows]
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
        orchestrate.enqueue(ag, f"selftest task {suf}", priority=5, requester=f"controller@{suf}")
        claimed = _pull(5)
        got = [r for r in claimed if r[1] == ag]
        # release it back so we don't actually spend an agent call in selftest
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE assignee=%s", (ag,)); c.commit()
        ok = len(got) == 1 and got[0][3].startswith("selftest task")
        print(f"claimed pending task for idle agent: {ok} (assignee={ag})")
        print("PASS: dispatcher claims + would invoke idle agents ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
