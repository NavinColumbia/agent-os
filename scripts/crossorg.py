#!/usr/bin/env python3
"""crossorg.py — CROSS-ORG operations: a CEO runs MANY orgs, and sometimes two should MERGE into one
combined product, or a feature built in one org should be PORTED ("stolen") into another.

These are consequential, owner-scoped, GOVERNED operations. Both the source and target org must belong
to the tenant. An op is proposed, then SCOPED (we read the source org's context — vision, products,
artifacts — to produce a lightweight plan of what to port/merge), then it goes to the human Approvals
inbox. Execution is deliberately a governed BOOKKEEPING + LINEAGE operation: it records that the target
was derived from the source (org_lineage) and files a 'spec' artifact on the target. It does NOT
autonomously move or rewrite another org's product files — that real code work stays human-approved and
is done by an approved, human-confirmed build step. We are honest about that boundary here.

    crossorg.py propose <tenant> <kind> <source_org> [target_org] [feature]   # kind: merge|steal_feature
    crossorg.py scope <op_id>
    crossorg.py request-approval <tenant> <op_id>
    crossorg.py execute <op_id> [--confirm]
    crossorg.py list <tenant>
    crossorg.py get <op_id>
    crossorg.py json <op_id>
    crossorg.py selftest
Run with the agent-os venv python.  Data/logic module only — binds no server.
"""
import json
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402
import orgs   # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS xorg_ops (
            id BIGSERIAL PRIMARY KEY, tenant_id TEXT, kind TEXT, source_org BIGINT, target_org BIGINT,
            feature TEXT, status TEXT DEFAULT 'proposed', plan JSONB, result JSONB,
            created_at TIMESTAMPTZ DEFAULT now())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS org_lineage (
            id BIGSERIAL PRIMARY KEY, org_id BIGINT, derived_from BIGINT, note TEXT,
            at TIMESTAMPTZ DEFAULT now())""")
        c.commit()


def _row(op_id):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id, tenant_id, kind, source_org, target_org, feature, status, plan, result,
                              created_at FROM xorg_ops WHERE id=%s""", (op_id,))
        r = cur.fetchone()
    if not r:
        return None
    return {"op_id": r[0], "tenant_id": r[1], "kind": r[2], "source_org": r[3], "target_org": r[4],
            "feature": r[5], "status": r[6], "plan": r[7], "result": r[8], "created_at": str(r[9])}


def propose(tenant_id, kind, source_org, target_org=None, feature=None):
    """Propose a cross-org op. BOTH orgs must be owned by the tenant (ownership-checked via orgs.get)."""
    _ensure()
    if kind not in ("merge", "steal_feature"):
        return {"error": "kind must be merge or steal_feature"}
    # ownership: source AND (if present) target must belong to this tenant.
    if orgs.get(tenant_id, source_org).get("error"):
        return {"error": "source_org not your org"}
    if target_org is not None and orgs.get(tenant_id, target_org).get("error"):
        return {"error": "target_org not your org"}
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO xorg_ops (tenant_id, kind, source_org, target_org, feature, status)
                       VALUES (%s,%s,%s,%s,%s,'proposed') RETURNING id""",
                    (tenant_id, kind, source_org, target_org, feature))
        op_id = cur.fetchone()[0]; c.commit()
    audit.append(actor="crossorg", action="XOrgProposed", resource=str(op_id), decision="proposed",
                 payload={"tenant": tenant_id, "kind": kind, "source": source_org,
                          "target": target_org, "feature": feature})
    return {"op_id": op_id, "status": "proposed"}


def scope(op_id):
    """Read the source org's context (vision/products/artifacts) to produce a LIGHTWEIGHT plan of what to
    port/merge. Honest + light — we don't deep-research another org here, we summarize what's enumerable."""
    _ensure()
    op = _row(op_id)
    if not op:
        return {"error": "no such op"}
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE xorg_ops SET status='scoping' WHERE id=%s", (op_id,)); c.commit()
    tid, src, tgt, feat = op["tenant_id"], op["source_org"], op["target_org"], op["feature"]
    src_brief = orgs.context_brief(tid, src)
    # enumerate the source org's products + artifacts (the "components" available to port).
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT product FROM tenant_products WHERE org_id=%s", (src,))
        src_products = [r[0] for r in cur.fetchall()]
        arts = []
        try:
            cur.execute("""SELECT kind, product, summary FROM org_artifacts WHERE org_id=%s
                           ORDER BY created_at DESC LIMIT 20""", (src,))
            arts = [{"kind": k, "product": p, "summary": (s or "")[:160]} for k, p, s in cur.fetchall()]
        except Exception:
            pass

    if op["kind"] == "steal_feature":
        plan = {
            "kind": "steal_feature",
            "feature": feat,
            "source_org": src, "target_org": tgt,
            "source_brief": src_brief,
            "source_products": src_products,
            "components": [a for a in arts
                          if feat and feat.lower() in (a["summary"] + " " + (a["product"] or "")).lower()]
                          or arts[:3],
            "strategy": f"extract '{feat}' from source org {src}, integrate into target org {tgt}",
            "considerations": [
                "human reviews the extracted feature before it touches the target's codebase",
                "actual code port is a separate, approved build step — this op only records intent + lineage",
                "verify the target's stack is compatible with the ported feature",
            ],
        }
    else:  # merge
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT product FROM tenant_products WHERE org_id=%s", (tgt,))
            tgt_products = [r[0] for r in cur.fetchall()]
        plan = {
            "kind": "merge",
            "source_org": src, "target_org": tgt,
            "source_brief": src_brief,
            "target_brief": orgs.context_brief(tid, tgt) if tgt else None,
            "components": src_products + tgt_products,
            "strategy": f"merge org {src} into org {tgt} as a combined product",
            "considerations": [
                "reconcile overlapping products/features between the two orgs",
                "decide the combined product's surviving name + vision (human call)",
                "actual code merge is a separate, approved build step — this op records intent + lineage",
            ],
        }

    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE xorg_ops SET plan=%s, status='planned' WHERE id=%s",
                    (json.dumps(plan), op_id)); c.commit()
    audit.append(actor="crossorg", action="XOrgScoped", resource=str(op_id), decision="planned",
                 payload={"kind": op["kind"], "components": len(plan.get("components", []))})
    return {"op_id": op_id, "plan": plan}


def request_approval(tenant_id, op_id):
    """Gate the op on a HUMAN. Cross-org ops are consequential, so they wait in await_approval until a
    person confirms execution. (Surfacing into the tenant approvals inbox is the human's entry point.)"""
    _ensure()
    op = _row(op_id)
    if not op:
        return {"error": "no such op"}
    if op["tenant_id"] != tenant_id:
        return {"error": "not your op"}
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE xorg_ops SET status='await_approval' WHERE id=%s", (op_id,)); c.commit()
    audit.append(actor="crossorg", action="XOrgAwaitApproval", resource=str(op_id),
                 decision="await_approval", payload={"tenant": tenant_id, "kind": op["kind"]})
    return {"op_id": op_id, "status": "await_approval"}


def execute(op_id, confirmed=False):
    """Execute a GOVERNED bookkeeping + lineage op. Without confirmation, returns the plan and asks for it.
    With confirmation: record org_lineage (target derived_from source), file a 'spec' artifact on the
    target, mark done. Deliberately does NOT autonomously move/rewrite another org's product files — that
    real work stays a human-approved build step. This is honest bookkeeping, not silent code surgery."""
    _ensure()
    op = _row(op_id)
    if not op:
        return {"error": "no such op"}
    if not confirmed:
        return {"requires_confirm": True, "plan": op["plan"], "op_id": op_id}

    src, tgt, feat, kind = op["source_org"], op["target_org"], op["feature"], op["kind"]
    derived = tgt if tgt is not None else src
    note = (f"ported '{feat}' from org {src}" if kind == "steal_feature"
            else f"merged from org {src}")
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE xorg_ops SET status='executing' WHERE id=%s", (op_id,))
        cur.execute("""INSERT INTO org_lineage (org_id, derived_from, note) VALUES (%s,%s,%s) RETURNING id""",
                    (derived, src, note))
        lineage_id = cur.fetchone()[0]
        c.commit()
    # record the ported intent as a context artifact on the target org (bookkeeping, not code).
    summary = (f"ported {feat} from org {src}" if kind == "steal_feature"
               else f"merged org {src} into this org")
    art = orgs.record_artifact(derived, "spec", summary, ref=f"xorg:{op_id}")
    result = {"lineage_id": lineage_id, "artifact_id": art.get("artifact_id"),
              "derived_org": derived, "note": note,
              "boundary": "lineage + spec recorded; actual code port/merge left to a human-approved build step"}
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE xorg_ops SET status='done', result=%s WHERE id=%s",
                    (json.dumps(result), op_id)); c.commit()
    audit.append(actor="crossorg", action="XOrgExecuted", resource=str(op_id), decision="done",
                 payload={"kind": kind, "derived_org": derived, "source": src, "lineage_id": lineage_id})
    return {"op_id": op_id, "status": "done", "result": result}


def list_ops(tenant_id):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id, kind, source_org, target_org, feature, status, created_at
                       FROM xorg_ops WHERE tenant_id=%s ORDER BY created_at DESC""", (tenant_id,))
        return [{"op_id": i, "kind": k, "source_org": s, "target_org": t, "feature": f,
                 "status": st, "created_at": str(ca)} for i, k, s, t, f, st, ca in cur.fetchall()]


def get_op(op_id):
    _ensure()
    return _row(op_id) or {"error": "no such op"}


def _selftest():
    import billing
    tid = billing.signup("crossorg-selftest", "free")["tenant_id"]
    foreign_tid = billing.signup("crossorg-foreign", "free")["tenant_id"]
    op_id = None
    try:
        a = orgs.create(tid, "Org A — auth-rich", "an app with great auth")["org_id"]
        b = orgs.create(tid, "Org B — needs auth", "an app that needs auth ported in")["org_id"]
        foreign = orgs.create(foreign_tid, "Someone else's org")["org_id"]

        prop = propose(tid, "steal_feature", a, b, "auth")
        op_id = prop["op_id"]
        proposed = prop["status"] == "proposed"

        sc = scope(op_id)
        planned = isinstance(sc.get("plan"), dict) and get_op(op_id)["status"] == "planned" \
            and "auth" in (sc["plan"].get("feature") or "")

        request_approval(tid, op_id)
        awaiting = get_op(op_id)["status"] == "await_approval"

        unconf = execute(op_id, confirmed=False)
        requires = unconf.get("requires_confirm") is True

        done = execute(op_id, confirmed=True)
        is_done = done.get("status") == "done"
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM org_lineage WHERE org_id=%s AND derived_from=%s", (b, a))
            lineage_ok = cur.fetchone()[0] >= 1

        # ownership guard: a foreign org may not be source or target.
        guard = propose(tid, "steal_feature", foreign, b, "x").get("error") == "source_org not your org" \
            and propose(tid, "steal_feature", a, foreign, "x").get("error") == "target_org not your org"

        ok = (proposed and planned and awaiting and requires and is_done and lineage_ok and guard)
        print(f"proposed={proposed} scoped/planned={planned} await_approval={awaiting} "
              f"requires_confirm={requires} done={is_done} lineage_row={lineage_ok} ownership_guard={guard}")
        print("PASS: cross-org ops are owner-scoped, scoped to a plan, human-gated, "
              "and executed as governed lineage bookkeeping ✅" if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM xorg_ops WHERE tenant_id IN (%s,%s)", (tid, foreign_tid))
            cur.execute("""DELETE FROM org_lineage WHERE org_id IN
                           (SELECT id FROM orgs WHERE tenant_id IN (%s,%s))""", (tid, foreign_tid))
            cur.execute("""DELETE FROM org_artifacts WHERE org_id IN
                           (SELECT id FROM orgs WHERE tenant_id IN (%s,%s))""", (tid, foreign_tid))
            cur.execute("DELETE FROM orgs WHERE tenant_id IN (%s,%s)", (tid, foreign_tid))
            cur.execute("DELETE FROM tenants WHERE tenant_id IN (%s,%s)", (tid, foreign_tid))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "propose" and len(a) >= 4:
        print(json.dumps(propose(a[1], a[2], int(a[3]),
                                 int(a[4]) if len(a) > 4 and a[4].lstrip("-").isdigit() else None,
                                 a[5] if len(a) > 5 else None), indent=2))
    elif a[0] == "scope" and len(a) > 1:
        print(json.dumps(scope(int(a[1])), indent=2, default=str))
    elif a[0] == "request-approval" and len(a) > 2:
        print(json.dumps(request_approval(a[1], int(a[2])), indent=2))
    elif a[0] == "execute" and len(a) > 1:
        print(json.dumps(execute(int(a[1]), confirmed="--confirm" in a), indent=2, default=str))
    elif a[0] == "list" and len(a) > 1:
        print(json.dumps(list_ops(a[1]), indent=2))
    elif a[0] in ("get", "json") and len(a) > 1:
        print(json.dumps(get_op(int(a[1])), indent=2, default=str))
    else:
        sys.exit("usage: crossorg.py propose <tenant> <kind> <source_org> [target_org] [feature] | "
                 "scope <op_id> | request-approval <tenant> <op_id> | execute <op_id> [--confirm] | "
                 "list <tenant> | get <op_id> | json <op_id> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
