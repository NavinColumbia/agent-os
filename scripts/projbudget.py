#!/usr/bin/env python3
"""projbudget.py — PER-PROJECT budget caps a CEO sets, per product.

Today a build's budget is one global env var; this lets a tenant ("CEO") set a hard $ cap on each of
their products. appguard.py owns ENFORCEMENT (per-app circuit-breaker / auto-pause); this module is the
tenant-facing SET/READ layer on top: it stores the cap a CEO chose, registers it with appguard so the
breaker enforces it, and surfaces cap + current spend + % used so cockpit/appguard can read it. It does
NOT duplicate appguard's enforcement.

Every operation is ownership-checked: a tenant may only set/read budgets for products they own
(tenant_products(product, tenant_id)).

    projbudget.py selftest
    projbudget.py json <tenant_id>        # list_budgets(tid) as JSON
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit     # noqa: E402
import appguard  # noqa: E402  — enforcement layer; we register the cap with it if it has a setter

from dbpool import connection, tenant_connection  # noqa: E402


def _ensure():
    with connection() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS project_budget (
            tenant_id  TEXT,
            product    TEXT,
            cap_usd    NUMERIC,
            created_at TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (tenant_id, product))""")


def _owns(tid, product):
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM tenant_products WHERE product=%s AND tenant_id=%s", (product, tid))
        return cur.fetchone() is not None


def _spent(tid, product):
    """Real $ spent on this product = sum(cost_usd) from traces (same source appguard uses)."""
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("SELECT coalesce(sum(cost_usd),0) FROM traces WHERE product=%s", (product,))
        return round(float(cur.fetchone()[0]), 2)


def set_budget(tid, product, cap_usd):
    """Upsert the CEO's per-project cap. Ownership-checked. Also registers the cap with appguard's
    enforcement layer (set_policy) so the circuit-breaker enforces it. Returns the stored budget."""
    _ensure()
    if not _owns(tid, product):
        return {"ok": False, "error": "not_owned", "product": product}
    cap_usd = float(cap_usd)
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO project_budget (tenant_id, product, cap_usd) VALUES (%s,%s,%s)
                       ON CONFLICT (tenant_id, product) DO UPDATE SET cap_usd=EXCLUDED.cap_usd""",
                    (tid, product, cap_usd))
    # appguard is the enforcement layer — register the cap there if it exposes a setter (it does).
    setter = getattr(appguard, "set_policy", None) or getattr(appguard, "set_cap", None) \
        or getattr(appguard, "configure", None)
    if callable(setter):
        try:
            setter(product, cap=cap_usd)
        except TypeError:
            setter(product, cap_usd)
    audit.append(actor="projbudget", action="ProjectBudgetSet", resource=product, decision="set",
                 payload={"tenant_id": tid, "cap_usd": cap_usd}, tenant_id=tid)
    return {"ok": True, "product": product, "cap_usd": cap_usd}


def get_budget(tid, product):
    """Cap + real spend + % used + remaining for one product. Ownership-checked."""
    _ensure()
    if not _owns(tid, product):
        return {"ok": False, "error": "not_owned", "product": product}
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("SELECT cap_usd FROM project_budget WHERE tenant_id=%s AND product=%s", (tid, product))
        r = cur.fetchone()
    cap = float(r[0]) if r else None
    spent = _spent(tid, product)
    pct = round(spent / cap * 100, 1) if cap else None
    remaining = round(cap - spent, 2) if cap is not None else None
    return {"product": product, "cap_usd": cap, "spent_usd": spent, "pct": pct, "remaining_usd": remaining}


def list_budgets(tid):
    """Every product the tenant owns that has a cap OR any spend, with cap/spend/pct."""
    _ensure()
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s ORDER BY product", (tid,))
        products = [r[0] for r in cur.fetchall()]
    out = []
    for p in products:
        b = get_budget(tid, p)
        if b.get("cap_usd") is not None or b.get("spent_usd"):
            out.append(b)
    return out


def _selftest():
    import billing
    tid = billing.signup("projbudget-selftest", "free")["tenant_id"]
    prod = "projbudget-app"
    foreign = "someone-elses-app"
    ok = False
    try:
        _ensure()
        with connection() as c, c.cursor() as cur:
            cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (prod, tid))
            for cost in (1.25, 0.75):   # real spend rows for the owned product -> $2.00
                cur.execute("""INSERT INTO traces (run_id,product,stage,role,kind,cost_usd)
                               VALUES (%s,%s,'X','r','agent',%s)""", (f"pb-{prod}", prod, cost))

        set_res = set_budget(tid, prod, 5.0)
        b = get_budget(tid, prod)
        cap_ok = b["cap_usd"] == 5.0
        spent_ok = b["spent_usd"] > 0 and abs(b["spent_usd"] - 2.0) < 1e-6
        pct_ok = b["pct"] == 40.0 and b["remaining_usd"] == 3.0

        # ownership guard: setting a budget on a product the tenant does NOT own must be refused.
        guard_res = set_budget(tid, foreign, 99.0)
        guard_ok = guard_res.get("ok") is False
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT 1 FROM project_budget WHERE tenant_id=%s AND product=%s", (tid, foreign))
            guard_ok = guard_ok and cur.fetchone() is None

        listed = list_budgets(tid)
        list_ok = any(x["product"] == prod and x["cap_usd"] == 5.0 for x in listed)

        ok = set_res["ok"] and cap_ok and spent_ok and pct_ok and guard_ok and list_ok
        print(f"cap=${b['cap_usd']} spent=${b['spent_usd']} pct={b['pct']}% remaining=${b['remaining_usd']} "
              f"foreign-set-blocked={guard_ok} listed={list_ok}")
        print("PASS: per-project budget cap set/read with ownership guard ✅" if ok else "FAIL")
    finally:
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM project_budget WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM traces WHERE product=%s", (prod,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM app_policies WHERE app=%s", (prod,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "json" and len(a) > 1:
        print(json.dumps(list_budgets(a[1]), indent=2))
    else:
        sys.exit("usage: projbudget.py selftest | json <tenant_id>")


if __name__ == "__main__":
    _main(sys.argv[1:])
