#!/usr/bin/env python3
"""billingview.py — Area 11: tenant Billing & Plans view for the agent-os factory.

Data/logic module ONLY (no web server). This is a thin UI-friendly WRAPPER over
scripts/billing.py — it does NOT duplicate any billing/metering logic, it just
shapes billing.usage / billing.quota / billing.invoice / billing.PLANS into a
single payload a UI can render (current plan, usage bars, plan compare table,
invoice breakdown), plus a SAFE in-app plan switch.

    billingview.py selftest
    billingview.py json <tenant_id>
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit  # noqa: E402
import billing  # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)


def _plans_table(current_plan):
    """Compare/upgrade table: one row per plan, flagging the tenant's current one."""
    rows = []
    for slug, p in billing.PLANS.items():
        rows.append({
            "slug": slug,
            "price": p["price"],
            "builds": p["builds"],
            "tokens": p["tokens"],
            "current": slug == current_plan,
        })
    return rows


def billing_view(tid):
    """Everything the Billing & Plans UI needs for one tenant, in one payload."""
    plan, suspended = billing._plan_of(tid)
    return {
        "tenant": tid,
        "plan": plan,
        "suspended": suspended,
        "usage": billing.usage(tid),
        "quota": billing.quota(tid),
        "plans": _plans_table(plan),
        # billing.py provides invoice(); reuse it rather than recomputing from PLANS+usage.
        "invoice": billing.invoice(tid),
    }


def change_plan(tid, plan):
    """SAFE in-app plan switch. Validates the plan and updates the tenant row.

    NOTE: this is intentionally money-free. Real payment / Stripe checkout is
    gated and OUT OF SCOPE here — this only flips the plan column so usage,
    quota and invoice math reflect the chosen tier. Wiring an actual payment
    provider would happen behind this function, not inside it.
    """
    if plan not in billing.PLANS:
        raise ValueError(f"unknown plan {plan}; choose {list(billing.PLANS)}")
    billing._ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE tenants SET plan=%s WHERE tenant_id=%s", (plan, tid))
        if cur.rowcount == 0:
            raise ValueError(f"no such tenant {tid}")
        c.commit()
    audit.append(actor=f"tenant:{tid}", action="PlanChanged", resource=tid,
                 decision="executed", payload={"plan": plan})
    return {"ok": True, "plan": plan}


def _selftest():
    billing._ensure()
    tid = billing.signup("billingview-selftest", "free")["tenant_id"]
    try:
        v = billing_view(tid)
        assert v["plan"] == "free", f"expected free, got {v['plan']}"
        assert len(v["plans"]) >= 2, f"expected >=2 plans, got {len(v['plans'])}"
        current = [r for r in v["plans"] if r["current"]]
        assert len(current) == 1 and current[0]["slug"] == "free", f"current flag wrong: {current}"
        assert "total" in v["invoice"], "invoice missing total"
        assert "within_quota" in v["quota"], "quota missing within_quota"

        r = change_plan(tid, "pro")
        assert r == {"ok": True, "plan": "pro"}, r
        v2 = billing_view(tid)
        assert v2["plan"] == "pro", f"plan did not flip to pro: {v2['plan']}"
        assert [r for r in v2["plans"] if r["current"]][0]["slug"] == "pro", "current flag not on pro"

        bad = False
        try:
            change_plan(tid, "platinum-unicorn")
        except ValueError:
            bad = True
        assert bad, "invalid plan was not rejected"

        print("PASS: billingview wraps billing into a UI payload; in-app plan switch works ✅")
        sys.exit(0)
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()


def _main(a):
    import json
    if not a:
        sys.exit("usage: billingview.py selftest | json <tenant_id>")
    if a[0] == "selftest":
        _selftest()
    elif a[0] == "json":
        if len(a) < 2:
            sys.exit("usage: billingview.py json <tenant_id>")
        print(json.dumps(billing_view(a[1]), indent=2))
    else:
        sys.exit(f"unknown command {a[0]}")


if __name__ == "__main__":
    _main(sys.argv[1:])
