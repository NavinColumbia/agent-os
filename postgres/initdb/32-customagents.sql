-- Custom / standing agents a TENANT (their CEO) defines for themselves: e.g. a weekly "market-updates"
-- agent that runs through the SAME governed factory (governed roles only, sanitized instructions,
-- per-tenant provider key) and reports back into their notification feed. Definitions live here; each
-- execution is recorded in custom_agent_runs. Recurring agents are wired into the scheduler by id.
CREATE TABLE IF NOT EXISTS custom_agents (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    TEXT,
    name         TEXT,
    role         TEXT DEFAULT 'research-growth',
    instructions TEXT,
    trigger      TEXT DEFAULT 'manual',
    interval_s   INT,
    output       TEXT DEFAULT 'report',
    product      TEXT,
    enabled      BOOLEAN DEFAULT true,
    last_run     TIMESTAMPTZ,
    last_status  TEXT,
    created_at   TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS custom_agent_runs (
    id         BIGSERIAL PRIMARY KEY,
    agent_id   BIGINT,
    tenant_id  TEXT,
    started_at TIMESTAMPTZ DEFAULT now(),
    rc         INT,
    cost_usd   NUMERIC DEFAULT 0,
    output_ref TEXT,
    summary    TEXT
);
