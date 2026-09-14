-- 45-askuser.sql — "ask the user to do hard things mid-loop".
-- A running agent loop can pause, post a question to the controller chat, and resume once answered.
-- This is the local fallback table; askuser.py also tries to delegate to agent_request.py when present.
CREATE TABLE IF NOT EXISTS ask_user_requests (
    id         BIGSERIAL PRIMARY KEY,
    tenant_id  TEXT,
    thread_id  BIGINT,
    product    TEXT,
    question   TEXT,
    status     TEXT DEFAULT 'open',     -- open | answered
    answer     TEXT,
    agent_request_id BIGINT,
    created_at TIMESTAMPTZ DEFAULT now()
);
