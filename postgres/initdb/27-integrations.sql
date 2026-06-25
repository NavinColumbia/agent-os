-- Integrations marketplace — per-tenant connect status (Area 9). The CATALOG of integrations lives in
-- code (scripts/integrationsview.py); this table records, per tenant, which catalog integrations are
-- connected. Secrets for api_key/byo integrations are NEVER stored here — they go to the scoped vault;
-- this row only tracks status + when it was connected (+ optional non-secret meta).
CREATE TABLE IF NOT EXISTS tenant_integrations (
    tenant_id    TEXT NOT NULL,
    slug         TEXT NOT NULL,                       -- catalog slug, e.g. 'stripe'
    status       TEXT DEFAULT 'disconnected',         -- 'connected' | 'disconnected'
    connected_at TIMESTAMPTZ,
    meta         JSONB DEFAULT '{}',
    PRIMARY KEY (tenant_id, slug)
);
CREATE INDEX IF NOT EXISTS tenant_integrations_tenant_idx ON tenant_integrations (tenant_id);
