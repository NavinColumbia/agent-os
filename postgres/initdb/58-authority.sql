-- Typed, durable delegation and escalation spine.
-- Runtime creation in scripts/authority.py is kept identical for upgrades.
CREATE TABLE IF NOT EXISTS authority_envelopes (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'company',
    revision INT NOT NULL DEFAULT 1,
    policy JSONB NOT NULL,
    active BOOLEAN NOT NULL DEFAULT true,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, scope, revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS authority_envelopes_active_idx
    ON authority_envelopes (tenant_id, scope) WHERE active;

CREATE TABLE IF NOT EXISTS authority_decisions (
    id BIGSERIAL PRIMARY KEY,
    correlation_id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    org_id TEXT,
    thread_id BIGINT,
    work_ref TEXT NOT NULL,
    kind TEXT NOT NULL,
    proposal JSONB NOT NULL DEFAULT '{}',
    disposition TEXT NOT NULL,
    status TEXT NOT NULL,
    owner_role TEXT,
    envelope_id BIGINT,
    envelope_revision INT,
    agent_request_id BIGINT,
    rationale TEXT,
    review_due_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS authority_decisions_review_idx
    ON authority_decisions (review_due_at, id)
    WHERE status IN ('manager_review','human_required');
CREATE INDEX IF NOT EXISTS authority_decisions_tenant_idx
    ON authority_decisions (tenant_id, status, id DESC);

CREATE TABLE IF NOT EXISTS authority_reviews (
    id BIGSERIAL PRIMARY KEY,
    decision_id BIGINT NOT NULL,
    tenant_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    trigger TEXT NOT NULL,
    action TEXT NOT NULL,
    rationale TEXT,
    state JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS authority_reviews_decision_idx
    ON authority_reviews (decision_id, id);
CREATE INDEX IF NOT EXISTS authority_reviews_tenant_idx
    ON authority_reviews (tenant_id, id DESC);

-- 56-rls-policies.sql runs before this additive migration on a fresh install,
-- so apply the same reviewed tenant-GUC policy to these later tables here.
DO $$
DECLARE
    tbl text;
    seq_name text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentos_app') THEN
        FOREACH tbl IN ARRAY ARRAY['authority_envelopes','authority_decisions','authority_reviews']
        LOOP
            EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tbl);
            EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', tbl);
            EXECUTE format('REVOKE ALL ON TABLE public.%I FROM agentos_app', tbl);
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO agentos_app', tbl);
            SELECT pg_get_serial_sequence(format('public.%I', tbl), 'id') INTO seq_name;
            IF seq_name IS NOT NULL THEN
                EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %s TO agentos_app', seq_name);
            END IF;
            EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_tenant_guc', tbl);
            EXECUTE format(
                'CREATE POLICY %I ON public.%I FOR ALL TO agentos_app ' ||
                'USING (tenant_id = current_setting(''app.tenant_id'', true)) ' ||
                'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))',
                tbl || '_tenant_guc', tbl);
        END LOOP;
    END IF;
END $$;
