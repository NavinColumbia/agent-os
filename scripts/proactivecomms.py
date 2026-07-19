#!/usr/bin/env python3
"""proactivecomms.py — the PROACTIVE briefing engine: the CEO learns about the calls that matter BEFORE they
wonder, never has to open the app to find a blocker, and is never spammed with noise.

North Star: "The CEO is briefed, consulted on the calls that matter, and never bothered with noise ... the
controller pings the CEO before the CEO ever wonders 'did something silently die?'." The pieces existed but
were PULL-only: approvals.inbox() already computes everything awaiting a human decision (paused apps, consent
gate, blocked builds, hire requests, AI→CEO questions, dead-letters), and notifications.send() can push to the
phone — but nothing connected them, so a CEO who closed the tab heard nothing. This engine is that connection:
on a cadence it sweeps each tenant's actionable items + anomalies, decides what's worth an interrupt vs the
feed (severity → level), and PUSHES the new ones, deduped so a standing blocker reminds but never spams.

    proactivecomms.py sweep <tenant>     # push this tenant's new actionable items
    proactivecomms.py sweep-all          # every active tenant (the scheduler drives this every ~5 min)
    proactivecomms.py selftest
Run with the agent-os venv python.
"""
import os
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from aoscfg import DB  # noqa: E402

# A new item pings immediately; a STANDING one re-reminds at most every RE_REMIND_S (so an unresolved decision
# isn't forgotten, but a persistent blocker never spams). Severity decides the altitude of the ping.
RE_REMIND_S = int(os.environ.get("AOS_PROACTIVE_REMIND_S", str(12 * 3600)))
_LEVEL = {"critical": "urgent", "high": "urgent", "medium": "standard", "low": "passive"}


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS proactive_sent (
                         tenant_id TEXT NOT NULL, sig TEXT NOT NULL,
                         last_sent TIMESTAMPTZ NOT NULL DEFAULT now(),
                         PRIMARY KEY (tenant_id, sig))""")
        c.commit()


def _due(tid, sig, cooldown_s):
    """True if this signal has never been pushed, or not within cooldown (so it re-reminds, not spams)."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT EXTRACT(EPOCH FROM now()-last_sent)::INT FROM proactive_sent
                       WHERE tenant_id=%s AND sig=%s""", (tid, sig))
        row = cur.fetchone()
    return row is None or (row[0] or 0) >= cooldown_s


def _mark(tid, sig):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO proactive_sent (tenant_id, sig, last_sent) VALUES (%s,%s, now())
                       ON CONFLICT (tenant_id, sig) DO UPDATE SET last_sent=now()""", (tid, sig))
        c.commit()


def _signals(tid, org):
    """The actionable list for this tenant — reuses approvals.inbox (the canonical 'awaiting a human' set:
    paused apps, consent gate, blocked builds, hire requests, AI→CEO questions, dead-letters). Fail-open to []
    so a bad source never silences the whole sweep."""
    try:
        import approvals
        return list(approvals.inbox(tid) or [])
    except Exception:
        return []


def sweep(tid, org=0, cooldown_s=RE_REMIND_S, notify=None, signals=None):
    """Push this tenant's NEW/overdue actionable items proactively. Returns the sigs pushed. Seams (notify,
    signals) are injectable for the offline selftest. Never raises — a comms error must not break anything."""
    _ensure()
    if notify is None:
        import notifications
        notify = notifications.send
    items = (signals or _signals)(tid, org)
    pushed = []
    for it in items:
        sig = str(it.get("id") or f"{it.get('kind')}:{it.get('ref')}")
        if not _due(tid, sig, cooldown_s):
            continue
        level = _LEVEL.get((it.get("severity") or "").lower(), "standard")
        title = it.get("title") or "Something needs your decision"
        body = (it.get("detail") or "")[:280] + (f"\n\n→ {it['action_label']}" if it.get("action_label") else "")
        try:
            notify(tid, "approvals", title, body, level=level, url="/#approvals")
            _mark(tid, sig)
            pushed.append(sig)
        except Exception:
            pass                                    # one failed push must not stop the rest
    return pushed


def _active_tenants():
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT tenant_id FROM tenants WHERE NOT COALESCE(suspended, false)")
            return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


def sweep_all():
    """Sweep every active tenant (the scheduler drives this). Returns {tenant: n_pushed}. Fail-open per tenant."""
    out = {}
    for tid in _active_tenants():
        try:
            n = sweep(tid)
            if n:
                out[tid] = len(n)
        except Exception:
            pass
    return out


def _selftest():
    import uuid
    tid = f"pc-selftest-{uuid.uuid4().hex[:8]}"
    sent = []
    fake_notify = lambda t, cat, title, body="", level="standard", url="": sent.append((t, level, title))
    items = [
        {"id": "blocked_build:acme", "kind": "blocked_build", "ref": "acme", "title": "Build 'acme' is blocked",
         "detail": "needs a Stripe key", "severity": "high", "action_label": "Connect Stripe"},
        {"id": "ai_question:42", "kind": "ai_question", "ref": "42", "title": "Your agent has a question",
         "detail": "Which region to deploy?", "severity": "medium"},
    ]
    fake_signals = lambda t, o: items
    try:
        first = sweep(tid, notify=fake_notify, signals=fake_signals)          # both are new -> both push
        second = sweep(tid, notify=fake_notify, signals=fake_signals)         # same items -> deduped (0)
        # a HIGH severity item pushed at 'urgent' (phone), a MEDIUM at 'standard'
        levels = {title: lvl for (_t, lvl, title) in sent}
        overdue = sweep(tid, cooldown_s=0, notify=fake_notify, signals=fake_signals)   # cooldown 0 -> re-remind

        ok = (len(first) == 2 and second == [] and len(overdue) == 2
              and levels.get("Build 'acme' is blocked") == "urgent"
              and levels.get("Your agent has a question") == "standard")
        print(f"first_push={len(first)} dedup_second={second} re_remind={len(overdue)} "
              f"levels={levels}")
        print("PASS: proactive comms — pushes the calls that matter at the right altitude, dedups a standing "
              "item, re-reminds when overdue ✅" if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM proactive_sent WHERE tenant_id=%s", (tid,)); c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "sweep" and len(a) > 1:
        print(json.dumps({"pushed": sweep(a[1])}))
    elif a[0] == "sweep-all":
        print(json.dumps(sweep_all()))
    else:
        sys.exit("usage: proactivecomms.py sweep <tenant> | sweep-all | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
