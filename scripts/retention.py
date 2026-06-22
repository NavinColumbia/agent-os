#!/usr/bin/env python3
"""retention.py — record expiry / retention sweeps (the "set expiry on records" capability).

Cron this. Deletes expired objects (object store TTL) and prunes old conversation/wait rows past a
retention window. The AUDIT log is deliberately NEVER pruned (it's the tamper-evident record).

    retention.py sweep [conversation_days]
    retention.py test
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import objstore  # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)


def sweep(conversation_days=30):
    blobs = objstore.gc()                      # object-store TTL
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("DELETE FROM conversations WHERE ts < now() - (%s || ' days')::interval", (conversation_days,))
        convs = cur.rowcount
        cur.execute("DELETE FROM secrets WHERE expires_at IS NOT NULL AND expires_at <= now()")
        secs = cur.rowcount
        cur.execute("DELETE FROM waits WHERE since < now() - interval '7 days'")  # stale waits
        waits = cur.rowcount
        c.commit()
    return {"blobs_expired": blobs, "conversations_pruned": convs, "secrets_expired": secs, "stale_waits": waits}


def _test():
    # seed an expired blob + an old conversation row, sweep, confirm gone
    e = objstore.put(b"retention-temp", ttl_seconds=-1)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO conversations(conversation_id,message_id,intent,sender,recipient,ts) "
                    "VALUES('ret-test','m1','inform','a','b', now() - interval '60 days')")
        c.commit()
    r = sweep(conversation_days=30)
    gone = objstore.get(e) is None
    print(f"sweep result: {r}")
    print("PASS: retention sweep — expired blob + old conversation pruned ✅"
          if (gone and r["conversations_pruned"] >= 1) else "FAIL")
    sys.exit(0 if (gone and r["conversations_pruned"] >= 1) else 1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        _test()
    else:
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        print(sweep(days))
