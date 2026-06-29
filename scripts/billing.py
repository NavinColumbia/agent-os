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
        # finding #16: every tenant gets a monthly billing-period anchor so metered usage/quota/invoice
        # reset each cycle instead of accumulating lifetime totals. New tenants anchor to the current
        # month; existing rows are backfilled to the current month boundary at migration time.
        cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS "
                    "period_start timestamptz NOT NULL DEFAULT date_trunc('month', now())")
        c.commit()


def _audit(action, tid, payload, actor="billing", decision="executed"):
    """Best-effort tamper-evident audit (reuse audit.py); never blocks the billing op if the DB/key is down."""
    try:
        import audit
        audit.append(actor=actor, action=action, resource=tid, decision=decision, payload=payload)
    except Exception:
        pass


def _period(tid):
    """Return (period_start, period_end) for the tenant's CURRENT monthly billing window, rolling the
    stored anchor forward through any whole months that have elapsed since it was last set (finding #16).
    Because the anchor always sits on a month boundary, advancing by the whole-month delta keeps it
    aligned and means usage()/quota()/invoice() only ever see THIS period's activity."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute(
            """UPDATE tenants
                  SET period_start = period_start + make_interval(months =>
                        GREATEST(0,
                          (extract(year  FROM now())::int - extract(year  FROM period_start)::int) * 12
                        + (extract(month FROM now())::int - extract(month FROM period_start)::int)))
                WHERE tenant_id=%s
            RETURNING period_start, period_start + interval '1 month'""",
            (tid,),
        )
        row = cur.fetchone()
        c.commit()
    if not row:
        raise ValueError(f"no such tenant {tid}")
    return row[0], row[1]


def suspend(tid, reason="", actor="billing:admin"):
    """Actually WRITE tenants.suspended=true (finding #18) and record an audit entry. This is the
    abuse/over-quota/admin enforcement path that the suspended flag (read by _plan_of/quota/mrr) was
    missing — without it the flag could never become true and a tenant could never be suspended."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE tenants SET suspended=true WHERE tenant_id=%s", (tid,))
        if cur.rowcount == 0:
            raise ValueError(f"no such tenant {tid}")
        c.commit()
    _audit("TenantSuspended", tid, {"reason": reason}, actor=actor, decision="deny")
    return {"tenant": tid, "suspended": True, "reason": reason}


def unsuspend(tid, actor="billing:admin"):
    """Lift a suspension (admin path): WRITE tenants.suspended=false + audit."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE tenants SET suspended=false WHERE tenant_id=%s", (tid,))
        if cur.rowcount == 0:
            raise ValueError(f"no such tenant {tid}")
        c.commit()
    _audit("TenantUnsuspended", tid, {}, actor=actor, decision="allow")
    return {"tenant": tid, "suspended": False}


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
    """Real metered usage for the tenant's products THIS billing period (finding #16): builds shipped
    (LAUNCHED) + tokens spent, filtered to the current monthly window [period_start, period_end) so
    quotas and invoices reset each cycle instead of comparing lifetime totals to a monthly plan limit."""
    start, end = _period(tid)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s", (tid,))
        prods = [r[0] for r in cur.fetchall()]
        if not prods:
            return {"products": 0, "builds": 0, "tokens": 0, "period_start": start.isoformat()}
        cur.execute("""SELECT count(*) FROM audit_log
                       WHERE actor='factory:controller' AND action='ProductComplete'
                         AND decision='LAUNCHED' AND resource = ANY(%s)
                         AND ts >= %s AND ts < %s""", (prods, start, end))
        builds = cur.fetchone()[0]
        cur.execute("""SELECT coalesce(sum(tokens_in+tokens_out),0) FROM org_metrics
                       WHERE product = ANY(%s) AND ts >= %s AND ts < %s""", (prods, start, end))
        tokens = cur.fetchone()[0]
    return {"products": len(prods), "builds": builds, "tokens": int(tokens),
            "period_start": start.isoformat()}


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
    over = u["builds"] >= p["builds"] or u["tokens"] >= p["tokens"]
    # Over-quota enforcement that actually WRITES the suspended flag (finding #18): a plan with NO
    # overage pricing (e.g. free) cannot bill for excess, so blowing past its hard limits is
    # unbillable abuse -> suspend the tenant. Plans that carry overage rates (pro/enterprise) are
    # BILLED for the excess by invoice() rather than suspended, so paying customers aren't cut off.
    if over and not suspended and p["ov_build"] == 0 and p["ov_1k_tok"] == 0:
        suspend(tid, reason=f"over-quota on no-overage plan '{plan}'", actor="billing:quota")
        suspended = True
    within = (not suspended) and not over
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
        sys.exit("usage: billing.py signup|plans|usage|invoice|quota|suspend|unsuspend|mrr ...")
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
    elif a[0] == "suspend":
        print(json.dumps(suspend(a[1], a[2] if len(a) > 2 else "admin action"), indent=2))
    elif a[0] == "unsuspend":
        print(json.dumps(unsuspend(a[1]), indent=2))
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
        # finding #16: an OUT-OF-PERIOD record (2 months ago) must NOT count toward this period's usage.
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO org_metrics (ts, product, task_id, event, tokens_in, tokens_out,
                           model, outcome) VALUES (now() - interval '2 months', %s,'old','state_change',
                           9_000_000, 0,'m','success')""", (prod,))
            c.commit()
        inv = invoice(tid)
        q = quota(tid)
        # the 9M out-of-period tokens are excluded -> usage reflects only the 6M from this period.
        period_scoped = inv["usage"]["tokens"] == 6_000_000 and "period_start" in inv["usage"]
        # finding #18: suspend() actually WRITES the flag; quota/_plan_of then read it; unsuspend reverts.
        suspend(tid, reason="selftest")
        _, susp_after = _plan_of(tid)
        q_susp = quota(tid)
        unsuspend(tid)
        _, unsusp_after = _plan_of(tid)
        suspend_works = susp_after is True and q_susp["within_quota"] is False and unsusp_after is False
        ok = (inv["plan"] == "pro" and inv["overage"]["tokens"] > 0
              and inv["total"] > PLANS["pro"]["price"] and not q["within_quota"]
              and period_scoped and suspend_works)
        print(f"invoice: {inv}")
        print(f"quota: {q}")
        print(f"period_scoped (out-of-period excluded): {period_scoped}")
        print(f"suspend_works (flag written/read/reverted): {suspend_works}")
        print("PASS: SaaS metering + period window + plans + invoice + quota + suspend ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
