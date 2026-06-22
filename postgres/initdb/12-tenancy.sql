-- Multi-tenancy: each customer is a tenant with their own API token; products belong to tenants.
-- This is the SaaS isolation boundary (extend with row-level security per table at cloud scale).
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id  TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    api_token  TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS tenant_products (
    product    TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL REFERENCES tenants(tenant_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
