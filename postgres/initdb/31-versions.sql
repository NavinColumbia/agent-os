-- Versioning + rollback ledger: every snapshot of a built product repo is a row here, so a tenant
-- can "go back to yesterday's version". The tar.gz snapshot itself lives on disk under the _versions
-- store; this table records the pointer + version number + label per (tenant, product).
CREATE TABLE IF NOT EXISTS product_versions (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT,
    product       TEXT,
    version       INT,
    label         TEXT,
    snapshot_path TEXT,
    created_at    TIMESTAMPTZ DEFAULT now()
);
