#!/usr/bin/env python3
"""orgview.py — the tenant's AGENT ORG CHART: the CEO's "here is my staff and who reports to whom".

The cockpit shows the factory floor (products, spend, queue). This is the org side: the reporting
TREE of the AI-agent company. A static role hierarchy — a CONTROLLER at the top, with functional
leads/specialists reporting to it — OVERLAID with the tenant's LIVE agents from the `directory`
table (which roles are actually active on this tenant's products right now, their status + current
task). So the owner sees both the intended org and who is really on the clock.

  orgchart(tid) -> {"tree":[{role,title,reports_to,live,status,task,count}...], "org_runs":[...]}
                   every static node, marked live with the count of that role's active instances +
                   most recent task — PLUS the REAL org (REBUILD-PLAN A1/B2): the tenant's recent
                   orchestra runs rendered from store.org_tree(), i.e. actually-hired agents with
                   names, roles, status, tenure and heartbeat age from real spawn data.
  org_runs(tid) -> just that real-spawn-data view (the durable store's nested org trees).
  roster(tid)   -> the flat list of the tenant's currently-live agents.

    orgview.py json <tenant_id>     # the org chart payload on the CLI
    orgview.py selftest
Run with the agent-os venv python. NO web server.
"""
import json
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "orchestra"))
import audit  # noqa: E402  (audit-trail the org reads, same convention as the other surfaces)
import store  # noqa: E402  (the durable org: real hired agents with identity/tenure/heartbeats)

from aoscfg import ENV, DB
ACTIVE_WINDOW = "15 minutes"

# The static org chart of the AI-agent company: a controller at top, functional leads/specialists
# reporting to it. (role, title, reports_to). Overlaid with the tenant's live `directory` agents.
ORG = [
    ("controller",       "Controller (chief of staff)",   None),
    ("planner",          "Planner / Architect",           "controller"),
    ("builder",          "Builder (backend/frontend)",    "controller"),
    ("qa",               "QA",                            "controller"),
    ("security",         "Security",                      "controller"),
    ("reviewer",         "Reviewer",                      "controller"),
    ("marketing-growth", "Marketing — Growth",            "controller"),
    ("research-growth",  "Research — Growth",             "controller"),
    ("devops",           "DevOps",                        "controller"),
]


def _products(cur, tid):
    cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s", (tid,))
    return [r[0] for r in cur.fetchall()]


def _live_by_role(cur, products):
    """Active directory agents on the tenant's products (15-min window), grouped by role:
    {role: {"count":N, "status":..., "task":...}} with the MOST RECENT instance's status/task."""
    if not products:
        return {}
    cur.execute(f"""SELECT role, status, task, updated_at FROM directory
                    WHERE product = ANY(%s) AND status='active'
                      AND updated_at > now() - interval '{ACTIVE_WINDOW}'
                    ORDER BY updated_at DESC""", (products,))
    by_role = {}
    for role, status, task, _ in cur.fetchall():
        r = by_role.setdefault(role, {"count": 0, "status": None, "task": None})
        if r["count"] == 0:               # rows are newest-first, so first seen = most recent
            r["status"], r["task"] = status, task
        r["count"] += 1
    return by_role


def org_runs(tid, limit=3):
    """THE REAL ORG (A1 seam, feeds B2): the tenant's most recent orchestra runs, each rendered
    from store.org_tree() — a nested tree of ACTUALLY-HIRED agents (Postgres rows from real spawn
    data) with name, control-plane role, status, assignment, tenure_s and last_active_age_s.
    Best-effort: an unreachable store yields [] rather than killing the whole chart."""
    try:
        runs = store.runs_for(tid, limit=limit)
        return [{"run": r, **store.org_tree(r["run_id"], tid)} for r in runs]
    except Exception:
        return []


def message_agent(tid, actor_id, text):
    """B2 — the CEO messages a SPECIFIC hired agent ("ask my CTO"). Validates the actor belongs to this
    tenant, then drops a durable event into that actor's inbox (context_update from CEO) — the runtime's
    decide-loop folds it into the agent's work. Kill-switch-gated at the store layer."""
    try:
        import store
        a = store.actor(int(actor_id), tid)               # tenant-scoped -> None if not this CEO's agent
    except Exception:
        a = None
    if not a:
        return {"error": "no such agent for this company"}
    if not (text or "").strip():
        return {"error": "empty message"}
    try:
        import store
        store.emit(a["run_id"], tid, None, a["actor_id"], "context_update",
                   {"from": "CEO", "text": text[:1000]})
        return {"ok": True, "delivered_to": a.get("name"), "role": a.get("role")}
    except Exception as e:
        return {"error": f"could not deliver: {e}"}


def orgchart(tid):
    """Every node of the static hierarchy, marked live + with the count of that role's active
    instances on the tenant's products and the most recent task. Root reports_to=null.
    PLUS org_runs: the durable store's real spawned-agent trees (see org_runs above)."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        products = _products(cur, tid)
        by_role = _live_by_role(cur, products)
    tree = []
    for role, title, reports_to in ORG:
        live = by_role.get(role)
        tree.append({
            "role": role,
            "title": title,
            "reports_to": reports_to,
            "live": live is not None,
            "status": live["status"] if live else None,
            "task": live["task"] if live else None,
            "count": live["count"] if live else 0,
        })
    return {"tenant": tid, "tree": tree, "org_runs": org_runs(tid)}


def roster(tid):
    """Flat list of the tenant's currently-live agents on their products."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        products = _products(cur, tid)
        if not products:
            return []
        cur.execute(f"""SELECT agent_id, role, status, task, product FROM directory
                        WHERE product = ANY(%s) AND status='active'
                          AND updated_at > now() - interval '{ACTIVE_WINDOW}'
                        ORDER BY updated_at DESC""", (products,))
        return [{"agent_id": a, "role": r, "status": s, "task": t, "product": p}
                for a, r, s, t, p in cur.fetchall()]


def _selftest():
    """Real tenant + product + a live directory agent; prove the org chart overlays live status."""
    import billing  # noqa: E402
    reg = billing.signup("orgview-selftest", "free")     # a REAL tenant (tenant_products FK -> tenants)
    tid = reg["tenant_id"]
    prod = tid.replace("t-", "")[:6] + "-org"
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (prod, tid))
        cur.execute("""INSERT INTO directory (agent_id, role, status, product, task, updated_at)
                       VALUES (%s,'builder','active',%s,'shipping the API', now())
                       ON CONFLICT (agent_id) DO UPDATE SET role=EXCLUDED.role, status='active',
                         product=EXCLUDED.product, task=EXCLUDED.task, updated_at=now()""", (f"builder@{prod}", prod))
        c.commit()
    # Seed a REAL orchestra run for this tenant (durable store rows: hired controller -> supervisor
    # -> worker) so the chart's org_runs section renders actual spawn data, not the static list.
    orc = store.start_run(tid, "grow the creator platform")["run_id"]
    s_ctrl = store.spawn_actor(orc, tid, "controller", "controller", kind="controller")
    s_sup = store.spawn_actor(orc, tid, "research-supervisor", "research-growth", kind="supervisor",
                              supervisor_id=s_ctrl["actor_id"], assignment="lead the research")
    store.spawn_actor(orc, tid, "researcher-01", "research-growth", kind="worker",
                      supervisor_id=s_sup["actor_id"], assignment="market sizing")
    try:
        oc = orgchart(tid)
        tree = oc["tree"]
        root = next((n for n in tree if n["reports_to"] is None), None)
        builder = next((n for n in tree if n["role"] == "builder"), None)
        rs = roster(tid)
        ok = (root is not None and root["role"] == "controller"
              and len(tree) >= 6
              and builder is not None and builder["live"] is True and builder["count"] >= 1
              and any(x["agent_id"] == f"builder@{prod}" for x in rs))
        # REAL spawn data on the chart: the seeded run appears with the nested hired agents, each
        # carrying identity + status + tenure (what the B2 living-org UI will render).
        runs = oc.get("org_runs") or []
        node = runs[0]["tree"][0] if runs and runs[0].get("tree") else {}
        sup_node = (node.get("reports") or [{}])[0]
        real_ok = (len(runs) == 1 and runs[0]["run"]["run_id"] == orc and runs[0]["actors"] == 3
                   and node.get("name") == "controller" and node.get("status") == "idle"
                   and isinstance(node.get("tenure_s"), int)
                   and sup_node.get("name") == "research-supervisor"
                   and sup_node.get("role") == "research-growth"
                   and (sup_node.get("reports") or [{}])[0].get("name") == "researcher-01")
        ok = ok and real_ok
        print(f"root={root['role'] if root else None}(reports_to={root['reports_to'] if root else '?'}) "
              f"nodes={len(tree)} builder.live={builder['live'] if builder else None} "
              f"builder.count={builder['count'] if builder else 0} roster={len(rs)} "
              f"org_runs(real spawn data)={real_ok}(runs={len(runs)},actors={runs[0]['actors'] if runs else 0})")
        print("PASS: org chart — static hierarchy overlaid with live directory agents + REAL "
              "orchestra org trees (names/roles/status/tenure from spawn rows) ✅" if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM directory WHERE product=%s", (prod,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM orchestra_actors WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM orchestra_runs WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "json" and len(a) > 1:
        print(json.dumps({"orgchart": orgchart(a[1]), "roster": roster(a[1])}, indent=2))
    else:
        sys.exit("usage: orgview.py json <tenant_id> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
