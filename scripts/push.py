#!/usr/bin/env python3
"""push.py — per-tenant push transport (the live wire behind the 'push' notification channel).

notify.py pages the FOUNDER (one fixed ntfy topic -> your phone). This is the per-TENANT equivalent: each
tenant gets its own ntfy topic, registered once, and send() publishes to THAT topic so a tenant's own
device/feed lights up — not yours. It reuses notify.py's exact ntfy mechanism (JSON POST to the local ntfy
base URL) but aims it at the tenant's topic. Delivery is best-effort: a down ntfy or a missing topic never
crashes the caller. With no topic on file, it falls back to paging the founder so the signal isn't lost.

    push.py register <tenant> [topic]     # mint/return the tenant's ntfy topic
    push.py send <tenant> "<title>" ["body"] [priority]
    push.py selftest
Run with the agent-os venv python.
"""
import os
import sys
import math
import threading
from pathlib import Path

import requests

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit    # noqa: E402
import notify   # noqa: E402  (founder pager — fallback + ntfy config/mechanism we reuse)
from dbpool import connection, tenant_connection  # noqa: E402

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


DB_LOCK_TIMEOUT_MS = int(_finite_number("AOS_PUSH_DB_LOCK_TIMEOUT_MS", 500, minimum=50, maximum=5000))
DB_STATEMENT_TIMEOUT_MS = int(_finite_number(
    "AOS_PUSH_DB_STATEMENT_TIMEOUT_MS", 5000, minimum=250, maximum=15000))
HTTP_TIMEOUT_S = _finite_number("AOS_PUSH_HTTP_TIMEOUT_S", 5, minimum=.1, maximum=15)


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
            cur.execute("""CREATE TABLE IF NOT EXISTS push_targets (
                tenant_id TEXT PRIMARY KEY, topic TEXT,
                registered_at TIMESTAMPTZ DEFAULT now())""")
        _ensured = True


def register(tenant_id, topic=None):
    """Bind a tenant to an ntfy topic (mint one if not supplied) and upsert it. Returns {topic}."""
    _ensure()
    if not topic:
        topic = f"aos-{tenant_id[:10]}-{os.urandom(3).hex()}"
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        _bounded_transaction(cur)
        cur.execute("""INSERT INTO push_targets (tenant_id, topic) VALUES (%s,%s)
                       ON CONFLICT (tenant_id) DO UPDATE SET topic=EXCLUDED.topic,
                       registered_at=now()""", (tenant_id, topic))
    audit.append(actor="push", action="PushRegister", resource=tenant_id, decision="registered",
                 payload={"topic": topic}, tenant_id=tenant_id)
    # A device may be registered after earlier delivery attempts found no topic. Make those durable
    # attempts immediately eligible again instead of waiting for an arbitrary backoff window.
    try:
        with tenant_connection(tenant_id) as c, c.cursor() as cur:
            _bounded_transaction(cur)
            cur.execute("""UPDATE notification_deliveries
                           SET status='failed', next_attempt_at=now(), attempts=0,
                               last_error='push target registered; delivery pending'
                           WHERE tenant_id=%s AND channel='push' AND status <> 'accepted'""", (tenant_id,))
    except Exception:
        pass
    return {"topic": topic}


def _topic_of(tenant_id):
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        _bounded_transaction(cur)
        cur.execute("SELECT topic FROM push_targets WHERE tenant_id=%s", (tenant_id,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def send(tenant_id, title, body, priority="default", fallback=True):
    """Best-effort push to the tenant's own ntfy topic (reusing notify.py's ntfy base URL + JSON POST).
    No topic registered -> page the founder instead so the signal isn't lost. Never raises."""
    try:
        topic = _topic_of(tenant_id)
    except Exception as exc:
        # Runtime schema/query contention is an unavailable transport, not permission to freeze the caller or
        # claim delivery. The process-cached ensure will retry and converge on a later invocation.
        return {"sent": False, "reason": f"push target lookup unavailable: {str(exc)[:160]}"}
    if not topic:
        fallback_sent = bool(fallback and notify.send(
            f"[no tenant topic] {tenant_id}: {title} — {body[:120]}",
            title=f"agent-os: {title}", priority=priority))
        audit.append(actor="push", action="PushSend", resource=tenant_id, decision="unavailable",
                     payload={"title": title[:120], "reason": "no tenant topic",
                              "fallback_sent": fallback_sent}, tenant_id=tenant_id)
        return {"sent": False, "reason": "no topic", "fallback_sent": fallback_sent}
    try:
        cfg = notify.load_env()                                  # reuse notify's ntfy config
        base = cfg["NTFY_BASE_URL"].rstrip("/")
        prio = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}.get(priority, 3)
        payload = {"topic": topic, "message": body, "title": title, "priority": prio}
        r = requests.post(base, json=payload, timeout=HTTP_TIMEOUT_S)
        sent = r.status_code < 400
    except Exception as e:
        sent = False
        audit.append(actor="push", action="PushSend", resource=tenant_id, decision="error",
                     payload={"topic": topic, "title": title[:120], "error": str(e)[:200]},
                     tenant_id=tenant_id)
        return {"sent": False, "reason": str(e)[:200] or "error", "topic": topic}
    audit.append(actor="push", action="PushSend", resource=tenant_id, decision="sent" if sent else "failed",
                 payload={"topic": topic, "title": title[:120], "priority": priority,
                          "http_status": r.status_code},
                 tenant_id=tenant_id)
    return {"sent": sent, "topic": topic,
            "reason": None if sent else f"http {r.status_code}"}


def _selftest():
    tid = "t-push-" + os.urandom(3).hex()
    reg = register(tid)                                          # mints a topic
    minted = bool(reg.get("topic")) and reg["topic"].startswith("aos-")
    res = send(tid, "selftest", "ntfy may be down; we only check it didn't crash", priority="high")
    is_dict = isinstance(res, dict)
    has_topic = _topic_of(tid) is not None
    with connection() as c, c.cursor() as cur:
        _bounded_transaction(cur)
        cur.execute("DELETE FROM push_targets WHERE tenant_id=%s", (tid,))
    ok = minted and is_dict and has_topic
    print(f"minted={minted} topic={reg.get('topic')} send_result={res} topic_on_file={has_topic}")
    print("PASS: push register mints a topic + send returns a dict without crashing ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "register" and len(a) > 1:
        print(json.dumps(register(a[1], a[2] if len(a) > 2 else None)))
    elif a[0] == "send" and len(a) >= 3:
        print(json.dumps(send(a[1], a[2], a[3] if len(a) > 3 else "",
                              priority=a[4] if len(a) > 4 else "default")))
    else:
        sys.exit('usage: push.py register <tenant> [topic] | '
                 'send <tenant> "<title>" ["body"] [priority] | selftest')


if __name__ == "__main__":
    _main(sys.argv[1:])
