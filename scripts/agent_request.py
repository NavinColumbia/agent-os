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
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit    # noqa: E402
import push     # noqa: E402  (per-tenant push transport)

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)
KINDS = ("credential", "decision", "do_task", "info")


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS agent_requests (
            id BIGSERIAL PRIMARY KEY, tenant_id TEXT, org_id TEXT, thread_id BIGINT,
            kind TEXT, question TEXT, status TEXT DEFAULT 'open', answer TEXT,
            created_at TIMESTAMPTZ DEFAULT now(), answered_at TIMESTAMPTZ)""")
        c.commit()


def _row(rid):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id, tenant_id, org_id, thread_id, kind, question, status, answer,
                       created_at, answered_at FROM agent_requests WHERE id=%s""", (rid,))
        r = cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "tenant_id": r[1], "org_id": r[2], "thread_id": r[3], "kind": r[4],
            "question": r[5], "status": r[6], "answer": r[7],
            "created_at": str(r[8]), "answered_at": str(r[9]) if r[9] else None}


def ask(tenant_id, question, kind="info", org_id=None, thread_id=None):
    """Record an open agent->human request, push the tenant, and post into the controller chat (if a thread
    is given). The phase that calls this should then poll is_answered() to resume. Returns {request_id, status}."""
    _ensure()
    kind = kind if kind in KINDS else "info"
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO agent_requests (tenant_id, org_id, thread_id, kind, question, status)
                       VALUES (%s,%s,%s,%s,%s,'open') RETURNING id""",
                    (tenant_id, org_id, thread_id, kind, question))
        rid = cur.fetchone()[0]; c.commit()
    push.send(tenant_id, f"Action needed: {kind}", question, priority="high")   # best-effort
    if thread_id is not None:
        try:
            import orchestrator
            orchestrator.post(tenant_id, thread_id, f"🔔 {question}",
                              {"kind": "agent_request", "request_id": rid})
        except Exception:
            pass
    audit.append(actor="agent_request", action="AgentRequestAsk", resource=tenant_id, decision="open",
                 payload={"request_id": rid, "kind": kind, "question": question[:200], "thread_id": thread_id})
    return {"request_id": rid, "status": "open"}


def answer(request_id, answer, tenant_id=None):
    """A human resolves the request. Flips it to 'answered' so a polling caller resumes. Returns {request_id,
    status}. When tenant_id is given (a tenant-facing call from the console), the write is SCOPED to that
    tenant so one tenant can NEVER answer another tenant's question by guessing an id (cross-tenant write
    guard); a mismatch matches 0 rows and raises just like a missing id."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        if tenant_id is not None:
            cur.execute("""UPDATE agent_requests SET status='answered', answer=%s, answered_at=now()
                           WHERE id=%s AND tenant_id=%s""", (answer, request_id, tenant_id))
        else:
            cur.execute("""UPDATE agent_requests SET status='answered', answer=%s, answered_at=now()
                           WHERE id=%s""", (answer, request_id))
        matched = cur.rowcount
        c.commit()
    if matched == 0:
        # No such request: the answer attached to nothing. Tell the caller instead of
        # falsely reporting success — a silent no-op here would leave a polling phase
        # (is_answered) blocked forever while the human believes the reply landed.
        audit.append(actor="agent_request", action="AgentRequestAnswer", resource=str(request_id),
                     decision="not_found", payload={"request_id": request_id})
        raise KeyError(f"no agent_request with id={request_id!r} (answer not recorded)")
    audit.append(actor="agent_request", action="AgentRequestAnswer", resource=str(request_id),
                 decision="answered", payload={"request_id": request_id, "answer": str(answer)[:200]})
    return {"request_id": request_id, "status": "answered"}


def get(request_id):
    """The full request row (or None)."""
    _ensure()
    return _row(request_id)


def open_requests(tenant_id):
    """All still-open requests for a tenant (the human's to-do list / the blocked phases)."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id FROM agent_requests WHERE tenant_id=%s AND status='open'
                       ORDER BY id""", (tenant_id,))
        ids = [r[0] for r in cur.fetchall()]
    return [_row(i) for i in ids]


def is_answered(request_id):
    """True once a human has answered — the poll a blocked phase loops on to resume."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT status FROM agent_requests WHERE id=%s", (request_id,))
        row = cur.fetchone()
    return bool(row and row[0] == "answered")


def _selftest():
    import billing
    t = billing.signup("agent-request-selftest")
    tid = t["tenant_id"]
    try:
        r = ask(tid, "Need the Stripe live key to wire payments — paste it?", kind="credential")
        rid = r["request_id"]
        opened = r["status"] == "open"
        before = is_answered(rid)                             # not yet answered -> False
        in_open = any(o["id"] == rid for o in open_requests(tid))
        answer(rid, "sk_live_REDACTED")
        after = is_answered(rid)                              # answered -> True
        still_open = any(o["id"] == rid for o in open_requests(tid))  # must now be excluded
        ans_ok = get(rid)["status"] == "answered"
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
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM agent_requests WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM push_targets WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
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
