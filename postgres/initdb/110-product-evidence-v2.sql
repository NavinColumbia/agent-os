-- Immutable, tenant-fenced product evidence.  A study records the evaluation
-- contract, observations bind raw artifact IDs to its exact revision, and each
-- decision is bound to the complete observation-set digest it evaluated.

CREATE TABLE IF NOT EXISTS public.aos_v2_product_studies (
    tenant_id text NOT NULL,
    study_id text NOT NULL,
    revision integer NOT NULL CHECK (revision >= 1),
    study jsonb NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    created_by text NOT NULL,
    idempotency_key text NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, study_id, revision),
    UNIQUE (tenant_id, created_by, idempotency_key),
    CHECK (length(tenant_id) BETWEEN 1 AND 128),
    CHECK (length(study_id) BETWEEN 1 AND 128),
    CHECK (length(created_by) BETWEEN 1 AND 255),
    CHECK (length(idempotency_key) BETWEEN 8 AND 200),
    CHECK (jsonb_typeof(study) = 'object'),
    CHECK (study->>'study_id' = study_id),
    CHECK ((study->>'revision')::integer = revision)
);

CREATE TABLE IF NOT EXISTS public.aos_v2_product_observations (
    tenant_id text NOT NULL,
    observation_id text NOT NULL,
    study_id text NOT NULL,
    study_revision integer NOT NULL CHECK (study_revision >= 1),
    observation jsonb NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    recorded_by text NOT NULL,
    idempotency_key text NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, observation_id),
    UNIQUE (tenant_id, recorded_by, idempotency_key),
    FOREIGN KEY (tenant_id, study_id, study_revision)
        REFERENCES public.aos_v2_product_studies (tenant_id, study_id, revision)
        ON DELETE CASCADE,
    CHECK (length(observation_id) BETWEEN 1 AND 128),
    CHECK (length(recorded_by) BETWEEN 1 AND 255),
    CHECK (length(idempotency_key) BETWEEN 8 AND 200),
    CHECK (jsonb_typeof(observation) = 'object'),
    CHECK (observation->>'observation_id' = observation_id),
    CHECK (observation->>'study_id' = study_id),
    CHECK ((observation->>'study_revision')::integer = study_revision),
    CHECK (observation->>'evidence_kind' IN (
        'deterministic', 'synthetic', 'human', 'production'
    ))
);

CREATE TABLE IF NOT EXISTS public.aos_v2_product_decisions (
    tenant_id text NOT NULL,
    decision_id text NOT NULL,
    study_id text NOT NULL,
    study_revision integer NOT NULL CHECK (study_revision >= 1),
    observation_set_sha256 text NOT NULL
        CHECK (observation_set_sha256 ~ '^[0-9a-f]{64}$'),
    decision jsonb NOT NULL,
    decided_by text NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, decision_id),
    UNIQUE (tenant_id, study_id, study_revision, observation_set_sha256),
    FOREIGN KEY (tenant_id, study_id, study_revision)
        REFERENCES public.aos_v2_product_studies (tenant_id, study_id, revision)
        ON DELETE CASCADE,
    CHECK (decision_id ~ '^product-decision-[0-9a-f]{64}$'),
    CHECK (length(decided_by) BETWEEN 1 AND 255),
    CHECK (jsonb_typeof(decision) = 'object'),
    CHECK (decision->>'disposition' IN (
        'adopt', 'controlled_experiment', 'human_validation_required',
        'insufficient_evidence', 'reject'
    ))
);

CREATE TABLE IF NOT EXISTS public.aos_v2_product_value_receipts (
    tenant_id text NOT NULL,
    receipt_id text NOT NULL,
    baseline_system_id text NOT NULL,
    candidate_system_id text NOT NULL,
    comparison jsonb NOT NULL,
    receipt jsonb NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    created_by text NOT NULL,
    idempotency_key text NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, receipt_id),
    UNIQUE (tenant_id, created_by, idempotency_key),
    CHECK (receipt_id ~ '^value-receipt-[0-9a-f]{64}$'),
    CHECK (length(baseline_system_id) BETWEEN 1 AND 128),
    CHECK (length(candidate_system_id) BETWEEN 1 AND 128),
    CHECK (length(created_by) BETWEEN 1 AND 255),
    CHECK (length(idempotency_key) BETWEEN 8 AND 200),
    CHECK (jsonb_typeof(comparison) = 'object'),
    CHECK (jsonb_typeof(receipt) = 'object'),
    CHECK (jsonb_typeof(receipt->'dominates_baseline') = 'boolean')
);

CREATE INDEX IF NOT EXISTS aos_v2_product_studies_timeline_idx
    ON public.aos_v2_product_studies
    (tenant_id, created_at DESC, study_id, revision DESC);
CREATE INDEX IF NOT EXISTS aos_v2_product_observations_study_idx
    ON public.aos_v2_product_observations
    (tenant_id, study_id, study_revision, created_at, observation_id);
CREATE INDEX IF NOT EXISTS aos_v2_product_decisions_study_idx
    ON public.aos_v2_product_decisions
    (tenant_id, study_id, study_revision, created_at DESC, decision_id);
CREATE INDEX IF NOT EXISTS aos_v2_product_value_receipts_timeline_idx
    ON public.aos_v2_product_value_receipts
    (tenant_id, created_at DESC, receipt_id);

DO $policy$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'aos_v2_product_studies',
        'aos_v2_product_observations',
        'aos_v2_product_decisions',
        'aos_v2_product_value_receipts'
    ]
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM PUBLIC', table_name);
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM agentos_app', table_name);
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM agentos_worker', table_name);
        EXECUTE format(
            'GRANT SELECT, INSERT ON TABLE public.%I TO agentos_app', table_name
        );
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I',
            table_name || '_tenant_guc', table_name);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL TO agentos_app '
            'USING (tenant_id = current_setting(''app.tenant_id'', true)) '
            'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))',
            table_name || '_tenant_guc', table_name
        );
    END LOOP;
END
$policy$;
