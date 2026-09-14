-- 44-agent-requests.sql — durable agent->human requests that block a phase until answered.
-- An agent ask()s a question (credential / decision / do_task / info); it lands here as 'open', pushes the
-- tenant, and (optionally) posts into the controller chat thread. A human answer()s -> 'answered', which a
-- polling caller detects via is_answered() and resumes. This is the proactive, reply-resumable handoff.

CREATE TABLE IF NOT EXISTS agent_requests (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT,
    org_id      TEXT,
    thread_id   BIGINT,
    kind        TEXT,
    question    TEXT,
    status      TEXT DEFAULT 'open',
    answer      TEXT,
    correlation_id TEXT,
    execution_scope TEXT NOT NULL DEFAULT 'production'
        CHECK (execution_scope IN ('production','test','legacy')),
    created_at  TIMESTAMPTZ DEFAULT now(),
    answered_at TIMESTAMPTZ
);
ALTER TABLE agent_requests ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE agent_requests ADD COLUMN IF NOT EXISTS execution_scope TEXT NOT NULL DEFAULT 'production';
CREATE UNIQUE INDEX IF NOT EXISTS agent_requests_correlation_idx
    ON agent_requests (tenant_id, correlation_id) WHERE correlation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS agent_requests_scope_open_idx
    ON agent_requests (execution_scope, status, tenant_id, id) WHERE status='open';
