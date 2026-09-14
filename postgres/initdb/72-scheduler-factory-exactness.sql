-- Occurrence-exact scheduler terminalization and factory resume ownership.
--
-- Both tables are global operational control-plane state. Tenant application
-- sessions have no direct authority over process execution or recovery claims.

ALTER TABLE scheduler_claims
    ADD COLUMN IF NOT EXISTS execution_started_at TIMESTAMPTZ;
ALTER TABLE scheduler_claims
    ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;

ALTER TABLE scheduler_runs
    ADD COLUMN IF NOT EXISTS occurrence_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS scheduler_runs_occurrence_uidx
    ON scheduler_runs(occurrence_id) WHERE occurrence_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS factory_resume_claims (
    claim_token TEXT PRIMARY KEY,
    product TEXT NOT NULL,
    generation_ts TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'claimed',
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_until TIMESTAMPTZ NOT NULL,
    worker_pid BIGINT,
    worker_start_ticks BIGINT,
    worker_boot_id TEXT,
    finished_at TIMESTAMPTZ,
    detail TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS factory_resume_one_active_product_idx
    ON factory_resume_claims(product) WHERE status IN ('claimed','running');
CREATE INDEX IF NOT EXISTS factory_resume_claims_lease_idx
    ON factory_resume_claims(lease_until) WHERE status IN ('claimed','running');

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname='factory_resume_claims_status_check'
                      AND conrelid='factory_resume_claims'::regclass) THEN
        ALTER TABLE factory_resume_claims
            ADD CONSTRAINT factory_resume_claims_status_check
            CHECK (status IN ('claimed','running','finished','crashed','released','degraded'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname='factory_resume_claims_terminal_check'
                      AND conrelid='factory_resume_claims'::regclass) THEN
        ALTER TABLE factory_resume_claims
            ADD CONSTRAINT factory_resume_claims_terminal_check
            CHECK ((status IN ('claimed','running')) = (finished_at IS NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname='factory_resume_claims_identity_check'
                      AND conrelid='factory_resume_claims'::regclass) THEN
        ALTER TABLE factory_resume_claims
            ADD CONSTRAINT factory_resume_claims_identity_check
            CHECK ((status <> 'running') OR
                   (worker_pid IS NOT NULL AND worker_start_ticks IS NOT NULL
                    AND worker_boot_id IS NOT NULL));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS factory_resume_sweep_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    cursor_generation_ts TIMESTAMPTZ,
    cursor_run_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO factory_resume_sweep_state(singleton)
VALUES (true) ON CONFLICT (singleton) DO NOTHING;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentos_app') THEN
        REVOKE ALL ON TABLE factory_resume_claims FROM agentos_app;
        REVOKE ALL ON TABLE factory_resume_sweep_state FROM agentos_app;
    END IF;
END $$;
