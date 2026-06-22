#!/usr/bin/env python3
"""flags.py — feature flags + gradual rollout (safe launches, A/B experiments).

evaluate(flag, subject) is deterministic per subject (stable bucketing by hash), so a 30% rollout
gives the same users the feature consistently. Lets products ship behind flags and ramp safely.

    flags.py set <name> <on|off> [rollout_pct]
    from flags import evaluate
Run with the agent-os venv python.
"""
import hashlib
import sys
from pathlib import Path

import psycopg

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)


def set_flag(name, enabled, rollout_pct=100):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO flags (name, enabled, rollout_pct) VALUES (%s,%s,%s) "
                    "ON CONFLICT (name) DO UPDATE SET enabled=EXCLUDED.enabled, rollout_pct=EXCLUDED.rollout_pct",
                    (name, enabled, rollout_pct))
        c.commit()


def evaluate(name, subject="global"):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT enabled, rollout_pct FROM flags WHERE name=%s", (name,))
        r = cur.fetchone()
    if not r or not r[0]:
        return False
    bucket = int(hashlib.sha256(f"{name}:{subject}".encode()).hexdigest(), 16) % 100
    return bucket < r[1]


def _test():
    set_flag("new-ui", True, 0);   off = any(evaluate("new-ui", f"u{i}") for i in range(50))
    set_flag("new-ui", True, 100); on = all(evaluate("new-ui", f"u{i}") for i in range(50))
    set_flag("new-ui", True, 50)
    frac = sum(evaluate("new-ui", f"u{i}") for i in range(1000)) / 1000
    # deterministic: same subject -> same answer
    stable = evaluate("new-ui", "u7") == evaluate("new-ui", "u7")
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("DELETE FROM flags WHERE name='new-ui'"); c.commit()
    ok = (not off) and on and (0.4 < frac < 0.6) and stable
    print(f"0%%={not off}, 100%%={on}, 50%%≈{frac:.2f}, deterministic={stable}")
    print("PASS: feature flags + gradual rollout ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    if a and a[0] == "set":
        set_flag(a[1], a[2] == "on", int(a[3]) if len(a) > 3 else 100); print(f"flag {a[1]} -> {a[2]}")
    elif a and a[0] == "test":
        _test()
    else:
        sys.exit("usage: flags.py set|test ...")


if __name__ == "__main__":
    _main(sys.argv[1:])
