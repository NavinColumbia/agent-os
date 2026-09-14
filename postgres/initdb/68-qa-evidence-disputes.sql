-- Durable, lease-fenced adjudication of disputed QA evidence.
-- Inputs are immutable stable internal-review records. A terminal disposition is senior-reviewed and may
-- cite only evidence mechanically bound to the sealed finding-time provenance manifest.

CREATE TABLE IF NOT EXISTS qa_evidence_disputes (
    case_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    review_id TEXT NOT NULL,
    thread_id BIGINT,
    run_id BIGINT,
    coordinator_actor_id BIGINT,
    authority_decision_id BIGINT,
    work_ref TEXT NOT NULL,
    repo TEXT NOT NULL,
    internal_review JSONB NOT NULL,
    internal_review_digest TEXT NOT NULL,
    state JSONB NOT NULL DEFAULT '{}',
    state_digest TEXT NOT NULL,
    state_generation BIGINT NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','leased','manager_review','resolved','external_authority')),
    current_tier INT NOT NULL DEFAULT 0 CHECK (current_tier BETWEEN 0 AND 2),
    lease_owner TEXT,
    lease_token TEXT,
    lease_until TIMESTAMPTZ,
    attempts INT NOT NULL DEFAULT 0,
    next_review_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    UNIQUE (tenant_id, review_id)
);
CREATE INDEX IF NOT EXISTS qa_evidence_disputes_claim_idx
    ON qa_evidence_disputes (next_review_at, created_at)
    WHERE status IN ('pending','leased');
CREATE INDEX IF NOT EXISTS qa_evidence_disputes_tenant_idx
    ON qa_evidence_disputes (tenant_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS qa_evidence_dispute_reviews (
    id BIGSERIAL PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES qa_evidence_disputes(case_id),
    tenant_id TEXT NOT NULL,
    state_digest TEXT NOT NULL,
    tier INT NOT NULL CHECK (tier BETWEEN 0 AND 2),
    actor_role TEXT NOT NULL,
    review JSONB NOT NULL,
    evidence_digest TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (case_id, state_digest, tier)
);
CREATE INDEX IF NOT EXISTS qa_evidence_dispute_reviews_tenant_idx
    ON qa_evidence_dispute_reviews (tenant_id, case_id, state_digest, tier);

CREATE OR REPLACE FUNCTION qa_dispute_immutable_input() RETURNS trigger AS $$
BEGIN
  IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
     OR NEW.review_id IS DISTINCT FROM OLD.review_id
     OR NEW.repo IS DISTINCT FROM OLD.repo
     OR NEW.internal_review IS DISTINCT FROM OLD.internal_review
     OR NEW.internal_review_digest IS DISTINCT FROM OLD.internal_review_digest THEN
    RAISE EXCEPTION 'qa evidence dispute input is immutable';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS qa_dispute_immutable_input_guard ON qa_evidence_disputes;
CREATE TRIGGER qa_dispute_immutable_input_guard BEFORE UPDATE ON qa_evidence_disputes
  FOR EACH ROW EXECUTE FUNCTION qa_dispute_immutable_input();

DO $$
DECLARE tbl TEXT; seq_name TEXT;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentos_app') THEN
    FOREACH tbl IN ARRAY ARRAY['qa_evidence_disputes','qa_evidence_dispute_reviews'] LOOP
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
    SELECT pg_get_serial_sequence('public.qa_evidence_dispute_reviews','id') INTO seq_name;
    IF seq_name IS NOT NULL THEN
      EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %s TO agentos_app', seq_name);
    END IF;
  END IF;
END $$;
