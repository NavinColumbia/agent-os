-- Keep release/selftest questions out of the production CEO-reminder lane.
-- Rows written before execution scope existed are not assumed to be real work.  A linked controller thread
-- is the only durable evidence that lets an old request inherit production/test scope; everything else is
-- retained as legacy and remains visible to explicit operator/tenant history reads.

ALTER TABLE agent_requests ADD COLUMN IF NOT EXISTS execution_scope TEXT;

UPDATE agent_requests ar
   SET execution_scope=cs.execution_scope
  FROM controller_state cs
 WHERE ar.execution_scope IS NULL
   AND ar.thread_id=cs.thread_id;

UPDATE agent_requests SET execution_scope='legacy' WHERE execution_scope IS NULL;

ALTER TABLE agent_requests ALTER COLUMN execution_scope SET DEFAULT 'production';
ALTER TABLE agent_requests ALTER COLUMN execution_scope SET NOT NULL;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname='agent_requests_execution_scope_check') THEN
    ALTER TABLE agent_requests ADD CONSTRAINT agent_requests_execution_scope_check
      CHECK (execution_scope IN ('production','test','legacy')) NOT VALID;
  END IF;
END $$;
ALTER TABLE agent_requests VALIDATE CONSTRAINT agent_requests_execution_scope_check;

CREATE INDEX IF NOT EXISTS agent_requests_scope_open_idx
  ON agent_requests(execution_scope,status,tenant_id,id) WHERE status='open';
