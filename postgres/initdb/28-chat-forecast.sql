-- Orchestrator chat threads + budget-forecast alert state.
-- chat_threads/chat_messages back the conversational "Direct your fleet" surface (orchestrator.py) — the
-- CEO describes an idea, the orchestrator asks clarifying questions, then proposes a governed build.
-- budget_alert_state makes the pre-emptive budget alerts (forecast.py) idempotent per threshold.
CREATE TABLE IF NOT EXISTS chat_threads (
    id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id BIGSERIAL PRIMARY KEY, thread_id BIGINT NOT NULL, tenant_id TEXT NOT NULL,
    role TEXT NOT NULL, content TEXT NOT NULL, meta JSONB DEFAULT '{}', ts TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_messages_thread_idx ON chat_messages (tenant_id, thread_id, id);
CREATE TABLE IF NOT EXISTS budget_alert_state (
    tenant_id TEXT NOT NULL, threshold INT NOT NULL, alerted_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (tenant_id, threshold)
);
