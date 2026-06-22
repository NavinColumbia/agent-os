-- Runs once on first cluster init (docker-entrypoint-initdb.d).
-- Durable memory + crash recovery schema for agent-os.

CREATE EXTENSION IF NOT EXISTS vector;

-- Crash recovery: latest known state per task, upserted as work progresses.
CREATE TABLE IF NOT EXISTS task_checkpoints (
    task_id    TEXT PRIMARY KEY,
    state      JSONB NOT NULL,
    seq        BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Durable semantic memory (pgvector). 384 dims = all-MiniLM-L6-v2 size (placeholder default).
CREATE TABLE IF NOT EXISTS memories (
    id         BIGSERIAL PRIMARY KEY,
    task_id    TEXT,
    content    TEXT NOT NULL,
    embedding  vector(384),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS memories_task_idx ON memories (task_id);
