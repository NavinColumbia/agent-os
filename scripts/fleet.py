#!/usr/bin/env python3
"""fleet.py — ambient visibility into what the whole agent fleet is doing, right now.

The audit log is already a complete, tamper-evident record of every action every agent takes; this
surfaces it live alongside running processes, in-flight products, and throughput — so you don't have
to prompt to know what's happening. Pair with proactive ntfy pings (agents call notify.send on key
events) for push, and this for pull.

    fleet.py status              # one-shot snapshot (what's running + recent agent activity)
    fleet.py watch [seconds]     # live dashboard, refreshes (run in a terminal pane; default 4s)
    fleet.py ping "<msg>"        # send yourself a test push
Run with the agent-os venv python.
"""
import subprocess
import sys
import time
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import notify  # noqa: E402

from aoscfg import ENV, DB

# label -> pgrep pattern (specific enough not to match fleet.py itself)
PROCS = [
    ("controller", "controller.py"),
    ("factory build", "factory.py build"),
    ("scheduler ticker", "ticker.sh"),
    ("http api", "api.py serve"),
    ("reply listener", "reply_listener.py"),
]
DOT = {"allow": "🟢", "executed": "🟢", "deny": "🔴", "ask": "🟡"}


def _running(pattern):
    r = subprocess.run(["pgrep", "-fc", pattern], capture_output=True, text=True)
    try:
        return int(r.stdout.strip() or "0")
    except ValueError:
        return 0


def snapshot():
    out = []
    out.append("\033[1m═══ agent-os fleet ═══\033[0m  " + time.strftime("%Y-%m-%d %H:%M:%S"))

    # 1) processes
    out.append("\n\033[1mProcesses\033[0m")
    for label, pat in PROCS:
        n = _running(pat)
        out.append(f"  {'🟢' if n else '⚪'} {label:18} {('×'+str(n)) if n else 'idle'}")

    with psycopg.connect(DB) as c, c.cursor() as cur:
        # 2) in-flight / recent products (from factory agent activity)
        cur.execute("""SELECT resource, max(ts) AS last, count(*) AS steps,
                              (array_agg(action ORDER BY id DESC))[1] AS last_action,
                              (array_agg(actor  ORDER BY id DESC))[1] AS last_actor
                       FROM audit_log WHERE actor LIKE 'factory:%%' AND ts > now() - interval '2 hours'
                       GROUP BY resource ORDER BY last DESC LIMIT 8""")
        rows = cur.fetchall()
        out.append("\n\033[1mProducts in flight (last 2h)\033[0m")
        if rows:
            for res, last, steps, action, actor in rows:
                who = (actor or "").replace("factory:", "")
                out.append(f"  • {res:16} {steps:>2} steps · last: {who}/{action}  {last:%H:%M:%S}")
        else:
            out.append("  (none)")

        # 3) recent agent activity — the live stream of who did what
        cur.execute("SELECT ts, actor, action, resource, decision FROM audit_log ORDER BY id DESC LIMIT 14")
        ev = cur.fetchall()
        out.append("\n\033[1mRecent agent activity\033[0m")
        for ts, actor, action, resource, decision in ev:
            d = DOT.get(decision, "•")
            out.append(f"  {ts:%H:%M:%S} {d} {actor:20} {action:14} {(resource or '')[:30]}")

        # 4) throughput
        cur.execute("SELECT count(*) FROM audit_log WHERE ts > now() - interval '10 minutes'")
        last10 = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM audit_log WHERE decision='deny' AND ts > now() - interval '1 hour'")
        denies = cur.fetchone()[0]
    out.append(f"\n\033[1mThroughput\033[0m  {last10} actions / last 10 min · "
               f"{denies} policy denials / last hour")
    return "\n".join(out)


def _main(a):
    if not a or a[0] == "status":
        print(snapshot())
    elif a[0] == "watch":
        interval = int(a[1]) if len(a) > 1 else 4
        try:
            while True:
                print("\033[2J\033[H" + snapshot(), flush=True)
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n(stopped)")
    elif a[0] == "ping":
        ok = notify.send(a[1] if len(a) > 1 else "fleet ping", title="agent-os fleet", tags="satellite")
        print("sent ✅" if ok else "not sent (check NTFY_TOPIC in .env.local)")
    else:
        sys.exit("usage: fleet.py status|watch [s]|ping <msg>")


if __name__ == "__main__":
    _main(sys.argv[1:])
