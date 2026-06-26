-- 46-alerts.sql — monitoring-agent → agent alert routing (deduped, owned, escalatable).
-- A monitoring agent raises an alert; it is routed to an active agent of target_role (an OWNER)
-- through the governed fabric (orchestrate.request_collaborator), deduped while still open.
CREATE TABLE IF NOT EXISTS agent_alerts (
    id          BIGSERIAL PRIMARY KEY,
    signature   TEXT,
    source      TEXT,
    target_role TEXT,
    severity    TEXT DEFAULT 'warn',
    body        TEXT,
    status      TEXT DEFAULT 'open',     -- open | resolved
    owner       TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    resolved_at TIMESTAMPTZ
);
-- Dedup: at most one OPEN alert per signature.
CREATE UNIQUE INDEX IF NOT EXISTS agent_alerts_open_sig
    ON agent_alerts (signature) WHERE status = 'open';
