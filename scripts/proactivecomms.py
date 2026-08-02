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


def _controller_gates(tid):
    """CONTROLLER-PIPELINE decision gates — the calls that matter that approvals.inbox did NOT surface (a
    build parked mid-pipeline waiting for the CEO to pick an option / approve a plan / answer a clarification /
    connect a provider). Without this, a CEO had NO WAY to know a build was sitting idle on a decision — the
    exact gap that made the owner poll. Reads controller_state directly. Fail-open to []."""
    try:
        import psycopg
        from aoscfg import DB
        rows = []
        with psycopg.connect(DB) as c, c.cursor() as cur:
            # Only GENUINELY-waiting builds: parked on a human gate, NOT delivered, NOT cancelled (cancel()
            # marks the latest job 'cancelled' but leaves awaiting='user_feedback' for retry — exclude those),
            # and touched recently (a stale thread from a past session shouldn't re-ping forever).
            cur.execute("""SELECT cs.thread_id, cs.phase, cs.awaiting, cs.product
                           FROM controller_state cs
                           WHERE cs.tenant_id=%s
                             AND cs.awaiting IN ('user_feedback','user_approval','credentials')
                             AND cs.phase <> 'DELIVER'
                             AND cs.updated_at > now() - interval '12 hours'
                             AND NOT EXISTS (
                                   SELECT 1 FROM controller_jobs cj
                                   WHERE cj.thread_id=cs.thread_id AND cj.status='cancelled'
                                     AND cj.id = (SELECT max(id) FROM controller_jobs WHERE thread_id=cs.thread_id))
                        """, (tid,))
            candidates = cur.fetchall()
        # exclude CANCELLED builds: cancel() trips killswitch.halt("thread-<id>") and leaves awaiting set for
        # retry, so a stopped build otherwise looks like a pending gate. A halted thread is not "waiting on you".
        try:
            import killswitch
            candidates = [r for r in candidates if not killswitch.is_halted(f"thread-{r[0]}").get("halted")]
        except Exception:
            pass
        for thread_id, phase, awaiting, product in candidates:
            what = {"user_approval": "pick a direction / approve the plan",
                    "user_feedback": "review & respond",
                    "credentials": "connect a model provider"}.get(awaiting, "your input")
            rows.append({
                "id": f"ceo_gate:{thread_id}", "kind": "ceo_decision", "ref": str(thread_id),
                "title": f"Your build is waiting on you ({phase})",
                "detail": f"It needs you to {what}. Reply to task 'ceo-{thread_id}' from your phone, "
                          f"or open the console.",
                "severity": "high", "action_label": "Answer in console"})
        return rows
    except Exception:
        return []


def _signals(tid, org):
    """The actionable list for this tenant: controller-pipeline decision gates (a build parked waiting on the
    CEO) PLUS approvals.inbox (paused apps, consent, blocked builds, hire requests, AI→CEO questions,
    dead-letters). Fail-open per-source so one bad source never silences the whole sweep."""
    out = _controller_gates(tid)
    try:
        import approvals
        out += list(approvals.inbox(tid) or [])
    except Exception:
        pass
    return out


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
        if not isinstance(it, dict):
            continue                                 # a source may yield a non-dict; skip defensively
        sig = str(it.get("id") or f"{it.get('kind')}:{it.get('ref')}")
        if not _due(tid, sig, cooldown_s):
            continue
        level = _LEVEL.get((it.get("severity") or "").lower(), "standard")
        title = it.get("title") or "Something needs your decision"
        body = (it.get("detail") or "")[:280] + (f"\n\n→ {it['action_label']}" if it.get("action_label") else "")
        try:
            notify(tid, "approvals", title, body, level=level, url="/#approvals")
            # A CEO DECISION GATE must reach the phone, not just the in-app feed — this is the whole point
            # ("never stall without me knowing why"). Ping the founder pager DIRECTLY too, so it doesn't
            # depend on the tenant's notification prefs being wired. Best-effort.
            if it.get("kind") == "ceo_decision":
                try:
                    import notify as _pager
                    _pager.send(f"{title} — {body[:180]}", title="agent-os · your build needs you",
                                priority="high", tags="speech_balloon")
                except Exception:
                    pass
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
