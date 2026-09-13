-- Crash-recoverable, horizontally claimable delivery for arbitrary graph actions.
-- action IDs are deterministic within a run, so tenant_id belongs in the key:
-- two tenants are allowed to choose the same run ID without colliding.

ALTER TABLE aos_v2_workflow_actions
    ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS available_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS lease_owner text,
    ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS completed_at timestamptz,
    ADD COLUMN IF NOT EXISTS result jsonb,
    ADD COLUMN IF NOT EXISTS last_error jsonb;

DO $$
DECLARE
    primary_name text;
BEGIN
    SELECT conname INTO primary_name
    FROM pg_constraint
    WHERE conrelid = 'public.aos_v2_workflow_actions'::regclass
      AND contype = 'p';
    IF primary_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE public.aos_v2_workflow_actions DROP CONSTRAINT %I', primary_name);
    END IF;
    ALTER TABLE public.aos_v2_workflow_actions
        ADD PRIMARY KEY (tenant_id, action_id);
END $$;

DROP INDEX IF EXISTS aos_v2_workflow_actions_pending_idx;
CREATE INDEX IF NOT EXISTS aos_v2_workflow_actions_recoverable_idx
    ON aos_v2_workflow_actions (tenant_id, status, available_at, created_at, position)
    WHERE status IN ('pending', 'executing');
