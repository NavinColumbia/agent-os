-- Crash-recoverable recurring scheduler executions and process birth identity.
ALTER TABLE IF EXISTS controller_jobs ADD COLUMN IF NOT EXISTS worker_start_ticks BIGINT;
ALTER TABLE IF EXISTS controller_jobs ADD COLUMN IF NOT EXISTS worker_boot_id TEXT;

-- Agent capacity is a host-wide fence, not tenant-owned application data.  Define the runtime-created table
-- canonically in migrations and keep it inaccessible to the tenant app role.
CREATE TABLE IF NOT EXISTS agent_slots (
    slot_id INT PRIMARY KEY,
    holder TEXT,
    acquired_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    owner_token TEXT
);
ALTER TABLE agent_slots ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;
ALTER TABLE agent_slots ADD COLUMN IF NOT EXISTS owner_token TEXT;

CREATE TABLE IF NOT EXISTS scheduler_claims (
    claim_token TEXT PRIMARY KEY,
    name TEXT NOT NULL REFERENCES schedules(name) ON DELETE CASCADE,
    command TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','executed','nonzero','timeout','error','rejected','expired')),
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_until TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    rc INT,
    detail TEXT,
    CHECK ((status='running') = (finished_at IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS scheduler_one_running_claim_idx
    ON scheduler_claims (name) WHERE status='running';
CREATE INDEX IF NOT EXISTS scheduler_claims_lease_idx
    ON scheduler_claims (lease_until) WHERE status='running';

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='scheduler_claims_status_check'
                   AND conrelid='scheduler_claims'::regclass) THEN
        ALTER TABLE scheduler_claims ADD CONSTRAINT scheduler_claims_status_check
            CHECK (status IN ('running','executed','nonzero','timeout','error','rejected','expired'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='scheduler_claims_terminal_check'
                   AND conrelid='scheduler_claims'::regclass) THEN
        ALTER TABLE scheduler_claims ADD CONSTRAINT scheduler_claims_terminal_check
            CHECK ((status='running') = (finished_at IS NULL));
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentos_app') THEN
        REVOKE ALL ON TABLE agent_slots FROM agentos_app;
        REVOKE ALL ON TABLE scheduler_claims FROM agentos_app;
    END IF;
END $$;
