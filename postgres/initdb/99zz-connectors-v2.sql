-- Tenant-owned external capability envelopes. Definitions contain only policy
-- and opaque credential references; secret bytes are injected into workers by
-- the deployment secret store and never enter PostgreSQL.

CREATE TABLE IF NOT EXISTS public.aos_v2_connectors (
    tenant_id text NOT NULL,
    connector_id text NOT NULL,
    definition jsonb NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    active boolean NOT NULL DEFAULT true,
    idempotency_key text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    disabled_by text,
    disabled_at timestamptz,
    disabled_reason text,
    disable_idempotency_key text,
    PRIMARY KEY (tenant_id, connector_id),
    UNIQUE (tenant_id, idempotency_key),
    CHECK (connector_id ~ '^[a-z][a-z0-9-]{0,62}[a-z0-9]$'),
    CHECK (definition->>'connector_id' = connector_id),
    CHECK (definition->>'base_url' ~ '^https://'),
    CHECK (
        (active AND disabled_by IS NULL AND disabled_at IS NULL
            AND disabled_reason IS NULL AND disable_idempotency_key IS NULL)
        OR
        (NOT active AND disabled_by IS NOT NULL AND disabled_at IS NOT NULL
            AND disabled_reason IS NOT NULL AND disable_idempotency_key IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS aos_v2_connectors_tenant_active_idx
    ON public.aos_v2_connectors (tenant_id, active, connector_id);

ALTER TABLE public.aos_v2_connectors ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_connectors FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_connectors FROM agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_connectors TO agentos_app;
DROP POLICY IF EXISTS aos_v2_connectors_tenant_guc ON public.aos_v2_connectors;
CREATE POLICY aos_v2_connectors_tenant_guc ON public.aos_v2_connectors
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
