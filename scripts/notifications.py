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
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit    # noqa: E402
import notify   # noqa: E402  (founder pager — used for urgent escalation)

from aoscfg import ENV, DB
LEVELS = ("silent", "passive", "standard", "urgent")


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS notifications (
            id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, channel TEXT NOT NULL DEFAULT 'in_app',
            category TEXT NOT NULL DEFAULT 'build', level TEXT NOT NULL DEFAULT 'standard',
            title TEXT NOT NULL, body TEXT, url TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), read_at TIMESTAMPTZ)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS notification_prefs (
            tenant_id TEXT NOT NULL, category TEXT NOT NULL, in_app BOOLEAN NOT NULL DEFAULT true,
            email BOOLEAN NOT NULL DEFAULT true, push BOOLEAN NOT NULL DEFAULT false,
            PRIMARY KEY (tenant_id, category))""")
        c.commit()


def _prefs(tid, category):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT in_app, email, push FROM notification_prefs WHERE tenant_id=%s AND category=%s",
                    (tid, category))
        row = cur.fetchone()
    return {"in_app": True, "email": True, "push": False} if not row else {"in_app": row[0], "email": row[1], "push": row[2]}


def _email(tid, title, body):
    """Best-effort email; uses emailer.send if present/configured, else logs. Never crashes the caller."""
    try:
        import emailer
        return emailer.send(tid, title, body)
    except Exception:
        print(f"[notifications] (email not configured) -> {tid}: {title}", flush=True)
        return False


def send(tid, category, title, body="", level="standard", url=""):
    """Deliver a notification by level + the tenant's per-category prefs. Returns the channels used."""
    _ensure()
    level = level if level in LEVELS else "standard"
    pr = _prefs(tid, category)
    used = []
    with psycopg.connect(DB) as c, c.cursor() as cur:           # in_app: always stored (the feed/ledger)
        cur.execute("""INSERT INTO notifications (tenant_id, channel, category, level, title, body, url)
                       VALUES (%s,'in_app',%s,%s,%s,%s,%s) RETURNING id""",
                    (tid, category, level, title, body, url))
        nid = cur.fetchone()[0]; c.commit()
    used.append("in_app")
    if level in ("standard", "urgent") and pr["email"]:
        if _email(tid, title, body):
            used.append("email")
    if level == "urgent":                                       # urgent also pages the founder (operator)
        notify.send(f"[{category}] {tid}: {title} — {body[:100]}", title="agent-os urgent", priority="urgent")
        used.append("operator_page")
    audit.append(actor="notifications", action="Notify", resource=tid, decision=level,
                 payload={"category": category, "title": title[:120], "channels": used})
    return {"id": nid, "channels": used, "level": level}


def feed(tid, unread_only=False, limit=50):
    _ensure()
    q = """SELECT id, channel, category, level, title, body, url, created_at, read_at
           FROM notifications WHERE tenant_id=%s {} ORDER BY id DESC LIMIT %s"""
    q = q.format("AND read_at IS NULL" if unread_only else "")
    import dbpool                                                # C2: polled every few seconds by every open console
    with dbpool.connection(autocommit=True) as c, c.cursor() as cur:
        cur.execute(q, (tid, limit))
        rows = cur.fetchall()
    return [{"id": r[0], "channel": r[1], "category": r[2], "level": r[3], "title": r[4],
             "body": r[5], "url": r[6], "created_at": str(r[7]), "read": r[8] is not None}
            for r in rows
            if r[3] != "silent" or unread_only is False]   # silent items show in full feed, not the badge


def unread_count(tid):
    _ensure()
    import dbpool                                                # C2: the notification-bell poll (very hot)
    with dbpool.connection(autocommit=True) as c, c.cursor() as cur:   # silent never contributes to the badge
        cur.execute("""SELECT count(*) FROM notifications WHERE tenant_id=%s AND read_at IS NULL
                       AND level <> 'silent'""", (tid,))
        return cur.fetchone()[0]


def mark_read(tid, nid):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE notifications SET read_at=now() WHERE tenant_id=%s AND id=%s", (tid, nid))
        c.commit()
    return True


def _selftest():
    tid = "t-notif-" + os.urandom(3).hex()
    silent = send(tid, "build", "bg sync", level="silent")
    standard = send(tid, "build", "Build LAUNCHED", "splitbill is ready", level="standard")
    badge_before = unread_count(tid)                  # silent must NOT count toward the badge -> 1, not 2
    full = feed(tid)                                   # full feed shows both (silent + standard)
    mark_read(tid, standard["id"])
    badge_after = unread_count(tid)                    # now 0
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("DELETE FROM notifications WHERE tenant_id=%s", (tid,)); c.commit()
    ok = ("in_app" in silent["channels"] and badge_before == 1 and len(full) == 2 and badge_after == 0)
    print(f"badge(silent-excluded)={badge_before} feed={len(full)} badge-after-read={badge_after}")
    print("PASS: notification taxonomy (silent/standard, feed, badge, read) ✅" if ok else "FAIL")
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
    else:
        sys.exit('usage: notifications.py send <tenant> <category> <level> "<title>" ["body"] | '
                 'feed <tenant> [--unread] | read <tenant> <id> | selftest')


if __name__ == "__main__":
    _main(sys.argv[1:])
