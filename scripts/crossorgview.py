#!/usr/bin/env python3
"""crossorgview.py — the CROSS-ORG view: ONE CEO, ACROSS all their orgs at once.

cockpit.py is one tenant's whole factory; portfolio.py is the platform P&L. But the grand vision
(orgs.py) is a CEO running MANY orgs — "a YouTube competitor", "an invoicing SaaS" — each its own
company. This is the surface that zooms OUT one more level: a company-of-companies dashboard that
groups everything BY ORG so the CEO sees the whole portfolio in one pane:

  • portfolio       — every org: products, spend, live/building/failed counts + portfolio totals
  • analytics       — across all orgs: total spend, success rate, spend-by-org chart, most active org
  • failures        — across all orgs: recent failed/BLOCKED builds joined to their org + open findings
  • recommendations — cross-org recommendations (best-effort, via recommend.recent)

Everything is tenant-scoped (orgs are owned by a tenant) and grouped by org_id. A build is a
LAUNCHED/INTEGRATED ProductComplete (success); anything else (BLOCKED_AT_QA, …) is a failure.

    crossorgview.py portfolio <tenant_id>
    crossorgview.py analytics <tenant_id>
    crossorgview.py failures <tenant_id>
    crossorgview.py recommendations <tenant_id>
    crossorgview.py selftest
Run with the agent-os venv python. NO web server — read-only cross-org summarizer of recorded truth.
"""
import json
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402,F401  (convention parity with the other surfaces)
import orgs   # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)

# A ProductComplete with one of these decisions is a SHIPPED build; everything else is a failure.
SUCCESS_DECISIONS = ("LAUNCHED", "INTEGRATED")


def _org_products(cur, tenant_id):
    """Map of org_id -> [products] for THIS tenant, from tenant_products.org_id. Also returns the flat
    product->org_id map so audit/findings rows (keyed by product) can be attributed to an org."""
    orgs._ensure()  # make sure org_id columns exist before we read them
    cur.execute("""SELECT tp.product, tp.org_id FROM tenant_products tp
                   JOIN orgs o ON o.id = tp.org_id
                   WHERE o.tenant_id=%s""", (tenant_id,))
    by_org, prod_org = {}, {}
    for product, org_id in cur.fetchall():
        by_org.setdefault(org_id, []).append(product)
        prod_org[product] = org_id
    return by_org, prod_org


def _latest_completions(cur, products):
    """For each product, its LATEST ProductComplete decision (the terminal build result), if any."""
    if not products:
        return {}
    cur.execute("""SELECT DISTINCT ON (resource) resource, decision, ts FROM audit_log
                   WHERE action='ProductComplete' AND resource = ANY(%s)
                   ORDER BY resource, id DESC""", (products,))
    return {r[0]: {"decision": r[1], "ts": r[2]} for r in cur.fetchall()}


def portfolio(tenant_id):
    """Per-org rollup + portfolio totals. For each org: its products, the spend (cost_usd from traces
    for those products), and live/building/failed counts from the latest ProductComplete per product."""
    org_meta = {o["org_id"]: o for o in orgs.list_orgs(tenant_id)}
    out_orgs = []
    with psycopg.connect(DB) as c, c.cursor() as cur:
        by_org, _ = _org_products(cur, tenant_id)
        for org_id, meta in org_meta.items():
            prods = by_org.get(org_id, [])
            spend = 0.0
            if prods:
                cur.execute("""SELECT COALESCE(sum(cost_usd),0) FROM traces WHERE product = ANY(%s)""", (prods,))
                spend = float(cur.fetchone()[0] or 0)
            comps = _latest_completions(cur, prods)
            live = building = failed = 0
            for p in prods:
                comp = comps.get(p)
                if comp is None:
                    building += 1                              # has a product row but no terminal result yet
                elif comp["decision"] in SUCCESS_DECISIONS:
                    live += 1
                else:
                    failed += 1
            out_orgs.append({
                "org_id": org_id, "name": meta["name"], "stage": meta["stage"],
                "products": len(prods), "spend_usd": round(spend, 4),
                "live": live, "building": building, "failed": failed,
            })
    totals = {
        "orgs": len(out_orgs),
        "products": sum(o["products"] for o in out_orgs),
        "spend_usd": round(sum(o["spend_usd"] for o in out_orgs), 4),
        "live": sum(o["live"] for o in out_orgs),
        "building": sum(o["building"] for o in out_orgs),
        "failed": sum(o["failed"] for o in out_orgs),
    }
    return {"orgs": out_orgs, "totals": totals}


def analytics(tenant_id):
    """Cross-org analytics: total spend, total products, build success rate, spend-by-org (chart),
    most active org. All derived from the same portfolio rollup so the numbers always agree."""
    p = portfolio(tenant_id)
    totals = p["totals"]
    decided = totals["live"] + totals["failed"]              # builds with a terminal result
    success_rate = round(100 * totals["live"] / decided, 1) if decided else 0.0
    spend_by_org = [{"org_id": o["org_id"], "name": o["name"], "spend_usd": o["spend_usd"]}
                    for o in p["orgs"]]
    most_active = max(p["orgs"], key=lambda o: (o["products"], o["spend_usd"]), default=None)
    return {
        "total_spend_usd": totals["spend_usd"],
        "total_products": totals["products"],
        "builds_decided": decided,
        "builds_succeeded": totals["live"],
        "build_success_rate": success_rate,
        "spend_by_org": spend_by_org,
        "most_active_org": ({"org_id": most_active["org_id"], "name": most_active["name"],
                             "products": most_active["products"], "spend_usd": most_active["spend_usd"]}
                            if most_active else None),
    }


def failures(tenant_id):
    """What's broken across the whole company-of-companies: recent failed/BLOCKED builds (a
    ProductComplete whose decision is NOT a success) joined to their org, plus open findings per org."""
    org_meta = {o["org_id"]: o for o in orgs.list_orgs(tenant_id)}
    failed_builds, findings_by_org = [], {}
    with psycopg.connect(DB) as c, c.cursor() as cur:
        by_org, prod_org = _org_products(cur, tenant_id)
        all_products = [p for ps in by_org.values() for p in ps]
        if all_products:
            cur.execute("""SELECT resource, decision, ts FROM audit_log
                           WHERE action='ProductComplete' AND resource = ANY(%s)
                             AND decision != ALL(%s)
                           ORDER BY id DESC LIMIT 50""",
                        (all_products, list(SUCCESS_DECISIONS)))
            for product, decision, ts in cur.fetchall():
                oid = prod_org.get(product)
                meta = org_meta.get(oid, {})
                failed_builds.append({
                    "product": product, "decision": decision,
                    "org_id": oid, "org_name": meta.get("name"),
                    "ts": ts.strftime("%Y-%m-%d %H:%M:%S") if ts else None,
                })
        # open findings per org (findings.org_id, added idempotently by orgs._ensure)
        if org_meta:
            try:
                cur.execute("""SELECT org_id, count(*) FROM findings
                               WHERE org_id = ANY(%s) AND status NOT IN ('resolved','dropped')
                               GROUP BY org_id""", (list(org_meta.keys()),))
                findings_by_org = {oid: int(n) for oid, n in cur.fetchall()}
            except Exception:
                findings_by_org = {}
    open_findings = [{"org_id": oid, "org_name": org_meta.get(oid, {}).get("name"), "open_findings": n}
                     for oid, n in sorted(findings_by_org.items())]
    return {
        "failed_builds": failed_builds,
        "failed_build_count": len(failed_builds),
        "open_findings_by_org": open_findings,
        "open_findings_total": sum(findings_by_org.values()),
    }


def recommendations(tenant_id):
    """Cross-org recommendations, best-effort. recommend.recent is tenant-scoped (spans all orgs)."""
    try:
        import recommend
        return recommend.recent(tenant_id) or []
    except Exception:
        return []


def _selftest():
    """Real tenant + 2 orgs, a product per org with traces, and one LAUNCHED + one BLOCKED_AT_QA build
    via the append-only audit log. Proves portfolio/analytics/failures surface the cross-org truth.
    Cleans up tenant_products/traces/orgs/tenants in finally — NEVER audit_log (hash-chained)."""
    import billing
    reg = billing.signup("crossorgview-selftest", "free")
    tid = reg["tenant_id"]
    a = orgs.create(tid, "YouTube competitor", "creator-first video")
    b = orgs.create(tid, "Invoicing SaaS", "billing for SMBs")
    pa = tid.replace("t-", "")[:6] + "-vid"       # org A's product (gets spend + a LAUNCHED build)
    pb = tid.replace("t-", "")[:6] + "-inv"       # org B's product (gets a BLOCKED_AT_QA build)
    ok = False
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("INSERT INTO tenant_products (product, tenant_id, org_id) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        (pa, tid, a["org_id"]))
            cur.execute("INSERT INTO tenant_products (product, tenant_id, org_id) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        (pb, tid, b["org_id"]))
            for st, cost in (("SPEC", 0.16), ("BUILD", 1.2)):
                cur.execute("""INSERT INTO traces (run_id, product, stage, role, kind, rc, cost_usd, tokens_in, tokens_out, elapsed_s, prompt, output, model)
                               VALUES (%s,%s,%s,'builder','agent',0,%s,1000,2000,30,'p','o','m')""",
                            (f"run-{pa}", pa, st, cost))
            c.commit()
        # terminal build results via the append-only audit chain (NEVER deleted afterwards)
        audit.append(actor="crossorgview-selftest", action="ProductComplete", resource=pa,
                     decision="LAUNCHED", payload={"org": a["org_id"]})
        audit.append(actor="crossorgview-selftest", action="ProductComplete", resource=pb,
                     decision="BLOCKED_AT_QA", payload={"org": b["org_id"]})

        port = portfolio(tid)
        an = analytics(tid)
        fail = failures(tid)

        two_orgs = port["totals"]["orgs"] == 2 and port["totals"]["products"] >= 2
        has_spend = port["totals"]["spend_usd"] >= 1.3
        org_a = next((o for o in port["orgs"] if o["org_id"] == a["org_id"]), {})
        org_b = next((o for o in port["orgs"] if o["org_id"] == b["org_id"]), {})
        counts = org_a.get("live") == 1 and org_b.get("failed") == 1
        analytics_ok = (an["total_spend_usd"] >= 1.3 and an["total_products"] >= 2
                        and isinstance(an["build_success_rate"], float) and an["most_active_org"])
        blocked_surfaced = any(fb["decision"] == "BLOCKED_AT_QA" and fb["org_id"] == b["org_id"]
                               and fb["org_name"] == "Invoicing SaaS" for fb in fail["failed_builds"])
        recs_ok = isinstance(recommendations(tid), list)

        ok = two_orgs and has_spend and counts and analytics_ok and blocked_surfaced and recs_ok
        print(f"orgs={port['totals']['orgs']} products={port['totals']['products']} "
              f"spend=${port['totals']['spend_usd']} success_rate={an['build_success_rate']}% "
              f"failed_builds={fail['failed_build_count']} blocked_surfaced={blocked_surfaced}")
        print("PASS: crossorgview rolls up portfolio + analytics + failures across all a CEO's orgs ✅" if ok else "FAIL")
    finally:
        # clean up everything EXCEPT audit_log (append-only, tamper-evident hash chain — never touched)
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM traces WHERE product = ANY(%s)", ([pa, pb],))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM orgs WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "portfolio" and len(a) > 1:
        print(json.dumps(portfolio(a[1]), indent=2))
    elif a[0] == "analytics" and len(a) > 1:
        print(json.dumps(analytics(a[1]), indent=2))
    elif a[0] == "failures" and len(a) > 1:
        print(json.dumps(failures(a[1]), indent=2))
    elif a[0] == "recommendations" and len(a) > 1:
        print(json.dumps(recommendations(a[1]), indent=2))
    else:
        sys.exit("usage: crossorgview.py portfolio <tid> | analytics <tid> | failures <tid> | "
                 "recommendations <tid> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
