-- 29-projbudget.sql — per-project (per-product) budget caps a CEO/tenant sets.
-- appguard.py owns ENFORCEMENT (auto-pause); this table is the tenant-facing SET/READ layer:
-- the cap a CEO chooses for one of their products, surfaced to appguard/cockpit alongside spend.
CREATE TABLE IF NOT EXISTS project_budget (
    tenant_id  TEXT,
    product    TEXT,
    cap_usd    NUMERIC,
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (tenant_id, product)
);
