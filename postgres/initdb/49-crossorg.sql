-- 49-crossorg.sql — CROSS-ORG operations store.
-- A CEO runs many orgs; sometimes two should MERGE into a combined product, or a feature built in one
-- org should be PORTED ("stolen") into another. These are consequential, owner-scoped, GOVERNED ops —
-- they require human approval and are recorded as bookkeeping + lineage, NOT autonomous code rewrites.
--
-- xorg_ops: one cross-org operation (merge | steal_feature) through its lifecycle.
--   status: proposed -> scoping -> planned -> await_approval -> executing -> done | blocked | rejected
-- org_lineage: provenance — which org a target was derived from (a merge/port leaves a lineage trail).

CREATE TABLE IF NOT EXISTS xorg_ops (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT,
    kind        TEXT,                          -- merge | steal_feature
    source_org  BIGINT,
    target_org  BIGINT,
    feature     TEXT,
    status      TEXT DEFAULT 'proposed',       -- proposed|scoping|planned|await_approval|executing|done|blocked|rejected
    plan        JSONB,
    result      JSONB,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS org_lineage (
    id           BIGSERIAL PRIMARY KEY,
    org_id       BIGINT,
    derived_from BIGINT,
    note         TEXT,
    at           TIMESTAMPTZ DEFAULT now()
);
