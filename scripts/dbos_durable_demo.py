#!/usr/bin/env python3
"""dbos_durable_demo.py — prove DBOS durable execution (ADR 0004 K1).

A workflow with two steps. We crash the process (os._exit) AFTER step_a and BEFORE step_b.
On restart, DBOS recovers the pending workflow and resumes from step_b WITHOUT re-running step_a.
Each step increments a persistent counter, so "ran exactly once across the crash" is provable.

    dbos_durable_demo.py start     # runs step_a, then hard-crashes (workflow left pending)
    dbos_durable_demo.py recover   # recovers pending workflow, runs step_b, prints result + counters
    dbos_durable_demo.py reset     # clear demo state
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
WF_FILE = Path("/tmp/dbos_demo_wf_id")  # start writes the unique id here; recover reads it


def _ensure_table():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS demo_counter (name TEXT PRIMARY KEY, n INT NOT NULL DEFAULT 0)")
        c.commit()


def _counter(name):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT n FROM demo_counter WHERE name=%s", (name,))
        r = cur.fetchone(); return r[0] if r else 0


def _bump(name):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO demo_counter(name,n) VALUES(%s,1) ON CONFLICT (name) DO UPDATE SET n=demo_counter.n+1 RETURNING n", (name,))
        n = cur.fetchone()[0]; c.commit(); return n


DBOS(config=DBOSConfig(name="agentos-demo", database_url=DB))


@DBOS.step()
def step_a():
    n = _bump("step_a")
    print(f"[step_a] ran (counter={n})", flush=True)
    return "A"


@DBOS.step()
def step_b():
    n = _bump("step_b")
    print(f"[step_b] ran (counter={n})", flush=True)
    return "B"


@DBOS.workflow()
def pipeline():
    # No crash logic in here — the workflow is deterministic. The crash is EXTERNAL
    # (the process is SIGKILLed during the durable sleep), faithfully simulating "the box died".
    a = step_a()
    time.sleep(45)   # blocking gap so the external SIGKILL reliably lands BETWEEN the steps
    b = step_b()
    return f"{a}/{b}"


def main(mode):
    if mode == "reset":
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS demo_counter"); c.commit()
        _ensure_table()  # create empty up front so concurrent threads never race on CREATE
        print("reset demo_counter"); return
    DBOS.launch()
    if mode == "start":
        _ensure_table()  # belt-and-suspenders: table exists before any worker thread runs
        # Synchronous: runs step_a (prints), then a 45s blocking gap. The harness watches for
        # "[step_a] ran" then SIGKILLs THIS process during the gap — a genuine external crash,
        # after step_a is durably recorded.
        import uuid
        wf_id = f"durable-demo-{uuid.uuid4().hex[:8]}"
        WF_FILE.write_text(wf_id)
        with SetWorkflowID(wf_id):
            DBOS.start_workflow(pipeline)  # async; runs on a worker thread
        for _ in range(120):               # wait until step_a is durably recorded
            if _counter("step_a") >= 1:
                break
            time.sleep(0.5)
        print("[start] step_a recorded; HARD-KILLING process mid-gap (workflow left PENDING)", flush=True)
        os._exit(9)                        # kills all threads incl. the in-gap workflow thread
    elif mode == "recover":
        wf_id = WF_FILE.read_text().strip()
        h = DBOS.retrieve_workflow(wf_id)
        result = h.get_result()  # blocks until the recovered workflow finishes step_b
        print(f"[recover] workflow result = {result!r}")
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT name,n FROM demo_counter ORDER BY name")
            counts = dict(cur.fetchall())
        print(f"[recover] counters = {counts}")
        ok = result == "A/B" and counts.get("step_a") == 1 and counts.get("step_b") == 1
        print("PASS: step_a ran exactly once across the crash; resumed at step_b ✅" if ok
              else f"FAIL: {result=} {counts=}")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "start")
