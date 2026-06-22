#!/usr/bin/env python3
"""nats_roundtrip_test.py — PROVE the JetStream work-queue + controller<->worker round-trip.

Topology:
  Stream WORK (WorkQueuePolicy) over subject  work.tasks
    - WorkQueuePolicy => each task delivered to exactly ONE worker, removed after ack.
  Controller publishes a task to  work.tasks  (durable in JetStream).
  Worker  (durable pull consumer 'workers') fetches the task, does the work, and replies
          to the per-task reply subject  work.reply.<id>  (core NATS).
  Controller waits on  work.reply.<id>  and prints the round-trip.

Run: ~/projects/agent-os/.venv/bin/python scripts/nats_roundtrip_test.py
"""
import asyncio
import json
import sys

import nats
from nats.js.api import StreamConfig, RetentionPolicy, ConsumerConfig

NATS_URL = "nats://127.0.0.1:4222"
STREAM = "WORK"
TASK_SUBJECT = "work.tasks"


async def ensure_stream(js):
    cfg = StreamConfig(
        name=STREAM,
        subjects=[TASK_SUBJECT],
        retention=RetentionPolicy.WORK_QUEUE,  # work-queue semantics
        max_msgs=10000,
    )
    try:
        await js.add_stream(cfg)
        print(f"[setup] created stream {STREAM} (work-queue) on '{TASK_SUBJECT}'")
    except Exception:
        await js.update_stream(cfg)
        print(f"[setup] stream {STREAM} already present (work-queue) on '{TASK_SUBJECT}'")


async def worker(nc, js, ready: asyncio.Event, stop: asyncio.Event):
    """Durable pull consumer: fetch one task, do work, reply."""
    sub = await js.pull_subscribe(
        TASK_SUBJECT, durable="workers",
        config=ConsumerConfig(durable_name="workers", ack_wait=30),
    )
    ready.set()
    while not stop.is_set():
        try:
            msgs = await sub.fetch(1, timeout=1)
        except Exception:
            continue
        for msg in msgs:
            task = json.loads(msg.data)
            print(f"[worker] received task #{task['id']}: {task['op']}({task['a']},{task['b']})")
            result = task["a"] + task["b"]  # the "work"
            await msg.ack()  # work-queue removes it after ack
            reply = {"id": task["id"], "result": result, "worker": "worker-1"}
            await nc.publish(f"work.reply.{task['id']}", json.dumps(reply).encode())
            await nc.flush()
            print(f"[worker] replied to work.reply.{task['id']}: result={result}")


async def main():
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    await ensure_stream(js)

    ready, stop = asyncio.Event(), asyncio.Event()
    wtask = asyncio.create_task(worker(nc, js, ready, stop))
    await ready.wait()

    # Controller side: subscribe to the reply subject, then publish the task.
    task = {"id": 7, "op": "add", "a": 40, "b": 2}
    inbox = await nc.subscribe(f"work.reply.{task['id']}")
    ack = await js.publish(TASK_SUBJECT, json.dumps(task).encode())
    print(f"[controller] published task #{task['id']} -> JetStream seq={ack.seq}")

    try:
        reply_msg = await inbox.next_msg(timeout=10)
    except Exception:
        print("FAIL: no reply within 10s")
        stop.set(); await wtask; await nc.close(); sys.exit(1)

    reply = json.loads(reply_msg.data)
    print(f"[controller] got reply: {reply}")

    stop.set()
    await wtask
    await nc.close()

    ok = reply.get("id") == 7 and reply.get("result") == 42
    print("PASS: full controller->queue->worker->reply round-trip" if ok else f"FAIL: {reply}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
