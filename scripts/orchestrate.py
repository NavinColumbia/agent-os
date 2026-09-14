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
import hashlib
import json
import threading
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import directory   # noqa: E402
import governance  # noqa: E402  (the read side of the governance manifest: may()/enforce())
from dbpool import connection, tenant_connection  # noqa: E402

ROLES_DIR = Path.home() / "projects" / "control-plane" / "roles"
OVERLOAD = 3   # an agent with this many pending tasks is considered overloaded
_ensured = False
_ensure_lock = threading.Lock()

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


def _conn(tenant_id=None):
    return tenant_connection(tenant_id) if tenant_id else connection()


def _agent_product(agent_id):
    raw = str(agent_id or "")
    return raw.split("@", 1)[1] if "@" in raw else None


def _tenant_for_product(product):
    if not product:
        return None
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT tenant_id FROM tenant_products WHERE product=%s ORDER BY created_at DESC LIMIT 1",
                        (product,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _tenant_for_requester(requester):
    raw = str(requester or "")
    if raw.startswith("finding:"):
        source = raw.split(":", 1)[1]
        if ":" in source:
            prefix, product = source.split(":", 1)
            if prefix in {"qa", "qa-agentic", "audit"}:
                return _tenant_for_product(product)
    return _tenant_for_product(_agent_product(raw))


def _required_tenant(tenant_id, *, assignee=None, requester=None):
    tenant = (tenant_id or _tenant_for_product(_agent_product(assignee))
              or _tenant_for_requester(requester))
    if not tenant:
        raise ValueError("tenant_id is required; pass '_platform' explicitly for operator work")
    return str(tenant)


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


def _ensure_schema():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS tasks (id BIGSERIAL PRIMARY KEY, assignee TEXT NOT NULL,
                       requester TEXT, role TEXT, title TEXT NOT NULL, priority INT NOT NULL DEFAULT 5,
                       status TEXT NOT NULL DEFAULT 'pending', tenant_id TEXT NOT NULL DEFAULT '_platform',
                       idempotency_key TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
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
        cur.execute("ALTER TABLE hire_requests ADD COLUMN IF NOT EXISTS idempotency_key TEXT")
        cur.execute("ALTER TABLE hire_requests ADD COLUMN IF NOT EXISTS fulfilled_agent_id TEXT")
        cur.execute("ALTER TABLE hire_requests ADD COLUMN IF NOT EXISTS priority INT NOT NULL DEFAULT 5")
        cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '_platform'")
        cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS idempotency_key TEXT")
        cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS tasks_tenant_idempotency_uidx
                       ON tasks(tenant_id,idempotency_key) WHERE idempotency_key IS NOT NULL""")
        cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hire_requests_tenant_idempotency_uidx
                       ON hire_requests(tenant_id,idempotency_key) WHERE idempotency_key IS NOT NULL""")
        cur.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS idempotency_key TEXT")
        cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS conversations_tenant_idempotency_uidx
                       ON conversations(tenant_id,idempotency_key) WHERE idempotency_key IS NOT NULL""")
        c.commit()


def _ensure():
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        _ensure_schema()
        _ensured = True


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


def _enqueue(cur, tenant_id, assignee, title, priority=5, requester=None, role=None,
             idempotency_key=None):
    cur.execute("""INSERT INTO tasks
        (tenant_id,assignee,requester,role,title,priority,idempotency_key)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (tenant_id,idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
        RETURNING id""", (tenant_id, assignee, requester, role, title,
                           max(1, min(9, int(priority))), idempotency_key))
    row = cur.fetchone()
    if row:
        return row[0], True
    cur.execute("SELECT id FROM tasks WHERE tenant_id=%s AND idempotency_key=%s",
                (tenant_id, idempotency_key))
    row = cur.fetchone()
    if not row:
        raise RuntimeError("idempotent task insert did not converge")
    return row[0], False


def enqueue(assignee, title, priority=5, requester=None, role=None, tenant_id=None,
            idempotency_key=None):
    _ensure()
    tenant_id = _required_tenant(tenant_id, assignee=assignee, requester=requester)
    with _conn(tenant_id) as c, c.cursor() as cur:
        tid, _ = _enqueue(cur, tenant_id, assignee, title, priority, requester, role, idempotency_key)
    return tid


def queue_of(assignee, tenant_id=None):
    _ensure()
    tenant_id = _required_tenant(tenant_id, assignee=assignee)
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""SELECT id, title, priority, status FROM tasks
                       WHERE tenant_id=%s AND assignee=%s AND status<>'done'
                       ORDER BY priority, id""", (tenant_id, assignee))
        return [{"id": i, "title": t, "priority": p, "status": s} for i, t, p, s in cur.fetchall()]


def next_task(assignee, tenant_id=None):
    """Pull the highest-priority pending task (lock-free concurrent-safe), mark it active."""
    _ensure()
    tenant_id = _required_tenant(tenant_id, assignee=assignee)
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""SELECT id, title, priority FROM tasks
                       WHERE tenant_id=%s AND assignee=%s AND status='pending'
                       ORDER BY priority, id FOR UPDATE SKIP LOCKED LIMIT 1""", (tenant_id, assignee))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("UPDATE tasks SET status='active' WHERE tenant_id=%s AND id=%s",
                    (tenant_id, row[0]))
        return {"id": row[0], "title": row[1], "priority": row[2]}


def complete(task_id, tenant_id=None):
    tenant_id = _required_tenant(tenant_id)
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("UPDATE tasks SET status='done' WHERE tenant_id=%s AND id=%s",
                    (tenant_id, task_id))
        if cur.rowcount != 1:
            raise PermissionError("task does not belong to tenant")


def _pending_count(assignee, tenant_id):
    tenant_id = _required_tenant(tenant_id, assignee=assignee)
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""SELECT count(*) FROM tasks
                       WHERE tenant_id=%s AND assignee=%s AND status<>'done'""",
                    (tenant_id, assignee))
        return cur.fetchone()[0]


def file_hire(requester, need_role, reason, title=None, priority=5, tenant_id=None,
              idempotency_key=None):
    """File a hire request. The motivating task (title + priority + requester) is persisted on the
    row so fulfill() can route it to the freshly-spawned instance — otherwise the work is silently
    dropped on the spawn path. tenant_id scopes it to the owning company for approvals.inbox;
    operator work must explicitly use ``_platform``."""
    _ensure()
    tenant_id = _required_tenant(tenant_id, requester=requester)
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO hire_requests
            (requester,need_role,reason,title,priority,tenant_id,idempotency_key)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (tenant_id,idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
            RETURNING id""", (requester, need_role, reason, title,
                               max(1, min(9, int(priority))), tenant_id, idempotency_key))
        row = cur.fetchone()
        if row:
            hid = row[0]
        else:
            cur.execute("SELECT id FROM hire_requests WHERE tenant_id=%s AND idempotency_key=%s",
                        (tenant_id, idempotency_key))
            row = cur.fetchone()
            if not row:
                raise RuntimeError("idempotent hire insert did not converge")
            hid = row[0]
    return hid


def _task_message(cur, tenant_id, requester, assignee, title, idempotency_key):
    message_key = f"{idempotency_key}:message" if idempotency_key else None
    if not message_key:
        return None
    message_id = "dm-" + hashlib.sha256(f"{tenant_id}:{message_key}".encode()).hexdigest()[:24]
    cur.execute("""INSERT INTO conversations
        (conversation_id,message_id,intent,sender,recipient,content,tenant_id,idempotency_key)
        VALUES (%s,%s,'task',%s,%s,%s,%s,%s)
        ON CONFLICT (tenant_id,idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
        RETURNING message_id""", (f"dm-{requester}-{assignee}", message_id, requester, assignee,
                                   json.dumps({"text": title}), tenant_id, message_key))
    created = cur.fetchone()
    if created:
        cur.execute("""INSERT INTO inbox(subscriber,message_id,tenant_id)
                       VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (assignee, message_id, tenant_id))
    return message_id


def request_collaborator(requester, need_role, title, priority=5, tenant_id=None,
                         idempotency_key=None):
    """The core decision: reuse an existing agent, ask the controller to spawn one, or flag an
    uncovered role. Returns a dict describing what happened."""
    _ensure()
    tenant_id = _required_tenant(tenant_id, requester=requester)
    role = _role_of(requester)
    # anti-circumvention: never route a gated action the requester itself may not perform.
    blocked = _delegation_block(role, title)
    if blocked:
        return {"action": "delegation_denied", "capability": blocked,
                "note": f"role '{role}' may not '{blocked}' (manifest), so it cannot delegate it"}
    active = directory.find(role=need_role, tenant_id=tenant_id)
    if active:
        # reuse: pick the least-loaded instance; spawn-more only if all are overloaded
        active.sort(key=lambda a: _pending_count(a["agent_id"], tenant_id))
        chosen = active[0]
        if _pending_count(chosen["agent_id"], tenant_id) < OVERLOAD:
            with _conn(tenant_id) as c, c.cursor() as cur:
                tid, _created = _enqueue(cur, tenant_id, chosen["agent_id"], title, priority,
                                         requester, need_role, idempotency_key)
                _task_message(cur, tenant_id, requester, chosen["agent_id"], title,
                              idempotency_key)
            if not idempotency_key:
                directory.contact(requester, chosen["agent_id"], "task", title,
                                  tenant_id=tenant_id)
            return {"action": "routed_to_existing", "assignee": chosen["agent_id"], "task_id": tid}
        # scaling out is a HIRE — gate it on can_request_hire (real action site for request_hire).
        if not governance.may(role, "request_hire"):
            return {"action": "hire_denied", "capability": "request_hire",
                    "note": f"role '{role}' may not request_hire (manifest)"}
        hid = file_hire(requester, need_role, f"all {need_role} instances overloaded", title, priority,
                        tenant_id=tenant_id, idempotency_key=idempotency_key)
        return {"action": "hire_requested_overloaded", "hire_id": hid}
    if need_role in known_roles():
        if not governance.may(role, "request_hire"):
            return {"action": "hire_denied", "capability": "request_hire",
                    "note": f"role '{role}' may not request_hire (manifest)"}
        hid = file_hire(requester, need_role, f"no active {need_role} — needs spawn", title, priority,
                        tenant_id=tenant_id, idempotency_key=idempotency_key)
        return {"action": "hire_requested_spawn", "hire_id": hid}
    near = nearest_role(need_role)
    if near:
        return {"action": "no_exact_role_use_nearest", "suggested_role": near}
    return {"action": "no_role", "note": f"no role covers '{need_role}' — escalate to resource-allocator/human to create one"}


def fulfill(hire_id, agent_id, tenant_id=None):
    """Controller-only: spawn (register a fresh instance in the directory) + route its queued task."""
    _ensure()
    governance.enforce("controller", "spawn")   # only the controller may spawn (manifest invariant)
    tenant_id = _required_tenant(tenant_id)
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""SELECT requester,need_role,reason,title,priority,status,fulfilled_agent_id
                       FROM hire_requests WHERE tenant_id=%s AND id=%s FOR UPDATE""",
                    (tenant_id, hire_id))
        row = cur.fetchone()
        if not row:
            raise PermissionError("hire request does not belong to tenant")
        requester, need_role, reason, title, priority, status, fulfilled_agent_id = row
        if status == "fulfilled" and fulfilled_agent_id and fulfilled_agent_id != agent_id:
            raise ValueError("hire request was already fulfilled by another agent")
        if status not in {"open", "fulfilled"}:
            return {"error": f"hire request is {status}"}
        cur.execute("""UPDATE hire_requests SET status='fulfilled',fulfilled_agent_id=%s
                       WHERE tenant_id=%s AND id=%s""", (agent_id, tenant_id, hire_id))
    directory.register(agent_id, need_role, None, "available", [], tenant_id=tenant_id)
    # Route the queued task that motivated this hire to the freshly-spawned instance. Without this the
    # task is dropped: the hire is closed and the agent registered, but the work never reaches its queue.
    task_id = None
    if title:
        route_key = f"hire:{tenant_id}:{hire_id}:fulfill"
        with _conn(tenant_id) as c, c.cursor() as cur:
            task_id, _created = _enqueue(cur, tenant_id, agent_id, title,
                                         priority if priority is not None else 5,
                                         requester, need_role, route_key)
            _task_message(cur, tenant_id, requester, agent_id, title, route_key)
    return {"action": "spawned", "agent_id": agent_id, "role": need_role, "for": requester,
            "task_id": task_id, "routed_title": title}


def _main(a):
    if not a:
        sys.exit("usage: orchestrate.py request|queue|next|hires|fulfill|selftest ...")
    import json
    if a[0] == "request":
        print(json.dumps(request_collaborator(a[1], a[2], a[3],
                                              int(a[4]) if len(a) > 4 else 5,
                                              tenant_id=a[5] if len(a) > 5 else "_platform"), indent=2))
    elif a[0] == "queue":
        for t in queue_of(a[1], tenant_id=a[2] if len(a) > 2 else "_platform"):
            print(f"  [p{t['priority']}] #{t['id']} {t['status']:7} {t['title']}")
    elif a[0] == "next":
        print(json.dumps(next_task(a[1], tenant_id=a[2] if len(a) > 2 else "_platform"), indent=2))
    elif a[0] == "hires":
        _ensure()
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT id, requester, need_role, reason FROM hire_requests WHERE status='open' ORDER BY id")
            for i, r, nr, rs in cur.fetchall():
                print(f"  hire#{i}  {r} needs {nr}  ({rs})")
    elif a[0] == "fulfill":
        print(json.dumps(fulfill(int(a[1]), a[2], a[3] if len(a) > 3 else "_platform"), indent=2))
    elif a[0] == "selftest":
        _ensure()
        import os
        import billing
        suf = os.urandom(3).hex()
        dev = f"builder@app-{suf}"
        legal = f"legal-compliance-regional@{suf}"
        tax = f"tax-advisor@{suf}"
        tenant_id = billing.signup(f"orchestrate-selftest-{suf}", "free")["tenant_id"]
        tenant_prod = f"orchestrate-prod-{suf}"
        tenant_dev = f"builder@{tenant_prod}"
        tenant_hire_scoped = False
        ok = False
        # EVERYTHING below registers synthetic agents in the LIVE directory and enqueues fixture tasks in
        # the LIVE tasks table, so cleanup MUST be in a finally. It used to sit on the happy path: one
        # crash (see the hire_id KeyError above) left 'tax-advisor@<suf>' active forever, which made the
        # NEXT run route to it instead of hiring, which crashed the same way — a compounding leak that
        # dispatched real agent sessions onto fictitious legal work ("URGENT: breach disclosure review").
        try:
            # Case A: legal already active -> route directly
            directory.register(legal, "legal-compliance-regional", None, "available", [],
                               tenant_id="_platform")
            r1 = request_collaborator(dev, "legal-compliance-regional",
                                      "review GDPR impact of new export", priority=5,
                                      tenant_id="_platform")
            # prioritization: a higher-priority task jumps the queue
            enqueue(legal, "URGENT: breach disclosure review", priority=1, requester=dev,
                    tenant_id="_platform")
            top = next_task(legal, tenant_id="_platform")
            # Case B: no instance active -> hire request -> controller spawns
            directory.release(legal)
            # TEST ISOLATION: a leftover active 'tax-advisor' (a prior run that didn't release it, or the live
            # fleet/scheduler) makes request_collaborator REUSE instead of HIRE, flaking this case. Guarantee a
            # clean slate for the target role so Case B deterministically exercises the hire path.
            with _conn() as _c, _c.cursor() as _cur:
                _cur.execute("UPDATE directory SET status='released' WHERE role='tax-advisor' AND status='active'")
                _c.commit()
            r2 = request_collaborator(dev, "tax-advisor", "review sales-tax nexus", priority=4,
                                      tenant_id="_platform")
            # Case B': controller fulfills the spawn — the queued task must reach the new agent's queue
            # (regression guard for #7: the task used to be dropped on the spawn path).
            # r2 only carries hire_id on the SPAWN path. If a stale active tax-advisor survives (exactly what
            # this test used to leak), request_collaborator returns routed_to_existing instead, whose dict has
            # no hire_id — the bare r2["hire_id"] then raised KeyError and killed the run BEFORE cleanup, so
            # every subsequent run leaked more and routed fixture work into the leaked agent's queue. Fail the
            # assertion honestly instead of exploding mid-test.
            ful = fulfill(r2["hire_id"], tax, tenant_id="_platform") if r2.get("hire_id") else {
                "action": r2.get("action"), "routed_title": None}
            routed = next_task(tax, tenant_id="_platform")
            # Case C: uncovered role
            r3 = request_collaborator(dev, "astrophysicist", "model orbital decay", priority=5,
                                      tenant_id="_platform")
            with _conn() as c, c.cursor() as cur:
                cur.execute("UPDATE directory SET status='released' WHERE role='tax-advisor' AND status='active'")
                cur.execute("""INSERT INTO tenant_products (product, tenant_id)
                               VALUES (%s,%s) ON CONFLICT DO NOTHING""", (tenant_prod, tenant_id))
            rh = request_collaborator(tenant_dev, "tax-advisor",
                                      "review tenant sales-tax nexus", priority=3)
            with _conn(tenant_id) as c, c.cursor() as cur:
                cur.execute("SELECT tenant_id FROM hire_requests WHERE id=%s", (rh.get("hire_id"),))
                hrow = cur.fetchone()
            tenant_hire_scoped = bool(rh.get("action") == "hire_requested_spawn" and hrow
                                      and hrow[0] == tenant_id)
            ok = (r1["action"] == "routed_to_existing" and top["priority"] == 1
                  and r2["action"] == "hire_requested_spawn"
                  and ful["action"] == "spawned" and ful["routed_title"] == "review sales-tax nexus"
                  and routed is not None and routed["title"] == "review sales-tax nexus" and routed["priority"] == 4
                  and r3["action"] in ("no_role", "no_exact_role_use_nearest")
                  and tenant_hire_scoped)
        finally:
            # Always reachable: purge this run's synthetic agents, its queued fixture work, and any hire
            # request it filed — keyed on the ids/requester this run owns, so it can never touch real rows.
            try:
                with _conn() as c, c.cursor() as cur:
                    cur.execute("DELETE FROM hire_requests WHERE requester IN (%s,%s)", (dev, tenant_dev))
                    cur.execute("DELETE FROM tasks WHERE assignee IN (%s,%s) OR requester IN (%s,%s)",
                                (legal, tax, dev, tenant_dev))
                    cur.execute("DELETE FROM directory WHERE agent_id IN (%s,%s,%s)", (legal, dev, tax))
                    cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tenant_id,))
                    cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tenant_id,))
                    c.commit()
            except Exception:
                pass
        print(f"A route-to-existing: {r1['action']}; priority pull: p{top['priority']} first; "
              f"B spawn-request: {r2['action']}; B' fulfilled->routed: {ful['routed_title']!r}; "
              f"C uncovered: {r3['action']}; tenant_hire_scoped={tenant_hire_scoped}")
        print("PASS: reuse-vs-spawn routing + priority queue + hire-then-route flow + uncovered-role ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
