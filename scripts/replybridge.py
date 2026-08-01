#!/usr/bin/env python3
"""replybridge.py — close the phone→controller loop for the real e2e experience.

reply_listener.py turns a phone ntfy reply into a command file at bridge/inbox/<task>.cmd. Nothing consumed
those for a build thread — so the CEO could be PINGED for a decision but couldn't ANSWER from their phone and
have the run continue. This bridge closes that loop: it watches the inbox for replies addressed to a
controller thread (task = "ceo-<thread_id>" or "ceo" for the tenant's single active build) and feeds the
reply text into loopcontroller.say(tenant, thread_id, text) — exactly as if the CEO typed it in the console.
That makes the pipeline answerable from anywhere: prompt in → jobd drives → controller pings you → you reply
from your phone → jobd continues → delivers.

    replybridge.py serve            # watch bridge/inbox and feed ceo-* replies into the controller (daemon)
    replybridge.py once             # process the inbox once and exit (for the scheduler / a tick)
Supervised like the other daemons (watchdog EXPECTED + responder DAEMONS).
"""
import os
import re
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit          # noqa: E402
import loopcontroller as lc  # noqa: E402

INBOX = Path.home() / "projects" / "agent-os" / "bridge" / "inbox"
DONE = INBOX.parent / "processed"
POLL_S = int(os.environ.get("AOS_REPLYBRIDGE_POLL_S", "5"))
# a reply is FOR a build thread when its task is "ceo", "ceo-<thread_id>", or just "<thread_id>"
_CEO_TASK = re.compile(r"^(?:ceo[-_]?)?(\d+)?$", re.I)


def _tenant_for_thread(thread_id):
    s = lc._st(thread_id)
    return s.get("tenant_id") if s else None


def _resolve_thread(task, body):
    """Which controller thread does this reply answer? 'ceo-<id>'/'<id>' -> that thread; bare 'ceo' -> the
    tenant's single most-recent non-delivered build (only if unambiguous). Returns (tenant, thread_id) or None."""
    m = _CEO_TASK.match((task or "").strip())
    if not m:
        return None
    tid = m.group(1)
    if tid:
        t = _tenant_for_thread(int(tid))
        return (t, int(tid)) if t else None
    # bare 'ceo': find the single active (awaiting a human answer) thread across tenants; ambiguous -> skip
    import psycopg
    from aoscfg import DB
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT thread_id, tenant_id FROM controller_state
                       WHERE awaiting IN ('user_feedback','user_approval','credentials')
                       ORDER BY thread_id DESC LIMIT 2""")
        rows = cur.fetchall()
    if len(rows) == 1:
        return (rows[0][1], rows[0][0])
    return None                                   # 0 or >1 candidates -> can't safely route a bare 'ceo' reply


def process_inbox() -> int:
    """Consume ceo-* reply files: feed each into loopcontroller.say(). Returns how many were applied.
    Best-effort per file — a bad one is moved aside, never blocks the rest."""
    if not INBOX.exists():
        return 0
    DONE.mkdir(parents=True, exist_ok=True)
    applied = 0
    for f in sorted(INBOX.glob("*.cmd")):
        task = f.stem
        try:
            body = f.read_text().strip()
        except Exception:
            continue
        route = _resolve_thread(task, body)
        if not route:
            continue                              # not a controller reply (some other bridge task) -> leave it
        tenant, thread_id = route
        try:
            lc.say(tenant, thread_id, body)       # exactly as if the CEO typed it in the console
            audit.append(actor="replybridge", action="CeoReplyApplied", resource=str(thread_id),
                         decision="applied", payload={"task": task, "chars": len(body)})
            applied += 1
        except Exception as e:
            audit.append(actor="replybridge", action="CeoReplyError", resource=str(thread_id),
                         decision="error", payload={"task": task, "err": str(e)[:200]})
        try:
            f.rename(DONE / f"{int(time.time())}-{f.name}")
        except Exception:
            try: f.unlink()
            except Exception: pass
    return applied


def serve():
    print(f"[replybridge] watching {INBOX} for ceo-* replies -> loopcontroller.say (poll {POLL_S}s)", flush=True)
    while True:
        try:
            process_inbox()
        except Exception as e:
            print(f"[replybridge] tick error (continuing): {e}", flush=True)
        time.sleep(POLL_S)


def _selftest():
    """Offline: a ceo-<id> reply file is routed to the right thread and fed into say(). Stubs say()/_st()."""
    import tempfile
    global INBOX, DONE
    tmp = Path(tempfile.mkdtemp())
    INBOX, DONE = tmp / "inbox", tmp / "processed"
    INBOX.mkdir(parents=True)
    real_say, real_st = lc.say, lc._st
    seen = {}
    lc._st = lambda t: {"tenant_id": "acme"} if int(t) == 42 else None
    lc.say = lambda tenant, thread, msg, **k: seen.update(tenant=tenant, thread=thread, msg=msg)
    try:
        (INBOX / "ceo-42.cmd").write_text("go with your recommendation and build it\n")
        (INBOX / "some-other-task.cmd").write_text("unrelated\n")   # must be left alone
        n = process_inbox()
        ok = (n == 1 and seen.get("tenant") == "acme" and seen.get("thread") == 42
              and "recommendation" in seen.get("msg", "")
              and (INBOX / "some-other-task.cmd").exists()          # non-ceo file untouched
              and not (INBOX / "ceo-42.cmd").exists())              # ceo file consumed
        print(f"applied={n} routed_to={seen.get('tenant')}/{seen.get('thread')} "
              f"non_ceo_left={(INBOX/'some-other-task.cmd').exists()}")
        print("replybridge selftest: PASS (phone reply routed to the right thread -> say()) ✅" if ok
              else "replybridge selftest: FAIL")
        return 0 if ok else 1
    finally:
        lc.say, lc._st = real_say, real_st
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "serve":
        serve()
    elif a and a[0] == "once":
        print(f"applied {process_inbox()} reply(ies)")
    elif a and a[0] == "selftest":
        sys.exit(_selftest())
    else:
        sys.exit("usage: replybridge.py serve | once | selftest")
