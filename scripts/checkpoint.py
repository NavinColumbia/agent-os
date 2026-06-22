#!/usr/bin/env python3
"""checkpoint.py — durable task-state save/restore for crash recovery (Postgres).

API:
    from checkpoint import save, restore
    save("task-42", {"step": 3, "done": ["a", "b"]})
    state = restore("task-42")   # -> dict or None

CLI:
    checkpoint.py save  <task_id> '<json-state>'
    checkpoint.py restore <task_id>
    checkpoint.py list

Reads DATABASE_URL from env or ~/projects/agent-os/.env.local.
Each save bumps a monotonic seq + updated_at so the latest write always wins.
Run with the agent-os venv python: ~/projects/agent-os/.venv/bin/python
"""
import json
import os
import sys
from pathlib import Path

import psycopg

ENV_LOCAL = Path.home() / "projects" / "agent-os" / ".env.local"


def _db_url():
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    if ENV_LOCAL.exists():
        for line in ENV_LOCAL.read_text().splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL=") and "=" in line:
                return line.split("=", 1)[1].strip()
    sys.exit("ERROR: DATABASE_URL not set (env or ~/projects/agent-os/.env.local)")


def save(task_id, state):
    """Upsert the latest state for task_id. Returns the new seq."""
    with psycopg.connect(_db_url()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO task_checkpoints (task_id, state, seq, updated_at)
            VALUES (%s, %s, 1, now())
            ON CONFLICT (task_id) DO UPDATE
              SET state = EXCLUDED.state,
                  seq   = task_checkpoints.seq + 1,
                  updated_at = now()
            RETURNING seq;
            """,
            (task_id, json.dumps(state)),
        )
        seq = cur.fetchone()[0]
        conn.commit()
        return seq


def restore(task_id):
    """Return the saved state dict for task_id, or None if absent."""
    with psycopg.connect(_db_url()) as conn, conn.cursor() as cur:
        cur.execute("SELECT state FROM task_checkpoints WHERE task_id = %s", (task_id,))
        row = cur.fetchone()
        return row[0] if row else None


def list_tasks():
    with psycopg.connect(_db_url()) as conn, conn.cursor() as cur:
        cur.execute("SELECT task_id, seq, updated_at FROM task_checkpoints ORDER BY updated_at DESC")
        return cur.fetchall()


def main(argv):
    if not argv:
        sys.exit("usage: checkpoint.py save <task_id> '<json>' | restore <task_id> | list")
    cmd = argv[0]
    if cmd == "save":
        task_id, state_json = argv[1], argv[2]
        seq = save(task_id, json.loads(state_json))
        print(f"saved task={task_id} seq={seq}")
    elif cmd == "restore":
        state = restore(argv[1])
        if state is None:
            print(f"NO CHECKPOINT for task={argv[1]}")
            sys.exit(3)
        print(json.dumps(state))
    elif cmd == "list":
        for task_id, seq, ts in list_tasks():
            print(f"{task_id}\tseq={seq}\t{ts}")
    else:
        sys.exit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main(sys.argv[1:])
