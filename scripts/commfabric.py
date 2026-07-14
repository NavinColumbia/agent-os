#!/usr/bin/env python3
"""commfabric.py — durable ask-await (ADR 0005 §3): suspend-until-reply, resume on send,
SURVIVING the asker's process death — and zero tokens/compute held while parked.

Proof choreography (3 separate processes, mimicking crash + later reply):
  ask     : start a requester workflow that records a wait-edge then DBOS.recv()s, wait until it's
            parked in recv, then HARD-EXIT the process (asker "crashes" while waiting).
  answer  : a different process DBOS.send()s the reply to the parked workflow.
  result  : launch DBOS (recovers the parked requester -> its recv returns the reply) and fetch the
            result, proving the ask resumed across the crash.

Run with the agent-os venv python.
"""
import os
import sys
import time
from pathlib import Path

import psycopg
from dbos import DBOS, DBOSConfig, SetWorkflowID

from aoscfg import ENV, DB
WF_FILE = Path("/tmp/commfabric_wf_id")
TOPIC = "ask-await-demo"
RECV_TIMEOUT = 300   # seconds the requester will durably suspend in recv awaiting a reply


def _wait_edge(waiter, awaited, add=True, reply_by_seconds=None):
    """Insert/remove a wait-for edge.

    When the edge is created for a reply-expecting act, persist reply_by — the SLA
    deadline by which a reply is due (mirrors messaging.Message.reply_by, REQUIRED for
    QUERY/CLARIFY/DELEGATE/PROPOSE). Without it, reply_by stays NULL and
    accountability.overdue_waits()'s `WHERE reply_by IS NOT NULL AND reply_by < now()`
    can never fire, making all overdue-wait / SLA-breach detection dead. The deadline is
    tied to the recv timeout so the wait is flagged overdue before (or as) the recv expires.
    """
    with psycopg.connect(DB) as c, c.cursor() as cur:
        if add:
            if reply_by_seconds is not None:
                cur.execute(
                    "INSERT INTO waits(waiter,awaited,reply_by) "
                    "VALUES(%s,%s, now() + make_interval(secs => %s)) "
                    "ON CONFLICT (waiter,awaited) DO UPDATE SET reply_by = EXCLUDED.reply_by",
                    (waiter, awaited, reply_by_seconds))
            else:
                cur.execute("INSERT INTO waits(waiter,awaited) VALUES(%s,%s) ON CONFLICT DO NOTHING", (waiter, awaited))
        else:
            cur.execute("DELETE FROM waits WHERE waiter=%s AND awaited=%s", (waiter, awaited))
        c.commit()


DBOS(config=DBOSConfig(name="agentos-comm", database_url=DB))


@DBOS.workflow()
def requester(question: str):
    me = DBOS.workflow_id
    # reply-expecting ask: persist the SLA deadline so overdue-wait/SLA-breach detection can fire.
    _wait_edge(me, "responder", add=True, reply_by_seconds=RECV_TIMEOUT)   # visible in the wait-for graph while parked
    print(f"[requester] asked {question!r}; suspending in recv (no tokens burned)", flush=True)
    reply = DBOS.recv(topic=TOPIC, timeout_seconds=RECV_TIMEOUT)   # DURABLE suspend — survives process death
    _wait_edge(me, "responder", add=False)
    print(f"[requester] resumed with reply: {reply!r}", flush=True)
    return reply


def main(mode):
    DBOS.launch()
    if mode == "ask":
        import uuid
        wf_id = f"ask-{uuid.uuid4().hex[:8]}"
        WF_FILE.write_text(wf_id)
        with SetWorkflowID(wf_id):
            DBOS.start_workflow(requester, "is SendGrid v2 still supported?")
        for _ in range(120):                       # wait until the workflow is parked in recv
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("SELECT 1 FROM waits WHERE waiter=%s", (wf_id,))
                if cur.fetchone():
                    break
            time.sleep(0.5)
        print(f"[ask] requester {wf_id} parked in recv; HARD-EXITING (simulating asker crash)", flush=True)
        sys.stdout.flush()
        os._exit(9)
    elif mode == "answer":
        wf_id = WF_FILE.read_text().strip()
        DBOS.send(wf_id, {"answer": "No — v2 is deprecated (410). Use v3 /mail/send."}, topic=TOPIC)
        print(f"[answer] reply sent to {wf_id}", flush=True)
    elif mode == "result":
        wf_id = WF_FILE.read_text().strip()
        res = DBOS.retrieve_workflow(wf_id).get_result()
        print(f"[result] requester returned: {res!r}")
        ok = isinstance(res, dict) and "v3" in res.get("answer", "")
        print("PASS: ask-await survived the asker's crash and resumed on reply ✅" if ok else f"FAIL: {res!r}")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ask")
