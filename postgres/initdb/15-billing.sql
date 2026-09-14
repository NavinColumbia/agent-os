-- SaaS billing: a plan per tenant. Usage is DERIVED from real activity (builds shipped + tokens
-- spent on the tenant's products), so metering can't drift from what actually happened.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'free';
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS suspended BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS auto_suspended BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS period_start timestamptz NOT NULL DEFAULT date_trunc('month', now());

CREATE TABLE IF NOT EXISTS billing_invoices (
    tenant_id    text        NOT NULL,
    period_start timestamptz NOT NULL,
    period_end   timestamptz NOT NULL,
    plan         text        NOT NULL,
    base         numeric     NOT NULL,
    builds       integer     NOT NULL,
    tokens       bigint      NOT NULL,
    overage_cost numeric     NOT NULL,
    total        numeric     NOT NULL,
    settled_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, period_start)
);

CREATE TABLE IF NOT EXISTS billing_subscriptions (
    tenant_id TEXT PRIMARY KEY,
    plan TEXT NOT NULL DEFAULT 'free',
    processor TEXT NOT NULL DEFAULT 'stripe',
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    stripe_checkout_session_id TEXT,
    status TEXT NOT NULL DEFAULT 'none',
    payment_failures INT NOT NULL DEFAULT 0,
    dunning_suspended BOOLEAN NOT NULL DEFAULT false,
    current_period_end TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS stripe_events (
    event_id TEXT PRIMARY KEY,
    tenant_id TEXT,
    type TEXT NOT NULL,
    payload JSONB NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS billing_invoices_tenant_rls_idx ON billing_invoices (tenant_id);
CREATE INDEX IF NOT EXISTS billing_subscriptions_tenant_rls_idx ON billing_subscriptions (tenant_id);
CREATE INDEX IF NOT EXISTS stripe_events_tenant_rls_idx ON stripe_events (tenant_id);
