-- Append-only, hash-chained, tamper-evident audit log for agent decisions/actions.
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor       TEXT NOT NULL,        -- agent role / id
    action      TEXT NOT NULL,        -- tool / op
    resource    TEXT,                 -- path / target / args digest
    decision    TEXT NOT NULL,        -- allow | deny | ask | executed
    payload     JSONB NOT NULL DEFAULT '{}',
    prev_hash   TEXT NOT NULL,        -- hash of previous entry ('' for genesis)
    entry_hash  TEXT NOT NULL,        -- HMAC-SHA256(key, canonical(business fields) || prev_hash)
    -- Per-tenant sub-chain (audit.py links each tenant's rows into their OWN hash chain so a tenant
    -- can verify their slice without seeing others'). NULL for platform-level rows with no tenant.
    tenant_id     TEXT,
    t_prev_hash   TEXT,               -- prev entry_hash within this tenant's sub-chain
    t_entry_hash  TEXT                -- HMAC over this row chained on t_prev_hash
);
CREATE INDEX IF NOT EXISTS audit_log_ts_idx ON audit_log (ts);
CREATE INDEX IF NOT EXISTS audit_log_tenant_idx ON audit_log (tenant_id, id);
-- Idempotent upgrade for DBs created before the per-tenant sub-chain existed (initdb CREATE above is
-- IF NOT EXISTS, so it won't add columns to an existing table — these ALTERs do).
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS tenant_id    TEXT;
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS t_prev_hash  TEXT;
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS t_entry_hash TEXT;
