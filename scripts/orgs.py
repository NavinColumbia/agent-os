#!/usr/bin/env python3
"""orgs.py — ORGANIZATIONS as first-class: a CEO runs MANY orgs (could be 100), each its own company.

The grand vision: one user is a CEO who spins up many orgs ("a YouTube competitor", "an invoicing SaaS"),
each with its own controller, context (research/design/dev/test), products, threads, and budget — plus
cross-org operations later ("merge these two", "port a feature from another org I built"). Everything used
to be keyed by tenant_id only, with no org layer. This is that layer: orgs owned by a tenant, each with a
vision and lifecycle, so per-org context and cross-org views become possible. Products/threads/findings get
an optional org_id (added idempotently) so existing single-org data keeps working (org_id NULL = default).

    orgs.py create <tenant> "<name>" ["vision"]
    orgs.py list <tenant>
    orgs.py get <tenant> <org_id>
    orgs.py vision <tenant> <org_id> "<vision>"
    orgs.py selftest
Run with the agent-os venv python.
"""
import sys
import threading
from pathlib import Path

from dbpool import connection, tenant_connection  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit   # noqa: E402

_ensured = False
_ensure_lock = threading.Lock()

def _ensure():
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        with connection() as c, c.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout='2s'")
            cur.execute("SET LOCAL statement_timeout='10s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS orgs (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL,
                vision TEXT DEFAULT '', stage TEXT NOT NULL DEFAULT 'new',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            cur.execute("CREATE INDEX IF NOT EXISTS orgs_tenant_idx ON orgs (tenant_id, status)")
            # org_artifacts is canonicalized by migration 79. Keep the owner-side
            # bootstrap for rolling upgrades, but never issue schema DDL through a
            # tenant/app-role transaction at the write callsite.
            cur.execute("""CREATE TABLE IF NOT EXISTS org_artifacts (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT, org_id BIGINT NOT NULL,
                kind TEXT NOT NULL, product TEXT, path TEXT, ref TEXT, summary TEXT,
                created_at TIMESTAMPTZ DEFAULT now())""")
            cur.execute("ALTER TABLE org_artifacts ADD COLUMN IF NOT EXISTS tenant_id TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS org_artifacts_tenant_id_rls_idx ON org_artifacts (tenant_id)")
            # Give existing org-scoped tables an optional org_id (NULL = the
            # tenant's default/legacy org), idempotently.
            for tbl in ("tenant_products", "chat_threads", "findings", "custom_agents"):
                cur.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS org_id BIGINT")
        _ensured = True


def create(tenant_id, name, vision=""):
    _ensure()
    name = (name or "").strip()
    if not name:
        return {"error": "org name required"}
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("INSERT INTO orgs (tenant_id, name, vision) VALUES (%s,%s,%s) RETURNING id",
                    (tenant_id, name[:120], vision))
        oid = cur.fetchone()[0]
    audit.append(actor="orgs", action="OrgCreated", resource=str(oid), decision="created",
                 payload={"tenant": tenant_id, "name": name[:80]}, tenant_id=tenant_id)
    return {"org_id": oid, "name": name, "stage": "new"}


def list_orgs(tenant_id, include_archived=False):
    _ensure()
    q = """SELECT id, name, vision, stage, status, created_at,
                  (SELECT count(*) FROM tenant_products tp WHERE tp.org_id = o.id) AS products
           FROM orgs o WHERE tenant_id=%s {} ORDER BY created_at DESC"""
    q = q.format("" if include_archived else "AND status='active'")
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute(q, (tenant_id,))
        return [{"org_id": i, "name": n, "vision": v, "stage": st, "status": stat,
                 "created_at": str(ca), "products": p} for i, n, v, st, stat, ca, p in cur.fetchall()]


HOME_ORG = 0  # the account-wide "All orgs (home)" context; org=N is a specific company.


def switcher(tenant_id):
    """The org list shaped for the inline context switcher / Assistant home selector. Returns the home
    sentinel (org_id=0, the account-wide "All orgs (home)" thread) followed by the tenant's active
    companies (most-recent first), each compact: id, name, vision, stage, products. This is the ONE
    backend definition of the switcher options, so the frontend + assistant.py agree on what "home" is
    instead of hardcoding it. `default_org` is where a fresh session lands (the only/first company, or
    home when there are none); `count` excludes the home sentinel."""
    companies = [{"org_id": o["org_id"], "name": o["name"], "vision": o["vision"],
                  "stage": o["stage"], "products": o["products"]} for o in list_orgs(tenant_id)]
    home = {"org_id": HOME_ORG, "name": "All orgs (home)",
            "vision": "Ask across every company · start a new one", "stage": "home", "products": 0}
    return {"orgs": [home] + companies, "count": len(companies),
            "default_org": companies[0]["org_id"] if companies else HOME_ORG}


def resolve(tenant_id, org_id):
    """Resolve a switcher selection to its context. org_id=0 (HOME_ORG) is the account-wide home thread
    (no company precondition); org_id=N is a specific company, ownership-checked. This is the ONE place
    the org=0-vs-org=N dispatch is decided, so the route/assistant never re-hardcodes it. Non-owned or
    non-numeric N returns an error rather than leaking another tenant's org."""
    try:
        oid = int(org_id or 0)
    except (TypeError, ValueError):
        oid = HOME_ORG
    if oid == HOME_ORG:
        return {"home": True, "org_id": HOME_ORG, "name": "All orgs (home)"}
    g = get(tenant_id, oid)
    if g.get("error"):
        return {"home": False, "org_id": oid, "error": g["error"]}
    return {"home": False, "org_id": oid, "name": g["name"], "vision": g["vision"],
            "stage": g["stage"], "status": g["status"]}


def get(tenant_id, org_id):
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("SELECT id, name, vision, stage, status, created_at FROM orgs WHERE tenant_id=%s AND id=%s",
                    (tenant_id, org_id))
        r = cur.fetchone()
    if not r:
        return {"error": "not your org"}
    return {"org_id": r[0], "name": r[1], "vision": r[2], "stage": r[3], "status": r[4], "created_at": str(r[5])}


def _owned(cur, tenant_id, org_id):
    cur.execute("SELECT 1 FROM orgs WHERE tenant_id=%s AND id=%s", (tenant_id, org_id))
    return cur.fetchone() is not None


def _tenant_for_org(org_id):
    try:
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT tenant_id FROM orgs WHERE id=%s", (org_id,))
            row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        return None


def set_vision(tenant_id, org_id, vision):
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        if not _owned(cur, tenant_id, org_id):
            return {"error": "not your org"}
        cur.execute("UPDATE orgs SET vision=%s, updated_at=now() WHERE id=%s", (vision, org_id))
    try:                                  # mirror into the requirements keeper (org scope) so this company's
        import visionkeeper               # vague idea becomes build-ready requirements without the CEO restating
        visionkeeper.set_vision(tenant_id, vision, scope=f"org:{org_id}")   # it. Best-effort; no spend here.
    except Exception:
        pass
    return {"org_id": org_id, "vision": vision}


def set_stage(tenant_id, org_id, stage):
    """Advance the org through its lifecycle (new->researching->designing->building->live)."""
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        if not _owned(cur, tenant_id, org_id):
            return {"error": "not your org"}
        cur.execute("UPDATE orgs SET stage=%s, updated_at=now() WHERE id=%s", (stage, org_id))
    audit.append(actor="orgs", action="OrgStage", resource=str(org_id), decision=stage, tenant_id=tenant_id)
    return {"org_id": org_id, "stage": stage}


def archive(tenant_id, org_id):
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        if not _owned(cur, tenant_id, org_id):
            return {"error": "not your org"}
        cur.execute("UPDATE orgs SET status='archived', updated_at=now() WHERE id=%s", (org_id,))
    return {"org_id": org_id, "status": "archived"}


def record_artifact(org_id, kind, summary, product=None, path=None, ref=None, tenant_id=None):
    """Record a context artifact (research_report|plan|design|product_repo|spec|qa_report) for an org —
    the ONE place an org's full context is enumerable (enables context_brief + cross-org ops)."""
    _ensure()
    tenant_id = tenant_id or _tenant_for_org(org_id)
    with (tenant_connection(tenant_id) if tenant_id else connection()) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO org_artifacts (tenant_id, org_id, kind, product, path, ref, summary)
                       VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (tenant_id, org_id, kind, product, path, ref, summary))
        aid = cur.fetchone()[0]
    return {"artifact_id": aid}


def context_brief(tenant_id, org_id):
    """Compact per-org context fed to the controller — this org's vision, stage, products, and the latest
    research/plan/design artifacts. The per-org analogue of orchestrator's _state_brief."""
    g = get(tenant_id, org_id)
    if g.get("error"):
        return g["error"]
    arts = {}
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        try:
            cur.execute("""SELECT DISTINCT ON (kind) kind, summary FROM org_artifacts
                           WHERE org_id=%s ORDER BY kind, created_at DESC""", (org_id,))
            arts = {k: (s or "")[:200] for k, s in cur.fetchall()}
        except Exception:
            pass
        cur.execute("SELECT product FROM tenant_products WHERE org_id=%s", (org_id,))
        prods = [r[0] for r in cur.fetchall()]
    return (f"ORG: {g['name']} — vision: {g['vision'] or '(none yet)'} — stage: {g['stage']}. "
            f"Products: {', '.join(prods) or 'none'}. "
            f"Latest research: {arts.get('research_report','—')}. Latest plan: {arts.get('plan','—')}.")


def _selftest():
    import billing
    tid = billing.signup("orgs-selftest", "free")["tenant_id"]
    try:
        a = create(tid, "YouTube competitor", "a creator-first video platform")
        b = create(tid, "Invoicing SaaS")
        listed = list_orgs(tid)
        two = len(listed) == 2 and any(o["name"] == "YouTube competitor" for o in listed)
        set_vision(tid, a["org_id"], "v2 vision"); set_stage(tid, a["org_id"], "researching")
        g = get(tid, a["org_id"]); updated = g["vision"] == "v2 vision" and g["stage"] == "researching"
        guard = get("t-someone-else", a["org_id"]).get("error") == "not your org"
        archive(tid, b["org_id"]); after = len(list_orgs(tid)) == 1   # archived hidden
        sw = switcher(tid)
        sw_ok = (sw["orgs"][0]["org_id"] == HOME_ORG and sw["count"] == 1
                 and sw["default_org"] == a["org_id"])
        rz_ok = (resolve(tid, 0)["home"] is True
                 and resolve(tid, a["org_id"]).get("name") == "YouTube competitor"
                 and resolve("t-someone-else", a["org_id"]).get("error") == "not your org")
        art = record_artifact(a["org_id"], "research_report", "creators want rev-share", tenant_id=tid)
        brief = context_brief(tid, a["org_id"])
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT tenant_id FROM org_artifacts WHERE id=%s", (art["artifact_id"],))
            art_tid = cur.fetchone()[0]
        art_ok = art_tid == tid and "creators want rev-share" in brief
        ok = a["org_id"] and two and updated and guard and after and sw_ok and rz_ok and art_ok
        print(f"created=2 listed={len(listed)} vision/stage-updated={updated} ownership-guard={guard} "
              f"archive-hides={after} switcher={sw_ok} resolve={rz_ok} artifact_tenant={art_ok}")
        print("PASS: orgs are first-class per-tenant (create/list/vision/stage/archive, owner-scoped) ✅" if ok else "FAIL")
    finally:
        with connection() as c, c.cursor() as cur:
            cur.execute("""DELETE FROM org_artifacts WHERE org_id IN
                           (SELECT id FROM orgs WHERE tenant_id=%s)""", (tid,))
            cur.execute("DELETE FROM orgs WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "create" and len(a) >= 3:
        print(json.dumps(create(a[1], a[2], a[3] if len(a) > 3 else "")))
    elif a[0] == "list" and len(a) > 1:
        print(json.dumps(list_orgs(a[1]), indent=2))
    elif a[0] == "switcher" and len(a) > 1:
        print(json.dumps(switcher(a[1]), indent=2))
    elif a[0] == "resolve" and len(a) > 2:
        print(json.dumps(resolve(a[1], int(a[2])), indent=2))
    elif a[0] == "get" and len(a) > 2:
        print(json.dumps(get(a[1], int(a[2])), indent=2))
    elif a[0] == "vision" and len(a) > 3:
        print(json.dumps(set_vision(a[1], int(a[2]), a[3])))
    else:
        sys.exit('usage: orgs.py create <tenant> "<name>" ["vision"] | list <tenant> | switcher <tenant> | '
                 'resolve <tenant> <org_id> | get <tenant> <id> | vision ... | selftest')


if __name__ == "__main__":
    _main(sys.argv[1:])
