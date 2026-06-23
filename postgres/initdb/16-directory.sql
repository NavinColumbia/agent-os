-- Live agent directory (presence + work registry). The static org is the role manifests; THIS is the
-- dynamic layer: who is active right now, what product/task they're on, and which resources (path globs)
-- they hold. Agents query it to discover + DIRECTLY address each other (no hierarchy routing, no sockets),
-- and overlapping resource claims surface as conflicts to coordinate on.
CREATE TABLE IF NOT EXISTS directory (
    agent_id   TEXT PRIMARY KEY,         -- e.g. 'builder@pomodoro'
    role       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',   -- active | idle
    product    TEXT,
    task       TEXT,
    resources  TEXT[] NOT NULL DEFAULT '{}',     -- path globs this agent is working in
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS directory_product_idx ON directory (product);
