#!/usr/bin/env python3
"""osq.py — the OS query plane. Everything the human dashboard shows is ALSO queryable here by AGENTS,
as JSON, so an agent can inspect the business/ops state, reason about it, and raise alerts to you. The
dashboard is just one consumer of this same data; agents are first-class consumers too.

    osq.py app <name>        # unified app view: complexity + real cost + status
    osq.py apps              # all apps (one line each)
    osq.py portfolio         # portfolio totals (JSON)
    osq.py alerts            # current business alerts an agent should act on / escalate
    osq.py raise "<msg>" [crit|warn]   # an agent escalates an alert to the human (ntfy)
    osq.py selftest
Run with the agent-os venv python (agents call it via Bash).
"""
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import appguard  # noqa: E402
import notify    # noqa: E402

from dbpool import connection  # noqa: E402
PRODUCTS = Path.home() / "projects" / "products"
CODE_EXT = (".py", ".js", ".ts", ".html", ".css")


def complexity(app):
    repo = PRODUCTS / app
    if not repo.exists():
        return {"files": 0, "loc": 0, "tests": 0}
    files = [p for p in repo.rglob("*") if p.suffix in CODE_EXT and "__pycache__" not in str(p)
             and "/launch/" not in str(p).replace("\\", "/") and "/intel/" not in str(p).replace("\\", "/")]
    loc = tests = 0
    for f in files:
        try:
            txt = f.read_text(errors="ignore")
        except Exception:
            continue
        loc += sum(1 for l in txt.splitlines() if l.strip())
        if f.suffix == ".py":
            tests += txt.count("def test_")
    return {"files": len(files), "loc": loc, "tests": tests}


def apps():
    with connection() as c, c.cursor() as cur:
        cur.execute("SELECT DISTINCT product FROM traces WHERE product IS NOT NULL")
        return sorted(r[0] for r in cur.fetchall())


def app(name):
    with connection() as c, c.cursor() as cur:
        cur.execute("""SELECT count(*), coalesce(sum(cost_usd),0), coalesce(sum(tokens_in+tokens_out),0),
                          coalesce(sum(elapsed_s),0) FROM traces WHERE product=%s""", (name,))
        steps, cost, tokens, secs = cur.fetchone()
        cur.execute("""SELECT 1 FROM audit_log WHERE resource=%s AND action='ProductComplete'
                       AND decision='LAUNCHED' LIMIT 1""", (name,))
        shipped = cur.fetchone() is not None
    pol = appguard._policy(name)
    return {
        "app": name,
        "complexity": complexity(name),
        "build_cost_usd": round(float(cost), 4),
        "tokens": int(tokens),
        "build_min": round(float(secs) / 60, 1),
        "stages_traced": steps,
        "shipped": shipped,
        "status": pol["status"],
        "has_launch_kit": (PRODUCTS / name / "launch").exists(),
        "spend_cap": pol["spend_cap"],
        "loss_limit": pol["loss_limit"],
    }


def alerts():
    out = []
    for p in appguard.paused_apps():
        out.append({"level": "crit", "msg": f"{p['app']} is PAUSED: {p['reason']}"})
    for a in apps():
        pol = appguard._policy(a)
        e = appguard.economics(a)
        if pol["status"] != "paused" and e["spend"] >= 0.8 * float(pol["spend_cap"]):
            out.append({"level": "warn", "msg": f"{a} near spend cap (${e['spend']}/${pol['spend_cap']})"})
    return out


def decisions():
    """What's waiting on a human decision — the queue a CEO most needs surfaced."""
    out = []
    for p in appguard.paused_apps():
        out.append({"what": f"Resume or retire '{p['app']}'", "why": f"auto-paused: {p['reason']}"})
    try:
        with connection() as c, c.cursor() as cur:
            cur.execute("""SELECT need_role, count(*), max(requester) FROM hire_requests
                           WHERE status='open' GROUP BY need_role ORDER BY count(*) DESC""")
            for role, n, req in cur.fetchall():
                out.append({"what": f"Approve spawning a '{role}'" + (f" (×{n})" if n > 1 else ""),
                            "why": f"requested by {req}" + (" +others" if n > 1 else "")})
    except Exception:
        pass
    return out


def raise_alert(msg, level="warn"):
    ok = notify.send(f"{'■' if level == 'crit' else '▲'} {msg}", title="agent → you",
                     priority="urgent" if level == "crit" else "high", tags="robot")
    return {"escalated": ok, "msg": msg, "level": level}


def _main(a):
    if not a or a[0] == "apps":
        for n in apps():
            x = app(n)
            print(f"  {n:16} {x['complexity']['loc']:>4} loc {x['complexity']['tests']:>3} tests  "
                  f"${x['build_cost_usd']:<7} {x['status']:7} {'shipped' if x['shipped'] else ''}")
    elif a[0] == "app":
        print(json.dumps(app(a[1]), indent=2))
    elif a[0] == "portfolio":
        import portfolio
        print(json.dumps(portfolio.summary(), indent=2, default=str))
    elif a[0] == "alerts":
        print(json.dumps(alerts(), indent=2))
    elif a[0] == "decisions":
        print(json.dumps(decisions(), indent=2))
    elif a[0] == "raise":
        print(json.dumps(raise_alert(a[1], a[2] if len(a) > 2 else "warn"), indent=2))
    elif a[0] == "selftest":
        # the query plane returns structured data + an agent can escalate
        al = alerts()
        ap = apps()
        ok = isinstance(al, list) and isinstance(ap, list) and callable(raise_alert)
        sample = app(ap[0]) if ap else {"app": None}
        print(f"apps queryable: {len(ap)}; alerts queryable: {len(al)}; sample keys: {sorted(sample.keys())[:5]}")
        print("PASS: OS query plane (agent-queryable + escalate) ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
