#!/usr/bin/env python3
"""memory.py — durable agent memory: episodic store, graph relations, and a reflection job
that consolidates episodic memories into semantic ones (ADR 0004 K6).

Graph = an edges table + recursive CTE over the existing pgvector `memories` table (native
Apache AGE is the future upgrade). pgvector embeddings stay available for similarity.

    memory.py demo     # record episodes, relate, traverse, consolidate — end-to-end proof
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

import psycopg

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)


def record(content, kind="episodic", task_id=None, meta=None):
    import json
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO memories (task_id, content, kind, meta) VALUES (%s,%s,%s,%s) RETURNING id",
                    (task_id, content, kind, json.dumps(meta or {})))
        mid = cur.fetchone()[0]; c.commit(); return mid


def relate(src, rel, dst):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO mem_edges (src,rel,dst) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (src, rel, dst))
        c.commit()


def traverse(start_id, max_depth=5):
    """Recursive-CTE graph walk from a memory node."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""
            WITH RECURSIVE walk(id, depth, path) AS (
                SELECT %s::bigint, 0, ARRAY[%s]::bigint[]
                UNION ALL
                SELECT e.dst, w.depth+1, w.path||e.dst
                FROM walk w JOIN mem_edges e ON e.src = w.id
                WHERE w.depth < %s AND NOT e.dst = ANY(w.path)
            )
            SELECT DISTINCT m.id, m.kind, m.content FROM walk w JOIN memories m ON m.id=w.id ORDER BY m.id
        """, (start_id, start_id, max_depth))
        return cur.fetchall()


def consolidate(task_id):
    """Reflection: fold a task's episodic memories into one semantic memory, linked by edges.
    (Stub summarizer = concatenation; swap in a local LLM later — the pipeline is what matters.)"""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT id, content FROM memories WHERE task_id=%s AND kind='episodic' ORDER BY id", (task_id,))
        eps = cur.fetchall()
    if not eps:
        return None
    summary = f"Consolidated learning for {task_id}: " + "; ".join(content for _, content in eps)
    sem_id = record(summary, kind="semantic", task_id=task_id, meta={"from_episodes": [e[0] for e in eps]})
    for ep_id, _ in eps:
        relate(sem_id, "derived_from", ep_id)
    return sem_id


def _demo():
    import os
    tid = f"mem-demo-{os.urandom(3).hex()}"
    e1 = record("SendGrid v2 returned 410 (deprecated)", task_id=tid)
    e2 = record("Migrated to SendGrid v3 /mail/send", task_id=tid)
    e3 = record("v3 needs personalizations[] payload shape", task_id=tid)
    relate(e1, "relates_to", e2); relate(e2, "relates_to", e3)
    print(f"recorded 3 episodic memories for {tid}: {e1},{e2},{e3}")
    print("graph traverse from e1:")
    for row in traverse(e1):
        print("   ", row)
    sem = consolidate(tid)
    print(f"reflection -> semantic memory id={sem}")
    print("semantic node + its derived_from edges (traverse):")
    for row in traverse(sem):
        print("   ", row)
    ok = sem is not None and len(traverse(sem)) == 4  # semantic + 3 episodes
    print("PASS: episodic->semantic consolidation + graph traversal ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _demo()
