#!/usr/bin/env python3
"""watchdog.py — constant health checks + proactive paging for the whole fleet.

Pull-visibility (the dashboard) tells you what's happening when you look. This is the PUSH side: it
runs on a short loop and pings your phone the moment something needs you — a daemon died, a build went
silent/stalled, a component is down, a wait blew its SLA, a deadlock formed, disk/backup pressure, or a
spike in policy denials. It dedupes with a cooldown so you get one ping per incident, plus a "recovered"
ping when it clears.

Liveness: long-running loops call beat(<component>) each cycle; the watchdog flags stale heartbeats.

    watchdog.py tick               # one pass: detect issues, page on new ones, mark recoveries
    watchdog.py check              # print current issues, don't page
    watchdog.py beat <component>   # record a liveness heartbeat (called by the loops)
    watchdog.py selftest
Run with the agent-os venv python.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import notify     # noqa: E402
import responder  # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)

# daemons recover.sh keeps alive — if one is missing, that's an incident
EXPECTED = {"ticker": "ticker.sh", "dashboard": "dashboard.py serve",
            "api": "api.py serve", "listener": "reply_listener.py"}
STALL_MIN = 8          # a running factory build with no audit activity for this long = stalled
COOLDOWN_S = 1800      # re-ping an unresolved issue at most every 30 min
PRIO = {"crit": "urgent", "warn": "high"}


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS heartbeats (component TEXT PRIMARY KEY,
                       ts TIMESTAMPTZ NOT NULL DEFAULT now(), meta JSONB NOT NULL DEFAULT '{}')""")
        cur.execute("""CREATE TABLE IF NOT EXISTS watchdog_alerts (signature TEXT PRIMARY KEY,
                       level TEXT NOT NULL, first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
                       last_sent TIMESTAMPTZ)""")
        cur.execute("ALTER TABLE watchdog_alerts ADD COLUMN IF NOT EXISTS fix_attempts INT NOT NULL DEFAULT 0")
        c.commit()


def beat(component, meta=None):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO heartbeats (component, ts, meta) VALUES (%s, now(), %s)
                       ON CONFLICT (component) DO UPDATE SET ts=now(), meta=EXCLUDED.meta""",
                    (component, json.dumps(meta or {})))
        c.commit()


def _pgrep(pat):
    return int(subprocess.run(["pgrep", "-fc", pat], capture_output=True, text=True).stdout.strip() or "0")


def check():
    """Return a list of current issues: {sig, level, msg}. Pure detection, no paging."""
    issues = []
    # 1) reuse the dashboard's derived alerts (health/disk/backup/deadlock/overdue-waits/denials)
    try:
        import contextlib
        import io
        import dashboard
        with contextlib.redirect_stdout(io.StringIO()):
            st = dashboard.state()
        for a in st.get("alerts", []):
            if a["level"] in ("crit", "warn"):
                issues.append({"sig": "alert:" + a["msg"][:40], "level": a["level"], "msg": a["msg"]})
    except Exception as e:
        issues.append({"sig": "watchdog:self", "level": "warn", "msg": f"state read failed: {e}"})
    # 2) expected daemons that died
    for name, pat in EXPECTED.items():
        if _pgrep(pat) == 0:
            issues.append({"sig": f"daemon:{name}", "level": "crit", "msg": f"{name} process is DOWN"})
    # 3) a factory build that's running but has gone silent (stalled mid-build)
    if _pgrep("factory.py build") > 0:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT resource, EXTRACT(EPOCH FROM now()-max(ts))/60
                           FROM audit_log WHERE actor LIKE 'factory:%%' AND ts > now()-interval '1 hour'
                           GROUP BY resource""")
            for res, idle_min in cur.fetchall():
                if idle_min and idle_min > STALL_MIN:
                    issues.append({"sig": f"stall:{res}", "level": "warn",
                                   "msg": f"build '{res}' silent {round(idle_min)}m — possible stall"})
    # 4) stale heartbeats (a loop that should be beating went quiet)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT component, EXTRACT(EPOCH FROM now()-ts) FROM heartbeats")
        for comp, age in cur.fetchall():
            if comp.startswith("selftest"):
                continue                      # one-off probes never beat again
            if age and age > 1800:            # >30 min (ticker beats every 15m; a dead loop is caught by pgrep too)
                issues.append({"sig": f"heartbeat:{comp}", "level": "warn",
                               "msg": f"{comp} heartbeat stale ({round(age/60)}m)"})
    return issues


def tick(auto_heal=True):
    """Detect → try a bounded auto-fix (responder) → page only what can't be auto-fixed or needs
    judgement. Recoveries announced for things a human was paged about."""
    _ensure()
    issues = check()
    now_sigs = {i["sig"] for i in issues}
    paged, healed = [], []
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT signature, last_sent, fix_attempts FROM watchdog_alerts")
        known = {s: (ls, fa) for s, ls, fa in cur.fetchall()}
        for i in issues:
            sig = i["sig"]
            ls, fa = known.get(sig, (None, 0))
            rem = responder.remediate(i) if (auto_heal and fa < 3) else None
            if rem is not None:                                   # we attempted a self-heal
                if rem["ok"]:
                    notify.send(f"🛠 auto-healed: {i['msg']} — {rem['action']}",
                                title="agent-os watchdog", tags="wrench")
                    healed.append(i["msg"])
                else:
                    notify.send(f"■ auto-fix FAILED: {i['msg']} ({rem['action']}) — needs you",
                                title="agent-os watchdog", priority="urgent", tags="rotating_light")
                    paged.append(i["msg"])
                cur.execute("""INSERT INTO watchdog_alerts (signature, level, last_sent, fix_attempts)
                               VALUES (%s,%s,now(),1) ON CONFLICT (signature) DO UPDATE
                               SET level=EXCLUDED.level, last_sent=now(),
                                   fix_attempts=watchdog_alerts.fix_attempts+1""", (sig, i["level"]))
            else:                                                 # not auto-fixable / gave up -> page
                fresh = ls is None
                cooled = ls is not None and (time.time() - ls.timestamp()) > COOLDOWN_S
                if fresh or cooled:
                    extra = " (auto-fix gave up — flapping)" if fa >= 3 else ""
                    notify.send(f"{'■' if i['level']=='crit' else '▲'} {i['msg']}{extra}",
                                title="agent-os watchdog", priority=PRIO[i["level"]], tags="rotating_light")
                    paged.append(i["msg"])
                    cur.execute("""INSERT INTO watchdog_alerts (signature, level, last_sent, fix_attempts)
                                   VALUES (%s,%s,now(),%s) ON CONFLICT (signature) DO UPDATE
                                   SET level=EXCLUDED.level, last_sent=now()""", (sig, i["level"], fa))
        # recoveries: previously-tracked sigs that are gone now
        recovered = [s for s in known if s not in now_sigs]
        for s in recovered:
            cur.execute("DELETE FROM watchdog_alerts WHERE signature=%s", (s,))
            ls, fa = known[s]
            if ls is not None and fa == 0:   # only for things a human was actually paged about
                notify.send(f"✓ recovered: {s}", title="agent-os watchdog", tags="white_check_mark")
        c.commit()
    beat("watchdog")
    return {"issues": len(issues), "healed": healed, "paged": paged, "recovered": len(recovered)}


def _main(a):
    if not a or a[0] == "tick":
        print(tick())
    elif a[0] == "check":
        for i in check():
            print(f"  [{i['level']}] {i['msg']}")
    elif a[0] == "beat":
        beat(a[1] if len(a) > 1 else "manual"); print("beat ok")
    elif a[0] == "selftest":
        beat("selftest-probe")
        iss = check()
        ok = isinstance(iss, list)
        print(f"watchdog check returned {len(iss)} issue(s); heartbeat write ok")
        print("PASS: watchdog detect + heartbeat ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    else:
        sys.exit("usage: watchdog.py tick|check|beat|selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
