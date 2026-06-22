#!/usr/bin/env python3
"""dispatch.py — distributed task dispatch over NATS JetStream (ADR 0005).

The Controller publishes a typed task to a durable work-queue; a worker — which may be a SEPARATE
process or machine — pulls it, does the work, and replies over the bus. This decouples workers from
the Controller and is the last step toward horizontal scale: spin up N worker processes and they
load-balance the queue. Each dispatch is audited.

    dispatch.py demo
Run with the agent-os venv python.
"""
import asyncio
import json
import sys
from pathlib import Path

import nats
from nats.js.api import StreamConfig, RetentionPolicy, ConsumerConfig

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402

NATS_URL = "nats://127.0.0.1:4222"
STREAM = "DISPATCH"
SUBJECT = "dispatch.tasks"


async def ensure_stream(js):
    cfg = StreamConfig(name=STREAM, subjects=[SUBJECT], retention=RetentionPolicy.WORK_QUEUE, max_msgs=100000)
    try:
        await js.add_stream(cfg)
    except Exception:
        await js.update_stream(cfg)


async def worker(nc, js, stop):
    """A decoupled worker: pulls tasks, does them, replies. Run as many as you like."""
    sub = await js.pull_subscribe(SUBJECT, durable="dispatch-workers",
                                  config=ConsumerConfig(durable_name="dispatch-workers", ack_wait=30))
    while not stop.is_set():
        try:
            msgs = await sub.fetch(1, timeout=1)
        except Exception:
            continue
        for m in msgs:
            task = json.loads(m.data)
            result = {"task_id": task["task_id"], "output": task["a"] * task["b"], "worker": "w1"}
            await m.ack()
            await nc.publish(f"dispatch.result.{task['task_id']}", json.dumps(result).encode())
            await nc.flush()


async def _demo():
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    await ensure_stream(js)
    stop = asyncio.Event()
    wt = asyncio.create_task(worker(nc, js, stop))

    task = {"task_id": "disp-1", "op": "mul", "a": 6, "b": 7}
    inbox = await nc.subscribe(f"dispatch.result.{task['task_id']}")
    ack = await js.publish(SUBJECT, json.dumps(task).encode())
    audit.append(actor="controller", action="DispatchTask", resource=task["task_id"], decision="executed",
                 payload={"seq": ack.seq})
    print(f"[controller] dispatched task {task['task_id']} (JetStream seq={ack.seq})")
    try:
        reply = await inbox.next_msg(timeout=10)
        result = json.loads(reply.data)
        print(f"[controller] worker (decoupled, via bus) returned: {result}")
        ok = result["output"] == 42
    except Exception:
        ok = False
    stop.set(); await wt; await nc.close()
    print("PASS: distributed dispatch — task published → decoupled worker → result over the bus ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        asyncio.run(_demo())
