#!/usr/bin/env python3
"""notifications.py — the tenant-facing notification taxonomy (silent / passive / standard / urgent).

notify.py is the FOUNDER's pager (ntfy → your phone). This is the USERS' notification system: a per-tenant
feed with a channel (in_app | email | push), a category (build | billing | changelog | incident | security),
and a level that decides how loudly it surfaces:

    silent   — stored only, no badge (background/telemetry; e.g. a silent data refresh)
    passive  — shows in the feed, no interruption (changelog, weekly digest)
    standard — feed + badge, and email if the category's email pref is on (build done/failed, receipts)
    urgent   — feed + email + (push if enabled) AND pages the founder too (security/billing-critical)

Delivery is best-effort and pref-aware: in_app is always written; email/push fire only if the tenant's
notification_prefs allow that category. Email uses emailer.send() if configured, else logs (no crash).

    notifications.py send <tenant> <category> <level> "<title>" ["body"]
    notifications.py feed <tenant> [--unread]
    notifications.py read <tenant> <id>
    notifications.py selftest
Run with the agent-os venv python.
"""
import os
import sys
import math
import threading
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit    # noqa: E402
import notify   # noqa: E402  (founder pager — used for urgent escalation)

from dbpool import connection, tenant_connection
LEVELS = ("silent", "passive", "standard", "urgent")
_ensured = False
_ensure_lock = threading.Lock()


def _finite_number(name, default, *, minimum, maximum):
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    if not math.isfinite(value):
        value = float(default)
    return min(maximum, max(minimum, value))


DB_LOCK_TIMEOUT_MS = int(_finite_number(
    "AOS_NOTIFICATIONS_DB_LOCK_TIMEOUT_MS", 500, minimum=50, maximum=5000))
DB_STATEMENT_TIMEOUT_MS = int(_finite_number(
    "AOS_NOTIFICATIONS_DB_STATEMENT_TIMEOUT_MS", 5000, minimum=250, maximum=15000))
# The scheduler hard-kills jobs after 120s. Keep the delivery pass comfortably inside that envelope, including
# one in-flight push and the two bounded DB transactions around it. A claim is one row, never a 50-row lease.
RETRY_BUDGET_S = _finite_number(
    "AOS_NOTIFICATION_RETRY_BUDGET_S", 75, minimum=1, maximum=90)
PUSH_TIMEOUT_S = _finite_number(
    "AOS_NOTIFICATION_PUSH_TIMEOUT_S", 5, minimum=.1, maximum=15)
RETRY_ATTEMPT_RUNWAY_S = _finite_number(
    "AOS_NOTIFICATION_RETRY_ATTEMPT_RUNWAY_S", 35, minimum=5, maximum=45)
RETRY_CLAIM_LEASE_S = int(_finite_number(
    "AOS_NOTIFICATION_RETRY_CLAIM_LEASE_S", 30, minimum=10, maximum=60))


def _bounded_transaction(cur, *, schema=False):
    statement_ms = min(DB_STATEMENT_TIMEOUT_MS, 3000) if schema else DB_STATEMENT_TIMEOUT_MS
    cur.execute("SELECT set_config('lock_timeout', %s, true)", (f"{DB_LOCK_TIMEOUT_MS}ms",))
    cur.execute("SELECT set_config('statement_timeout', %s, true)", (f"{statement_ms}ms",))


def _ensure():
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        with connection() as c, c.cursor() as cur:
            _bounded_transaction(cur, schema=True)
            cur.execute("""CREATE TABLE IF NOT EXISTS notifications (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, channel TEXT NOT NULL DEFAULT 'in_app',
                category TEXT NOT NULL DEFAULT 'build', level TEXT NOT NULL DEFAULT 'standard',
                title TEXT NOT NULL, body TEXT, url TEXT, context_key TEXT, resolved_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(), read_at TIMESTAMPTZ)""")
            cur.execute("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS context_key TEXT")
            cur.execute("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ")
            cur.execute("""CREATE TABLE IF NOT EXISTS notification_prefs (
                tenant_id TEXT NOT NULL, category TEXT NOT NULL, in_app BOOLEAN NOT NULL DEFAULT true,
                email BOOLEAN NOT NULL DEFAULT true, push BOOLEAN NOT NULL DEFAULT false,
                PRIMARY KEY (tenant_id, category))""")
            cur.execute("""CREATE TABLE IF NOT EXISTS notification_deliveries (
                notification_id BIGINT NOT NULL, tenant_id TEXT NOT NULL, channel TEXT NOT NULL,
                status TEXT NOT NULL, attempts INT NOT NULL DEFAULT 0, last_error TEXT,
                attempted_at TIMESTAMPTZ, accepted_at TIMESTAMPTZ, next_attempt_at TIMESTAMPTZ,
                PRIMARY KEY (notification_id, channel))""")
            cur.execute("""CREATE INDEX IF NOT EXISTS notification_deliveries_retry_idx
                           ON notification_deliveries (next_attempt_at) WHERE status = 'failed'""")
        _ensured = True


def _prefs(tid, category):
    with tenant_connection(tid) as c, c.cursor() as cur:
        _bounded_transaction(cur)
        cur.execute("SELECT in_app, email, push FROM notification_prefs WHERE tenant_id=%s AND category=%s",
                    (tid, category))
        row = cur.fetchone()
    if not row:
        # Self-host founders should be able to leave the tab during long builds and still get milestone pings.
        # Other categories stay opt-in for push so billing/changelog noise does not surprise tenants.
        return {"in_app": True, "email": True, "push": category in ("build", "approvals")}
    return {"in_app": row[0], "email": row[1], "push": row[2]}


def _email(tid, title, body):
    """Best-effort email; uses emailer.send if present/configured, else logs. Never crashes the caller."""
    try:
        import emailer
        return emailer.send(tid, title, body)
    except Exception:
        print(f"[notifications] (email not configured) -> {tid}: {title}", flush=True)
        return False


def _delivery(nid, tid, channel, status, error=""):
    """Persist only an observed transport fact. `accepted` means the transport acknowledged the request;
    it is deliberately not called `delivered`, because ntfy/email cannot prove a device was opened."""
    retry = status == "failed"
    with tenant_connection(tid) as c, c.cursor() as cur:
        _bounded_transaction(cur)
        cur.execute("""INSERT INTO notification_deliveries
                         (notification_id, tenant_id, channel, status, attempts, last_error,
                          attempted_at, accepted_at, next_attempt_at)
                       VALUES (%s,%s,%s,%s,1,%s,now(),CASE WHEN %s='accepted' THEN now() END,
                               CASE WHEN %s = 'failed' THEN now()+interval '15 minutes' END)
                       ON CONFLICT (notification_id, channel) DO UPDATE SET
                         status=EXCLUDED.status, attempts=notification_deliveries.attempts+1,
                         last_error=EXCLUDED.last_error, attempted_at=now(),
                         accepted_at=CASE WHEN EXCLUDED.status='accepted' THEN now()
                                          ELSE notification_deliveries.accepted_at END,
                         next_attempt_at=CASE WHEN EXCLUDED.status='failed'
                                              THEN now()+interval '15 minutes' ELSE NULL END""",
                    (nid, tid, channel, status, (error or "")[:300], status, status))
    return not retry


def _push_now(tid, title, body, priority="high"):
    try:
        import push
        return push.send(tid, title, body or "", priority=priority, fallback=False)
    except Exception as e:
        return {"sent": False, "reason": str(e)[:200] or "error"}


def send(tid, category, title, body="", level="standard", url="", context_key=None):
    """Deliver a notification by level + the tenant's per-category prefs. Returns the channels used."""
    _ensure()
    level = level if level in LEVELS else "standard"
    pr = _prefs(tid, category)
    used = []
    duplicate = False
    with tenant_connection(tid) as c, c.cursor() as cur:       # in_app: always stored (the feed/ledger)
        _bounded_transaction(cur)
        if context_key:
            # Crash/concurrency-safe producer idempotency without requiring live DDL. Context keys denote one
            # semantic notification (agent request, daily brief, etc.); a retry reuses the durable feed item and
            # must not repeat external delivery.
            cur.execute("SET LOCAL lock_timeout='500ms'")
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                        (f"notification:{tid}:{context_key}",))
            cur.execute("""SELECT id FROM notifications
                           WHERE tenant_id=%s AND context_key=%s ORDER BY id LIMIT 1""",
                        (tid, context_key))
            prior = cur.fetchone()
            if prior:
                nid, duplicate = prior[0], True
        if duplicate:
            pass
        else:
            cur.execute("""INSERT INTO notifications
                             (tenant_id, channel, category, level, title, body, url, context_key)
                           VALUES (%s,'in_app',%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (tid, category, level, title, body, url, context_key))
            nid = cur.fetchone()[0]
    if duplicate:
        return {"id": nid, "channels": [], "attempted": {}, "level": level, "duplicate": True}
    used.append("in_app")
    attempted = {}
    if level in ("standard", "urgent") and pr["email"]:
        email_ok = bool(_email(tid, title, body))
        attempted["email"] = "accepted" if email_ok else "unavailable"
        _delivery(nid, tid, "email", attempted["email"], "email transport not configured" if not email_ok else "")
        if email_ok:
            used.append("email")
    if level == "urgent" and pr["push"]:
        result = _push_now(tid, title, (body or "")[:160], priority="high")
        push_ok = bool(result.get("sent"))
        attempted["push"] = "accepted" if push_ok else "unavailable" if result.get("reason") == "no topic" else "failed"
        _delivery(nid, tid, "push", attempted["push"], result.get("reason") or "")
        if push_ok:
            used.append("push")
    if level == "urgent":                                       # urgent also pages the founder (operator)
        # Operator escalation is not the tenant channel. Include routing metadata, never tenant body text;
        # the operator can open the scoped console without receiving potentially sensitive CEO content.
        page_ok = bool(notify.send(f"[{category}] tenant {tid}: urgent notification requires attention",
                                   title="agent-os urgent", priority="urgent"))
        attempted["operator_page"] = "accepted" if page_ok else "unavailable"
        _delivery(nid, tid, "operator_page", attempted["operator_page"],
                  "operator pager not configured or unavailable" if not page_ok else "")
        if page_ok:
            used.append("operator_page")
    audit.append(actor="notifications", action="Notify", resource=tid, decision=level,
                 payload={"category": category, "title": title[:120], "accepted_channels": used,
                          "attempted": attempted}, tenant_id=tid)
    return {"id": nid, "channels": used, "attempted": attempted, "level": level,
            "duplicate": False}


def resolve(tid, context_key):
    """Close actionable notifications after the corresponding human request is answered."""
    _ensure()
    with tenant_connection(tid) as c, c.cursor() as cur:
        _bounded_transaction(cur)
        cur.execute("""UPDATE notifications SET resolved_at=now(), read_at=COALESCE(read_at, now())
                       WHERE tenant_id=%s AND context_key=%s AND resolved_at IS NULL""", (tid, context_key))
        return cur.rowcount


def _claim_pending(notification_id=None):
    """Lease exactly one due push row.

    Selecting and leasing in the same short transaction preserves SKIP LOCKED's exactly-one claimant
    semantics without pre-delaying a whole batch. If the process is killed in the tiny commit-to-send gap,
    the short lease makes that one row eligible again promptly; all unclaimed tail rows remain due now.
    """
    notification_filter = ""
    params = []
    if notification_id is not None:
        notification_filter = "AND nd.notification_id=%s"
        params.append(int(notification_id))
    with connection() as c, c.cursor() as cur:
        _bounded_transaction(cur)
        cur.execute(f"""SELECT nd.notification_id, nd.tenant_id, n.title, n.body, n.level
                       FROM notification_deliveries nd JOIN notifications n ON n.id=nd.notification_id
                       WHERE nd.channel='push' AND nd.status = 'failed'
                         AND nd.next_attempt_at <= now() AND nd.attempts < 20
                         {notification_filter}
                       ORDER BY nd.next_attempt_at,nd.notification_id
                       FOR UPDATE OF nd SKIP LOCKED LIMIT 1""",
                    params)
        row = cur.fetchone()
        if row:
            cur.execute("""UPDATE notification_deliveries
                           SET next_attempt_at=now()+(%s * interval '1 second')
                           WHERE notification_id=%s AND channel='push'""",
                        (RETRY_CLAIM_LEASE_S, row[0]))
        return row


def retry_pending(limit=50, notification_id=None, budget_s=None):
    """Retry due push attempts inside an absolute scheduler-safe operation budget.

    Work is claimed one row at a time. This is intentionally sequential: transport acceptance is recorded
    before another row is leased, so process cancellation cannot strand the unattempted tail behind the old
    15-minute batch lease. The external transport itself has a finite timeout in push.send().
    """
    limit = max(0, int(limit))
    if limit == 0:
        return {"attempted": 0, "accepted": 0}
    requested_budget = RETRY_BUDGET_S if budget_s is None else float(budget_s)
    if not math.isfinite(requested_budget):
        requested_budget = RETRY_BUDGET_S
    budget = min(RETRY_BUDGET_S, max(0.0, requested_budget))
    deadline = time.monotonic() + budget
    _ensure()

    attempted = accepted = 0
    while attempted < limit:
        # Do not lease work unless there is enough budget to claim, transport, and durably record its result.
        if deadline - time.monotonic() < min(RETRY_ATTEMPT_RUNWAY_S, budget):
            break
        row = _claim_pending(notification_id=notification_id)
        if not row:
            break
        nid, tid, title, body, level = row
        result = _push_now(tid, title, (body or "")[:160],
                           priority="high" if level == "urgent" else "default")
        ok = bool(result.get("sent"))
        status = "accepted" if ok else "unavailable" if result.get("reason") == "no topic" else "failed"
        _delivery(nid, tid, "push", status, result.get("reason") or "")
        attempted += 1
        accepted += int(ok)
    return {"attempted": attempted, "accepted": accepted}


def feed(tid, unread_only=False, limit=50):
    _ensure()
    q = """SELECT id, channel, category, level, title, body, url, created_at, read_at,
                  context_key, resolved_at
           FROM notifications WHERE tenant_id=%s {} ORDER BY id DESC LIMIT %s"""
    q = q.format("AND read_at IS NULL" if unread_only else "")
    with tenant_connection(tid) as c, c.cursor() as cur:
        _bounded_transaction(cur)
        cur.execute(q, (tid, limit))
        rows = cur.fetchall()
        ids = [r[0] for r in rows]
        deliveries = {}
        if ids:
            cur.execute("""SELECT notification_id,channel,status,attempts,last_error,attempted_at,accepted_at
                           FROM notification_deliveries
                           WHERE tenant_id=%s AND notification_id=ANY(%s)
                           ORDER BY notification_id,channel""", (tid, ids))
            for nid, channel, status, attempts, error, attempted_at, accepted_at in cur.fetchall():
                deliveries.setdefault(nid, []).append({
                    "channel": channel, "status": status, "attempts": attempts,
                    "error": error or None, "attempted_at": str(attempted_at) if attempted_at else None,
                    "accepted_at": str(accepted_at) if accepted_at else None})
    return [{"id": r[0], "channel": r[1], "category": r[2], "level": r[3], "title": r[4],
             "body": r[5], "url": r[6], "created_at": str(r[7]), "read": r[8] is not None,
             "context_key": r[9], "resolved": r[10] is not None,
             "deliveries": deliveries.get(r[0], [])}
            for r in rows
            if r[3] != "silent" or unread_only is False]   # silent items show in full feed, not the badge


def unread_count(tid):
    _ensure()
    with tenant_connection(tid) as c, c.cursor() as cur:  # silent never contributes to the badge
        _bounded_transaction(cur)
        cur.execute("""SELECT count(*) FROM notifications WHERE tenant_id=%s AND read_at IS NULL
                       AND level <> 'silent'""", (tid,))
        return cur.fetchone()[0]


def mark_read(tid, nid):
    _ensure()
    with tenant_connection(tid) as c, c.cursor() as cur:
        _bounded_transaction(cur)
        cur.execute("UPDATE notifications SET read_at=now() WHERE tenant_id=%s AND id=%s", (tid, nid))
    return True


def _selftest():
    tid = "t-notif-" + os.urandom(3).hex()
    pushed, paged = [], []
    real_push, real_page, real_email = _push_now, notify.send, _email
    try:
        globals()["_push_now"] = lambda *a, **k: pushed.append((a, k)) or {"sent": True}
        notify.send = lambda *a, **k: paged.append((a, k)) or True
        globals()["_email"] = lambda *a, **k: True

        silent = send(tid, "build", "bg sync", level="silent")
        standard = send(tid, "build", "Build LAUNCHED", "splitbill is ready", level="standard")
        urgent_build = send(tid, "build", "Options ready", "pick a direction", level="urgent")
        with tenant_connection(tid) as c, c.cursor() as cur:
            _bounded_transaction(cur)
            cur.execute("""INSERT INTO notification_prefs (tenant_id, category, in_app, email, push)
                           VALUES (%s,'incident',true,true,false)
                           ON CONFLICT (tenant_id, category) DO UPDATE SET push=false""", (tid,))
        urgent_incident = send(tid, "incident", "Incident", "operator only", level="urgent")

        badge_before = unread_count(tid)              # silent must NOT count toward the badge -> 3, not 4
        full = feed(tid)                               # full feed shows silent + standard + urgent x2
        mark_read(tid, standard["id"])
        badge_after = unread_count(tid)                # now 2
    finally:
        globals()["_push_now"] = real_push
        notify.send = real_page
        globals()["_email"] = real_email
        with connection() as c, c.cursor() as cur:
            _bounded_transaction(cur)
            cur.execute("DELETE FROM notifications WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM notification_prefs WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM notification_deliveries WHERE tenant_id=%s", (tid,))
    ok = ("in_app" in silent["channels"] and badge_before == 3 and len(full) == 4 and badge_after == 2
          and "push" in urgent_build["channels"] and "operator_page" in urgent_build["channels"]
          and "push" not in urgent_incident["channels"] and len(pushed) == 1 and len(paged) == 2)
    print(f"badge(silent-excluded)={badge_before} feed={len(full)} badge-after-read={badge_after} "
          f"pushes={len(pushed)} pages={len(paged)}")
    print("PASS: notification taxonomy (silent/standard/urgent, feed, badge, push, read) ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "send" and len(a) >= 5:
        print(json.dumps(send(a[1], a[2], a[4], a[5] if len(a) > 5 else "", level=a[3])))
    elif a[0] == "feed" and len(a) > 1:
        print(json.dumps(feed(a[1], unread_only="--unread" in a), indent=2))
    elif a[0] == "read" and len(a) > 2:
        print(json.dumps({"read": mark_read(a[1], int(a[2]))}))
    elif a[0] == "retry":
        print(json.dumps(retry_pending()))
    else:
        sys.exit('usage: notifications.py send <tenant> <category> <level> "<title>" ["body"] | '
                 'feed <tenant> [--unread] | read <tenant> <id> | retry | selftest')


if __name__ == "__main__":
    _main(sys.argv[1:])
