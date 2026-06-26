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
    created_at  TIMESTAMPTZ DEFAULT now(),
    answered_at TIMESTAMPTZ
);
