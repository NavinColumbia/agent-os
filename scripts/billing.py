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
    billing.py settle <tenant_id>          # settle every fully-elapsed month -> invoices (scheduler/cron)
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
        # liveness fix: remember WHETHER a suspension was automatic (over-quota enforcement by quota())
        # vs manual (admin). Only automatic suspensions are auto-lifted once a new billing period brings
        # the tenant back within limits; admin suspensions stay sticky. Defaults false (== admin/none).
        cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS "
                    "auto_suspended BOOLEAN NOT NULL DEFAULT false")
        # finding #16: every tenant gets a monthly billing-period anchor so metered usage/quota/invoice
        # reset each cycle instead of accumulating lifetime totals. New tenants anchor to the current
        # month; existing rows are backfilled to the current month boundary at migration time.
        cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS "
                    "period_start timestamptz NOT NULL DEFAULT date_trunc('month', now())")
        # finding #16 follow-up: durable per-period settlement ledger. Each fully-elapsed billing month
        # is settled into exactly one row here (idempotent on (tenant_id, period_start)) so that letting
        # the invoicing cadence lapse beyond a calendar month no longer drops the intervening months'
        # usage — every elapsed month is metered and invoiced before the anchor rolls past it.
        cur.execute("""CREATE TABLE IF NOT EXISTS billing_invoices (
                           tenant_id    text        NOT NULL,
                           period_start timestamptz NOT NULL,
                           period_end   timestamptz NOT NULL,
                           plan         text        NOT NULL,
                           base         numeric     NOT NULL,
                           builds       integer     NOT NULL,
                           tokens       bigint      NOT NULL,
                           overage_cost numeric     NOT NULL,
                           total        numeric     NOT NULL,
                           settled_at   timestamptz NOT NULL DEFAULT now(),
                           PRIMARY KEY (tenant_id, period_start))""")
        c.commit()


def _audit(action, tid, payload, actor="billing", decision="executed"):
    """Best-effort tamper-evident audit (reuse audit.py); never blocks the billing op if the DB/key is down."""
    try:
        import audit
        audit.append(actor=actor, action=action, resource=tid, decision=decision, payload=payload)
    except Exception:
        pass


def _usage_window(cur, prods, start, end):
    """Count builds shipped (LAUNCHED) + tokens spent for the tenant's products within [start, end),
    using an ALREADY-OPEN cursor so callers can meter inside their own transaction. Single source of
    truth for the metering SQL shared by usage() and settlement. Returns (builds, tokens)."""
    if not prods:
        return 0, 0
    cur.execute("""SELECT count(*) FROM audit_log
                   WHERE actor='factory:controller' AND action='ProductComplete'
                     AND decision='LAUNCHED' AND resource = ANY(%s)
                     AND ts >= %s AND ts < %s""", (prods, start, end))
    builds = cur.fetchone()[0]
    cur.execute("""SELECT coalesce(sum(tokens_in+tokens_out),0) FROM org_metrics
                   WHERE product = ANY(%s) AND ts >= %s AND ts < %s""", (prods, start, end))
    tokens = int(cur.fetchone()[0])
    return builds, tokens


def _settle_elapsed(tid):
    """Settle every WHOLE month that has fully elapsed since the stored anchor, ONE period at a time,
    so no month's usage is skipped when the invoicing cadence exceeds a month (finding #16 follow-up:
    the old _period() collapsed the whole gap in a single jump straight to the current month, billing
    nothing for the intervening months — a silent under-count).

    Each iteration runs in a SINGLE transaction (fail-closed on revenue: any failure rolls the whole
    step back, so the month is retried on the next call rather than dropped): roll the anchor forward
    EXACTLY one month, meter that just-closed window, and persist its invoice idempotently on
    (tenant_id, period_start). The current, still-open month is left untouched. Scoped to this tenant
    only via _plan_of (which also validates the tenant exists). Returns settled invoices, oldest first."""
    plan, _ = _plan_of(tid)
    p = PLANS[plan]
    settled = []
    while True:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute(
                """UPDATE tenants
                      SET period_start = period_start + interval '1 month'
                    WHERE tenant_id=%s
                      AND period_start + interval '1 month' <= date_trunc('month', now())
                RETURNING period_start - interval '1 month', period_start""",
                (tid,),
            )
            row = cur.fetchone()
            if not row:
                break
            wstart, wend = row[0], row[1]
            cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s", (tid,))
            prods = [r[0] for r in cur.fetchall()]
            builds, tokens = _usage_window(cur, prods, wstart, wend)
            over_builds = max(0, builds - p["builds"])
            over_tokens = max(0, tokens - p["tokens"])
            ov_cost = round(over_builds * p["ov_build"] + (over_tokens / 1000) * p["ov_1k_tok"], 2)
            total = round(p["price"] + ov_cost, 2)
            cur.execute(
                """INSERT INTO billing_invoices
                       (tenant_id, period_start, period_end, plan, base, builds, tokens, overage_cost, total)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, period_start) DO NOTHING""",
                (tid, wstart, wend, plan, p["price"], builds, tokens, ov_cost, total),
            )
            c.commit()
        settled.append({"tenant": tid, "plan": plan, "base": p["price"],
                        "period_start": wstart.isoformat(), "period_end": wend.isoformat(),
                        "usage": {"builds": builds, "tokens": tokens},
                        "overage": {"builds": over_builds, "tokens": over_tokens, "cost": ov_cost},
                        "total": total})
    return settled


def settle(tid):
    """Settle every fully-elapsed billing month for a tenant and return the emitted invoices. This is
    the guaranteed-cadence entry point a scheduler/cron calls per tenant; it is idempotent, so running
    it more often than monthly is harmless and running it less often still bills each elapsed month."""
    _ensure()
    return _settle_elapsed(tid)


def _period(tid):
    """Return (period_start, period_end) for the tenant's CURRENT (still-open) monthly billing window.
    Before reading it, settle every whole month that has fully elapsed since the stored anchor (see
    _settle_elapsed): the anchor is rolled forward ONE month per elapsed period and each is invoiced,
    instead of collapsing the gap in a single jump and dropping the intervening months' usage. The
    anchor always lands on the current month boundary afterwards, so usage()/quota()/invoice() still
    only ever see THIS period's activity (finding #16) — now with no per-month under-count."""
    _ensure()
    _settle_elapsed(tid)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT period_start, period_start + interval '1 month' "
                    "FROM tenants WHERE tenant_id=%s", (tid,))
        row = cur.fetchone()
    if not row:
        raise ValueError(f"no such tenant {tid}")
    return row[0], row[1]


def suspend(tid, reason="", actor="billing:admin", auto=False):
    """Actually WRITE tenants.suspended=true (finding #18) and record an audit entry. This is the
    abuse/over-quota/admin enforcement path that the suspended flag (read by _plan_of/quota/mrr) was
    missing — without it the flag could never become true and a tenant could never be suspended.

    ``auto=True`` marks the suspension as automatic over-quota enforcement (quota() path) so it can be
    auto-lifted at period rollover; ``auto=False`` (default) marks a sticky admin/manual suspension."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE tenants SET suspended=true, auto_suspended=%s WHERE tenant_id=%s", (auto, tid))
        if cur.rowcount == 0:
            raise ValueError(f"no such tenant {tid}")
        c.commit()
    _audit("TenantSuspended", tid, {"reason": reason, "auto": auto}, actor=actor, decision="deny")
    return {"tenant": tid, "suspended": True, "reason": reason, "auto": auto}


def unsuspend(tid, actor="billing:admin"):
    """Lift a suspension (admin or automatic period-rollover path): WRITE tenants.suspended=false,
    clear the auto_suspended marker, and audit."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE tenants SET suspended=false, auto_suspended=false WHERE tenant_id=%s", (tid,))
        if cur.rowcount == 0:
            raise ValueError(f"no such tenant {tid}")
        c.commit()
    _audit("TenantUnsuspended", tid, {}, actor=actor, decision="allow")
    return {"tenant": tid, "suspended": False}


def _auto_suspended(tid):
    """Was this tenant's current suspension applied automatically (over-quota) rather than by an admin?"""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT auto_suspended FROM tenants WHERE tenant_id=%s", (tid,))
        row = cur.fetchone()
    if not row:
        raise ValueError(f"no such tenant {tid}")
    return bool(row[0])


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
        builds, tokens = _usage_window(cur, prods, start, end)
    return {"products": len(prods), "builds": builds, "tokens": tokens,
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
    no_overage = p["ov_build"] == 0 and p["ov_1k_tok"] == 0
    # Over-quota enforcement that actually WRITES the suspended flag (finding #18): a plan with NO
    # overage pricing (e.g. free) cannot bill for excess, so blowing past its hard limits is
    # unbillable abuse -> suspend the tenant. Plans that carry overage rates (pro/enterprise) are
    # BILLED for the excess by invoice() rather than suspended, so paying customers aren't cut off.
    if over and not suspended and no_overage:
        suspend(tid, reason=f"over-quota on no-overage plan '{plan}'", actor="billing:quota", auto=True)
        suspended = True
    # Liveness fix: usage is period-windowed (finding #16), so a one-time over-quota spike no longer
    # over-quotas the tenant once a new billing period starts. An AUTOMATIC over-quota suspension must
    # therefore be auto-lifted when the tenant is back within this period's limits — otherwise a free
    # tenant is locked out permanently (undoing finding #16's no-permanent-lockout goal). Admin/manual
    # suspensions (auto_suspended=false) are NOT lifted here and stay sticky until admin unsuspend().
    elif suspended and not over and _auto_suspended(tid):
        unsuspend(tid, actor="billing:quota")
        suspended = False
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
        sys.exit("usage: billing.py signup|plans|usage|invoice|quota|settle|suspend|unsuspend|mrr ...")
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
    elif a[0] == "settle":
        print(json.dumps(settle(a[1]), indent=2))
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
        # liveness fix: a free (no-overage) tenant auto-suspended for an over-quota spike must be
        # auto-UNSUSPENDED once a new billing period brings it back within limits — and an ADMIN
        # suspension must stay sticky across that same recompute.
        tf = signup(f"free-{os.urandom(3).hex()}", "free")
        ftid = tf["tenant_id"]
        fprod = f"free-demo-{os.urandom(3).hex()}"
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s)", (fprod, ftid))
            c.commit()
        metrics.record("state_change", product=fprod, task_id="t", tokens_in=200_000, tokens_out=0,
                       model="m", outcome="success")  # 200k > free's 100k limit -> over quota
        qf_over = quota(ftid)  # auto-suspends (no-overage plan over hard limit)
        # advance the tenant into a fresh, empty billing period so this period's usage is back to 0
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("UPDATE tenants SET period_start = date_trunc('month', now()) + interval '1 month'"
                        " WHERE tenant_id=%s", (ftid,))
            c.commit()
        qf_next = quota(ftid)  # within limits again -> auto-unsuspend the AUTOMATIC suspension
        suspend(ftid, reason="admin hold", actor="billing:admin")  # sticky admin suspension (auto=False)
        qf_admin = quota(ftid)  # within quota, but admin suspension must NOT be auto-lifted
        unsuspend(ftid)
        auto_lift_works = (qf_over["suspended"] is True and qf_over["within_quota"] is False
                           and qf_next["suspended"] is False and qf_next["within_quota"] is True
                           and qf_admin["suspended"] is True and qf_admin["within_quota"] is False)
        ok = (inv["plan"] == "pro" and inv["overage"]["tokens"] > 0
              and inv["total"] > PLANS["pro"]["price"] and not q["within_quota"]
              and period_scoped and suspend_works and auto_lift_works)
        print(f"invoice: {inv}")
        print(f"quota: {q}")
        print(f"period_scoped (out-of-period excluded): {period_scoped}")
        print(f"suspend_works (flag written/read/reverted): {suspend_works}")
        print(f"auto_lift_works (auto-suspend lifts at rollover; admin stays sticky): {auto_lift_works}")
        print("PASS: SaaS metering + period window + plans + invoice + quota + suspend ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
