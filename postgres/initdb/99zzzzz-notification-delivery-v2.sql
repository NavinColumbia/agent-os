-- Tenant-owned external notification routes and crash-recoverable delivery.
-- Secret bytes remain in the connector secret backend; this schema stores only
-- approved capability references, immutable notification truth, and receipts.

CREATE TABLE IF NOT EXISTS public.aos_v2_notification_routes (
    tenant_id text NOT NULL,
    route_id text NOT NULL,
    connector_id text NOT NULL,
    categories jsonb NOT NULL,
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
    PRIMARY KEY (tenant_id, route_id),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, connector_id)
        REFERENCES public.aos_v2_connectors (tenant_id, connector_id),
    CHECK (route_id ~ '^[a-z][a-z0-9-]{0,62}[a-z0-9]$'),
    CHECK (jsonb_typeof(categories) = 'array' AND jsonb_array_length(categories) BETWEEN 1 AND 16),
    CHECK (definition->>'route_id' = route_id),
    CHECK (definition->>'connector_id' = connector_id),
    CHECK (definition->>'payload_format' IN ('agent-os', 'slack')),
    CHECK (
        (active AND disabled_by IS NULL AND disabled_at IS NULL
            AND disabled_reason IS NULL AND disable_idempotency_key IS NULL)
        OR
        (NOT active AND disabled_by IS NOT NULL AND disabled_at IS NOT NULL
            AND disabled_reason IS NOT NULL AND disable_idempotency_key IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS aos_v2_notification_routes_tenant_active_idx
    ON public.aos_v2_notification_routes (tenant_id, active, route_id);

CREATE TABLE IF NOT EXISTS public.aos_v2_notification_deliveries (
    tenant_id text NOT NULL,
    delivery_id text NOT NULL,
    notification_id text NOT NULL,
    route_id text NOT NULL,
    status text NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_expires_at timestamptz,
    last_error jsonb,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz,
    redrive_idempotency_key text,
    redriven_by text,
    PRIMARY KEY (tenant_id, delivery_id),
    FOREIGN KEY (tenant_id, notification_id)
        REFERENCES public.aos_v2_notifications (tenant_id, notification_id)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, route_id)
        REFERENCES public.aos_v2_notification_routes (tenant_id, route_id),
    CHECK (delivery_id ~ '^delivery-[0-9a-f]{64}$'),
    CHECK (status IN ('pending', 'executing', 'delivered', 'failed', 'cancelled')),
    CHECK (attempts >= 0),
    CHECK (
        (status = 'executing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR
        (status <> 'executing' AND lease_owner IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK ((status = 'delivered') = (delivered_at IS NOT NULL)),
    CHECK ((redrive_idempotency_key IS NULL) = (redriven_by IS NULL))
);

CREATE INDEX IF NOT EXISTS aos_v2_notification_deliveries_ready_idx
    ON public.aos_v2_notification_deliveries (tenant_id, available_at, delivery_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS aos_v2_notification_deliveries_expired_idx
    ON public.aos_v2_notification_deliveries (tenant_id, lease_expires_at, delivery_id)
    WHERE status = 'executing';
CREATE INDEX IF NOT EXISTS aos_v2_notification_deliveries_ready_global_idx
    ON public.aos_v2_notification_deliveries (available_at, tenant_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS aos_v2_notification_deliveries_expired_global_idx
    ON public.aos_v2_notification_deliveries (lease_expires_at, tenant_id)
    WHERE status = 'executing';

ALTER TABLE public.aos_v2_notification_routes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_notification_routes FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_notification_routes FROM agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_notification_routes TO agentos_app;
DROP POLICY IF EXISTS aos_v2_notification_routes_tenant_guc
    ON public.aos_v2_notification_routes;
CREATE POLICY aos_v2_notification_routes_tenant_guc
    ON public.aos_v2_notification_routes
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE public.aos_v2_notification_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_notification_deliveries FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_notification_deliveries FROM agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_notification_deliveries TO agentos_app;
DROP POLICY IF EXISTS aos_v2_notification_deliveries_tenant_guc
    ON public.aos_v2_notification_deliveries;
CREATE POLICY aos_v2_notification_deliveries_tenant_guc
    ON public.aos_v2_notification_deliveries
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- Cross-tenant discovery sees only scheduling columns. Actual claim/read/write
-- switches to agentos_app with a tenant GUC, preserving isolation.
REVOKE ALL ON TABLE public.aos_v2_notification_deliveries FROM agentos_worker;
GRANT SELECT (tenant_id, status, available_at, lease_expires_at)
    ON TABLE public.aos_v2_notification_deliveries TO agentos_worker;
DROP POLICY IF EXISTS aos_v2_notification_deliveries_worker_discovery
    ON public.aos_v2_notification_deliveries;
CREATE POLICY aos_v2_notification_deliveries_worker_discovery
    ON public.aos_v2_notification_deliveries
    FOR SELECT TO agentos_worker
    USING (true);
