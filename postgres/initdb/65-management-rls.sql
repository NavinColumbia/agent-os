-- Durable management conversations are tenant-owned, including their event and decision children.
-- Older installations created only tenant_id on the parent case; backfill and fence the full family.

CREATE TABLE IF NOT EXISTS management_cases (
    case_id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL DEFAULT '_platform', product TEXT, work_id TEXT,
    subject TEXT NOT NULL, worker TEXT, manager_role TEXT NOT NULL DEFAULT 'team-lead',
    management_level INT NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'open',
    trigger TEXT NOT NULL, state JSONB NOT NULL DEFAULT '{}', state_fingerprint TEXT NOT NULL,
    progress_seq BIGINT NOT NULL DEFAULT 0, last_progress_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_review_at TIMESTAMPTZ NOT NULL DEFAULT now(), lease_owner TEXT, lease_until TIMESTAMPTZ,
    human_request_id BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), resolved_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS management_events (
    event_id BIGSERIAL PRIMARY KEY, case_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT '_platform', event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS management_decisions (
    decision_id BIGSERIAL PRIMARY KEY, case_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT '_platform', manager_role TEXT NOT NULL,
    trigger TEXT NOT NULL, action TEXT NOT NULL, rationale TEXT,
    confidence DOUBLE PRECISION, decision JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS management_questions (
    question_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT '_platform', asker TEXT NOT NULL,
    recipient TEXT NOT NULL, question TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
    answer TEXT, reply_by TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    answered_at TIMESTAMPTZ
);

ALTER TABLE management_cases ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE management_cases ALTER COLUMN tenant_id SET DEFAULT '_platform';
UPDATE management_cases SET tenant_id='_platform' WHERE tenant_id IS NULL;
ALTER TABLE management_cases ALTER COLUMN tenant_id SET NOT NULL;

ALTER TABLE management_events ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE management_decisions ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE management_questions ADD COLUMN IF NOT EXISTS tenant_id TEXT;
UPDATE management_events x SET tenant_id=m.tenant_id FROM management_cases m
 WHERE x.case_id=m.case_id AND x.tenant_id IS DISTINCT FROM m.tenant_id;
UPDATE management_decisions x SET tenant_id=m.tenant_id FROM management_cases m
 WHERE x.case_id=m.case_id AND x.tenant_id IS DISTINCT FROM m.tenant_id;
UPDATE management_questions x SET tenant_id=m.tenant_id FROM management_cases m
 WHERE x.case_id=m.case_id AND x.tenant_id IS DISTINCT FROM m.tenant_id;
ALTER TABLE management_events ALTER COLUMN tenant_id SET DEFAULT '_platform';
ALTER TABLE management_decisions ALTER COLUMN tenant_id SET DEFAULT '_platform';
ALTER TABLE management_questions ALTER COLUMN tenant_id SET DEFAULT '_platform';
ALTER TABLE management_events ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE management_decisions ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE management_questions ALTER COLUMN tenant_id SET NOT NULL;

CREATE OR REPLACE FUNCTION management_set_child_tenant() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE parent_tenant TEXT;
BEGIN
    SELECT tenant_id INTO parent_tenant FROM management_cases WHERE case_id=NEW.case_id;
    IF parent_tenant IS NULL THEN
        RAISE EXCEPTION 'management child references missing case %', NEW.case_id;
    END IF;
    NEW.tenant_id := parent_tenant;
    RETURN NEW;
END $$;

DO $$
DECLARE tbl TEXT;
BEGIN
  FOREACH tbl IN ARRAY ARRAY['management_events','management_decisions','management_questions'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname=tbl || '_tenant_from_case') THEN
      EXECUTE format('CREATE TRIGGER %I BEFORE INSERT OR UPDATE OF case_id ON %I '
                     'FOR EACH ROW EXECUTE FUNCTION management_set_child_tenant()',
                     tbl || '_tenant_from_case', tbl);
    END IF;
  END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS management_cases_tenant_idx ON management_cases(tenant_id);
CREATE INDEX IF NOT EXISTS management_events_tenant_idx ON management_events(tenant_id);
CREATE INDEX IF NOT EXISTS management_decisions_tenant_idx ON management_decisions(tenant_id);
CREATE INDEX IF NOT EXISTS management_questions_tenant_idx ON management_questions(tenant_id);

DO $$
DECLARE tbl TEXT; seq_name TEXT;
BEGIN
  FOREACH tbl IN ARRAY ARRAY['management_cases','management_events','management_decisions','management_questions'] LOOP
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
  FOREACH seq_name IN ARRAY ARRAY['management_events_event_id_seq','management_decisions_decision_id_seq'] LOOP
    IF to_regclass(seq_name) IS NOT NULL THEN
      EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %I TO agentos_app', seq_name);
    END IF;
  END LOOP;
END $$;
