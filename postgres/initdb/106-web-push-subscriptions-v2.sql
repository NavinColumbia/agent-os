-- Per-person, per-device Web Push enrollment. Subscription endpoints are
-- bearer capabilities and auth/p256dh values are encryption material, so the
-- database stores one AES-GCM ciphertext plus keyed hashes, never plaintext.

CREATE TABLE IF NOT EXISTS public.aos_v2_push_subscriptions (
    tenant_id text NOT NULL,
    subscription_id text NOT NULL,
    subject_id text NOT NULL,
    device_id text NOT NULL,
    device_name text NOT NULL,
    audience_ids jsonb NOT NULL,
    provider text NOT NULL,
    endpoint_hash text NOT NULL,
    sealed_subscription bytea NOT NULL,
    expiration_time bigint,
    active boolean NOT NULL,
    version integer NOT NULL,
    fingerprint text NOT NULL,
    registration_idempotency_key text NOT NULL,
    registered_by text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    revoked_by text,
    revoked_at timestamptz,
    revoked_reason text,
    revocation_idempotency_key text,
    PRIMARY KEY (tenant_id, subscription_id),
    UNIQUE (tenant_id, subject_id, device_id),
    UNIQUE (tenant_id, endpoint_hash),
    CHECK (subscription_id ~ '^push-[0-9a-f]{64}$'),
    CHECK (length(subject_id) BETWEEN 1 AND 256),
    CHECK (device_id ~ '^[A-Za-z0-9_-]{16,128}$'),
    CHECK (length(device_name) BETWEEN 1 AND 200),
    CHECK (jsonb_typeof(audience_ids) = 'array' AND jsonb_array_length(audience_ids) BETWEEN 1 AND 32),
    CHECK (length(provider) BETWEEN 1 AND 255),
    CHECK (endpoint_hash ~ '^[0-9a-f]{64}$'),
    CHECK (octet_length(sealed_subscription) BETWEEN 30 AND 16384),
    CHECK (expiration_time IS NULL OR expiration_time BETWEEN 1 AND 9999999999999),
    CHECK (version >= 1),
    CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    CHECK (
        (active AND revoked_by IS NULL AND revoked_at IS NULL
            AND revoked_reason IS NULL AND revocation_idempotency_key IS NULL)
        OR
        (NOT active AND revoked_by IS NOT NULL AND revoked_at IS NOT NULL
            AND revoked_reason IS NOT NULL AND revocation_idempotency_key IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS aos_v2_push_subscriptions_recipient_idx
    ON public.aos_v2_push_subscriptions (tenant_id, subject_id, active);

CREATE TABLE IF NOT EXISTS public.aos_v2_push_subscription_mutations (
    tenant_id text NOT NULL,
    idempotency_key text NOT NULL,
    action text NOT NULL,
    subscription_id text NOT NULL,
    fingerprint text NOT NULL,
    record jsonb NOT NULL,
    actor_id text NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, subscription_id)
        REFERENCES public.aos_v2_push_subscriptions (tenant_id, subscription_id)
        ON DELETE CASCADE,
    CHECK (action IN ('register', 'revoke')),
    CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    CHECK (record->>'subscription_id' = subscription_id),
    CHECK (NOT record ? 'endpoint'),
    CHECK (NOT record ? 'keys'),
    CHECK (NOT record ? 'sealed_subscription')
);

CREATE TABLE IF NOT EXISTS public.aos_v2_web_push_deliveries (
    tenant_id text NOT NULL,
    delivery_id text NOT NULL,
    notification_id text NOT NULL,
    subscription_id text NOT NULL,
    status text NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL,
    lease_owner text,
    lease_expires_at timestamptz,
    last_error jsonb,
    result jsonb,
    created_at timestamptz NOT NULL,
    delivered_at timestamptz,
    PRIMARY KEY (tenant_id, delivery_id),
    FOREIGN KEY (tenant_id, notification_id)
        REFERENCES public.aos_v2_notifications (tenant_id, notification_id)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, subscription_id)
        REFERENCES public.aos_v2_push_subscriptions (tenant_id, subscription_id),
    CHECK (delivery_id ~ '^push-delivery-[0-9a-f]{64}$'),
    CHECK (status IN ('pending', 'executing', 'delivered', 'failed', 'cancelled')),
    CHECK (attempts >= 0),
    CHECK (
        (status = 'executing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR
        (status <> 'executing' AND lease_owner IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK ((status = 'delivered') = (delivered_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS aos_v2_web_push_deliveries_ready_idx
    ON public.aos_v2_web_push_deliveries (tenant_id, available_at, delivery_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS aos_v2_web_push_deliveries_expired_idx
    ON public.aos_v2_web_push_deliveries (tenant_id, lease_expires_at, delivery_id)
    WHERE status = 'executing';
CREATE INDEX IF NOT EXISTS aos_v2_web_push_deliveries_ready_global_idx
    ON public.aos_v2_web_push_deliveries (available_at, tenant_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS aos_v2_web_push_deliveries_expired_global_idx
    ON public.aos_v2_web_push_deliveries (lease_expires_at, tenant_id)
    WHERE status = 'executing';

ALTER TABLE public.aos_v2_push_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_push_subscriptions FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_push_subscriptions FROM agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_push_subscriptions TO agentos_app;
DROP POLICY IF EXISTS aos_v2_push_subscriptions_tenant_guc
    ON public.aos_v2_push_subscriptions;
CREATE POLICY aos_v2_push_subscriptions_tenant_guc
    ON public.aos_v2_push_subscriptions
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE public.aos_v2_push_subscription_mutations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_push_subscription_mutations FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_push_subscription_mutations FROM agentos_app;
GRANT SELECT, INSERT ON TABLE public.aos_v2_push_subscription_mutations TO agentos_app;
DROP POLICY IF EXISTS aos_v2_push_subscription_mutations_tenant_guc
    ON public.aos_v2_push_subscription_mutations;
CREATE POLICY aos_v2_push_subscription_mutations_tenant_guc
    ON public.aos_v2_push_subscription_mutations
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE public.aos_v2_web_push_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_web_push_deliveries FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_web_push_deliveries FROM agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_web_push_deliveries TO agentos_app;
DROP POLICY IF EXISTS aos_v2_web_push_deliveries_tenant_guc
    ON public.aos_v2_web_push_deliveries;
CREATE POLICY aos_v2_web_push_deliveries_tenant_guc
    ON public.aos_v2_web_push_deliveries
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- Cross-tenant scheduling sees no endpoint/key material. The actual claim
-- switches to agentos_app with a tenant GUC before ciphertext is read.
REVOKE ALL ON TABLE public.aos_v2_web_push_deliveries FROM agentos_worker;
GRANT SELECT (tenant_id, status, available_at, lease_expires_at)
    ON TABLE public.aos_v2_web_push_deliveries TO agentos_worker;
DROP POLICY IF EXISTS aos_v2_web_push_deliveries_worker_discovery
    ON public.aos_v2_web_push_deliveries;
CREATE POLICY aos_v2_web_push_deliveries_worker_discovery
    ON public.aos_v2_web_push_deliveries
    FOR SELECT TO agentos_worker
    USING (true);

REVOKE ALL ON TABLE public.aos_v2_push_subscriptions FROM agentos_worker;
REVOKE ALL ON TABLE public.aos_v2_push_subscription_mutations FROM agentos_worker;
