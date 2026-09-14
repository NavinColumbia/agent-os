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
import os
import sys
import threading
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit        # noqa: E402
import orchestrate  # noqa: E402

from dbpool import connection  # noqa: E402

ESCALATE_AFTER_MIN = {
    "crit": int(os.environ.get("AOS_ALERT_CRIT_ESCALATE_MIN", "15")),
    "critical": int(os.environ.get("AOS_ALERT_CRIT_ESCALATE_MIN", "15")),
    "high": int(os.environ.get("AOS_ALERT_HIGH_ESCALATE_MIN", "60")),
    "warn": int(os.environ.get("AOS_ALERT_WARN_ESCALATE_MIN", "240")),
}
ESCALATE_COOLDOWN_MIN = int(os.environ.get("AOS_ALERT_ESCALATE_COOLDOWN_MIN", "120"))
SWEEP_LIMIT = max(1, min(200, int(os.environ.get("AOS_ALERT_SWEEP_LIMIT", "20"))))
CLAIM_TTL_MIN = max(1, min(60, int(os.environ.get("AOS_ALERT_CLAIM_TTL_MIN", "5"))))
_ensured = False
_ensure_lock = threading.Lock()


def _conn():
    return connection()


def _ensure():
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        with _conn() as c, c.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS agent_alerts (
                id BIGSERIAL PRIMARY KEY, signature TEXT, source TEXT, target_role TEXT,
                severity TEXT DEFAULT 'warn', body TEXT, status TEXT DEFAULT 'open', owner TEXT,
                created_at TIMESTAMPTZ DEFAULT now(), resolved_at TIMESTAMPTZ)""")
            cur.execute("ALTER TABLE agent_alerts ADD COLUMN IF NOT EXISTS escalated_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE agent_alerts ADD COLUMN IF NOT EXISTS escalation_claimed_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE agent_alerts ADD COLUMN IF NOT EXISTS escalation_claim_token TEXT")
            # Rows created before scope existed cannot be proven production work. Preserve them for history as
            # legacy, but never let a July selftest fixture page the operator in August.
            cur.execute("ALTER TABLE agent_alerts ADD COLUMN IF NOT EXISTS execution_scope TEXT")
            cur.execute("UPDATE agent_alerts SET execution_scope='legacy' WHERE execution_scope IS NULL")
            cur.execute("ALTER TABLE agent_alerts ALTER COLUMN execution_scope SET DEFAULT 'production'")
            cur.execute("ALTER TABLE agent_alerts ALTER COLUMN execution_scope SET NOT NULL")
            cur.execute("DROP INDEX IF EXISTS agent_alerts_open_sig")
            cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS agent_alerts_open_scope_sig
                           ON agent_alerts (execution_scope,signature) WHERE status='open'""")
        _ensured = True


def _sig(target_role, body):
    return hashlib.sha256(f"{target_role}|{(body or '')[:80]}".encode()).hexdigest()[:32]


def _execution_scope(value=None):
    if value is None:
        test = (os.environ.get("AOS_SELFTEST", "").strip().lower() in {"1", "true", "yes", "on"}
                or bool(os.environ.get("PYTEST_CURRENT_TEST")))
        return "test" if test else "production"
    value = str(value).strip().lower()
    if value not in {"production", "test", "legacy"}:
        raise ValueError("execution_scope must be production, test, or legacy")
    return value


def raise_alert(source, target_role, body, severity="warn", signature=None, execution_scope=None):
    """Raise a deduped alert and route it to an active agent of target_role (so it gets an OWNER).
    Returns {alert_id, owner, deduped}. A duplicate of an OPEN signature is deduped (no new row, no re-route)."""
    _ensure()
    sig = signature or _sig(target_role, body)
    scope = _execution_scope(execution_scope)
    with _conn() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO agent_alerts
                         (signature, source, target_role, severity, body, execution_scope)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (execution_scope,signature) WHERE status='open' DO NOTHING
                       RETURNING id""", (sig, source, target_role, severity, body, scope))
        row = cur.fetchone()
    if not row:
        # An open alert with this signature already exists — deduped, already owned/being worked.
        audit.append(actor="alerts", action="AlertDeduped", resource=sig, decision="deduped",
                     payload={"source": source, "target_role": target_role})
        return {"alert_id": None, "owner": None, "deduped": True, "execution_scope": scope}
    alert_id = row[0]
    # ROUTE through the governed fabric to an active agent of target_role -> the alert gets an OWNER.
    r = orchestrate.request_collaborator(f"monitor:{source}", target_role, body, tenant_id="_platform")
    owner = r.get("assignee") or r.get("suggested_role")
    with _conn() as c, c.cursor() as cur:
        cur.execute("UPDATE agent_alerts SET owner=%s WHERE id=%s", (owner, alert_id))
    audit.append(actor="alerts", action="AlertRaised", resource=str(alert_id), decision=severity,
                 payload={"source": source, "target_role": target_role, "owner": owner, "routing": r.get("action")})
    return {"alert_id": alert_id, "owner": owner, "deduped": False, "execution_scope": scope}


def resolve(alert_id):
    """Clear an alert; this re-opens the signature so a genuine recurrence can raise (and re-route) again."""
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("UPDATE agent_alerts SET status='resolved', resolved_at=now() WHERE id=%s", (alert_id,))
    audit.append(actor="alerts", action="AlertResolved", resource=str(alert_id), decision="resolved")
    return {"alert_id": alert_id, "status": "resolved"}


def open_alerts(execution_scope=None):
    """All currently-open (unresolved) alerts — the live, owned alert backlog."""
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT id, signature, source, target_role, severity, body, owner,
                              execution_scope,
                              EXTRACT(EPOCH FROM now()-created_at)::int/60 AS age_min
                       FROM agent_alerts WHERE status='open'
                         AND (%s::text IS NULL OR execution_scope=%s)
                       ORDER BY created_at""", (execution_scope, execution_scope))
        return [{"alert_id": i, "signature": sig, "source": s, "target_role": tr, "severity": sev,
                 "body": b, "owner": o, "execution_scope": scope, "age_min": am}
                for i, sig, s, tr, sev, b, o, scope, am in cur.fetchall()]


def _claim_due(limit=SWEEP_LIMIT, dry=False, execution_scope="production"):
    """Claim a bounded SLA page. A crashed sweeper's rows become eligible after CLAIM_TTL_MIN."""
    token = None if dry else uuid.uuid4().hex
    threshold = """CASE lower(COALESCE(severity,'warn'))
                      WHEN 'crit' THEN %s WHEN 'critical' THEN %s
                      WHEN 'high' THEN %s ELSE %s END"""
    execution_scope = _execution_scope(execution_scope)
    args = (execution_scope,
            ESCALATE_AFTER_MIN["critical"], ESCALATE_AFTER_MIN["critical"],
            ESCALATE_AFTER_MIN["high"], ESCALATE_AFTER_MIN["warn"],
            ESCALATE_COOLDOWN_MIN, CLAIM_TTL_MIN, max(1, int(limit or 1)))
    with _conn() as c, c.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout='1s'")
        cur.execute("SET LOCAL statement_timeout='5s'")
        if dry:
            cur.execute(f"""SELECT id, source, target_role, severity, body, owner,
                                    EXTRACT(EPOCH FROM now()-created_at)::int/60 AS age_min
                               FROM agent_alerts
                              WHERE status='open' AND execution_scope=%s
                                AND created_at <= now() - make_interval(mins => {threshold})
                                AND (escalated_at IS NULL OR escalated_at < now()-make_interval(mins => %s))
                                AND (escalation_claimed_at IS NULL
                                     OR escalation_claimed_at < now()-make_interval(mins => %s))
                              ORDER BY CASE lower(COALESCE(severity,'warn'))
                                         WHEN 'crit' THEN 0 WHEN 'critical' THEN 0
                                         WHEN 'high' THEN 1 ELSE 2 END,
                                       created_at, id
                              LIMIT %s""", args)
        else:
            cur.execute(f"""WITH candidates AS (
                               SELECT id FROM agent_alerts
                                WHERE status='open' AND execution_scope=%s
                                  AND created_at <= now() - make_interval(mins => {threshold})
                                  AND (escalated_at IS NULL OR escalated_at < now()-make_interval(mins => %s))
                                  AND (escalation_claimed_at IS NULL
                                       OR escalation_claimed_at < now()-make_interval(mins => %s))
                                ORDER BY CASE lower(COALESCE(severity,'warn'))
                                           WHEN 'crit' THEN 0 WHEN 'critical' THEN 0
                                           WHEN 'high' THEN 1 ELSE 2 END,
                                         created_at, id
                                FOR UPDATE SKIP LOCKED
                                LIMIT %s
                             ), claimed AS (
                               UPDATE agent_alerts a
                                  SET escalation_claimed_at=now(), escalation_claim_token=%s
                                 FROM candidates c
                                WHERE a.id=c.id
                               RETURNING a.id, a.source, a.target_role, a.severity, a.body, a.owner,
                                         a.created_at
                             )
                             SELECT id, source, target_role, severity, body, owner,
                                    EXTRACT(EPOCH FROM now()-created_at)::int/60 AS age_min
                               FROM claimed""", args + (token,))
        rows = cur.fetchall()
    due = [{"alert_id": aid, "source": source, "target_role": target_role,
            "severity": (severity or "warn").lower(), "body": body, "owner": owner,
            "age_min": int(age_min or 0)}
           for aid, source, target_role, severity, body, owner, age_min in rows]
    return token, due


def _finish_escalation(token, ids, accepted):
    if not token or not ids:
        return 0
    with _conn() as c, c.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout='1s'")
        cur.execute("SET LOCAL statement_timeout='5s'")
        cur.execute("""UPDATE agent_alerts
                          SET escalated_at=CASE WHEN %s THEN now() ELSE escalated_at END,
                              escalation_claimed_at=NULL, escalation_claim_token=NULL
                        WHERE id=ANY(%s) AND escalation_claim_token=%s""",
                    (bool(accepted), ids, token))
        return cur.rowcount


def sweep(notify_fn=None, dry=False, limit=SWEEP_LIMIT, execution_scope="production"):
    """Age-based escalation for open alerts. Owned alerts should be worked by agents, but a critical/high
    alert aging past its SLA is itself a process failure, so page upward and audit it once per cooldown."""
    _ensure()
    token, due = _claim_due(limit=limit, dry=dry, execution_scope=execution_scope)
    if dry or not due:
        return {"open_due": len(due), "escalated": 0,
                "ids": [d["alert_id"] for d in due]}

    text = "\n".join(
        f"#{d['alert_id']} {d['severity'].upper()} {d['age_min']}m owner={d.get('owner') or 'unowned'}: "
        f"{(d.get('body') or '')[:120]}"
        for d in due)
    sent = False
    try:
        send = notify_fn
        if send is None:
            import notify as _n
            send = lambda t: _n.send(t, title="agent-os: alert SLA breached",
                                     priority="urgent", tags="rotating_light")
        sent = bool(send("Open agent alert(s) past their escalation SLA:\n" + text))
    except Exception:
        sent = False

    ids = [d["alert_id"] for d in due]
    finalized = _finish_escalation(token, ids, sent)
    audit.append(actor="alerts", action="AlertSlaBreached", resource="agent_alerts",
                 decision="escalated" if sent else "delivery_failed", payload={"count": len(due),
                                                "ids": ids, "finalized": finalized,
                                                "notified": sent})
    return {"open_due": len(due), "attempted": len(due), "escalated": len(due) if sent else 0,
            "notified": sent, "ids": ids, "finalized": finalized}


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
        directory.register(agent_id, role, product=f"alerts-{suf}", task="watching",
                           tenant_id="_platform")
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
        r3 = raise_alert(src, role, "controller job heartbeat lapsed", severity="critical",
                         signature=f"alert-sla-{suf}")
        a3 = r3["alert_id"]
        with _conn() as c, c.cursor() as cur:
            cur.execute("""UPDATE agent_alerts
                              SET created_at = now() - interval '45 minutes',
                                  escalated_at = NULL
                            WHERE id=%s""", (a3,))
        sent = []
        esc = sweep(notify_fn=lambda text: sent.append(text) or True, execution_scope="test")
        esc_row = next(x for x in open_alerts() if x["alert_id"] == a3)
        esc_again = sweep(notify_fn=lambda text: sent.append(text) or True, execution_scope="test")
        escalates_once = (esc["escalated"] >= 1 and a3 in esc["ids"] and sent
                          and esc_row["age_min"] >= ESCALATE_AFTER_MIN["critical"]
                          and a3 not in esc_again["ids"])
        ok = routed and listed and deduped and cleared and escalates_once
        print(f"routed_to_owner={routed} in_open={listed} duplicate_deduped={deduped} resolve_clears={cleared}")
        print(f"alert_sla_escalates_once={escalates_once}")
        print("PASS: monitor raises -> ROUTED to an owner -> dedup on repeat -> resolve clears -> "
              "SLA sweep pages stale criticals ✅" if ok else "FAIL")
    finally:
        with _conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM agent_alerts WHERE source=%s", (src,))
            cur.execute("DELETE FROM tasks WHERE assignee=%s OR requester=%s", (agent_id, f"monitor:{src}"))
            cur.execute("DELETE FROM directory WHERE agent_id=%s", (agent_id,))
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
    elif a[0] == "sweep":
        print(json.dumps(sweep(dry="--dry" in a), indent=2))
    else:
        sys.exit('usage: alerts.py raise <source> <target_role> "<body>" [sev] [sig] | '
                 'resolve <id> | open | sweep [--dry] | selftest')


if __name__ == "__main__":
    _main(sys.argv[1:])
