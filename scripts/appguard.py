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
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit   # noqa: E402
import notify  # noqa: E402

from dbpool import connection, tenant_connection
PRODUCTS = Path.home() / "projects" / "products"
RATE_PER_1K = 0.003   # $/1k tokens — operating-spend proxy
# Liberal by default (backstops must not be conservative): a from-scratch full-stack Opus build (build + multi-
# round quality loop + agentic QA) legitimately spends real money, so a tight cap would strangle its own build.
# This is only a RUNAWAY hard cap; the loss-limit (below) guards a live product's profitability, and only once
# it's actually earning (revenue > 0). Both env-configurable.
DEFAULT_CAP = float(os.environ.get("AOS_APP_SPEND_CAP", "500.0"))
DEFAULT_LOSS = float(os.environ.get("AOS_APP_LOSS_LIMIT", "100.0"))


def _conn(tenant_id=None):
    return tenant_connection(tenant_id) if tenant_id else connection()


def _tenant_for(app):
    """Best-effort ownership lookup for product-keyed guard APIs."""
    if not app:
        return None
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT tenant_id FROM tenant_products WHERE product=%s ORDER BY created_at DESC LIMIT 1", (app,))
            r = cur.fetchone()
            return r[0] if r else None
    except Exception:
        return None


def _ensure():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS app_policies (app TEXT PRIMARY KEY,
                       spend_cap NUMERIC NOT NULL DEFAULT 100, loss_limit NUMERIC NOT NULL DEFAULT 20,
                       status TEXT NOT NULL DEFAULT 'active', reason TEXT,
                       updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS app_spend_reservations (
                       token TEXT PRIMARY KEY, app TEXT NOT NULL, amount NUMERIC NOT NULL CHECK (amount >= 0),
                       owner TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                       expires_at TIMESTAMPTZ NOT NULL)""")
        cur.execute("CREATE INDEX IF NOT EXISTS app_spend_reservations_app_idx "
                    "ON app_spend_reservations(app, expires_at)")


def _reservation_allowed(spend, reserved, requested, cap, status="active"):
    """Pure atomic-reservation decision used by the live transaction and tests."""
    if str(status) != "active":
        return False
    return float(spend) + float(reserved) + float(requested) <= float(cap)


def reserve_call(app, amount, *, owner=None, lease_s=1800):
    """Atomically reserve worst-case headroom before starting a paid provider call.

    The old pre-spend check read only completed traces, so two concurrent calls could both start just below the
    cap and overshoot it together. This transaction serializes admission per app and counts every unexpired
    in-flight reservation. It returns a token that must be settled or released by the provider wrapper.
    """
    if not app:
        return {"ok": True, "token": None, "amount": 0.0}
    amount = max(0.0, float(amount or 0.0))
    token = uuid.uuid4().hex
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"app-spend:{app}",))
        cur.execute("DELETE FROM app_spend_reservations WHERE expires_at <= now()")
        cur.execute("""INSERT INTO app_policies(app,spend_cap,loss_limit,status)
                       VALUES (%s,%s,%s,'active') ON CONFLICT (app) DO NOTHING""",
                    (app, DEFAULT_CAP, DEFAULT_LOSS))
        cur.execute("SELECT spend_cap,status,reason FROM app_policies WHERE app=%s FOR UPDATE", (app,))
        cap, status, reason = cur.fetchone()
        cur.execute("SELECT COALESCE(sum(cost_usd),0) FROM traces WHERE product=%s", (app,))
        spend = float(cur.fetchone()[0] or 0)
        cur.execute("SELECT COALESCE(sum(amount),0) FROM app_spend_reservations "
                    "WHERE app=%s AND expires_at > now()", (app,))
        reserved = float(cur.fetchone()[0] or 0)
        if not _reservation_allowed(spend, reserved, amount, cap, status):
            return {"ok": False, "token": None, "app": app, "spend": spend,
                    "reserved": reserved, "requested": amount, "cap": float(cap),
                    "reason": reason or ("app is paused" if status != "active" else
                    "paid call would exceed the app spend cap")}
        cur.execute("""INSERT INTO app_spend_reservations(token,app,amount,owner,expires_at)
                       VALUES (%s,%s,%s,%s,now()+(%s || ' seconds')::interval)""",
                    (token, app, amount, str(owner or "")[:300], str(max(60, int(lease_s)))))
    return {"ok": True, "token": token, "app": app, "amount": amount,
            "spend": spend, "reserved_before": reserved, "cap": float(cap)}


def settle_call(token, actual_cost=0.0, *, trace_grace_s=5):
    """Keep actual cost reserved briefly while the caller writes its trace, then let expiry remove it."""
    if not token:
        return
    actual = max(0.0, float(actual_cost or 0.0))
    with _conn() as c, c.cursor() as cur:
        if actual <= 0 or int(trace_grace_s) <= 0:
            cur.execute("DELETE FROM app_spend_reservations WHERE token=%s", (token,))
        else:
            cur.execute("""UPDATE app_spend_reservations SET amount=%s,
                           expires_at=now()+(%s || ' seconds')::interval WHERE token=%s""",
                        (actual, str(max(1, int(trace_grace_s))), token))


def _policy(app):
    _ensure()
    # app_policies is a deliberately global, owner-only financial control table. Tenant RLS applies to the
    # usage facts, not to this cross-tenant circuit-breaker ledger.
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT spend_cap, loss_limit, status, reason FROM app_policies WHERE app=%s", (app,))
        r = cur.fetchone()
    if r:
        return {"app": app, "spend_cap": float(r[0]), "loss_limit": float(r[1]), "status": r[2], "reason": r[3]}
    return {"app": app, "spend_cap": DEFAULT_CAP, "loss_limit": DEFAULT_LOSS, "status": "active", "reason": None}


def set_policy(app, cap=None, loss=None):
    _ensure()
    p = _policy(app)
    with _conn() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO app_policies (app, spend_cap, loss_limit) VALUES (%s,%s,%s)
                       ON CONFLICT (app) DO UPDATE SET spend_cap=EXCLUDED.spend_cap,
                         loss_limit=EXCLUDED.loss_limit, updated_at=now()""",
                    (app, cap if cap is not None else p["spend_cap"], loss if loss is not None else p["loss_limit"]))


def economics(app):
    """Recorded model-cost estimate from traces and attributed revenue.

    API-backed providers may report billed cost directly, while Codex CLI calls are
    estimated from recorded tokens and the explicitly selected model.  Calling the
    aggregate "real spend" was misleading, especially for subscription-backed CLI
    calls and legacy traces that did not retain model/cache provenance.
    """
    tenant_id = _tenant_for(app)
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT coalesce(sum(cost_usd),0), coalesce(sum(tokens_in+tokens_out),0)
                       FROM traces WHERE product=%s""", (app,))
        spend, tokens = cur.fetchone()
    spend = round(float(spend), 2)
    revenue = 0.0   # real once a product is sold to paying tenants (attribution wired, $0 today)
    return {"tokens": int(tokens), "spend": spend, "revenue": revenue, "profit": round(revenue - spend, 2)}


def pause(app, reason, policy=None):
    # A first pause used to INSERT only (app,status,reason), silently applying the table's historical
    # 100/20 defaults even though the decision had just been made against the current 500/100 defaults.
    # The resulting row claimed a different authority envelope from the one that actually fired. Persist the
    # exact evaluated envelope so status, notifications, and a later resume all tell the same truth.
    policy = dict(policy or _policy(app))
    spend_cap = float(policy.get("spend_cap", DEFAULT_CAP))
    loss_limit = float(policy.get("loss_limit", DEFAULT_LOSS))
    tenant_id = _tenant_for(app)
    with _conn(tenant_id) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO app_policies (app, spend_cap, loss_limit, status, reason)
                       VALUES (%s,%s,%s,'paused',%s)
                       ON CONFLICT (app) DO UPDATE SET status='paused', reason=EXCLUDED.reason,
                         spend_cap=EXCLUDED.spend_cap, loss_limit=EXCLUDED.loss_limit, updated_at=now()""",
                    (app, spend_cap, loss_limit, reason))
    # drop a friendly pause page in the product
    d = PRODUCTS / app
    if d.exists():
        body = (
            "<!doctype html><meta charset=utf-8><title>Paused</title>"
            "<style>body{background:#0a0d13;color:#d7dee8;font:16px system-ui;display:grid;place-items:center;"
            "height:100vh;margin:0;text-align:center}div{max-width:30rem;padding:2rem}h1{color:#d29922}</style>"
            f"<div><h1>{app} is paused</h1><p>We've temporarily paused this app while we sort out its budget. "
            "We'll be back soon — thanks for your patience.</p></div>")
        target = d / "PAUSED.html"
        try:
            current = target.read_text() if target.exists() else None
        except OSError:
            current = None
        if current != body:
            tmp = d / f".PAUSED.html.{os.getpid()}.tmp"
            try:
                tmp.write_text(body)
                tmp.replace(target)
            finally:
                tmp.unlink(missing_ok=True)
    audit.append(actor="appguard", action="PauseApp", resource=app, decision="paused",
                 payload={"reason": reason}, tenant_id=tenant_id)
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
        pause(app, why, policy=p)
        return {"app": app, "state": "paused", "reason": why, **e}
    return {"app": app, "state": "active", **e}


def _apps():
    # Enumerate guard targets from the SAME ledger economics() measures real spend on (traces),
    # UNION the metrics rollup (org_metrics) so an app is evaluated the moment it spends a cent —
    # not only once it happens to emit an org_metrics row. Enumerating from org_metrics alone
    # silently skipped whole product classes (custom agents, project builds, research fleets) that
    # write cost_usd to traces but never call metrics.record, letting them bleed money uncapped.
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT DISTINCT product FROM traces WHERE product IS NOT NULL
                       UNION
                       SELECT DISTINCT product FROM org_metrics WHERE product IS NOT NULL""")
        return [r[0] for r in cur.fetchall()]


def _guard_rows():
    """One owner-side snapshot for the global financial control plane.

    The old hourly sweep opened roughly three transactions per app (over 1,000 apps in the current ledger),
    which made a protective control slow and allowed one tenant-role policy error to abort the entire tail.
    """
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""WITH apps AS (
                         SELECT DISTINCT product AS app FROM traces WHERE product IS NOT NULL
                         UNION
                         SELECT DISTINCT product AS app FROM org_metrics WHERE product IS NOT NULL
                       ), spend AS (
                         SELECT product AS app,
                                COALESCE(sum(cost_usd),0) AS spend,
                                COALESCE(sum(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)),0) AS tokens
                           FROM traces WHERE product IS NOT NULL GROUP BY product
                       )
                       SELECT a.app,
                              COALESCE(p.spend_cap,%s::numeric),
                              COALESCE(p.loss_limit,%s::numeric),
                              COALESCE(p.status,'active'), p.reason,
                              COALESCE(s.spend,0), COALESCE(s.tokens,0)
                         FROM apps a
                         LEFT JOIN app_policies p ON p.app=a.app
                         LEFT JOIN spend s ON s.app=a.app
                        ORDER BY a.app""", (DEFAULT_CAP, DEFAULT_LOSS))
        return cur.fetchall()


def guard():
    paused, errors, evaluated = [], [], 0
    for app, cap, loss, status, prior_reason, spend, tokens in _guard_rows():
        try:
            evaluated += 1
            economics_row = {"tokens": int(tokens), "spend": round(float(spend), 2),
                             "revenue": 0.0, "profit": round(-float(spend), 2)}
            if status == "paused":
                paused.append(app)
                continue
            policy = {"app": app, "spend_cap": float(cap), "loss_limit": float(loss),
                      "status": status, "reason": prior_reason}
            why = _pause_reason(economics_row, policy)
            if why:
                pause(app, why, policy=policy)
                paused.append(app)
        except Exception as exc:
            # One corrupt app or unavailable notification must not blind the guard for every later app.
            errors.append({"app": str(app), "error": f"{type(exc).__name__}: {str(exc)[:180]}"})
    return {"evaluated": evaluated, "paused": paused, "failed": len(errors), "errors": errors[:20]}


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
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT app, reason FROM app_policies WHERE status='paused' ORDER BY updated_at DESC")
        return [{"app": a, "reason": r} for a, r in cur.fetchall()]


def resume(app):
    """Human-approved un-pause (raising/clearing a spend limit is a spend decision)."""
    tenant_id = _tenant_for(app)
    with _conn() as c, c.cursor() as cur:
        cur.execute("UPDATE app_policies SET status='active', reason=NULL, updated_at=now() WHERE app=%s", (app,))
    (PRODUCTS / app / "PAUSED.html").unlink(missing_ok=True)
    audit.append(actor="human", action="ResumeApp", resource=app, decision="approved", tenant_id=tenant_id)
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
        result = guard()
        print(json.dumps(result, indent=2))
        if result["failed"]:
            raise SystemExit(2)
    elif a[0] == "resume":
        print(json.dumps(resume(a[1]), indent=2))
    elif a[0] == "selftest":
        import os
        ok = True

        def _spend(app, usd, rev=0.0):
            with _conn(_tenant_for(app)) as c, c.cursor() as cur:
                cur.execute("""INSERT INTO traces (run_id,product,stage,role,kind,cost_usd)
                               VALUES (%s,%s,'X','r','agent',%s)""", (f"g-{app}", app, usd))
                if rev:
                    try:
                        cur.execute("""INSERT INTO org_metrics (product, revenue_usd) VALUES (%s,%s)""", (app, rev))
                    except Exception:
                        pass

        def _clean(app):
            with _conn(_tenant_for(app)) as c, c.cursor() as cur:
                cur.execute("DELETE FROM app_policies WHERE app=%s", (app,))
                cur.execute("DELETE FROM traces WHERE product=%s", (app,))
                try:
                    cur.execute("DELETE FROM org_metrics WHERE product=%s", (app,))
                except Exception:
                    pass

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
