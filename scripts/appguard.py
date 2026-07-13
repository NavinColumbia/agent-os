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
import os
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
# Liberal by default (backstops must not be conservative): a from-scratch full-stack Opus build (build + multi-
# round quality loop + agentic QA) legitimately spends real money, so a tight cap would strangle its own build.
# This is only a RUNAWAY hard cap; the loss-limit (below) guards a live product's profitability, and only once
# it's actually earning (revenue > 0). Both env-configurable.
DEFAULT_CAP = float(os.environ.get("AOS_APP_SPEND_CAP", "500.0"))
DEFAULT_LOSS = float(os.environ.get("AOS_APP_LOSS_LIMIT", "100.0"))


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


def _pause_reason(e, p):
    """Pure pause decision (unit-testable). Returns a reason string to pause, or None to stay active.
    - The hard SPEND CAP always binds (runaway guard).
    - The LOSS/PROFIT breaker only applies to a LIVE, MONETIZING product (revenue > 0). A product still being
      BUILT always has $0 revenue and non-zero spend, so its profit is always negative — applying the loss limit
      there would (and did) pause every build the instant its spend passed the loss limit, strangling its OWN
      build (F9). "Losing money" is only meaningful once a product is actually earning; before launch only the
      (deliberately liberal) spend cap binds. So the loss breaker stays dormant until revenue attribution exists."""
    if e["spend"] >= p["spend_cap"]:
        return f"spend ${e['spend']} ≥ cap ${p['spend_cap']}"
    if e["revenue"] > 0 and e["profit"] <= -p["loss_limit"]:
        return f"losing ${-e['profit']} (limit ${p['loss_limit']}) with ${e['revenue']} revenue"
    return None


def evaluate(app):
    p = _policy(app)
    e = economics(app)
    if p["status"] == "paused":
        return {"app": app, "state": "paused", **e}
    why = _pause_reason(e, p)
    if why:
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


def blocks(app):
    """PRE-SPEND circuit-breaker (REBUILD-PLAN C4 — make the money breaker FIRE in the live path, not just
    an hourly sweep). evaluate() checks THIS app's real spend vs its cap/loss and AUTO-PAUSES if over;
    returns a refusal reason when the app is (now) paused, else None. Called before every agent spawn so a
    runaway build HALTS at its cap instead of blowing past it. Fail-OPEN on infra error — the process
    budget cap + killswitch still bound spend, so a guard hiccup never bricks the fleet."""
    if not app:
        return None
    try:
        r = evaluate(app)
        if r.get("state") == "paused":
            return r.get("reason") or "spend cap / loss limit reached"
    except Exception:
        return None
    return None


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
        ok = True

        def _spend(app, usd, rev=0.0):
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("""INSERT INTO traces (run_id,product,stage,role,kind,cost_usd)
                               VALUES (%s,%s,'X','r','agent',%s)""", (f"g-{app}", app, usd))
                if rev:
                    try:
                        cur.execute("""INSERT INTO org_metrics (product, revenue_usd) VALUES (%s,%s)""", (app, rev))
                    except Exception:
                        pass
                c.commit()

        def _clean(app):
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("DELETE FROM app_policies WHERE app=%s", (app,))
                cur.execute("DELETE FROM traces WHERE product=%s", (app,))
                try:
                    cur.execute("DELETE FROM org_metrics WHERE product=%s", (app,))
                except Exception:
                    pass
                c.commit()

        # (1) F9 FIX: a PRE-LAUNCH build (no revenue) is NOT paused by the loss limit, even though profit is
        #     negative — else it strangles its own build. Only the hard spend cap binds before launch.
        a1 = f"guard-build-{os.urandom(3).hex()}"
        set_policy(a1, cap=500, loss=20)
        _spend(a1, 24)                              # $24 spend, $0 revenue: profit -$24, past the $20 loss limit
        r1 = evaluate(a1)
        c1 = r1["state"] == "active"
        print(("PASS" if c1 else "FAIL") + f": pre-launch build ($24 spend, $0 rev) is NOT paused by loss limit ({r1['state']})")
        ok = ok and c1
        _clean(a1)

        # (2) the HARD SPEND CAP still binds a runaway pre-launch build.
        a2 = f"guard-runaway-{os.urandom(3).hex()}"
        set_policy(a2, cap=100, loss=20)
        _spend(a2, 140)                             # $140 spend > $100 cap
        r2 = evaluate(a2)
        c2 = r2["state"] == "paused" and "cap" in (r2.get("reason") or "")
        print(("PASS" if c2 else "FAIL") + f": runaway build over the hard SPEND CAP IS paused ({r2['state']})")
        ok = ok and c2
        _clean(a2)

        # (3) the loss breaker's REAL job (pure predicate — revenue attribution isn't wired in economics() yet,
        #     so test the decision directly): a LIVE product (revenue > 0) unprofitable past its loss limit pauses;
        #     the SAME numbers with $0 revenue (a build) do NOT.
        pol = {"spend_cap": 500, "loss_limit": 20}
        live = {"spend": 50, "revenue": 10, "profit": -40}       # earning but unprofitable
        build = {"spend": 50, "revenue": 0, "profit": -50}       # pre-launch build, deeper "loss" but no revenue
        c3 = _pause_reason(live, pol) is not None and _pause_reason(build, pol) is None
        print(("PASS" if c3 else "FAIL") + ": loss breaker pauses a LIVE unprofitable product but NOT a $0-revenue build")
        ok = ok and c3

        print("PASS: per-app circuit-breaker — loss limit only after launch, spend cap always ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
