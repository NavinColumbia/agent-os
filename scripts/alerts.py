#!/usr/bin/env python3
"""alerts.py — a monitoring agent alerting another agent (deduped, owned, escalatable).

The vision: a monitoring agent that watches the fleet doesn't just log "disk filling" into the void —
it raises an alert that gets ROUTED to an active agent of the right role through the governed fabric, so
the alert has an OWNER who is accountable for it (exactly like findings.py routes review findings). Two
guards make this safe to run in a tight monitoring loop:
  * dedup — at most one OPEN alert per signature (a UNIQUE partial index), so a flapping condition raised
    a thousand times produces ONE owned alert, not a thousand. INSERT ... ON CONFLICT DO NOTHING.
  * route-on-new — only a newly inserted alert is routed (request_collaborator), so we don't re-spam an
    owner for a condition already being worked.
resolve() clears the alert, which re-opens the signature for the next genuine occurrence.

    alerts.py raise <source> <target_role> "<body>" [severity] [signature]
    alerts.py resolve <alert_id>
    alerts.py open
    alerts.py selftest
Run with the agent-os venv python.
"""
import hashlib
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit        # noqa: E402
import orchestrate  # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS agent_alerts (
            id BIGSERIAL PRIMARY KEY, signature TEXT, source TEXT, target_role TEXT,
            severity TEXT DEFAULT 'warn', body TEXT, status TEXT DEFAULT 'open', owner TEXT,
            created_at TIMESTAMPTZ DEFAULT now(), resolved_at TIMESTAMPTZ)""")
        cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS agent_alerts_open_sig
                       ON agent_alerts (signature) WHERE status='open'""")
        c.commit()


def _sig(target_role, body):
    return hashlib.sha256(f"{target_role}|{(body or '')[:80]}".encode()).hexdigest()[:32]


def raise_alert(source, target_role, body, severity="warn", signature=None):
    """Raise a deduped alert and route it to an active agent of target_role (so it gets an OWNER).
    Returns {alert_id, owner, deduped}. A duplicate of an OPEN signature is deduped (no new row, no re-route)."""
    _ensure()
    sig = signature or _sig(target_role, body)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO agent_alerts (signature, source, target_role, severity, body)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (signature) WHERE status='open' DO NOTHING
                       RETURNING id""", (sig, source, target_role, severity, body))
        row = cur.fetchone()
        c.commit()
    if not row:
        # An open alert with this signature already exists — deduped, already owned/being worked.
        audit.append(actor="alerts", action="AlertDeduped", resource=sig, decision="deduped",
                     payload={"source": source, "target_role": target_role})
        return {"alert_id": None, "owner": None, "deduped": True}
    alert_id = row[0]
    # ROUTE through the governed fabric to an active agent of target_role -> the alert gets an OWNER.
    r = orchestrate.request_collaborator(f"monitor:{source}", target_role, body)
    owner = r.get("assignee") or r.get("suggested_role")
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE agent_alerts SET owner=%s WHERE id=%s", (owner, alert_id))
        c.commit()
    audit.append(actor="alerts", action="AlertRaised", resource=str(alert_id), decision=severity,
                 payload={"source": source, "target_role": target_role, "owner": owner, "routing": r.get("action")})
    return {"alert_id": alert_id, "owner": owner, "deduped": False}


def resolve(alert_id):
    """Clear an alert; this re-opens the signature so a genuine recurrence can raise (and re-route) again."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE agent_alerts SET status='resolved', resolved_at=now() WHERE id=%s", (alert_id,))
        c.commit()
    audit.append(actor="alerts", action="AlertResolved", resource=str(alert_id), decision="resolved")
    return {"alert_id": alert_id, "status": "resolved"}


def open_alerts():
    """All currently-open (unresolved) alerts — the live, owned alert backlog."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id, signature, source, target_role, severity, body, owner,
                              EXTRACT(EPOCH FROM now()-created_at)::int/60 AS age_min
                       FROM agent_alerts WHERE status='open' ORDER BY created_at""")
        return [{"alert_id": i, "signature": sig, "source": s, "target_role": tr, "severity": sev,
                 "body": b, "owner": o, "age_min": am}
                for i, sig, s, tr, sev, b, o, am in cur.fetchall()]


def _selftest():
    import os
    import directory
    suf = os.urandom(3).hex()
    role = f"sre-oncall-{suf}"
    agent_id = f"sre@alerts-{suf}"
    src = f"diskmon-{suf}"
    a1 = None
    try:
        # an active agent of the target role exists -> the alert must route to it (an OWNER)
        directory.register(agent_id, role, product=f"alerts-{suf}", task="watching")
        r1 = raise_alert(src, role, "disk filling on node-7", severity="warn")
        a1 = r1["alert_id"]
        routed = (not r1["deduped"]) and r1["owner"] == agent_id and a1 is not None
        listed = any(x["alert_id"] == a1 for x in open_alerts())
        # a duplicate of the SAME open signature -> deduped, no new row
        before = len(open_alerts())
        r2 = raise_alert(src, role, "disk filling on node-7", severity="warn")
        deduped = r2["deduped"] and r2["alert_id"] is None and len(open_alerts()) == before
        # resolve clears it from the open backlog
        resolve(a1)
        cleared = not any(x["alert_id"] == a1 for x in open_alerts())
        ok = routed and listed and deduped and cleared
        print(f"routed_to_owner={routed} in_open={listed} duplicate_deduped={deduped} resolve_clears={cleared}")
        print("PASS: monitor raises -> ROUTED to an owner -> dedup on repeat -> resolve clears ✅" if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM agent_alerts WHERE source=%s", (src,))
            cur.execute("DELETE FROM tasks WHERE assignee=%s OR requester=%s", (agent_id, f"monitor:{src}"))
            cur.execute("DELETE FROM directory WHERE agent_id=%s", (agent_id,))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "raise" and len(a) >= 4:
        print(json.dumps(raise_alert(a[1], a[2], a[3], a[4] if len(a) > 4 else "warn",
                                     a[5] if len(a) > 5 else None), indent=2))
    elif a[0] == "resolve" and len(a) > 1:
        print(json.dumps(resolve(int(a[1])), indent=2))
    elif a[0] == "open":
        print(json.dumps(open_alerts(), indent=2))
    else:
        sys.exit('usage: alerts.py raise <source> <target_role> "<body>" [sev] [sig] | resolve <id> | open | selftest')


if __name__ == "__main__":
    _main(sys.argv[1:])
