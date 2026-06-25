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
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit          # noqa: E402
import billing        # noqa: E402
import notifications  # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)
WINDOW_DAYS = 30          # the billing/projection window
THRESHOLDS = [80, 100]    # pre-emptive alert points (% of projected-vs-quota)


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS budget_alert_state (
            tenant_id TEXT NOT NULL, threshold INT NOT NULL, alerted_at TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (tenant_id, threshold))""")
        c.commit()


def forecast(tid):
    """Burn-rate from the traces time-series + projection to the window end vs the plan token quota."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
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
    with psycopg.connect(DB) as c, c.cursor() as cur:
        for th in THRESHOLDS:
            if pct >= th:
                cur.execute("SELECT 1 FROM budget_alert_state WHERE tenant_id=%s AND threshold=%s", (tid, th))
                if cur.fetchone():
                    continue                                # already alerted this threshold
                cur.execute("INSERT INTO budget_alert_state (tenant_id, threshold) VALUES (%s,%s)", (tid, th))
                c.commit()
                lvl = "urgent" if th >= 100 else "standard"
                notifications.send(tid, "billing",
                                   f"Budget alert: projected ~{pct}% of your {f['plan']} quota",
                                   f"At your current burn rate you're trending to {pct}% of the monthly token "
                                   f"quota{(' in ~'+str(f['eta_days_to_quota'])+' days') if f['eta_days_to_quota'] else ''}. "
                                   f"Raise your plan or slow builds to avoid a hard stop.", level=lvl)
                fired.append(th)
            else:
                cur.execute("DELETE FROM budget_alert_state WHERE tenant_id=%s AND threshold=%s", (tid, th))
                c.commit()                                  # dropped back below -> re-arm
    if fired:
        audit.append(actor="forecast", action="BudgetAlert", resource=tid, decision="alerted",
                     payload={"pct": pct, "thresholds": fired})
    return {"pct": pct, "level": f["level"], "alerted": fired}


def sweep():
    """check_alerts for every tenant that has products (drive from a scheduler job)."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT DISTINCT tenant_id FROM tenant_products")
        tids = [r[0] for r in cur.fetchall()]
    out = [{"tenant": t, **check_alerts(t)} for t in tids]
    return {"checked": len(out), "alerted": [o for o in out if o["alerted"]]}


def _selftest():
    import psycopg as pg
    reg = billing.signup("forecast-selftest", "free")        # free: 100k token quota
    tid = reg["tenant_id"]; prod = tid.replace("t-", "")[:6] + "-demo"
    with pg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (prod, tid))
        # 5 days ago start, heavy usage so projection blows past the 100k quota -> should alert
        cur.execute("""INSERT INTO traces (run_id, product, stage, role, kind, rc, cost_usd, tokens_in, tokens_out, elapsed_s, ts, prompt, output, model)
                       VALUES (%s,%s,'BUILD','builder','agent',0,4.0,30000,30000,40, now()-interval '4 days','p','o','m')""", (f"r1-{prod}", prod))
        c.commit()
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
        with pg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM traces WHERE product=%s", (prod,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM budget_alert_state WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM notifications WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
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
