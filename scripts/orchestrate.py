#!/usr/bin/env python3
"""orchestrate.py — how an agent gets a collaborator, and how agents prioritize their work.

Answers the concrete scenario: a developer needs (say) a legal review.
  1) It does NOT spawn legal itself — only the controller may spawn (manifest invariant).
  2) request_collaborator() decides:
       * legal already ACTIVE in the directory and not overloaded -> route the task DIRECTLY to it
         (enqueue in its priority queue + a brokered mailbox message). No controller in the loop.
       * legal active but every instance overloaded -> file a hire_request so the controller spawns
         another instance (scale out).
       * no legal instance active (but the role exists) -> file a hire_request; the controller spawns
         one, registers it in the directory, and the queued task is routed to it.
       * the needed role is NOT covered by any role -> suggest the nearest skill-matched role; if none,
         return 'no_role' to escalate to the resource-allocator / human (create a new role).
  The requester then waits via the durable suspend-until-reply (commfabric ask-await) — it is parked,
  not holding a connection, and resumes when the reply arrives (crash-safe).

Each agent drains its queue HIGHEST-PRIORITY-FIRST (priority 1..9, then FIFO).

    orchestrate.py request <requester> <need_role> "<task>" [priority]
    orchestrate.py queue <assignee>            # an agent's prioritized task list
    orchestrate.py next <assignee>             # pull the top task
    orchestrate.py hires                        # open hire requests (controller's inbox)
    orchestrate.py fulfill <hire_id> <agent_id> # controller spawns + routes
    orchestrate.py selftest
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import directory   # noqa: E402
import governance  # noqa: E402  (the read side of the governance manifest: may()/enforce())

from aoscfg import ENV, DB
ROLES_DIR = Path.home() / "projects" / "control-plane" / "roles"
OVERLOAD = 3   # an agent with this many pending tasks is considered overloaded

# A gated-action phrase (detected in a collaborator request title) -> the capability the REQUESTER
# must ITSELF hold to be allowed to delegate it. You cannot route around a capability you lack by
# asking a collaborator to perform it for you — the anti-circumvention rule governance.py exists for.
# These are the governance capabilities that have no direct in-tree action site yet, so this is where
# their flag is genuinely READ on the live request-routing path (not merely defined in the manifest).
_DELEGATION_CAPS = {
    "deploy": "deploy",
    "merge to main": "merge_main",
    "merge main": "merge_main",
    "publish public": "post_publicly",
    "post public": "post_publicly",
    "post publicly": "post_publicly",
    "modify registry": "modify_registry",
    "registry change": "modify_registry",
    "open bounded meeting": "open_bounded_meeting",
    "bounded meeting": "open_bounded_meeting",
}


def _role_of(agent_id):
    """The role portion of an agent_id ('builder@app-7f3' -> 'builder'); the manifest lookup key."""
    return str(agent_id).split("@", 1)[0]


def _delegation_block(role, title):
    """If a request asks a collaborator to perform a GATED action the requester itself may not perform,
    return that capability (so the caller refuses to route it). Genuinely consults governance.may() for
    every no-direct-action-site capability (deploy/merge_main/post_publicly/modify_registry/
    open_bounded_meeting) on the live collaborator path."""
    t = (title or "").lower()
    for phrase, cap in _DELEGATION_CAPS.items():
        if phrase in t and not governance.may(role, cap):
            return cap
    return None


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS tasks (id BIGSERIAL PRIMARY KEY, assignee TEXT NOT NULL,
                       requester TEXT, role TEXT, title TEXT NOT NULL, priority INT NOT NULL DEFAULT 5,
                       status TEXT NOT NULL DEFAULT 'pending', created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS hire_requests (id BIGSERIAL PRIMARY KEY, requester TEXT NOT NULL,
                       need_role TEXT NOT NULL, reason TEXT, title TEXT, priority INT NOT NULL DEFAULT 5,
                       status TEXT NOT NULL DEFAULT 'open',
                       created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        # The queued task that motivated the hire MUST survive on the row so fulfill() can route it
        # (older deployments predate these columns — add them idempotently).
        cur.execute("ALTER TABLE hire_requests ADD COLUMN IF NOT EXISTS title TEXT")
        # tenant_id scopes a hire to the owning company so approvals.inbox never leaks it cross-tenant
        # (NULL = a platform/shared-pool hire with no owning tenant — surfaced to no CEO inbox).
        cur.execute("ALTER TABLE hire_requests ADD COLUMN IF NOT EXISTS tenant_id TEXT")
        cur.execute("ALTER TABLE hire_requests ADD COLUMN IF NOT EXISTS priority INT NOT NULL DEFAULT 5")
        c.commit()


def known_roles():
    return {p.stem for p in ROLES_DIR.glob("*.yaml")} if ROLES_DIR.exists() else set()


def nearest_role(need):
    """If the literal role doesn't exist, find the closest role via the skills registry (keyword match)."""
    try:
        import skills
        need_l = need.replace("-", " ").lower()
        best = None
        for name, cat, desc, roles, tools, status, engine in skills.CATALOG:
            hay = f"{name} {desc} {cat}".lower()
            if any(w in hay for w in need_l.split() if len(w) > 3):
                best = roles[0] if roles else None
                if best in known_roles():
                    return best
        return best
    except Exception:
        return None


def enqueue(assignee, title, priority=5, requester=None, role=None):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO tasks (assignee, requester, role, title, priority) VALUES (%s,%s,%s,%s,%s)
                       RETURNING id""", (assignee, requester, role, title, max(1, min(9, priority))))
        tid = cur.fetchone()[0]
        c.commit()
    return tid


def queue_of(assignee):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id, title, priority, status FROM tasks WHERE assignee=%s AND status<>'done'
                       ORDER BY priority, id""", (assignee,))
        return [{"id": i, "title": t, "priority": p, "status": s} for i, t, p, s in cur.fetchall()]


def next_task(assignee):
    """Pull the highest-priority pending task (lock-free concurrent-safe), mark it active."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id, title, priority FROM tasks WHERE assignee=%s AND status='pending'
                       ORDER BY priority, id FOR UPDATE SKIP LOCKED LIMIT 1""", (assignee,))
        row = cur.fetchone()
        if not row:
            c.commit(); return None
        cur.execute("UPDATE tasks SET status='active' WHERE id=%s", (row[0],))
        c.commit()
        return {"id": row[0], "title": row[1], "priority": row[2]}


def complete(task_id):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE tasks SET status='done' WHERE id=%s", (task_id,))
        c.commit()


def _pending_count(assignee):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM tasks WHERE assignee=%s AND status<>'done'", (assignee,))
        return cur.fetchone()[0]


def file_hire(requester, need_role, reason, title=None, priority=5, tenant_id=None):
    """File a hire request. The motivating task (title + priority + requester) is persisted on the
    row so fulfill() can route it to the freshly-spawned instance — otherwise the work is silently
    dropped on the spawn path. tenant_id scopes it to the owning company for approvals.inbox (None =
    a shared-pool hire with no owning tenant — it surfaces to no CEO inbox, never cross-tenant)."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO hire_requests (requester, need_role, reason, title, priority, tenant_id)
                       VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (requester, need_role, reason, title, max(1, min(9, priority)), tenant_id))
        hid = cur.fetchone()[0]
        c.commit()
    return hid


def request_collaborator(requester, need_role, title, priority=5):
    """The core decision: reuse an existing agent, ask the controller to spawn one, or flag an
    uncovered role. Returns a dict describing what happened."""
    _ensure()
    role = _role_of(requester)
    # anti-circumvention: never route a gated action the requester itself may not perform.
    blocked = _delegation_block(role, title)
    if blocked:
        return {"action": "delegation_denied", "capability": blocked,
                "note": f"role '{role}' may not '{blocked}' (manifest), so it cannot delegate it"}
    active = directory.find(role=need_role)          # active instances of the needed role
    if active:
        # reuse: pick the least-loaded instance; spawn-more only if all are overloaded
        active.sort(key=lambda a: _pending_count(a["agent_id"]))
        chosen = active[0]
        if _pending_count(chosen["agent_id"]) < OVERLOAD:
            tid = enqueue(chosen["agent_id"], title, priority, requester, need_role)
            directory.contact(requester, chosen["agent_id"], "task", title)   # direct brokered message
            return {"action": "routed_to_existing", "assignee": chosen["agent_id"], "task_id": tid}
        # scaling out is a HIRE — gate it on can_request_hire (real action site for request_hire).
        if not governance.may(role, "request_hire"):
            return {"action": "hire_denied", "capability": "request_hire",
                    "note": f"role '{role}' may not request_hire (manifest)"}
        hid = file_hire(requester, need_role, f"all {need_role} instances overloaded", title, priority)
        return {"action": "hire_requested_overloaded", "hire_id": hid}
    if need_role in known_roles():
        if not governance.may(role, "request_hire"):
            return {"action": "hire_denied", "capability": "request_hire",
                    "note": f"role '{role}' may not request_hire (manifest)"}
        hid = file_hire(requester, need_role, f"no active {need_role} — needs spawn", title, priority)
        return {"action": "hire_requested_spawn", "hire_id": hid}
    near = nearest_role(need_role)
    if near:
        return {"action": "no_exact_role_use_nearest", "suggested_role": near}
    return {"action": "no_role", "note": f"no role covers '{need_role}' — escalate to resource-allocator/human to create one"}


def fulfill(hire_id, agent_id):
    """Controller-only: spawn (register a fresh instance in the directory) + route its queued task."""
    _ensure()
    governance.enforce("controller", "spawn")   # only the controller may spawn (manifest invariant)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT requester, need_role, reason, title, priority FROM hire_requests
                       WHERE id=%s AND status='open'""", (hire_id,))
        row = cur.fetchone()
        if not row:
            return {"error": "no such open hire request"}
        requester, need_role, reason, title, priority = row
        cur.execute("UPDATE hire_requests SET status='fulfilled' WHERE id=%s", (hire_id,))
        c.commit()
    directory.register(agent_id, need_role, None, "available", [])
    # Route the queued task that motivated this hire to the freshly-spawned instance. Without this the
    # task is dropped: the hire is closed and the agent registered, but the work never reaches its queue.
    task_id = None
    if title:
        task_id = enqueue(agent_id, title, priority if priority is not None else 5, requester, need_role)
        directory.contact(requester, agent_id, "task", title)   # brokered hand-off, same as direct routing
    return {"action": "spawned", "agent_id": agent_id, "role": need_role, "for": requester,
            "task_id": task_id, "routed_title": title}


def _main(a):
    if not a:
        sys.exit("usage: orchestrate.py request|queue|next|hires|fulfill|selftest ...")
    import json
    if a[0] == "request":
        print(json.dumps(request_collaborator(a[1], a[2], a[3], int(a[4]) if len(a) > 4 else 5), indent=2))
    elif a[0] == "queue":
        for t in queue_of(a[1]):
            print(f"  [p{t['priority']}] #{t['id']} {t['status']:7} {t['title']}")
    elif a[0] == "next":
        print(json.dumps(next_task(a[1]), indent=2))
    elif a[0] == "hires":
        _ensure()
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT id, requester, need_role, reason FROM hire_requests WHERE status='open' ORDER BY id")
            for i, r, nr, rs in cur.fetchall():
                print(f"  hire#{i}  {r} needs {nr}  ({rs})")
    elif a[0] == "fulfill":
        print(json.dumps(fulfill(int(a[1]), a[2]), indent=2))
    elif a[0] == "selftest":
        _ensure()
        import os
        suf = os.urandom(3).hex()
        dev = f"builder@app-{suf}"
        legal = f"legal-compliance-regional@{suf}"
        # Case A: legal already active -> route directly
        directory.register(legal, "legal-compliance-regional", None, "available", [])
        r1 = request_collaborator(dev, "legal-compliance-regional", "review GDPR impact of new export", priority=5)
        # prioritization: a higher-priority task jumps the queue
        enqueue(legal, "URGENT: breach disclosure review", priority=1, requester=dev)
        top = next_task(legal)
        # Case B: no instance active -> hire request -> controller spawns
        directory.release(legal)
        # TEST ISOLATION: a leftover active 'tax-advisor' (a prior run that didn't release it, or the live
        # fleet/scheduler) makes request_collaborator REUSE instead of HIRE, flaking this case. Guarantee a
        # clean slate for the target role so Case B deterministically exercises the hire path.
        with psycopg.connect(DB) as _c, _c.cursor() as _cur:
            _cur.execute("UPDATE directory SET status='released' WHERE role='tax-advisor' AND status='active'")
            _c.commit()
        r2 = request_collaborator(dev, "tax-advisor", "review sales-tax nexus", priority=4)
        # Case B': controller fulfills the spawn — the queued task must reach the new agent's queue
        # (regression guard for #7: the task used to be dropped on the spawn path).
        tax = f"tax-advisor@{suf}"
        ful = fulfill(r2["hire_id"], tax)
        routed = next_task(tax)
        # Case C: uncovered role
        r3 = request_collaborator(dev, "astrophysicist", "model orbital decay", priority=5)
        ok = (r1["action"] == "routed_to_existing" and top["priority"] == 1
              and r2["action"] == "hire_requested_spawn"
              and ful["action"] == "spawned" and ful["routed_title"] == "review sales-tax nexus"
              and routed is not None and routed["title"] == "review sales-tax nexus" and routed["priority"] == 4
              and r3["action"] in ("no_role", "no_exact_role_use_nearest"))
        with psycopg.connect(DB) as c, c.cursor() as cur:   # self-clean so test data doesn't accumulate
            cur.execute("DELETE FROM hire_requests WHERE requester=%s", (dev,))
            cur.execute("DELETE FROM tasks WHERE assignee IN (%s,%s) OR requester=%s", (legal, tax, dev))
            cur.execute("DELETE FROM directory WHERE agent_id IN (%s,%s,%s)", (legal, dev, tax))
            c.commit()
        print(f"A route-to-existing: {r1['action']}; priority pull: p{top['priority']} first; "
              f"B spawn-request: {r2['action']}; B' fulfilled->routed: {ful['routed_title']!r}; C uncovered: {r3['action']}")
        print("PASS: reuse-vs-spawn routing + priority queue + hire-then-route flow + uncovered-role ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
