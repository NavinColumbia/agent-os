-- Crash-safe controller acknowledgement for answered agentic decisions.
-- Resolving the human request and applying its side effect are separate
-- transactions/process turns.  Keep resolved answers replayable until the
-- controller durably acknowledges successful application.

DO $$
DECLARE acknowledgement_already_installed boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='public' AND table_name='agentic_decisions'
           AND column_name='applied_at'
    ) INTO acknowledgement_already_installed;

    ALTER TABLE agentic_decisions ADD COLUMN IF NOT EXISTS applied_at TIMESTAMPTZ;
    ALTER TABLE agentic_decisions ADD COLUMN IF NOT EXISTS apply_attempts INT NOT NULL DEFAULT 0;
    ALTER TABLE agentic_decisions ADD COLUMN IF NOT EXISTS apply_error TEXT;

    -- Existing resolved rows predate this acknowledgement protocol and have
    -- already passed through the old controller path. Treat them as applied
    -- only on the first installation so an idempotent migration rerun cannot
    -- acknowledge a new, legitimately pending application.
    IF NOT acknowledgement_already_installed THEN
        UPDATE agentic_decisions
           SET applied_at=COALESCE(applied_at,resolved_at,updated_at,now())
         WHERE status='resolved' AND applied_at IS NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS agentic_decisions_pending_apply_idx
    ON agentic_decisions (tenant_id, id)
    WHERE status='resolved' AND applied_at IS NULL;
