-- Management semantic generations and tenant-owned task delivery.
--
-- Legacy queue rows predate tenant ownership. They are deliberately assigned to
-- the operator-only _platform tenant; tenant APIs never infer ownership from them.

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS tenant_id TEXT;
UPDATE tasks SET tenant_id='_platform'
 WHERE tenant_id IS NULL OR tenant_id='' OR tenant_id='unknown';
ALTER TABLE tasks ALTER COLUMN tenant_id SET DEFAULT '_platform';
ALTER TABLE tasks ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

DROP INDEX IF EXISTS tasks_queue_idx;
DROP INDEX IF EXISTS tasks_active_lease_idx;
DROP INDEX IF EXISTS tasks_dead_idx;
CREATE INDEX IF NOT EXISTS tasks_tenant_queue_idx
    ON tasks(tenant_id,assignee,status,priority,id);
CREATE INDEX IF NOT EXISTS tasks_tenant_dispatch_idx
    ON tasks(tenant_id,status,not_before,priority,created_at,id);
CREATE INDEX IF NOT EXISTS tasks_tenant_active_lease_idx
    ON tasks(tenant_id,status,locked_at) WHERE status='active';
CREATE INDEX IF NOT EXISTS tasks_tenant_dead_idx
    ON tasks(tenant_id,status,id) WHERE status='dead';
CREATE UNIQUE INDEX IF NOT EXISTS tasks_tenant_idempotency_uidx
    ON tasks(tenant_id,idempotency_key) WHERE idempotency_key IS NOT NULL;

-- Hiring and its eventual task/message hand-off share the management dispatch
-- key. This lets a process retry after any crash boundary without manufacturing
-- another hire, task, or mailbox delivery.
ALTER TABLE hire_requests ADD COLUMN IF NOT EXISTS tenant_id TEXT;
UPDATE hire_requests SET tenant_id='_platform'
 WHERE tenant_id IS NULL OR tenant_id='' OR tenant_id='unknown';
ALTER TABLE hire_requests ALTER COLUMN tenant_id SET DEFAULT '_platform';
ALTER TABLE hire_requests ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE hire_requests ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE hire_requests ADD COLUMN IF NOT EXISTS priority INT NOT NULL DEFAULT 5;
ALTER TABLE hire_requests ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE hire_requests ADD COLUMN IF NOT EXISTS fulfilled_agent_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS hire_requests_tenant_idempotency_uidx
    ON hire_requests(tenant_id,idempotency_key) WHERE idempotency_key IS NOT NULL;

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS conversations_tenant_idempotency_uidx
    ON conversations(tenant_id,idempotency_key) WHERE idempotency_key IS NOT NULL;

-- `state` remains the latest complete evidence document for compatibility.
-- Only semantic_state participates in generation/fingerprint changes; observation
-- is replaceable telemetry such as age/silence and never wakes management.
ALTER TABLE management_cases ADD COLUMN IF NOT EXISTS semantic_state JSONB NOT NULL DEFAULT '{}';
ALTER TABLE management_cases ADD COLUMN IF NOT EXISTS observation JSONB NOT NULL DEFAULT '{}';
ALTER TABLE management_cases ADD COLUMN IF NOT EXISTS semantic_generation BIGINT NOT NULL DEFAULT 1;
ALTER TABLE management_cases ADD COLUMN IF NOT EXISTS reviewed_generation BIGINT NOT NULL DEFAULT 0;
ALTER TABLE management_cases ADD COLUMN IF NOT EXISTS last_event_generation BIGINT NOT NULL DEFAULT 0;
ALTER TABLE management_cases ADD COLUMN IF NOT EXISTS last_observed_at TIMESTAMPTZ NOT NULL DEFAULT now();
UPDATE management_cases
   SET semantic_state=state,
       last_event_generation=GREATEST(last_event_generation,semantic_generation),
       reviewed_generation=CASE WHEN EXISTS (
           SELECT 1 FROM management_decisions d WHERE d.case_id=management_cases.case_id
       ) THEN semantic_generation ELSE reviewed_generation END;

ALTER TABLE management_events ADD COLUMN IF NOT EXISTS semantic_generation BIGINT;
ALTER TABLE management_decisions ADD COLUMN IF NOT EXISTS semantic_generation BIGINT;
ALTER TABLE management_decisions ADD COLUMN IF NOT EXISTS target TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS management_decisions_case_generation_uidx
    ON management_decisions(case_id,semantic_generation)
    WHERE semantic_generation IS NOT NULL;

-- The original global dedupe constraint allowed the same logical key in one
-- tenant to overwrite another tenant's case. New cases are tenant-qualified.
ALTER TABLE management_cases DROP CONSTRAINT IF EXISTS management_cases_dedupe_key_key;
CREATE UNIQUE INDEX IF NOT EXISTS management_cases_tenant_dedupe_uidx
    ON management_cases(tenant_id,dedupe_key);
CREATE INDEX IF NOT EXISTS management_cases_tenant_due_generation_idx
    ON management_cases(tenant_id,status,reviewed_generation,semantic_generation,next_review_at);

CREATE TABLE IF NOT EXISTS management_dispatches (
    dispatch_key TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    semantic_generation BIGINT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(case_id,semantic_generation,action,target)
);
CREATE INDEX IF NOT EXISTS management_dispatches_tenant_case_idx
    ON management_dispatches(tenant_id,case_id,semantic_generation);

CREATE TABLE IF NOT EXISTS management_duty_cursors (
    tenant_id TEXT NOT NULL DEFAULT '_platform',
    source TEXT NOT NULL,
    last_key TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,source)
);
CREATE INDEX IF NOT EXISTS management_duty_cursors_tenant_idx
    ON management_duty_cursors(tenant_id,source);

CREATE TABLE IF NOT EXISTS task_dispatch_cursor (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK(singleton),
    tenant_id TEXT NOT NULL DEFAULT '_platform',
    last_tenant_id TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS task_dispatch_cursor_tenant_idx
    ON task_dispatch_cursor(tenant_id,singleton);
INSERT INTO task_dispatch_cursor(singleton,tenant_id) VALUES (true,'_platform')
ON CONFLICT (singleton) DO NOTHING;

DO $$
DECLARE tbl TEXT;
BEGIN
  FOREACH tbl IN ARRAY ARRAY[
    'tasks','hire_requests','management_dispatches','management_duty_cursors','task_dispatch_cursor'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tbl);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', tbl);
    EXECUTE format('REVOKE ALL ON TABLE %I FROM agentos_app', tbl);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I TO agentos_app', tbl);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', tbl || '_tenant_guc', tbl);
    EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO agentos_app '
                   'USING (tenant_id=current_setting(''app.tenant_id'',true)) '
                   'WITH CHECK (tenant_id=current_setting(''app.tenant_id'',true))',
                   tbl || '_tenant_guc', tbl);
  END LOOP;
  IF to_regclass('tasks_id_seq') IS NOT NULL THEN
    GRANT USAGE, SELECT ON SEQUENCE tasks_id_seq TO agentos_app;
  END IF;
  IF to_regclass('hire_requests_id_seq') IS NOT NULL THEN
    GRANT USAGE, SELECT ON SEQUENCE hire_requests_id_seq TO agentos_app;
  END IF;
END $$;
