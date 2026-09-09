-- Tenant subscription truth projected only from verified Stripe events.

CREATE TABLE IF NOT EXISTS public.aos_v2_billing_accounts (
    tenant_id text PRIMARY KEY,
    plan_id text NOT NULL,
    subscription_status text NOT NULL,
    stripe_customer_id text,
    stripe_subscription_id text,
    monthly_model_budget_cents integer NOT NULL
        CHECK (monthly_model_budget_cents > 0),
    current_period_end timestamptz,
    last_event_created bigint NOT NULL DEFAULT 0 CHECK (last_event_created >= 0),
    last_event_id text NOT NULL DEFAULT 'none',
    last_subscription_event_created bigint NOT NULL DEFAULT 0
        CHECK (last_subscription_event_created >= 0),
    last_subscription_event_id text NOT NULL DEFAULT 'none',
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS aos_v2_billing_accounts_customer_idx
    ON public.aos_v2_billing_accounts (stripe_customer_id)
    WHERE stripe_customer_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS aos_v2_billing_accounts_subscription_idx
    ON public.aos_v2_billing_accounts (stripe_subscription_id)
    WHERE stripe_subscription_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.aos_v2_billing_events (
    tenant_id text NOT NULL,
    event_id text NOT NULL,
    event_type text NOT NULL,
    event_created bigint NOT NULL CHECK (event_created >= 0),
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    applied boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, event_id)
);

CREATE INDEX IF NOT EXISTS aos_v2_billing_events_order_idx
    ON public.aos_v2_billing_events (tenant_id, event_created DESC, event_id DESC);

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['aos_v2_billing_accounts', 'aos_v2_billing_events']
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM agentos_app', table_name);
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE ON TABLE public.%I TO agentos_app', table_name
        );
        EXECUTE format(
            'DROP POLICY IF EXISTS %I ON public.%I',
            table_name || '_tenant_isolation', table_name
        );
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL TO agentos_app ' ||
            'USING (tenant_id = current_setting(''app.tenant_id'', true)) ' ||
            'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))',
            table_name || '_tenant_isolation', table_name
        );
    END LOOP;
END $$;
