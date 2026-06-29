#!/usr/bin/env python3
"""appguard.py — the per-app financial circuit-breaker. Stops an app from bleeding money.

Each app has a hard spend cap and a loss limit. Operating spend is derived from real token usage
(org_metrics); revenue from billing attribution. When an app exceeds its cap, or its profit drops below
-loss_limit, the guard AUTO-PAUSES it: it stops further spend/deploy, drops a friendly "out of budget"
pause page, audits, and pages you. Auto-pause is SAFE (protective) and needs no approval; RESUMING or
raising a limit is a spend decision and requires your approval.

    appguard.py status [app]              # policy + economics + state
    appguard.py set <app> --cap N --loss N
    appguard.py guard                      # evaluate ALL apps, auto-pause the bleeders (run on a schedule)
    appguard.py resume <app>               # human-approved un-pause
    appguard.py selftest
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit   # noqa: E402
import notify  # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)
PRODUCTS = Path.home() / "projects" / "products"
RATE_PER_1K = 0.003   # $/1k tokens — operating-spend proxy
DEFAULT_CAP, DEFAULT_LOSS = 100.0, 20.0


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS app_policies (app TEXT PRIMARY KEY,
                       spend_cap NUMERIC NOT NULL DEFAULT 100, loss_limit NUMERIC NOT NULL DEFAULT 20,
                       status TEXT NOT NULL DEFAULT 'active', reason TEXT,
                       updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        c.commit()


def _policy(app):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT spend_cap, loss_limit, status, reason FROM app_policies WHERE app=%s", (app,))
        r = cur.fetchone()
    if r:
        return {"app": app, "spend_cap": float(r[0]), "loss_limit": float(r[1]), "status": r[2], "reason": r[3]}
    return {"app": app, "spend_cap": DEFAULT_CAP, "loss_limit": DEFAULT_LOSS, "status": "active", "reason": None}


def set_policy(app, cap=None, loss=None):
    _ensure()
    p = _policy(app)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO app_policies (app, spend_cap, loss_limit) VALUES (%s,%s,%s)
                       ON CONFLICT (app) DO UPDATE SET spend_cap=EXCLUDED.spend_cap,
                         loss_limit=EXCLUDED.loss_limit, updated_at=now()""",
                    (app, cap if cap is not None else p["spend_cap"], loss if loss is not None else p["loss_limit"]))
        c.commit()


def economics(app):
    """Operating spend (REAL $ from claude usage in traces) and revenue (billing attribution)."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT coalesce(sum(cost_usd),0), coalesce(sum(tokens_in+tokens_out),0)
                       FROM traces WHERE product=%s""", (app,))
        spend, tokens = cur.fetchone()
    spend = round(float(spend), 2)
    revenue = 0.0   # real once a product is sold to paying tenants (attribution wired, $0 today)
    return {"tokens": int(tokens), "spend": spend, "revenue": revenue, "profit": round(revenue - spend, 2)}


def pause(app, reason):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO app_policies (app, status, reason) VALUES (%s,'paused',%s)
                       ON CONFLICT (app) DO UPDATE SET status='paused', reason=EXCLUDED.reason, updated_at=now()""",
                    (app, reason))
        c.commit()
    # drop a friendly pause page in the product
    d = PRODUCTS / app
    if d.exists():
        (d / "PAUSED.html").write_text(
            "<!doctype html><meta charset=utf-8><title>Paused</title>"
            "<style>body{background:#0a0d13;color:#d7dee8;font:16px system-ui;display:grid;place-items:center;"
            "height:100vh;margin:0;text-align:center}div{max-width:30rem;padding:2rem}h1{color:#d29922}</style>"
            f"<div><h1>{app} is paused</h1><p>We've temporarily paused this app while we sort out its budget. "
            "We'll be back soon — thanks for your patience.</p></div>")
    audit.append(actor="appguard", action="PauseApp", resource=app, decision="paused", payload={"reason": reason})
    notify.send(f"⏸ {app} auto-paused — {reason}", title="app circuit-breaker", priority="high", tags="pause_button")


def evaluate(app):
    p = _policy(app)
    e = economics(app)
    if p["status"] == "paused":
        return {"app": app, "state": "paused", **e}
    over_cap = e["spend"] >= p["spend_cap"]
    losing = e["profit"] <= -p["loss_limit"]
    if over_cap or losing:
        why = (f"spend ${e['spend']} ≥ cap ${p['spend_cap']}" if over_cap
               else f"losing ${-e['profit']} (limit ${p['loss_limit']}) with ${e['revenue']} revenue")
        pause(app, why)
        return {"app": app, "state": "paused", "reason": why, **e}
    return {"app": app, "state": "active", **e}


def _apps():
    # Enumerate guard targets from the SAME ledger economics() measures real spend on (traces),
    # UNION the metrics rollup (org_metrics) so an app is evaluated the moment it spends a cent —
    # not only once it happens to emit an org_metrics row. Enumerating from org_metrics alone
    # silently skipped whole product classes (custom agents, project builds, research fleets) that
    # write cost_usd to traces but never call metrics.record, letting them bleed money uncapped.
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT DISTINCT product FROM traces WHERE product IS NOT NULL
                       UNION
                       SELECT DISTINCT product FROM org_metrics WHERE product IS NOT NULL""")
        return [r[0] for r in cur.fetchall()]


def guard():
    results = [evaluate(a) for a in _apps()]
    paused = [r for r in results if r["state"] == "paused"]
    return {"evaluated": len(results), "paused": [p["app"] for p in paused]}


def paused_apps():
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT app, reason FROM app_policies WHERE status='paused' ORDER BY updated_at DESC")
        return [{"app": a, "reason": r} for a, r in cur.fetchall()]


def resume(app):
    """Human-approved un-pause (raising/clearing a spend limit is a spend decision)."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE app_policies SET status='active', reason=NULL, updated_at=now() WHERE app=%s", (app,))
        c.commit()
    (PRODUCTS / app / "PAUSED.html").unlink(missing_ok=True)
    audit.append(actor="human", action="ResumeApp", resource=app, decision="approved")
    return {"app": app, "state": "active"}


def _main(a):
    import json
    if not a or a[0] == "status":
        if len(a) > 1:
            print(json.dumps({**_policy(a[1]), **economics(a[1])}, indent=2))
        else:
            for app in _apps():
                p, e = _policy(app), economics(app)
                print(f"  {app:16} {p['status']:7} spend ${e['spend']:<6} cap ${p['spend_cap']} loss-limit ${p['loss_limit']}")
    elif a[0] == "set":
        cap = float(a[a.index("--cap") + 1]) if "--cap" in a else None
        loss = float(a[a.index("--loss") + 1]) if "--loss" in a else None
        set_policy(a[1], cap, loss); print("policy set")
    elif a[0] == "guard":
        print(json.dumps(guard(), indent=2))
    elif a[0] == "resume":
        print(json.dumps(resume(a[1]), indent=2))
    elif a[0] == "selftest":
        import os
        app = f"guard-demo-{os.urandom(3).hex()}"
        set_policy(app, cap=100, loss=20)
        # real operating spend with no revenue: a $24 agent step (over the $20 loss limit)
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO traces (run_id,product,stage,role,kind,cost_usd)
                           VALUES (%s,%s,'X','r','agent',24)""", (f"g-{app}", app))
            c.commit()
        before = _policy(app)["status"]
        r = evaluate(app)
        after = _policy(app)["status"]
        ok = before == "active" and after == "paused" and r["state"] == "paused"
        with psycopg.connect(DB) as c, c.cursor() as cur:   # cleanup
            cur.execute("DELETE FROM app_policies WHERE app=%s", (app,))
            cur.execute("DELETE FROM traces WHERE product=%s", (app,)); c.commit()
        print(f"app spent $24 with $0 revenue -> auto-paused: {before}→{after} ({r.get('reason')})")
        print("PASS: per-app circuit-breaker auto-pause ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
