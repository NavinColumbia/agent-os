#!/usr/bin/env python3
"""scheduler.py — recurring autonomous jobs (ADR 0004): periodic data ingestion, retention sweeps,
monitors. register() a job + an interval; tick() runs whatever is due, audits each run, advances
next_run. Drive tick() from cron/systemd-timer/a loop. Combined with connectors.py this gives
"live data feeding from any source on a cadence".

    scheduler.py register <name> <interval_s> '<command>'
    scheduler.py tick
    scheduler.py list
    scheduler.py test
Run with the agent-os venv python.
"""
import subprocess
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)


def register(name, command, interval_s):
    # first run is ONE INTERVAL out, not immediately — so registering e.g. a weekly eval doesn't fire
    # a fleet of builds the instant it's set up.
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO schedules (name, command, interval_s, next_run)
                       VALUES (%s,%s,%s, now() + (%s||' seconds')::interval)
                       ON CONFLICT (name) DO UPDATE SET command=EXCLUDED.command, interval_s=EXCLUDED.interval_s""",
                    (name, command, interval_s, interval_s))
        c.commit()


def tick():
    """Run all due jobs; return how many ran."""
    ran = 0
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT name, command FROM schedules WHERE enabled AND next_run <= now() FOR UPDATE SKIP LOCKED")
        due = cur.fetchall()
        for name, command in due:
            rc = subprocess.run(command, shell=True, capture_output=True, timeout=120).returncode
            cur.execute("UPDATE schedules SET last_run=now(), next_run=now() + (interval_s || ' seconds')::interval WHERE name=%s", (name,))
            audit.append(actor="scheduler", action="RunJob", resource=name, decision="executed", payload={"rc": rc})
            ran += 1
        c.commit()
    return ran


def _test():
    import time
    register("selftest-job", "true", interval_s=3600)
    with psycopg.connect(DB) as c, c.cursor() as cur:   # force it due now (register defers first run)
        cur.execute("UPDATE schedules SET next_run=now() WHERE name='selftest-job'"); c.commit()
    n1 = tick()                                          # should run it now
    n2 = tick()                                          # immediately after -> not due
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT last_run IS NOT NULL, next_run > now() FROM schedules WHERE name='selftest-job'")
        ran_recorded, scheduled_future = cur.fetchone()
        cur.execute("DELETE FROM schedules WHERE name='selftest-job'"); c.commit()
    ok = n1 >= 1 and n2 == 0 and ran_recorded and scheduled_future
    print(f"tick1 ran={n1}, tick2 ran={n2}, recorded={ran_recorded}, next_run advanced={scheduled_future}")
    print("PASS: scheduler runs due jobs once, advances next_run, audited ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    if a and a[0] == "register":
        register(a[1], a[3], int(a[2])); print(f"registered '{a[1]}' every {a[2]}s")
    elif a and a[0] == "tick":
        print(f"ran {tick()} due job(s)")
    elif a and a[0] == "list":
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT name, interval_s, last_run, next_run FROM schedules ORDER BY name")
            for r in cur.fetchall():
                print(r)
    elif a and a[0] == "test":
        _test()
    else:
        sys.exit("usage: scheduler.py register|tick|list|test ...")


if __name__ == "__main__":
    _main(sys.argv[1:])
