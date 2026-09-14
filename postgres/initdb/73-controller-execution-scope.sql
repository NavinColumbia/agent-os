-- Test and diagnostic controller work must never be discoverable by production
-- daemons.  The discriminator lives in the durable queue itself so isolation
-- survives process boundaries, imports, and concurrent always-on services.

ALTER TABLE controller_state
  ADD COLUMN IF NOT EXISTS execution_scope TEXT NOT NULL DEFAULT 'production';
ALTER TABLE controller_jobs
  ADD COLUMN IF NOT EXISTS execution_scope TEXT NOT NULL DEFAULT 'production';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'controller_state_execution_scope_check'
  ) THEN
    ALTER TABLE controller_state
      ADD CONSTRAINT controller_state_execution_scope_check
      CHECK (execution_scope IN ('production', 'test')) NOT VALID;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'controller_jobs_execution_scope_check'
  ) THEN
    ALTER TABLE controller_jobs
      ADD CONSTRAINT controller_jobs_execution_scope_check
      CHECK (execution_scope IN ('production', 'test')) NOT VALID;
  END IF;
END $$;

ALTER TABLE controller_state VALIDATE CONSTRAINT controller_state_execution_scope_check;
ALTER TABLE controller_jobs VALIDATE CONSTRAINT controller_jobs_execution_scope_check;

CREATE INDEX IF NOT EXISTS controller_state_execution_queue_idx
  ON controller_state(execution_scope, updated_at, thread_id)
  WHERE awaiting IS NULL AND phase <> 'DELIVER';
CREATE INDEX IF NOT EXISTS controller_jobs_execution_active_idx
  ON controller_jobs(execution_scope, started_at, id)
  WHERE status IN ('running', 'pending');
