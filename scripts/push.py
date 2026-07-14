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
from pathlib import Path

import psycopg
import requests

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit    # noqa: E402
import notify   # noqa: E402  (founder pager — fallback + ntfy config/mechanism we reuse)

from aoscfg import ENV, DB


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS push_targets (
            tenant_id TEXT PRIMARY KEY, topic TEXT,
            registered_at TIMESTAMPTZ DEFAULT now())""")
        c.commit()


def register(tenant_id, topic=None):
    """Bind a tenant to an ntfy topic (mint one if not supplied) and upsert it. Returns {topic}."""
    _ensure()
    if not topic:
        topic = f"aos-{tenant_id[:10]}-{os.urandom(3).hex()}"
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO push_targets (tenant_id, topic) VALUES (%s,%s)
                       ON CONFLICT (tenant_id) DO UPDATE SET topic=EXCLUDED.topic,
                       registered_at=now()""", (tenant_id, topic))
        c.commit()
    audit.append(actor="push", action="PushRegister", resource=tenant_id, decision="registered",
                 payload={"topic": topic})
    return {"topic": topic}


def _topic_of(tenant_id):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT topic FROM push_targets WHERE tenant_id=%s", (tenant_id,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def send(tenant_id, title, body, priority="default"):
    """Best-effort push to the tenant's own ntfy topic (reusing notify.py's ntfy base URL + JSON POST).
    No topic registered -> page the founder instead so the signal isn't lost. Never raises."""
    topic = _topic_of(tenant_id)
    if not topic:
        notify.send(f"[no tenant topic] {tenant_id}: {title} — {body[:120]}",
                    title=f"agent-os: {title}", priority=priority)
        return {"sent": False, "reason": "no topic"}
    try:
        cfg = notify.load_env()                                  # reuse notify's ntfy config
        base = cfg["NTFY_BASE_URL"].rstrip("/")
        prio = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}.get(priority, 3)
        payload = {"topic": topic, "message": body, "title": title, "priority": prio}
        r = requests.post(base, json=payload, timeout=10)        # same JSON-API POST notify.py uses
        sent = r.status_code < 400
    except Exception as e:
        sent = False
        audit.append(actor="push", action="PushSend", resource=tenant_id, decision="error",
                     payload={"topic": topic, "title": title[:120], "error": str(e)[:200]})
        return {"sent": False, "reason": "error", "topic": topic}
    audit.append(actor="push", action="PushSend", resource=tenant_id, decision="sent" if sent else "failed",
                 payload={"topic": topic, "title": title[:120], "priority": priority})
    return {"sent": sent, "topic": topic}


def _selftest():
    tid = "t-push-" + os.urandom(3).hex()
    reg = register(tid)                                          # mints a topic
    minted = bool(reg.get("topic")) and reg["topic"].startswith("aos-")
    res = send(tid, "selftest", "ntfy may be down; we only check it didn't crash", priority="high")
    is_dict = isinstance(res, dict)
    has_topic = _topic_of(tid) is not None
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("DELETE FROM push_targets WHERE tenant_id=%s", (tid,)); c.commit()
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
