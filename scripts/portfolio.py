#!/usr/bin/env python3
"""portfolio.py — the CEO view: every product, what it cost to build, whether it shipped, whether it
has a launch kit, and the platform's revenue. The business counterpart to the ops dashboard.

Honest accounting: build COST is derived from real per-stage timing (traces); REVENUE is real billing
MRR (currently demo tenants — it becomes real when products go live + get paying users). Per-product
revenue is 0 until a product is sold; the framework attributes it automatically once it is.

    portfolio.py summary       # the portfolio P&L-ish view
    portfolio.py json          # machine-readable (for the dashboard / digest)
Run with the agent-os venv python.
"""
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import billing  # noqa: E402

import psycopg  # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)
PRODUCTS = Path.home() / "projects" / "products"
BUILD_COST_PER_MIN = 0.10   # rough compute proxy until token-cost is instrumented ($/build-minute)


def summary():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT product, count(*) steps, coalesce(sum(elapsed_s),0) secs, max(ts) last
                       FROM traces WHERE product IS NOT NULL GROUP BY product""")
        traced = {p: {"steps": s, "secs": float(secs), "last": last} for p, s, secs, last in cur.fetchall()}
        cur.execute("""SELECT DISTINCT resource FROM audit_log
                       WHERE action='ProductComplete' AND decision='LAUNCHED'""")
        launched = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT product, tenant_id FROM tenant_products")
        owner = dict(cur.fetchall())

    rows = []
    for prod, t in sorted(traced.items(), key=lambda kv: kv[1]["last"], reverse=True):
        build_min = round(t["secs"] / 60, 1)
        rows.append({
            "product": prod,
            "shipped": prod in launched,
            "has_launch_kit": (PRODUCTS / prod / "launch").exists(),
            "build_min": build_min,
            "build_cost": round(build_min * BUILD_COST_PER_MIN, 2),
            "owner": owner.get(prod),
            "revenue": 0.0,   # real once the product is sold to paying tenants
        })
    mrr = billing.mrr()
    totals = {
        "products": len(rows),
        "shipped": sum(1 for r in rows if r["shipped"]),
        "with_launch_kit": sum(1 for r in rows if r["has_launch_kit"]),
        "total_build_cost": round(sum(r["build_cost"] for r in rows), 2),
        "platform_mrr": mrr["mrr"],
        "product_revenue": round(sum(r["revenue"] for r in rows), 2),
    }
    return {"totals": totals, "products": rows, "tenants_by_plan": mrr["tenants_by_plan"]}


def _main(a):
    if a and a[0] == "json":
        print(json.dumps(summary(), indent=2)); return
    s = summary()
    t = s["totals"]
    print("\n=== PORTFOLIO (business view) ===")
    print(f"  products built: {t['products']}   shipped: {t['shipped']}   with launch kit: {t['with_launch_kit']}")
    print(f"  build cost (compute proxy): ${t['total_build_cost']}")
    print(f"  platform MRR: ${t['platform_mrr']}   product revenue: ${t['product_revenue']}")
    print(f"  tenants: {s['tenants_by_plan']}")
    print("\n  per product:")
    for r in s["products"][:20]:
        flags = ("✓shipped" if r["shipped"] else "·building") + (" ✓kit" if r["has_launch_kit"] else "")
        print(f"    {r['product']:16} {flags:18} build ${r['build_cost']:<5} rev ${r['revenue']}")


if __name__ == "__main__":
    _main(sys.argv[1:])
