#!/usr/bin/env python3
"""billing.py — the SaaS layer: onboarding, plans, metered usage, quotas, invoices.

This is what turns the platform into a subscription business. Each tenant is on a plan; usage is
DERIVED from real activity (products shipped + tokens spent on their products), so metering reflects
exactly what the OS actually did — no separate counter to drift or game. Quotas gate over-limit usage;
invoices = plan base + metered overage.

    billing.py signup <name> [plan]        # onboard a tenant -> tenant_id + api_token
    billing.py plans                       # list plans
    billing.py usage <tenant_id>           # metered usage this period
    billing.py invoice <tenant_id>         # plan base + overage = total
    billing.py quota <tenant_id>           # within limits? (enforcement)
    billing.py mrr                          # platform MRR across tenants
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import tenancy  # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)

# plan -> monthly price, included builds, included tokens, overage rates
PLANS = {
    "free":       {"price": 0,   "builds": 3,    "tokens": 100_000,     "ov_build": 0.00, "ov_1k_tok": 0.000},
    "pro":        {"price": 49,  "builds": 50,   "tokens": 5_000_000,   "ov_build": 0.50, "ov_1k_tok": 0.002},
    "enterprise": {"price": 499, "builds": 1000, "tokens": 100_000_000, "ov_build": 0.25, "ov_1k_tok": 0.001},
}


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'free'")
        cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS suspended BOOLEAN NOT NULL DEFAULT false")
        c.commit()


def signup(name, plan="free"):
    if plan not in PLANS:
        raise ValueError(f"unknown plan {plan}; choose {list(PLANS)}")
    _ensure()
    t = tenancy.create_tenant(name)
    tid, token = t["tenant_id"], t["api_token"]
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE tenants SET plan=%s WHERE tenant_id=%s", (plan, tid))
        c.commit()
    return {"tenant_id": tid, "api_token": token, "plan": plan}


def _plan_of(tid):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT plan, suspended FROM tenants WHERE tenant_id=%s", (tid,))
        row = cur.fetchone()
    if not row:
        raise ValueError(f"no such tenant {tid}")
    return row[0], row[1]


def usage(tid):
    """Real metered usage for the tenant's products: builds shipped (LAUNCHED) + tokens spent."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s", (tid,))
        prods = [r[0] for r in cur.fetchall()]
        if not prods:
            return {"products": 0, "builds": 0, "tokens": 0}
        cur.execute("""SELECT count(*) FROM audit_log
                       WHERE actor='factory:controller' AND action='ProductComplete'
                         AND decision='LAUNCHED' AND resource = ANY(%s)""", (prods,))
        builds = cur.fetchone()[0]
        cur.execute("SELECT coalesce(sum(tokens_in+tokens_out),0) FROM org_metrics WHERE product = ANY(%s)", (prods,))
        tokens = cur.fetchone()[0]
    return {"products": len(prods), "builds": builds, "tokens": int(tokens)}


def invoice(tid):
    plan, _ = _plan_of(tid)
    p = PLANS[plan]
    u = usage(tid)
    over_builds = max(0, u["builds"] - p["builds"])
    over_tokens = max(0, u["tokens"] - p["tokens"])
    ov_cost = round(over_builds * p["ov_build"] + (over_tokens / 1000) * p["ov_1k_tok"], 2)
    return {"tenant": tid, "plan": plan, "base": p["price"], "usage": u,
            "overage": {"builds": over_builds, "tokens": over_tokens, "cost": ov_cost},
            "total": round(p["price"] + ov_cost, 2)}


def quota(tid):
    plan, suspended = _plan_of(tid)
    p = PLANS[plan]
    u = usage(tid)
    within = (not suspended) and u["builds"] < p["builds"] and u["tokens"] < p["tokens"]
    return {"tenant": tid, "plan": plan, "within_quota": within, "suspended": suspended,
            "builds": f"{u['builds']}/{p['builds']}", "tokens": f"{u['tokens']}/{p['tokens']}"}


def mrr():
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT plan, count(*) FROM tenants WHERE NOT suspended GROUP BY plan")
        by = dict(cur.fetchall())
    total = sum(PLANS.get(pl, {"price": 0})["price"] * n for pl, n in by.items())
    return {"tenants_by_plan": by, "mrr": total}


def _main(a):
    import json
    if not a:
        sys.exit("usage: billing.py signup|plans|usage|invoice|quota|mrr ...")
    if a[0] == "signup":
        print(json.dumps(signup(a[1], a[2] if len(a) > 2 else "free"), indent=2))
    elif a[0] == "plans":
        print(json.dumps(PLANS, indent=2))
    elif a[0] == "usage":
        print(json.dumps(usage(a[1]), indent=2))
    elif a[0] == "invoice":
        print(json.dumps(invoice(a[1]), indent=2))
    elif a[0] == "quota":
        print(json.dumps(quota(a[1]), indent=2))
    elif a[0] == "mrr":
        print(json.dumps(mrr(), indent=2))
    elif a[0] == "test":
        _ensure()
        import os
        import audit
        import metrics
        t = signup(f"acme-{os.urandom(3).hex()}", "pro")
        tid = t["tenant_id"]
        # attribute a shipped product + tokens to this tenant via the REAL chain-preserving APIs
        prod = f"saas-demo-{os.urandom(3).hex()}"
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s)", (prod, tid))
            c.commit()
        audit.append(actor="factory:controller", action="ProductComplete", resource=prod, decision="LAUNCHED")
        metrics.record("state_change", product=prod, task_id="t", tokens_in=6_000_000, tokens_out=0,
                       model="m", outcome="success")
        inv = invoice(tid)
        q = quota(tid)
        ok = inv["plan"] == "pro" and inv["overage"]["tokens"] > 0 and inv["total"] > PLANS["pro"]["price"] and not q["within_quota"]
        print(f"invoice: {inv}")
        print(f"quota: {q}")
        print("PASS: SaaS metering + plans + invoice + quota ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
