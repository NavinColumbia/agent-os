-- Opaque-capability static previews. The unauthenticated HTTP route supplies
-- the decoded tenant as an RLS GUC and must still match the 256-bit public ID.

CREATE TABLE IF NOT EXISTS aos_v2_preview_deployments (
    tenant_id text NOT NULL,
    deployment_id text NOT NULL,
    public_id text NOT NULL,
    artifact_id text NOT NULL,
    receipt_artifact_id text NOT NULL,
    idempotency_key text NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    public_url text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, deployment_id),
    UNIQUE (tenant_id, public_id),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, artifact_id)
        REFERENCES aos_v2_artifacts (tenant_id, artifact_id),
    FOREIGN KEY (tenant_id, receipt_artifact_id)
        REFERENCES aos_v2_artifacts (tenant_id, artifact_id),
    CHECK (deployment_id ~ '^deployment-[0-9a-f]{64}$'),
    CHECK (public_id ~ '^[A-Za-z0-9_-]{43}$'),
    CHECK (record->>'tenant_id' = tenant_id),
    CHECK (record->>'deployment_id' = deployment_id),
    CHECK (record->>'public_id' = public_id),
    CHECK (record->>'artifact_id' = artifact_id),
    CHECK (record->>'receipt_artifact_id' = receipt_artifact_id)
);

CREATE INDEX IF NOT EXISTS aos_v2_preview_deployments_tenant_created_idx
    ON aos_v2_preview_deployments (tenant_id, created_at DESC, deployment_id DESC);

ALTER TABLE public.aos_v2_preview_deployments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_preview_deployments FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_preview_deployments FROM agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_preview_deployments TO agentos_app;
DROP POLICY IF EXISTS aos_v2_preview_deployments_tenant_guc
    ON public.aos_v2_preview_deployments;
CREATE POLICY aos_v2_preview_deployments_tenant_guc
    ON public.aos_v2_preview_deployments
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
