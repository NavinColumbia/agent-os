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
import threading
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from aoscfg import DB  # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402
import audit  # noqa: E402

# A new item pings immediately; a STANDING one re-reminds at most every RE_REMIND_S (so an unresolved decision
# isn't forgotten, but a persistent blocker never spams). Severity decides the altitude of the ping.
RE_REMIND_S = int(os.environ.get("AOS_PROACTIVE_REMIND_S", str(12 * 3600)))
PROGRESS_MIN_AGE_S = int(os.environ.get("AOS_PROACTIVE_PROGRESS_MIN_S", "900"))
PROGRESS_REMIND_S = int(os.environ.get("AOS_PROACTIVE_PROGRESS_REMIND_S", "3600"))
SWEEP_ALL_LIMIT = int(os.environ.get("AOS_PROACTIVE_SWEEP_LIMIT", "25"))
SWEEP_PRIORITY_LIMIT = int(os.environ.get("AOS_PROACTIVE_PRIORITY_LIMIT", "25"))
SCHEDULE_ACTIVE_H = max(1, min(24 * 30, int(os.environ.get("AOS_PROACTIVE_ACTIVE_H", "24"))))
_LEVEL = {"critical": "urgent", "high": "urgent", "medium": "standard", "low": "passive"}
_ensured = False
_ensure_lock = threading.Lock()


def _ensure():
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        with connection() as c, c.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS proactive_sent (
                             tenant_id TEXT NOT NULL, sig TEXT NOT NULL,
                             last_sent TIMESTAMPTZ NOT NULL DEFAULT now(),
                             PRIMARY KEY (tenant_id, sig))""")
            cur.execute("""CREATE TABLE IF NOT EXISTS proactive_sweep_state (
                             name TEXT PRIMARY KEY,
                             cursor_tenant_id TEXT NOT NULL DEFAULT '')""")
        _ensured = True


def _due(tid, sig, cooldown_s):
    """True if this signal has never been pushed, or not within cooldown (so it re-reminds, not spams)."""
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""SELECT EXTRACT(EPOCH FROM now()-last_sent)::INT FROM proactive_sent
                       WHERE tenant_id=%s AND sig=%s""", (tid, sig))
        row = cur.fetchone()
    return row is None or (row[0] or 0) >= cooldown_s


def _mark(tid, sig):
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO proactive_sent (tenant_id, sig, last_sent) VALUES (%s,%s, now())
                       ON CONFLICT (tenant_id, sig) DO UPDATE SET last_sent=now()""", (tid, sig))


def _controller_gates(tid, execution_scope=None):
    """CONTROLLER-PIPELINE decision gates — the calls that matter that approvals.inbox did NOT surface (a
    build parked mid-pipeline waiting for the CEO to pick an option / approve a plan / answer a clarification /
    connect a provider). Without this, a CEO had NO WAY to know a build was sitting idle on a decision — the
    exact gap that made the owner poll. Reads controller_state directly. Fail-open to []."""
    try:
        rows = []
        with tenant_connection(tid) as c, c.cursor() as cur:
            # Only GENUINELY-waiting builds: parked on a human gate, NOT delivered, NOT cancelled (cancel()
            # marks the latest job 'cancelled' but leaves awaiting='user_feedback' for retry — exclude those),
            # and touched recently (a stale thread from a past session shouldn't re-ping forever).
            cur.execute("""SELECT cs.thread_id, cs.phase, cs.awaiting, cs.product
                           FROM controller_state cs
                           WHERE cs.tenant_id=%s
                             AND (%s::text IS NULL OR cs.execution_scope=%s)
                             AND (%s::text IS NULL
                                  OR cs.updated_at >= now()-make_interval(hours=>%s)
                                  OR EXISTS (SELECT 1 FROM agent_requests ar
                                             WHERE ar.thread_id=cs.thread_id AND ar.status='open'))
                             AND cs.awaiting IN ('user_feedback','user_approval','credentials')
                             AND cs.phase <> 'DELIVER'
                             AND NOT EXISTS (
                                   SELECT 1 FROM controller_jobs cj
                                   WHERE cj.thread_id=cs.thread_id AND cj.status='cancelled'
                                     AND cj.id = (SELECT max(id) FROM controller_jobs WHERE thread_id=cs.thread_id))
                        """, (tid, execution_scope, execution_scope,
                              execution_scope, SCHEDULE_ACTIVE_H))
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


def _fmt_age(seconds):
    seconds = int(seconds or 0)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _progress_update(tid, pulse_rows=None):
    """Human-pattern operating update for healthy long-running work.

    Decision gates answer "what needs me?". This answers the equally important North-Star question "is my
    team still working, and on what?" Pulling from pulse.live() covers durable fleet actors plus non-fleet
    work that beats through the unified pulse plane. One aggregate item per tenant prevents per-agent spam.
    """
    if pulse_rows is None:
        try:
            import pulse
            pulse_rows = pulse.live() or []
        except Exception:
            return []
    rows = [r for r in pulse_rows if str(r.get("tenant_id") or "") == str(tid)]
    live_rows = [r for r in rows if str(r.get("status") or "").lower() not in ("done", "failed", "incomplete", "reaped")]
    if not live_rows:
        return []
    oldest = max(int(r.get("age_s") or 0) for r in live_rows)
    if oldest < PROGRESS_MIN_AGE_S:
        return []
    stalled = [r for r in live_rows if r.get("stalled") or str(r.get("status") or "").lower() == "stalled"]
    blocked = [r for r in live_rows if str(r.get("status") or "").lower() == "blocked"]
    working = [r for r in live_rows if r not in stalled and r not in blocked]
    sample = []
    for r in sorted(live_rows, key=lambda x: int(x.get("age_s") or 0), reverse=True)[:3]:
        label = (r.get("label") or r.get("work_id") or "work").strip()
        stage = (r.get("stage") or "").strip()
        progress = (r.get("progress") or "").strip()
        bit = label[:72]
        if stage:
            bit += f" · {stage[:40]}"
        if progress:
            bit += f" · {progress[:80]}"
        sample.append(bit)
    severity = "high" if stalled else ("medium" if blocked else "low")
    title = "Your AI team is still working"
    if stalled:
        title = "Your AI team has stalled work"
    elif blocked:
        title = "Your AI team has blocked work"
    detail = (
        f"{len(live_rows)} active work item(s): {len(working)} working"
        f", {len(blocked)} blocked, {len(stalled)} stalled. Oldest has been running {_fmt_age(oldest)}."
    )
    if sample:
        detail += "\n" + "\n".join(f"- {s}" for s in sample)
    return [{
        "id": "progress:active-work",
        "kind": "progress_update",
        "ref": tid,
        "title": title,
        "detail": detail,
        "severity": severity,
        "action_label": "Open cockpit",
        "_cooldown_s": PROGRESS_REMIND_S,
    }]


def _signals(tid, org, pulse_rows=None, execution_scope=None):
    """The actionable list for this tenant: controller-pipeline decision gates (a build parked waiting on the
    CEO) PLUS approvals.inbox (paused apps, consent, blocked builds, hire requests, AI→CEO questions,
    dead-letters) PLUS a bounded operating update when work has been active long enough that a human CEO
    would expect a standup/status ping. Fail-open per-source so one bad source never silences the whole sweep."""
    out = _controller_gates(tid, execution_scope=execution_scope)
    out += _progress_update(tid, pulse_rows=pulse_rows)
    try:
        import approvals
        inbox = approvals.inbox(tid, execution_scope=execution_scope) or []
        # approvals.inbox() returns {"items": [...], "count": N}. `list(dict)` yields its KEYS, so this
        # appended the strings 'items' and 'count' instead of the actual approvals — and sweep()'s
        # defensive `if not isinstance(it, dict): continue` then skipped them without a sound. Net effect:
        # the proactive engine ran every 5 minutes and pushed ZERO approvals, ever, while looking healthy.
        # Accept either shape so a future change to either side cannot silently re-break it.
        out += list(inbox.get("items") or []) if isinstance(inbox, dict) else list(inbox)
    except Exception:
        pass
    return [i for i in out if isinstance(i, dict)]


def sweep(tid, org=0, cooldown_s=RE_REMIND_S, notify=None, signals=None, pulse_rows=None,
          execution_scope=None):
    """Push this tenant's NEW/overdue actionable items proactively. Returns the sigs pushed. Seams (notify,
    signals) are injectable for the offline selftest. Never raises — a comms error must not break anything."""
    _ensure()
    default_notify = notify is None
    if default_notify:
        import notifications
        notify = notifications.send
    items = (signals(tid, org) if signals else
             _signals(tid, org, pulse_rows=pulse_rows, execution_scope=execution_scope))
    pushed = []
    for it in items:
        if not isinstance(it, dict):
            continue                                 # a source may yield a non-dict; skip defensively
        sig = str(it.get("id") or f"{it.get('kind')}:{it.get('ref')}")
        if not _due(tid, sig, int(it.get("_cooldown_s") or cooldown_s)):
            continue
        level = _LEVEL.get((it.get("severity") or "").lower(), "standard")
        title = it.get("title") or "Something needs your decision"
        body = (it.get("detail") or "")[:280] + (f"\n\n→ {it['action_label']}" if it.get("action_label") else "")
        try:
            notify(tid, "approvals", title, body, level=level, url="/#approvals")
            # A CEO DECISION GATE must reach the phone, not just the in-app feed — this is the whole point
            # ("never stall without me knowing why"). Ping the founder pager DIRECTLY too, so it doesn't
            # depend on the tenant's notification prefs being wired. Best-effort.
            # notifications.send already owns the operator-page transport in production. Keep this fallback
            # only for injected/custom notifiers, otherwise every gate is paged twice.
            if it.get("kind") == "ceo_decision" and not default_notify:
                try:
                    import notify as _pager
                    _pager.send(f"{title} — {body[:180]}", title="agent-os · your build needs you",
                                priority="high", tags="speech_balloon")
                except Exception:
                    pass
            _mark(tid, sig)
            pushed.append(sig)
        except Exception as e:
            audit.append(actor="proactivecomms", action="ProactiveNotify", resource=tid,
                         decision="failed", payload={"sig": sig, "error": str(e)[:300]}, tenant_id=tid)
            # one failed push must not stop the rest; importantly, do NOT mark the signal as sent
    return pushed


def _active_tenants():
    try:
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT tenant_id FROM tenants WHERE NOT COALESCE(suspended, false)")
            return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


def _active_tenant_batch(limit=SWEEP_ALL_LIMIT, execution_scope=None):
    """Return a bounded, rotating batch of active tenants for scheduler-friendly sweep-all.

    A local/dev database can contain thousands of active synthetic tenants. Sweeping every tenant every five
    minutes made proactive-comms routinely run for 90-120s and sometimes hit the scheduler timeout, which then
    became a critical monitoring alert. Cursoring gives every tenant coverage without allowing one tick to grow
    unbounded.
    """
    _ensure()
    limit = max(1, int(limit or 1))
    # The explicit scheduled scope is crucial on a long-lived development host: selftests intentionally create
    # tenant fixtures, but those fixtures are not customers and must never receive a later scheduler-driven
    # consent/approval page. A production tenant remains eligible while it has recent production activity or
    # an unresolved production request. Manual calls leave execution_scope=None and retain the old inspection
    # behavior for operators/tests.
    if execution_scope not in (None, "production", "test"):
        raise ValueError("execution_scope must be production, test, or None")
    scope_sql = ""
    scope_args = []
    if execution_scope is not None:
        scope_sql = """AND (
            EXISTS (SELECT 1 FROM controller_state cs
                     WHERE cs.tenant_id=tenants.tenant_id AND cs.execution_scope=%s
                       AND cs.updated_at >= now()-interval '24 hours')
            OR EXISTS (SELECT 1 FROM agent_requests ar
                       LEFT JOIN controller_state cs ON cs.thread_id=ar.thread_id
                       WHERE ar.tenant_id=tenants.tenant_id AND ar.status='open'
                         AND (ar.thread_id IS NULL OR cs.execution_scope=%s))
        )"""
        scope_args = [execution_scope, execution_scope]
    with connection() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO proactive_sweep_state (name, cursor_tenant_id)
                       VALUES ('sweep-all', '')
                       ON CONFLICT (name) DO NOTHING""")
        cur.execute("""SELECT cursor_tenant_id FROM proactive_sweep_state
                       WHERE name='sweep-all' FOR UPDATE""")
        cursor = (cur.fetchone() or [""])[0] or ""
        cur.execute(f"""SELECT tenant_id FROM tenants
                       WHERE NOT COALESCE(suspended, false) AND tenant_id > %s
                         {scope_sql}
                       ORDER BY tenant_id LIMIT %s""", (cursor, *scope_args, limit))
        rows = [r[0] for r in cur.fetchall()]
        if len(rows) < limit:
            cur.execute(f"""SELECT tenant_id FROM tenants
                           WHERE NOT COALESCE(suspended, false) AND tenant_id <= %s
                             {scope_sql}
                           ORDER BY tenant_id LIMIT %s""",
                        (cursor, *scope_args, limit - len(rows)))
            rows.extend(r[0] for r in cur.fetchall())
        if rows:
            cur.execute("""UPDATE proactive_sweep_state SET cursor_tenant_id=%s
                           WHERE name='sweep-all'""", (rows[-1],))
    return rows


def _priority_tenant_batch(limit=SWEEP_PRIORITY_LIMIT, execution_scope=None):
    """Select tenants with unresolved CEO gates/questions independently of the bulk tenant list.

    With thousands of synthetic/real tenants, a single 25-row cursor can take hours to revisit a CEO whose
    team just blocked. Never-notified signals sort first; then the least-recently notified. Once a batch is
    swept and marked, the next unsent batch naturally rises to the top without a fragile process-local cursor.
    """
    _ensure()
    limit = max(1, int(limit or 1))
    with connection() as c, c.cursor() as cur:
        # A durable unresolved request stays important until it is answered. Never expire it by wall clock.
        # Cancelled controller jobs are excluded just as _controller_gates excludes them.
        cur.execute("""WITH actionable AS (
                     SELECT cs.tenant_id, 'ceo_gate:' || cs.thread_id::text AS sig
                     FROM controller_state cs
                     WHERE cs.awaiting IN ('user_feedback','user_approval','credentials')
                       AND (%s::text IS NULL OR cs.execution_scope=%s)
                       AND (%s::text IS NULL
                            OR cs.updated_at >= now()-make_interval(hours=>%s)
                            OR EXISTS (SELECT 1 FROM agent_requests ar
                                       WHERE ar.thread_id=cs.thread_id AND ar.status='open'))
                       AND cs.phase <> 'DELIVER'
                       AND NOT EXISTS (
                         SELECT 1 FROM controller_jobs cj
                         WHERE cj.thread_id=cs.thread_id AND cj.status='cancelled'
                           AND cj.id=(SELECT max(id) FROM controller_jobs WHERE thread_id=cs.thread_id))
                     UNION
                     SELECT ar.tenant_id, 'question:' || ar.id::text AS sig
                     FROM agent_requests ar
                     LEFT JOIN controller_state cs ON cs.thread_id=ar.thread_id
                     WHERE ar.status='open'
                       AND (%s::text IS NULL OR ar.execution_scope=%s)
                   )
                   SELECT a.tenant_id, min(ps.last_sent) AS last_sent
                   FROM actionable a
                   JOIN tenants t ON t.tenant_id=a.tenant_id
                   LEFT JOIN proactive_sent ps ON ps.tenant_id=a.tenant_id AND ps.sig=a.sig
                   WHERE NOT COALESCE(t.suspended,false)
                   GROUP BY a.tenant_id
                   ORDER BY min(ps.last_sent) NULLS FIRST, a.tenant_id
                   LIMIT %s""", (execution_scope, execution_scope,
                                  execution_scope, SCHEDULE_ACTIVE_H,
                                  execution_scope, execution_scope, limit))
        rows = [r[0] for r in cur.fetchall()]
    return rows


def sweep_all(limit=SWEEP_ALL_LIMIT):
    """Sweep a bounded rotating batch of active tenants. Returns metadata plus per-tenant pushes."""
    out = {}
    priority = _priority_tenant_batch(SWEEP_PRIORITY_LIMIT, execution_scope="production")
    tenants = list(dict.fromkeys(
        priority + _active_tenant_batch(limit, execution_scope="production")))
    # pulse.live() scans the global work plane.  Snapshot it once for this sweep and partition in memory;
    # calling it once per tenant turns a bounded tenant page into O(tenants × global fleet) pressure.
    try:
        import pulse
        pulse_rows = pulse.live() or []
    except Exception:
        pulse_rows = []
    for tid in tenants:
        try:
            n = sweep(tid, pulse_rows=pulse_rows, execution_scope="production")
            if n:
                out[tid] = len(n)
        except Exception:
            pass
    return {"checked": len(tenants), "limit": int(limit or 0),
            "priority_checked": len(priority), "pushed": out}


def _selftest():
    import types
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

        real_pulse = sys.modules.get("pulse")
        fake_pulse = types.ModuleType("pulse")
        fake_pulse.live = lambda: [
            {"work_id": "qa:1", "kind": "qa-run", "label": "QA explorer", "tenant_id": tid,
             "status": "active", "stage": "J4 wait-watch", "progress": "checking live activity",
             "age_s": PROGRESS_MIN_AGE_S + 30, "stalled": False},
            {"work_id": "actor:7", "kind": "fleet-actor", "label": "researcher · Maya", "tenant_id": tid,
             "status": "working", "stage": "working", "progress": "provider shortlist",
             "age_s": PROGRESS_MIN_AGE_S + 90, "stalled": False},
            {"work_id": "other", "kind": "qa-run", "label": "other tenant", "tenant_id": tid + "-other",
             "status": "active", "age_s": 9999, "stalled": False},
        ]
        sys.modules["pulse"] = fake_pulse
        try:
            progress = _progress_update(tid)
            assert len(progress) == 1 and progress[0]["id"] == "progress:active-work", progress
            assert progress[0]["severity"] == "low" and "2 active work item" in progress[0]["detail"]
            progress_sent = []
            progress_first = sweep(tid, notify=lambda *a, **k: progress_sent.append((a, k)),
                                   signals=lambda _t, _o: progress)
            progress_second = sweep(tid, notify=lambda *a, **k: progress_sent.append((a, k)),
                                    signals=lambda _t, _o: progress)
            assert progress_first == ["progress:active-work"] and progress_second == [], \
                (progress_first, progress_second)

            fake_pulse.live = lambda: [
                {"work_id": "qa:stalled", "kind": "qa-run", "label": "QA explorer", "tenant_id": tid,
                 "status": "active", "stage": "browser", "progress": "no beat",
                 "age_s": PROGRESS_MIN_AGE_S + 100, "stalled": True},
            ]
            stalled = _progress_update(tid)
            assert stalled and stalled[0]["severity"] == "high" and "stalled" in stalled[0]["title"].lower()
            progress_ok = True
        finally:
            if real_pulse is not None:
                sys.modules["pulse"] = real_pulse
            else:
                sys.modules.pop("pulse", None)
        ok = ok and progress_ok
        batch_tids = [f"{tid}-a", f"{tid}-b", f"{tid}-c"]
        old_cursor = None
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT cursor_tenant_id FROM proactive_sweep_state WHERE name='sweep-all'")
            row = cur.fetchone()
            old_cursor = row[0] if row else None
            for batch_tid in batch_tids:
                cur.execute("""INSERT INTO tenants (tenant_id, name, api_token, plan, period_start)
                               VALUES (%s,%s,%s,'free', now())
                               ON CONFLICT (tenant_id) DO NOTHING""", (batch_tid, batch_tid, f"tok-{batch_tid}"))
            cur.execute("""INSERT INTO proactive_sweep_state (name, cursor_tenant_id)
                           VALUES ('sweep-all', %s)
                           ON CONFLICT (name) DO UPDATE SET cursor_tenant_id=EXCLUDED.cursor_tenant_id""",
                        (f"{tid}-a",))
        first_batch = _active_tenant_batch(limit=2)
        second_batch = _active_tenant_batch(limit=2)
        batch_ok = (len(first_batch) == 2 and len(second_batch) == 2
                    and first_batch != second_batch and any(t in first_batch + second_batch for t in batch_tids))
        ok = ok and batch_ok

        print(f"first_push={len(first)} dedup_second={second} re_remind={len(overdue)} "
              f"levels={levels} progress_update={progress_ok}")
        print("PASS: proactive comms — pushes the calls that matter at the right altitude, dedups a standing "
              "item, re-reminds when overdue, sends bounded operating updates, and batches sweep-all ✅"
              if ok else "FAIL")
    finally:
        with tenant_connection(tid) as c, c.cursor() as cur:
            cur.execute("DELETE FROM proactive_sent WHERE tenant_id=%s", (tid,))
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM tenants WHERE tenant_id = ANY(%s)", (batch_tids if 'batch_tids' in locals() else [] ,))
            if 'old_cursor' in locals() and old_cursor is not None:
                cur.execute("""INSERT INTO proactive_sweep_state (name, cursor_tenant_id)
                               VALUES ('sweep-all', %s)
                               ON CONFLICT (name) DO UPDATE SET cursor_tenant_id=EXCLUDED.cursor_tenant_id""",
                            (old_cursor,))
            else:
                cur.execute("DELETE FROM proactive_sweep_state WHERE name='sweep-all'")
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
