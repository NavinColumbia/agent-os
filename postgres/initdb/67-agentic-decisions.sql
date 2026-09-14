-- Durable worker -> manager -> senior-manager product decisions.
CREATE TABLE IF NOT EXISTS agentic_decisions (
    id BIGSERIAL PRIMARY KEY,
    correlation_id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    thread_id BIGINT,
    work_ref TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    input JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'reviewing',
    current_tier INT NOT NULL DEFAULT 0,
    outcome JSONB,
    authority_decision_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS agentic_decisions_attention_idx
    ON agentic_decisions(status,updated_at) WHERE status IN ('reviewing','human_wait');
CREATE INDEX IF NOT EXISTS agentic_decisions_tenant_idx
    ON agentic_decisions(tenant_id,id DESC);
CREATE TABLE IF NOT EXISTS agentic_decision_reviews (
    id BIGSERIAL PRIMARY KEY,
    decision_id BIGINT NOT NULL,
    tenant_id TEXT NOT NULL,
    tier INT NOT NULL,
    actor_role TEXT NOT NULL,
    decision JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(decision_id,tier)
);
CREATE INDEX IF NOT EXISTS agentic_decision_reviews_tenant_idx
    ON agentic_decision_reviews(tenant_id,decision_id,tier);

DO $$
DECLARE tbl TEXT; seq_name TEXT;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentos_app') THEN
    FOREACH tbl IN ARRAY ARRAY['agentic_decisions','agentic_decision_reviews'] LOOP
      EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tbl);
      EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', tbl);
      EXECUTE format('REVOKE ALL ON TABLE public.%I FROM agentos_app', tbl);
      EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO agentos_app', tbl);
      EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_tenant_guc', tbl);
      EXECUTE format('CREATE POLICY %I ON public.%I FOR ALL TO agentos_app '
                     'USING (tenant_id=current_setting(''app.tenant_id'',true)) '
                     'WITH CHECK (tenant_id=current_setting(''app.tenant_id'',true))',
                     tbl || '_tenant_guc', tbl);
    END LOOP;
    FOREACH seq_name IN ARRAY ARRAY['agentic_decisions_id_seq','agentic_decision_reviews_id_seq'] LOOP
      IF to_regclass('public.' || seq_name) IS NOT NULL THEN
        EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE public.%I TO agentos_app', seq_name);
      END IF;
    END LOOP;
  END IF;
END $$;
