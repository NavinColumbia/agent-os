-- Alert SLA routing must distinguish production incidents from selftest history.
-- Existing rows predate this provenance boundary, so preserve them as legacy and never infer production.
ALTER TABLE agent_alerts ADD COLUMN IF NOT EXISTS execution_scope TEXT;
UPDATE agent_alerts SET execution_scope='legacy' WHERE execution_scope IS NULL;
ALTER TABLE agent_alerts ALTER COLUMN execution_scope SET DEFAULT 'production';
ALTER TABLE agent_alerts ALTER COLUMN execution_scope SET NOT NULL;

DO $$ BEGIN
  ALTER TABLE agent_alerts ADD CONSTRAINT agent_alerts_execution_scope_check
    CHECK (execution_scope IN ('production','test','legacy'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DROP INDEX IF EXISTS agent_alerts_open_sig;
CREATE UNIQUE INDEX IF NOT EXISTS agent_alerts_open_scope_sig
  ON agent_alerts(execution_scope,signature) WHERE status='open';
CREATE INDEX IF NOT EXISTS agent_alerts_scope_sla_idx
  ON agent_alerts(execution_scope,severity,created_at,id) WHERE status='open';
