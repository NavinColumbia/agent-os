-- Durable tenant model-turn reservations and settled usage for quota and billing truth.

CREATE TABLE IF NOT EXISTS public.aos_v2_usage_accounts (
    tenant_id text PRIMARY KEY,
    monthly_budget_cents integer NOT NULL CHECK (monthly_budget_cents > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.aos_v2_usage_events (
    tenant_id text NOT NULL,
    source_id text NOT NULL,
    run_id text NOT NULL,
    billing_period text NOT NULL CHECK (billing_period ~ '^[0-9]{4}-[0-9]{2}$'),
    category text NOT NULL,
    model text NOT NULL,
    maximum_cost_cents integer NOT NULL CHECK (maximum_cost_cents > 0),
    charged_cost_cents integer CHECK (charged_cost_cents >= 0),
    provider_cost_usd_micros bigint CHECK (provider_cost_usd_micros >= 0),
    requests integer CHECK (requests >= 0),
    tool_calls integer CHECK (tool_calls >= 0),
    input_tokens bigint CHECK (input_tokens >= 0),
    output_tokens bigint CHECK (output_tokens >= 0),
    total_tokens bigint CHECK (total_tokens >= 0),
    status text NOT NULL CHECK (status IN ('reserved', 'settled')),
    reservation_fingerprint text NOT NULL CHECK (reservation_fingerprint ~ '^[0-9a-f]{64}$'),
    settlement_fingerprint text CHECK (settlement_fingerprint ~ '^[0-9a-f]{64}$'),
    usage jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz,
    PRIMARY KEY (tenant_id, source_id),
    CHECK (
        (status = 'reserved' AND charged_cost_cents IS NULL AND settlement_fingerprint IS NULL)
        OR
        (status = 'settled' AND charged_cost_cents IS NOT NULL AND settlement_fingerprint IS NOT NULL
         AND requests IS NOT NULL AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL
         AND total_tokens IS NOT NULL AND settled_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS aos_v2_usage_events_period_idx
    ON public.aos_v2_usage_events (tenant_id, billing_period, status);
CREATE INDEX IF NOT EXISTS aos_v2_usage_events_run_idx
    ON public.aos_v2_usage_events (tenant_id, run_id, created_at DESC);

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['aos_v2_usage_accounts', 'aos_v2_usage_events']
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE ON public.%I TO agentos_app', table_name);
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', table_name || '_tenant_isolation', table_name);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I USING (tenant_id = current_setting(''app.tenant_id'', true)) ' ||
            'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))',
            table_name || '_tenant_isolation', table_name
        );
    END LOOP;
END $$;
