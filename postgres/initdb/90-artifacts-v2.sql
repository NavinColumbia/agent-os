-- Small immutable control-plane artifacts. Large build outputs move behind the
-- same application port to GCS/OCI; the DB adapter enforces a strict byte cap.

CREATE TABLE IF NOT EXISTS aos_v2_artifacts (
    tenant_id text NOT NULL,
    artifact_id text NOT NULL,
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    byte_length bigint NOT NULL CHECK (byte_length >= 0),
    media_type text NOT NULL,
    content bytea NOT NULL,
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, artifact_id),
    CHECK (artifact_id ~ '^artifact-[0-9a-f]{64}$'),
    CHECK (octet_length(content) = byte_length),
    CHECK (record->>'tenant_id' = tenant_id),
    CHECK (record->>'artifact_id' = artifact_id),
    CHECK (record->>'digest' = digest)
);

CREATE INDEX IF NOT EXISTS aos_v2_artifacts_tenant_created_idx
    ON aos_v2_artifacts (tenant_id, created_at DESC, artifact_id DESC);

CREATE TABLE IF NOT EXISTS aos_v2_artifact_writes (
    tenant_id text NOT NULL,
    idempotency_key text NOT NULL,
    artifact_id text NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, artifact_id)
        REFERENCES aos_v2_artifacts (tenant_id, artifact_id)
);

ALTER TABLE public.aos_v2_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_artifacts FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_artifacts FROM agentos_app;
GRANT SELECT, INSERT ON TABLE public.aos_v2_artifacts TO agentos_app;
DROP POLICY IF EXISTS aos_v2_artifacts_tenant_guc ON public.aos_v2_artifacts;
CREATE POLICY aos_v2_artifacts_tenant_guc ON public.aos_v2_artifacts
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE public.aos_v2_artifact_writes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_artifact_writes FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_artifact_writes FROM agentos_app;
GRANT SELECT, INSERT ON TABLE public.aos_v2_artifact_writes TO agentos_app;
DROP POLICY IF EXISTS aos_v2_artifact_writes_tenant_guc ON public.aos_v2_artifact_writes;
CREATE POLICY aos_v2_artifact_writes_tenant_guc ON public.aos_v2_artifact_writes
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
