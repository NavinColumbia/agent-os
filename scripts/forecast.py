#!/usr/bin/env python3
"""forecast.py — budget burn-rate + projection + PRE-EMPTIVE alerts ("am I about to blow my budget?").

The #1 anxiety of a non-technical CEO paying per token: the existing surfaces are descriptive (spend so
far) and the caps are deny-only (you hit a wall with no warning). This adds the PREDICTIVE piece — derive
a burn-rate from the traces time-series, project spend/tokens to end of the 30-day window, compare to the
plan quota, and fire a pre-emptive notification at 80% and 100% of projection (standard, then urgent). A
scheduler job runs check_alerts across tenants. Idempotent per threshold (won't spam).

    forecast.py json <tenant_id>     # the forecast payload
    forecast.py check <tenant_id>    # compute + send threshold alerts if crossed
    forecast.py sweep                # check_alerts for every tenant with usage (scheduler job)
    forecast.py selftest
Run with the agent-os venv python.
"""
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit          # noqa: E402
import billing        # noqa: E402
import notifications  # noqa: E402

from dbpool import connection, tenant_connection
WINDOW_DAYS = 30          # the billing/projection window
THRESHOLDS = [80, 100]    # pre-emptive alert points (% of projected-vs-quota)
SWEEP_LIMIT = max(1, min(200, int(os.environ.get("AOS_FORECAST_SWEEP_LIMIT", "25"))))
_ensured = False
_ensure_lock = threading.Lock()


def _ensure():
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        with connection() as c, c.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout='1s'")
            cur.execute("SET LOCAL statement_timeout='3s'")
            cur.execute("""CREATE TABLE IF NOT EXISTS budget_alert_state (
                tenant_id TEXT NOT NULL, threshold INT NOT NULL, alerted_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (tenant_id, threshold))""")
            cur.execute("""CREATE TABLE IF NOT EXISTS forecast_sweep_state (
                name TEXT PRIMARY KEY, cursor_tenant_id TEXT NOT NULL DEFAULT '')""")
            cur.execute("""INSERT INTO forecast_sweep_state(name, cursor_tenant_id)
                           VALUES ('forecast-sweep','') ON CONFLICT (name) DO NOTHING""")
        _ensured = True


def _alert_context(threshold, now=None):
    """Stable semantic key for one threshold in one billing month."""
    current = now or datetime.now(timezone.utc)
    return f"budget-forecast:{current.astimezone(timezone.utc):%Y-%m}:{int(threshold)}"


def forecast(tid):
    """Burn-rate from the traces time-series + projection to the window end vs the plan token quota."""
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s", (tid,))
        prods = [r[0] for r in cur.fetchall()]
        used_tokens = used_usd = days_active = 0
        if prods:
            cur.execute("""SELECT COALESCE(sum(tokens_in+tokens_out),0), COALESCE(sum(cost_usd),0),
                                  GREATEST(1, EXTRACT(DAY FROM now() - min(ts))::int)
                           FROM traces WHERE product = ANY(%s) AND ts > now() - interval '30 days'""", (prods,))
            used_tokens, used_usd, days_active = cur.fetchone()
    used_tokens = int(used_tokens or 0); used_usd = float(used_usd or 0); days_active = max(1, int(days_active or 1))
    try:
        plan, _ = billing._plan_of(tid)
    except Exception:
        plan = "free"
    quota_tokens = billing.PLANS.get(plan, billing.PLANS["free"])["tokens"]
    burn_tok = used_tokens / days_active                     # tokens/day
    burn_usd = used_usd / days_active                        # $/day
    projected_tokens = int(burn_tok * WINDOW_DAYS)
    projected_usd = round(burn_usd * WINDOW_DAYS, 2)
    pct = round(100 * projected_tokens / quota_tokens, 1) if quota_tokens else 0.0
    # days until the quota is hit at the current burn rate (None if not trending to exceed)
    remaining = max(0, quota_tokens - used_tokens)
    eta_days = round(remaining / burn_tok, 1) if burn_tok > 0 else None
    level = "ok" if pct < 80 else ("warn" if pct < 100 else "over")
    return {"tenant": tid, "plan": plan, "window_days": WINDOW_DAYS,
            "used_tokens": used_tokens, "used_usd": round(used_usd, 2),
            "burn_tokens_per_day": int(burn_tok), "burn_usd_per_day": round(burn_usd, 4),
            "quota_tokens": quota_tokens, "projected_tokens": projected_tokens, "projected_usd": projected_usd,
            "pct_of_quota_projected": pct, "will_exceed": pct >= 100, "eta_days_to_quota": eta_days,
            "level": level,
            "headline": (f"On track: ~{pct}% of your {plan} token quota this month" if level == "ok"
                         else f"Heads up: projected to reach ~{pct}% of your {plan} quota"
                         + (f" in ~{eta_days}d" if eta_days else ""))}


def check_alerts(tid):
    """Fire a pre-emptive notification when PROJECTED usage first crosses 80% / 100%. Idempotent."""
    _ensure()
    f = forecast(tid)
    pct = f["pct_of_quota_projected"]
    fired = []
    for th in THRESHOLDS:
        if pct >= th:
            with tenant_connection(tid) as c, c.cursor() as cur:
                cur.execute("SELECT 1 FROM budget_alert_state WHERE tenant_id=%s AND threshold=%s", (tid, th))
                if cur.fetchone():
                    continue
            lvl = "urgent" if th >= 100 else "standard"
            # Never hold a database transaction while doing transport work.  The notification context is the
            # semantic outbox key: a crash after send but before the state marker reuses the same feed/delivery
            # record on retry, while ON CONFLICT below makes concurrent sweepers converge.
            try:
                notifications.send(tid, "billing",
                                   f"Budget alert: projected ~{pct}% of your {f['plan']} quota",
                                   f"At your current burn rate you're trending to {pct}% of the monthly token "
                                   f"quota{(' in ~'+str(f['eta_days_to_quota'])+' days') if f['eta_days_to_quota'] else ''}. "
                                   f"Raise your plan or slow builds to avoid a hard stop.", level=lvl,
                                   context_key=_alert_context(th))
            except Exception as e:
                print(f"forecast: budget alert send failed for {tid} @ {th}% (will retry next run): {e}",
                      file=sys.stderr)
                continue
            with tenant_connection(tid) as c, c.cursor() as cur:
                cur.execute("""INSERT INTO budget_alert_state (tenant_id, threshold) VALUES (%s,%s)
                               ON CONFLICT (tenant_id, threshold) DO NOTHING""", (tid, th))
                if cur.rowcount:
                    fired.append(th)
        else:
            with tenant_connection(tid) as c, c.cursor() as cur:
                cur.execute("DELETE FROM budget_alert_state WHERE tenant_id=%s AND threshold=%s", (tid, th))
    if fired:
        audit.append(actor="forecast", action="BudgetAlert", resource=tid, decision="alerted",
                     payload={"pct": pct, "thresholds": fired}, tenant_id=tid)
    return {"pct": pct, "level": f["level"], "alerted": fired}


def _claim_sweep_page(limit=SWEEP_LIMIT):
    """Atomically reserve one bounded, rotating tenant page without holding the lock during work."""
    _ensure()
    with connection() as c, c.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout='1s'")
        cur.execute("SET LOCAL statement_timeout='3s'")
        cur.execute("""SELECT cursor_tenant_id FROM forecast_sweep_state
                       WHERE name='forecast-sweep' FOR UPDATE""")
        cursor = (cur.fetchone() or [""])[0]
        cur.execute("""SELECT DISTINCT tenant_id FROM tenant_products
                       WHERE tenant_id > %s ORDER BY tenant_id LIMIT %s""", (cursor, limit))
        tids = [r[0] for r in cur.fetchall()]
        if not tids and cursor:
            cur.execute("""SELECT DISTINCT tenant_id FROM tenant_products
                           ORDER BY tenant_id LIMIT %s""", (limit,))
            tids = [r[0] for r in cur.fetchall()]
        if tids:
            cur.execute("""UPDATE forecast_sweep_state SET cursor_tenant_id=%s
                           WHERE name='forecast-sweep'""", (tids[-1],))
    return tids


def sweep(limit=SWEEP_LIMIT):
    """Check one bounded rotating tenant page; persistent prefixes cannot starve the tail."""
    tids = _claim_sweep_page(limit)
    out = [{"tenant": t, **check_alerts(t)} for t in tids]
    return {"checked": len(out), "alerted": [o for o in out if o["alerted"]]}


def _selftest():
    reg = billing.signup("forecast-selftest", "free")        # free: 100k token quota
    tid = reg["tenant_id"]; prod = tid.replace("t-", "")[:6] + "-demo"
    with connection() as c, c.cursor() as cur:
        cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (prod, tid))
        # 5 days ago start, heavy usage so projection blows past the 100k quota -> should alert
        cur.execute("""INSERT INTO traces (run_id, product, stage, role, kind, rc, cost_usd, tokens_in, tokens_out, elapsed_s, ts, prompt, output, model)
                       VALUES (%s,%s,'BUILD','builder','agent',0,4.0,30000,30000,40, now()-interval '4 days','p','o','m')""", (f"r1-{prod}", prod))
    try:
        f = forecast(tid)
        projects = f["projected_tokens"] > f["used_tokens"] and f["burn_tokens_per_day"] > 0
        trending = f["pct_of_quota_projected"] >= 100 and f["will_exceed"]   # 60k/4d*30 = 450k >> 100k
        a1 = check_alerts(tid)                                # first call fires 80 + 100
        a2 = check_alerts(tid)                                # second call is idempotent (no re-fire)
        idempotent = bool(a1["alerted"]) and not a2["alerted"]
        ok = projects and trending and 100 in a1["alerted"] and idempotent
        print(f"projected={f['projected_tokens']} tok ({f['pct_of_quota_projected']}%) eta={f['eta_days_to_quota']}d "
              f"alerted={a1['alerted']} re-fire={a2['alerted']}")
        print("PASS: forecast projects burn-rate + fires pre-emptive alerts once ✅" if ok else "FAIL")
    finally:
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM traces WHERE product=%s", (prod,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM budget_alert_state WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM notifications WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "json" and len(a) > 1:
        print(json.dumps(forecast(a[1]), indent=2))
    elif a[0] == "check" and len(a) > 1:
        print(json.dumps(check_alerts(a[1]), indent=2))
    elif a[0] == "sweep":
        print(json.dumps(sweep(), indent=2))
    else:
        sys.exit("usage: forecast.py json <tid> | check <tid> | sweep | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
