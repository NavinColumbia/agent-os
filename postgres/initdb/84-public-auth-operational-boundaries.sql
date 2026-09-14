-- Public-readiness closure for tables introduced after the original RLS rollout.
--
-- Browser sessions are tenant-owned even though the pre-authentication resolver must
-- locate a session before it knows the tenant.  Normal tenant-role access therefore
-- gets the same transaction-local GUC boundary as the rest of the application; the
-- deliberately small auth resolver continues to use the database-owner boundary.
-- The other three tables are global coordination/security queues, not customer rows,
-- and must never become reachable through the tenant application role.

CREATE TABLE IF NOT EXISTS auth_sessions (
  token_hash TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  email TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS auth_sessions_tenant_idx ON auth_sessions(tenant_id);

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentos_app') THEN
    RAISE EXCEPTION 'agentos_app role is missing; apply migration 56 first';
  END IF;

  ALTER TABLE public.auth_sessions ENABLE ROW LEVEL SECURITY;
  ALTER TABLE public.auth_sessions FORCE ROW LEVEL SECURITY;
  REVOKE ALL ON TABLE public.auth_sessions FROM PUBLIC;
  REVOKE ALL ON TABLE public.auth_sessions FROM agentos_app;
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.auth_sessions TO agentos_app;

  DROP POLICY IF EXISTS auth_sessions_tenant_guc ON public.auth_sessions;
  CREATE POLICY auth_sessions_tenant_guc ON public.auth_sessions
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
END $$;

DO $$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'app_spend_reservations',
    'auth_rate_limits',
    'qa_evidence_encoding_jobs'
  ]
  LOOP
    IF to_regclass('public.' || table_name) IS NOT NULL THEN
      EXECUTE format('REVOKE ALL ON TABLE public.%I FROM PUBLIC', table_name);
      EXECUTE format('REVOKE ALL ON TABLE public.%I FROM agentos_app', table_name);
    END IF;
  END LOOP;

  IF to_regclass('public.qa_evidence_encoding_jobs_id_seq') IS NOT NULL THEN
    REVOKE ALL ON SEQUENCE public.qa_evidence_encoding_jobs_id_seq FROM PUBLIC;
    REVOKE ALL ON SEQUENCE public.qa_evidence_encoding_jobs_id_seq FROM agentos_app;
  END IF;
END $$;
