#!/usr/bin/env python3
"""askuser.py — ask the user to do hard things mid-loop.

Sometimes an agent loop hits something it cannot do itself: "please grant the OAuth scope", "approve
this charge", "paste the 2FA code". Instead of failing or guessing, the loop PAUSES, posts a question
to the controller chat, and RESUMES once the user answers. This module is that pause/resume primitive:
ask() files an open question, a loop polls is_answered() to know when to resume, and answer() records
the user's reply.

This is a THIN convenience wrapper over agent_request.py (built in parallel) — imported lazily so we
don't hard-depend on it; if it isn't importable yet we still work off our own local ask_user_requests
table, and once it lands the question also surfaces through the shared request fabric.

    askuser.py ask <tenant> <thread_id> <product> "<question>"
    askuser.py answer <ask_id> "<answer>"
    askuser.py pending <tenant>
    askuser.py answered <ask_id>          # True/False — a loop polls this to resume
    askuser.py selftest
Run with the agent-os venv python.
"""
import sys
import threading
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402

from aoscfg import ENV, DB
from dbpool import connection, tenant_connection

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
            cur.execute("""CREATE TABLE IF NOT EXISTS ask_user_requests (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT, thread_id BIGINT, product TEXT, question TEXT,
                status TEXT DEFAULT 'open', answer TEXT, agent_request_id BIGINT,
                created_at TIMESTAMPTZ DEFAULT now())""")
            cur.execute("ALTER TABLE ask_user_requests ADD COLUMN IF NOT EXISTS agent_request_id BIGINT")
        _ensured = True


def ask(tenant_id, thread_id, product, question):
    """File an open question to the user and (best-effort) surface it through the shared request fabric.
    Returns {ask_id, status:'open'}; a paused loop later polls is_answered(ask_id) to resume."""
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO ask_user_requests (tenant_id, thread_id, product, question)
                       VALUES (%s,%s,%s,%s) RETURNING id""", (tenant_id, thread_id, product, question))
        ask_id = cur.fetchone()[0]
    # Delegate to the shared request fabric and DURABLY LINK the two rows. Previously this created an
    # unlinked duplicate: answering ask_user_requests left agent_requests open forever, so the CEO kept an
    # immortal badge and the proactive engine kept re-reminding an already-answered question.
    agent_request_id = None
    try:
        import agent_request  # noqa: E402
        shared = agent_request.ask(tenant_id, question, kind="do_task", thread_id=thread_id)
        agent_request_id = shared.get("request_id")
        with tenant_connection(tenant_id) as c, c.cursor() as cur:
            cur.execute("UPDATE ask_user_requests SET agent_request_id=%s WHERE id=%s AND tenant_id=%s",
                        (agent_request_id, ask_id, tenant_id))
    except Exception as e:
        audit.append(actor="askuser", action="AskUserSharedRequest", resource=str(ask_id), decision="failed",
                     payload={"error": str(e)[:300]}, tenant_id=tenant_id)
    audit.append(actor="askuser", action="AskUser", resource=str(ask_id), decision="open",
                 payload={"tenant_id": tenant_id, "thread_id": thread_id, "product": product,
                          "agent_request_id": agent_request_id}, tenant_id=tenant_id)
    return {"ask_id": ask_id, "status": "open", "agent_request_id": agent_request_id}


def answer(ask_id, answer, tenant_id=None):
    """Record the user's reply; flips the request to 'answered' so a polling loop can resume.

    Pass tenant_id for tenant-facing calls. A tenant mismatch raises instead of silently reporting success;
    otherwise a loop can stay parked while the human believes their answer landed.
    """
    _ensure()
    lookup = tenant_connection(tenant_id) if tenant_id is not None else connection()
    with lookup as c, c.cursor() as cur:
        if tenant_id is not None:
            cur.execute("SELECT tenant_id, agent_request_id FROM ask_user_requests WHERE id=%s AND tenant_id=%s",
                        (ask_id, tenant_id))
        else:
            cur.execute("SELECT tenant_id, agent_request_id FROM ask_user_requests WHERE id=%s", (ask_id,))
        row = cur.fetchone()
    if not row:
        audit.append(actor="askuser", action="AskUserAnswered", resource=str(ask_id), decision="not_found",
                     payload={"ask_id": ask_id}, tenant_id=tenant_id)
        raise KeyError(f"no ask_user_request with id={ask_id!r} (answer not recorded)")
    owner_tid, shared_id = row
    # Resolve the canonical shared request first. If this fails, leave the local row open rather than
    # manufacturing a successful answer while the actual blocked phase remains parked.
    if shared_id is not None:
        import agent_request
        agent_request.answer(shared_id, answer, tenant_id=owner_tid)
    conn = tenant_connection(tenant_id) if tenant_id is not None else connection()
    with conn as c, c.cursor() as cur:
        if tenant_id is not None:
            cur.execute("""UPDATE ask_user_requests SET status='answered', answer=%s
                           WHERE id=%s AND tenant_id=%s""", (answer, ask_id, tenant_id))
        else:
            cur.execute("UPDATE ask_user_requests SET status='answered', answer=%s WHERE id=%s",
                        (answer, ask_id))
        matched = cur.rowcount
    if matched == 0:
        audit.append(actor="askuser", action="AskUserAnswered", resource=str(ask_id), decision="not_found",
                     payload={"ask_id": ask_id}, tenant_id=tenant_id)
        raise KeyError(f"no ask_user_request with id={ask_id!r} (answer not recorded)")
    audit.append(actor="askuser", action="AskUserAnswered", resource=str(ask_id), decision="answered",
                 payload={"answer": answer[:200]}, tenant_id=tenant_id)
    return {"ask_id": ask_id, "status": "answered"}


def is_answered(ask_id, tenant_id=None):
    """True once the user has answered — the loop polls this to know it may resume."""
    _ensure()
    conn = tenant_connection(tenant_id) if tenant_id is not None else connection()
    with conn as c, c.cursor() as cur:
        if tenant_id is not None:
            cur.execute("SELECT status, tenant_id, agent_request_id FROM ask_user_requests WHERE id=%s AND tenant_id=%s",
                        (ask_id, tenant_id))
        else:
            cur.execute("SELECT status, tenant_id, agent_request_id FROM ask_user_requests WHERE id=%s", (ask_id,))
        row = cur.fetchone()
    if not row:
        return False
    if row[0] == "answered":
        return True
    if row[2] is not None:
        try:
            import agent_request
            if agent_request.is_answered(row[2], tenant_id=row[1]):
                shared = agent_request.get(row[2], tenant_id=row[1]) or {}
                with tenant_connection(row[1]) as c, c.cursor() as cur:
                    cur.execute("""UPDATE ask_user_requests SET status='answered', answer=%s
                                   WHERE id=%s AND tenant_id=%s""", (shared.get("answer"), ask_id, row[1]))
                return True
        except Exception:
            pass
    return False


def get_answer(ask_id, tenant_id=None):
    """The user's answer text (or None if still open) — what the resumed loop consumes."""
    _ensure()
    conn = tenant_connection(tenant_id) if tenant_id is not None else connection()
    with conn as c, c.cursor() as cur:
        if tenant_id is not None:
            cur.execute("SELECT answer FROM ask_user_requests WHERE id=%s AND tenant_id=%s", (ask_id, tenant_id))
        else:
            cur.execute("SELECT answer FROM ask_user_requests WHERE id=%s", (ask_id,))
        row = cur.fetchone()
    return row[0] if row else None


def pending(tenant_id):
    """Open (unanswered) questions for a tenant — the user's to-do list of things the loop is waiting on."""
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("""SELECT id, thread_id, product, question, created_at FROM ask_user_requests
                       WHERE tenant_id=%s AND status='open' ORDER BY created_at""", (tenant_id,))
        return [{"ask_id": i, "thread_id": th, "product": p, "question": q, "created_at": str(ts)}
                for i, th, p, q, ts in cur.fetchall()]


def _selftest():
    import billing
    t = billing.signup("askuser-" + __import__("os").urandom(3).hex())
    tid = t["tenant_id"]
    ask_id = None
    try:
        r = ask(tid, 1, "billing", "Please approve the upgrade charge")
        ask_id = r["ask_id"]
        opened = r["status"] == "open"
        before = is_answered(ask_id, tenant_id=tid)           # not answered yet -> loop stays paused
        in_pending = any(p["ask_id"] == ask_id for p in pending(tid))
        bad_tenant_raises = False
        try:
            answer(ask_id, "wrong tenant", tenant_id="not-" + tid)
        except KeyError:
            bad_tenant_raises = True
        answer(ask_id, "yes, approved", tenant_id=tid)
        after = is_answered(ask_id, tenant_id=tid)            # answered -> loop resumes
        got = get_answer(ask_id, tenant_id=tid) == "yes, approved"
        cleared = not any(p["ask_id"] == ask_id for p in pending(tid))  # answered drops from pending
        ok = opened and (not before) and in_pending and bad_tenant_raises and after and got and cleared
        print(f"opened={opened} pre={before} in_pending={in_pending} post={after} answer_kept={got} pending_clears={cleared}")
        print("PASS: ask pauses the loop, is_answered gates resume, answer unblocks + clears pending ✅" if ok else "FAIL")
    finally:
        with tenant_connection(tid) as c, c.cursor() as cur:
            if ask_id is not None:
                cur.execute("DELETE FROM ask_user_requests WHERE id=%s", (ask_id,))
            cur.execute("DELETE FROM agent_requests WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM notification_deliveries WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM notifications WHERE tenant_id=%s", (tid,))
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "ask" and len(a) >= 5:
        print(json.dumps(ask(a[1], int(a[2]), a[3], a[4]), indent=2))
    elif a[0] == "answer" and len(a) >= 3:
        print(json.dumps(answer(int(a[1]), a[2]), indent=2))
    elif a[0] == "pending" and len(a) > 1:
        print(json.dumps(pending(a[1]), indent=2))
    elif a[0] == "answered" and len(a) > 1:
        print(json.dumps({"ask_id": int(a[1]), "is_answered": is_answered(int(a[1]))}))
    else:
        sys.exit('usage: askuser.py ask <tenant> <thread_id> <product> "<q>" | answer <id> "<a>" | pending <tenant> | answered <id> | selftest')


if __name__ == "__main__":
    _main(sys.argv[1:])
