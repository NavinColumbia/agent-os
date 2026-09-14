-- 40-organizations.sql - canonical tenant-owned organization registry.
--
-- Organization helpers historically created this table lazily.  Cross-org
-- lineage (migration 49) and tenant-spine backfills (migration 54) depend on
-- it during a clean, unattended installation, so it belongs in the ordered
-- schema before either dependency.
CREATE TABLE IF NOT EXISTS orgs (
    id         BIGSERIAL PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    name       TEXT NOT NULL,
    vision     TEXT DEFAULT '',
    stage      TEXT NOT NULL DEFAULT 'new',
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS orgs_tenant_idx
    ON orgs (tenant_id, status);
