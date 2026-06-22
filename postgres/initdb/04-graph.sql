-- Graph memory over plain Postgres (ADR 0004 K6, safe variant). Native Apache AGE is the future
-- upgrade (needs a custom image + careful pgdata migration — tracked separately). An edges table +
-- recursive CTE gives entity/relation memory alongside the existing pgvector `memories` table now.
ALTER TABLE memories ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'episodic';  -- episodic|semantic
ALTER TABLE memories ADD COLUMN IF NOT EXISTS meta JSONB NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS mem_edges (
    src  BIGINT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    rel  TEXT   NOT NULL,         -- e.g. derived_from, relates_to, about
    dst  BIGINT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    PRIMARY KEY (src, rel, dst)
);
CREATE INDEX IF NOT EXISTS mem_edges_dst_idx ON mem_edges (dst);
