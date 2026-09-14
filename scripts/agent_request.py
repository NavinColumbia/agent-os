#!/usr/bin/env python3
"""agent_request.py — durable, push-notifying, reply-resumable agent->human requests.

Sometimes an agent can't finish a phase alone: it needs a credential, a decision, a task done by a human, or
just an answer. Instead of failing or guessing, it ask()s — which durably records the question (status
'open'), pushes the tenant (push.send, high priority), and posts it into the controller chat thread so the
human sees it where the dialogue lives. The phase then BLOCKS, polling is_answered(); when the human
answer()s, status flips to 'answered' and the caller resumes with the answer. This is the proactive handoff:
the agent reaches out, the human replies, the work continues.

    agent_request.py ask <tenant> "<question>" [kind] [thread_id]   # kind: credential|decision|do_task|info
    agent_request.py answer <request_id> "<answer>"
    agent_request.py get <request_id>
    agent_request.py open <tenant>
    agent_request.py selftest
Run with the agent-os venv python.
"""
import os
import sys
import threading
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit    # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402

from aoscfg import ENV, DB
KINDS = ("credential", "decision", "do_task", "info", "resource", "process_change")
EXECUTION_SCOPES = ("production", "test", "legacy")
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
            cur.execute("""CREATE TABLE IF NOT EXISTS agent_requests (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT, org_id TEXT, thread_id BIGINT,
                kind TEXT, question TEXT, status TEXT DEFAULT 'open', answer TEXT,
                correlation_id TEXT,
                created_at TIMESTAMPTZ DEFAULT now(), answered_at TIMESTAMPTZ)""")
            cur.execute("ALTER TABLE agent_requests ADD COLUMN IF NOT EXISTS correlation_id TEXT")
            cur.execute("ALTER TABLE agent_requests ADD COLUMN IF NOT EXISTS execution_scope TEXT")
            cur.execute("SELECT to_regclass('public.controller_state')")
            if cur.fetchone()[0]:
                cur.execute("""UPDATE agent_requests ar SET execution_scope=COALESCE(
                                  (SELECT cs.execution_scope FROM controller_state cs
                                    WHERE cs.thread_id=ar.thread_id LIMIT 1), 'legacy')
                               WHERE ar.execution_scope IS NULL""")
            else:
                cur.execute("UPDATE agent_requests SET execution_scope='legacy' WHERE execution_scope IS NULL")
            cur.execute("ALTER TABLE agent_requests ALTER COLUMN execution_scope SET DEFAULT 'production'")
            cur.execute("ALTER TABLE agent_requests ALTER COLUMN execution_scope SET NOT NULL")
            cur.execute("""DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint
                               WHERE conname='agent_requests_execution_scope_check') THEN
                  ALTER TABLE agent_requests ADD CONSTRAINT agent_requests_execution_scope_check
                    CHECK (execution_scope IN ('production','test','legacy'));
                END IF;
              END $$""")
            cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS agent_requests_correlation_idx
                           ON agent_requests (tenant_id, correlation_id)
                           WHERE correlation_id IS NOT NULL""")
            cur.execute("""CREATE INDEX IF NOT EXISTS agent_requests_scope_open_idx
                           ON agent_requests (execution_scope, status, tenant_id, id)
                           WHERE status='open'""")
        _ensured = True


def _execution_scope(value=None):
    if value is None:
        is_test = (os.environ.get("AOS_SELFTEST", "").strip().lower() in {"1", "true", "yes", "on"}
                   or bool(os.environ.get("PYTEST_CURRENT_TEST")))
        return "test" if is_test else "production"
    value = str(value).strip().lower()
    if value not in EXECUTION_SCOPES:
        raise ValueError("execution_scope must be production, test, or legacy")
    return value


def _row(rid, tenant_id=None):
    conn = tenant_connection(tenant_id) if tenant_id is not None else connection()
    with conn as c, c.cursor() as cur:
        if tenant_id is not None:
            cur.execute("""SELECT id, tenant_id, org_id, thread_id, kind, question, status, answer,
                           created_at, answered_at, execution_scope
                           FROM agent_requests WHERE id=%s AND tenant_id=%s""",
                        (rid, tenant_id))
        else:
            cur.execute("""SELECT id, tenant_id, org_id, thread_id, kind, question, status, answer,
                           created_at, answered_at, execution_scope
                           FROM agent_requests WHERE id=%s""", (rid,))
        r = cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "tenant_id": r[1], "org_id": r[2], "thread_id": r[3], "kind": r[4],
            "question": r[5], "status": r[6], "answer": r[7],
            "created_at": str(r[8]), "answered_at": str(r[9]) if r[9] else None,
            "execution_scope": r[10]}


def ask(tenant_id, question, kind="info", org_id=None, thread_id=None, correlation_id=None,
        execution_scope=None):
    """Record an open agent->human request, push the tenant, and post into the controller chat (if a thread
    is given). The phase that calls this should then poll is_answered() to resume. Returns {request_id, status}."""
    _ensure()
    kind = kind if kind in KINDS else "info"
    correlation_id = str(correlation_id) if correlation_id else None
    scope = _execution_scope(execution_scope)
    created = True
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        if correlation_id:
            cur.execute("""INSERT INTO agent_requests
                           (tenant_id, org_id, thread_id, kind, question, status, correlation_id,
                            execution_scope)
                           VALUES (%s,%s,%s,%s,%s,'open',%s,%s)
                           ON CONFLICT (tenant_id, correlation_id) WHERE correlation_id IS NOT NULL
                           DO NOTHING RETURNING id,status,execution_scope""",
                        (tenant_id, org_id, thread_id, kind, question, correlation_id, scope))
            row = cur.fetchone()
            if row:
                rid, request_status, scope = row
            else:
                created = False
                cur.execute("""SELECT id,status,execution_scope FROM agent_requests
                               WHERE tenant_id=%s AND correlation_id=%s""",
                            (tenant_id, correlation_id))
                rid, request_status, scope = cur.fetchone()
        else:
            cur.execute("""INSERT INTO agent_requests
                           (tenant_id, org_id, thread_id, kind, question, status, execution_scope)
                           VALUES (%s,%s,%s,%s,%s,'open',%s)
                           RETURNING id,status,execution_scope""",
                        (tenant_id, org_id, thread_id, kind, question, scope))
            rid, request_status, scope = cur.fetchone()
    # Retrying a correlated ask after a crash returns the original durable request. It must not page or post
    # the same question a second time.
    if not created:
        return {"request_id": rid, "status": request_status, "delivery": None, "duplicate": True,
                "execution_scope": scope}
    # Persist in the CEO feed and synchronously attempt bounded external delivery. notifications.send records
    # accepted/unavailable/failed separately and the scheduler retries; no daemon thread can vanish on exit.
    delivery = None
    try:
        import notifications
        delivery = notifications.send(
            tenant_id, "approvals", f"Action needed: {kind}", question,
            level="urgent", url="/#approvals", context_key=f"agent_request:{rid}")
    except Exception as e:
        audit.append(actor="agent_request", action="AgentRequestNotify", resource=str(rid),
                     decision="failed", payload={"error": str(e)[:300]}, tenant_id=tenant_id)
    if thread_id is not None:
        try:
            import orchestrator
            orchestrator.post(tenant_id, thread_id, f"🔔 {question}",
                              {"kind": "agent_request", "request_id": rid})
        except Exception as e:
            audit.append(actor="agent_request", action="AgentRequestChatPost", resource=str(rid),
                         decision="failed", payload={"thread_id": thread_id, "error": str(e)[:300]},
                         tenant_id=tenant_id)
    audit.append(actor="agent_request", action="AgentRequestAsk", resource=tenant_id, decision="open",
                 payload={"request_id": rid, "kind": kind, "question": question[:200], "thread_id": thread_id,
                          "delivery": delivery, "correlation_id": correlation_id},
                 tenant_id=tenant_id)
    return {"request_id": rid, "status": request_status, "delivery": delivery, "duplicate": False,
            "execution_scope": scope}


def answer(request_id, answer, tenant_id=None):
    """A human resolves the request. Flips it to 'answered' so a polling caller resumes. Returns {request_id,
    status}. When tenant_id is given (a tenant-facing call from the console), the write is SCOPED to that
    tenant so one tenant can NEVER answer another tenant's question by guessing an id (cross-tenant write
    guard); a mismatch matches 0 rows and raises just like a missing id."""
    _ensure()
    before = _row(request_id, tenant_id=tenant_id)
    conn = tenant_connection(tenant_id) if tenant_id is not None else connection()
    with conn as c, c.cursor() as cur:
        if tenant_id is not None:
            cur.execute("""UPDATE agent_requests SET status='answered', answer=%s, answered_at=now()
                           WHERE id=%s AND tenant_id=%s""", (answer, request_id, tenant_id))
        else:
            cur.execute("""UPDATE agent_requests SET status='answered', answer=%s, answered_at=now()
                           WHERE id=%s""", (answer, request_id))
        matched = cur.rowcount
    if matched == 0:
        # No such request: the answer attached to nothing. Tell the caller instead of
        # falsely reporting success — a silent no-op here would leave a polling phase
        # (is_answered) blocked forever while the human believes the reply landed.
        audit.append(actor="agent_request", action="AgentRequestAnswer", resource=str(request_id),
                     decision="not_found", payload={"request_id": request_id}, tenant_id=tenant_id)
        raise KeyError(f"no agent_request with id={request_id!r} (answer not recorded)")
    audit.append(actor="agent_request", action="AgentRequestAnswer", resource=str(request_id),
                 decision="answered", payload={"request_id": request_id, "answer": str(answer)[:200]},
                 tenant_id=tenant_id)
    owner_tid = tenant_id or (before or {}).get("tenant_id")
    if owner_tid:
        try:
            import notifications
            notifications.resolve(owner_tid, f"agent_request:{request_id}")
        except Exception as e:
            audit.append(actor="agent_request", action="AgentRequestNotificationResolve",
                         resource=str(request_id), decision="failed", payload={"error": str(e)[:300]},
                         tenant_id=owner_tid)
    return {"request_id": request_id, "status": "answered"}


def get(request_id, tenant_id=None):
    """The full request row (or None). Pass tenant_id for tenant-facing scoped reads."""
    _ensure()
    return _row(request_id, tenant_id=tenant_id)


def open_requests(tenant_id, execution_scope=None):
    """All still-open requests for a tenant (the human's to-do list / the blocked phases)."""
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("""SELECT id, tenant_id, org_id, thread_id, kind, question, status, answer,
                       created_at, answered_at, execution_scope
                       FROM agent_requests WHERE tenant_id=%s AND status='open'
                         AND (%s::text IS NULL OR execution_scope=%s)
                       ORDER BY id""", (tenant_id, execution_scope, execution_scope))
        rows = cur.fetchall()
    return [{"id": r[0], "tenant_id": r[1], "org_id": r[2], "thread_id": r[3], "kind": r[4],
             "question": r[5], "status": r[6], "answer": r[7],
             "created_at": str(r[8]), "answered_at": str(r[9]) if r[9] else None,
             "execution_scope": r[10]}
            for r in rows]


def is_answered(request_id, tenant_id=None):
    """True once a human has answered — the poll a blocked phase loops on to resume.

    Pass tenant_id when the caller already knows tenant context so the read runs under the same app-role/RLS
    contract as tenant-facing answer/open paths. The id-only form remains an admin/back-compat path for older
    blocked phases that only persisted the request id.
    """
    _ensure()
    conn = tenant_connection(tenant_id) if tenant_id is not None else connection()
    with conn as c, c.cursor() as cur:
        if tenant_id is not None:
            cur.execute("SELECT status FROM agent_requests WHERE id=%s AND tenant_id=%s", (request_id, tenant_id))
        else:
            cur.execute("SELECT status FROM agent_requests WHERE id=%s", (request_id,))
        row = cur.fetchone()
    return bool(row and row[0] == "answered")


def reconcile_satisfied_provider_requests(limit=20):
    """Close model-provider requests once the provider fact is independently true.

    Credential requests are not interchangeable: a connected Codex account must
    never auto-close a Stripe/API/domain request. Only the controller's stable
    provider correlation (plus the exact legacy wording used before correlation
    was added) is eligible.
    """
    _ensure()
    limit = max(1, min(100, int(limit or 1)))
    with connection() as c, c.cursor() as cur:
        cur.execute("""SELECT id,tenant_id FROM agent_requests
                        WHERE status='open' AND kind='credential'
                          AND (correlation_id LIKE 'controller:%%:provider'
                               OR (question ILIKE '%%model provider%%'
                                   AND question ILIKE '%%Anthropic%%'
                                   AND question ILIKE '%%Codex%%'))
                        ORDER BY id LIMIT %s""", (limit,))
        candidates = cur.fetchall()
    resolved = []
    for request_id, tenant_id in candidates:
        try:
            import auth
            if not auth.provider_resolved(tenant_id):
                continue
            answer(request_id,
                   "Automatically reconciled: a working model provider connection is now verified.",
                   tenant_id=tenant_id)
            resolved.append(request_id)
        except Exception as exc:
            audit.append(actor="agent_request", action="ProviderRequestReconcile",
                         resource=str(request_id), decision="deferred",
                         payload={"error": str(exc)[:300]}, tenant_id=tenant_id)
    return resolved


def _selftest():
    import billing
    t = billing.signup("agent-request-selftest")
    tid = t["tenant_id"]
    try:
        r = ask(tid, "Need the Stripe live key to wire payments — paste it?", kind="credential")
        rid = r["request_id"]
        opened = r["status"] == "open"
        before = is_answered(rid, tenant_id=tid)               # not yet answered -> False
        in_open = any(o["id"] == rid for o in open_requests(tid))
        answer(rid, "sk_live_REDACTED", tenant_id=tid)
        after = is_answered(rid, tenant_id=tid)                # answered -> True
        still_open = any(o["id"] == rid for o in open_requests(tid))  # must now be excluded
        ans_ok = get(rid, tenant_id=tid)["status"] == "answered"
        bad_raises = False                                    # answering a bad id must fail loudly
        try:
            answer(-1, "no such request")
        except KeyError:
            bad_raises = True
        ok = (opened and (not before) and in_open and after and ans_ok
              and (not still_open) and bad_raises)
        print(f"opened={opened} before={before} in_open={in_open} after={after} "
              f"excluded_after_answer={not still_open} bad_id_raises={bad_raises}")
        print("PASS: ask creates open req, is_answered flips on answer, open_requests resolves ✅"
              if ok else "FAIL")
    finally:
        with tenant_connection(tid) as c, c.cursor() as cur:
            cur.execute("DELETE FROM agent_requests WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM push_targets WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM notification_deliveries WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM notifications WHERE tenant_id=%s", (tid,))
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "ask" and len(a) >= 3:
        print(json.dumps(ask(a[1], a[2], kind=a[3] if len(a) > 3 else "info",
                             thread_id=int(a[4]) if len(a) > 4 else None)))
    elif a[0] == "answer" and len(a) >= 3:
        print(json.dumps(answer(int(a[1]), a[2])))
    elif a[0] == "get" and len(a) > 1:
        print(json.dumps(get(int(a[1])), indent=2))
    elif a[0] == "open" and len(a) > 1:
        print(json.dumps(open_requests(a[1]), indent=2))
    else:
        sys.exit('usage: agent_request.py ask <tenant> "<question>" [kind] [thread_id] | '
                 'answer <request_id> "<answer>" | get <request_id> | open <tenant> | selftest')


if __name__ == "__main__":
    _main(sys.argv[1:])
