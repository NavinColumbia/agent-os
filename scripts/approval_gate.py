#!/usr/bin/env python3
"""approval_gate.py — durable human-in-the-loop approval (ADR 0005 #6/#18).

A gated action suspends as a durable workflow (zero tokens/process held), notifies the human via
ntfy, and resumes ONLY when the human's decision arrives — surviving a crash while it waits. The
live path: phone reply → ntfy reply topic → reply_listener → DBOS.send. Here `resolve` stands in
for that bridge so the durable gate is provable without a live phone reply.

    approval_gate.py ask "<action>"   # notify phone, suspend until decided (parks + exits)
    approval_gate.py resolve approve|reject
    approval_gate.py result
Run with the agent-os venv python.
"""
import os
import sys
import subprocess
import time
from pathlib import Path

import psycopg
from dbos import DBOS, DBOSConfig, SetWorkflowID

SCRIPTS = Path(__file__).resolve().parent
ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)
WF = Path("/tmp/approval_wf_id")
TOPIC = "human-approval"


def _wait(wf, on):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        if on:
            cur.execute("INSERT INTO waits(waiter,awaited) VALUES(%s,'human') ON CONFLICT DO NOTHING", (wf,))
        else:
            cur.execute("DELETE FROM waits WHERE waiter=%s AND awaited='human'", (wf,))
        c.commit()


def _notify(text):
    try:
        subprocess.run([str(SCRIPTS / ".." / ".venv" / "bin" / "python"), str(SCRIPTS / "notify.py"),
                        "--title", "APPROVAL NEEDED 🔐", "--priority", "urgent", text], timeout=15, check=False)
    except Exception:
        pass


DBOS(config=DBOSConfig(name="agentos-approval", database_url=DB))


@DBOS.workflow()
def gated(action: str):
    me = DBOS.workflow_id
    _wait(me, True)
    print(f"[gate] '{action}' awaiting human approval; suspended (no tokens burned)", flush=True)
    decision = DBOS.recv(topic=TOPIC, timeout_seconds=3600)  # durable, survives crash
    _wait(me, False)
    if decision is None:
        return "TIMEOUT->escalate"   # no unbounded wait — SLA breach escalates
    return f"{action}:{decision}"


def main(mode, arg=None):
    DBOS.launch()
    if mode == "ask":
        action = arg or "deploy NoUpload to production"
        import uuid
        wf = f"appr-{uuid.uuid4().hex[:8]}"; WF.write_text(wf)
        with SetWorkflowID(wf):
            DBOS.start_workflow(gated, action)
        for _ in range(120):
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("SELECT 1 FROM waits WHERE waiter=%s", (wf,))
                if cur.fetchone():
                    break
            time.sleep(0.5)
        _notify(f"Approve? {action}  (reply approve/reject)")
        print(f"[ask] gate {wf} suspended + phone notified; exiting (durable wait persists)", flush=True)
        sys.stdout.flush(); os._exit(9)
    elif mode == "resolve":
        wf = WF.read_text().strip()
        DBOS.send(wf, arg or "approve", topic=TOPIC)
        print(f"[resolve] sent '{arg}' to {wf}")
    elif mode == "result":
        wf = WF.read_text().strip()
        res = DBOS.retrieve_workflow(wf).get_result()
        print(f"[result] gate resolved: {res!r}")
        ok = res and res.endswith(":approve")
        print("PASS: durable human-approval gate suspended + resumed on decision ✅" if ok else f"FAIL: {res!r}")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ask", sys.argv[2] if len(sys.argv) > 2 else None)
