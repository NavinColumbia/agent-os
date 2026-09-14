-- Tenant-owned model policy. Configuration stores only an opaque credential
-- reference; provider keys are resolved by the worker's secret backend.

CREATE TABLE IF NOT EXISTS public.aos_v2_model_settings (
    tenant_id text PRIMARY KEY CHECK (length(tenant_id) BETWEEN 1 AND 128),
    version integer NOT NULL CHECK (version >= 1),
    configuration jsonb NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    updated_by text NOT NULL CHECK (length(updated_by) BETWEEN 1 AND 255),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (configuration->>'provider' IN ('openai', 'anthropic', 'google')),
    CHECK (length(configuration->>'model_name') BETWEEN 1 AND 256),
    CHECK (configuration->>'credential_source' IN ('platform', 'tenant')),
    CHECK (
        (configuration->>'credential_source' = 'platform'
            AND configuration->'credential_ref' = 'null'::jsonb)
        OR
        (configuration->>'credential_source' = 'tenant'
            AND length(configuration->>'credential_ref') BETWEEN 1 AND 128)
    )
);

CREATE TABLE IF NOT EXISTS public.aos_v2_model_setting_events (
    tenant_id text NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    version integer NOT NULL CHECK (version >= 1),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 200),
    configuration jsonb NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    actor_id text NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 255),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, version),
    UNIQUE (tenant_id, idempotency_key),
    CHECK (configuration->>'provider' IN ('openai', 'anthropic', 'google')),
    CHECK (length(configuration->>'model_name') BETWEEN 1 AND 256),
    CHECK (configuration->>'credential_source' IN ('platform', 'tenant'))
);

ALTER TABLE public.aos_v2_model_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_model_settings FORCE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_model_setting_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_model_setting_events FORCE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.aos_v2_model_settings FROM agentos_app;
REVOKE ALL ON TABLE public.aos_v2_model_setting_events FROM agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_model_settings TO agentos_app;
GRANT SELECT, INSERT ON TABLE public.aos_v2_model_setting_events TO agentos_app;

DROP POLICY IF EXISTS aos_v2_model_settings_tenant_guc ON public.aos_v2_model_settings;
CREATE POLICY aos_v2_model_settings_tenant_guc ON public.aos_v2_model_settings
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

DROP POLICY IF EXISTS aos_v2_model_setting_events_tenant_guc
    ON public.aos_v2_model_setting_events;
CREATE POLICY aos_v2_model_setting_events_tenant_guc
    ON public.aos_v2_model_setting_events
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
