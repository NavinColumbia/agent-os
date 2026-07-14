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
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402

from aoscfg import ENV, DB


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS ask_user_requests (
            id BIGSERIAL PRIMARY KEY, tenant_id TEXT, thread_id BIGINT, product TEXT, question TEXT,
            status TEXT DEFAULT 'open', answer TEXT, created_at TIMESTAMPTZ DEFAULT now())""")
        c.commit()


def ask(tenant_id, thread_id, product, question):
    """File an open question to the user and (best-effort) surface it through the shared request fabric.
    Returns {ask_id, status:'open'}; a paused loop later polls is_answered(ask_id) to resume."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO ask_user_requests (tenant_id, thread_id, product, question)
                       VALUES (%s,%s,%s,%s) RETURNING id""", (tenant_id, thread_id, product, question))
        ask_id = cur.fetchone()[0]
        c.commit()
    # Lazy, best-effort delegation to the parallel agent_request fabric (may not exist yet).
    try:
        import agent_request  # noqa: E402
        agent_request.ask(tenant_id, question, kind="do_task", thread_id=thread_id)
    except Exception:
        pass
    audit.append(actor="askuser", action="AskUser", resource=str(ask_id), decision="open",
                 payload={"tenant_id": tenant_id, "thread_id": thread_id, "product": product})
    return {"ask_id": ask_id, "status": "open"}


def answer(ask_id, answer):
    """Record the user's reply; flips the request to 'answered' so a polling loop can resume."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("UPDATE ask_user_requests SET status='answered', answer=%s WHERE id=%s",
                    (answer, ask_id))
        c.commit()
    audit.append(actor="askuser", action="AskUserAnswered", resource=str(ask_id), decision="answered",
                 payload={"answer": answer[:200]})
    return {"ask_id": ask_id, "status": "answered"}


def is_answered(ask_id):
    """True once the user has answered — the loop polls this to know it may resume."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT status FROM ask_user_requests WHERE id=%s", (ask_id,))
        row = cur.fetchone()
    return bool(row and row[0] == "answered")


def get_answer(ask_id):
    """The user's answer text (or None if still open) — what the resumed loop consumes."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT answer FROM ask_user_requests WHERE id=%s", (ask_id,))
        row = cur.fetchone()
    return row[0] if row else None


def pending(tenant_id):
    """Open (unanswered) questions for a tenant — the user's to-do list of things the loop is waiting on."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
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
        before = is_answered(ask_id)                         # not answered yet -> loop stays paused
        in_pending = any(p["ask_id"] == ask_id for p in pending(tid))
        answer(ask_id, "yes, approved")
        after = is_answered(ask_id)                          # answered -> loop resumes
        got = get_answer(ask_id) == "yes, approved"
        cleared = not any(p["ask_id"] == ask_id for p in pending(tid))  # answered drops from pending
        ok = opened and (not before) and in_pending and after and got and cleared
        print(f"opened={opened} pre={before} in_pending={in_pending} post={after} answer_kept={got} pending_clears={cleared}")
        print("PASS: ask pauses the loop, is_answered gates resume, answer unblocks + clears pending ✅" if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            if ask_id is not None:
                cur.execute("DELETE FROM ask_user_requests WHERE id=%s", (ask_id,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
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
