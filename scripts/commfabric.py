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

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)
WF_FILE = Path("/tmp/commfabric_wf_id")
TOPIC = "ask-await-demo"


def _wait_edge(waiter, awaited, add=True):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        if add:
            cur.execute("INSERT INTO waits(waiter,awaited) VALUES(%s,%s) ON CONFLICT DO NOTHING", (waiter, awaited))
        else:
            cur.execute("DELETE FROM waits WHERE waiter=%s AND awaited=%s", (waiter, awaited))
        c.commit()


DBOS(config=DBOSConfig(name="agentos-comm", database_url=DB))


@DBOS.workflow()
def requester(question: str):
    me = DBOS.workflow_id
    _wait_edge(me, "responder", add=True)         # visible in the wait-for graph while parked
    print(f"[requester] asked {question!r}; suspending in recv (no tokens burned)", flush=True)
    reply = DBOS.recv(topic=TOPIC, timeout_seconds=300)   # DURABLE suspend — survives process death
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
