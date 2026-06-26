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
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit   # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS orgs (
            id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL,
            vision TEXT DEFAULT '', stage TEXT NOT NULL DEFAULT 'new',   -- new|researching|designing|building|live|archived
            status TEXT NOT NULL DEFAULT 'active',                        -- active|archived
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("CREATE INDEX IF NOT EXISTS orgs_tenant_idx ON orgs (tenant_id, status)")
        # give existing org-scoped tables an optional org_id (NULL = the tenant's default/legacy org),
        # idempotently — so single-org data keeps working while new data is org-scoped.
        for tbl in ("tenant_products", "chat_threads", "findings", "custom_agents"):
            try:
                cur.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS org_id BIGINT")
            except Exception:
                pass
        c.commit()


def create(tenant_id, name, vision=""):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO orgs (tenant_id, name, vision) VALUES (%s,%s,%s) RETURNING id",
                    (tenant_id, name[:120], vision))
        oid = cur.fetchone()[0]; c.commit()
    audit.append(actor="orgs", action="OrgCreated", resource=str(oid), decision="created",
                 payload={"tenant": tenant_id, "name": name[:80]})
    return {"org_id": oid, "name": name, "stage": "new"}


def list_orgs(tenant_id, include_archived=False):
    _ensure()
    q = """SELECT id, name, vision, stage, status, created_at,
                  (SELECT count(*) FROM tenant_products tp WHERE tp.org_id = o.id) AS products
           FROM orgs o WHERE tenant_id=%s {} ORDER BY created_at DESC"""
    q = q.format("" if include_archived else "AND status='active'")
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute(q, (tenant_id,))
        return [{"org_id": i, "name": n, "vision": v, "stage": st, "status": stat,
                 "created_at": str(ca), "products": p} for i, n, v, st, stat, ca, p in cur.fetchall()]


def get(tenant_id, org_id):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT id, name, vision, stage, status, created_at FROM orgs WHERE tenant_id=%s AND id=%s",
                    (tenant_id, org_id))
        r = cur.fetchone()
    if not r:
        return {"error": "not your org"}
    return {"org_id": r[0], "name": r[1], "vision": r[2], "stage": r[3], "status": r[4], "created_at": str(r[5])}


def _owned(cur, tenant_id, org_id):
    cur.execute("SELECT 1 FROM orgs WHERE tenant_id=%s AND id=%s", (tenant_id, org_id))
    return cur.fetchone() is not None


def set_vision(tenant_id, org_id, vision):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        if not _owned(cur, tenant_id, org_id):
            return {"error": "not your org"}
        cur.execute("UPDATE orgs SET vision=%s, updated_at=now() WHERE id=%s", (vision, org_id)); c.commit()
    return {"org_id": org_id, "vision": vision}


def set_stage(tenant_id, org_id, stage):
    """Advance the org through its lifecycle (new->researching->designing->building->live)."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        if not _owned(cur, tenant_id, org_id):
            return {"error": "not your org"}
        cur.execute("UPDATE orgs SET stage=%s, updated_at=now() WHERE id=%s", (stage, org_id)); c.commit()
    audit.append(actor="orgs", action="OrgStage", resource=str(org_id), decision=stage)
    return {"org_id": org_id, "stage": stage}


def archive(tenant_id, org_id):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        if not _owned(cur, tenant_id, org_id):
            return {"error": "not your org"}
        cur.execute("UPDATE orgs SET status='archived', updated_at=now() WHERE id=%s", (org_id,)); c.commit()
    return {"org_id": org_id, "status": "archived"}


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
        ok = a["org_id"] and two and updated and guard and after
        print(f"created=2 listed={len(listed)} vision/stage-updated={updated} ownership-guard={guard} archive-hides={after}")
        print("PASS: orgs are first-class per-tenant (create/list/vision/stage/archive, owner-scoped) ✅" if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM orgs WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "create" and len(a) >= 3:
        print(json.dumps(create(a[1], a[2], a[3] if len(a) > 3 else "")))
    elif a[0] == "list" and len(a) > 1:
        print(json.dumps(list_orgs(a[1]), indent=2))
    elif a[0] == "get" and len(a) > 2:
        print(json.dumps(get(a[1], int(a[2])), indent=2))
    elif a[0] == "vision" and len(a) > 3:
        print(json.dumps(set_vision(a[1], int(a[2]), a[3])))
    else:
        sys.exit('usage: orgs.py create <tenant> "<name>" ["vision"] | list <tenant> | get <tenant> <id> | vision ... | selftest')


if __name__ == "__main__":
    _main(sys.argv[1:])
