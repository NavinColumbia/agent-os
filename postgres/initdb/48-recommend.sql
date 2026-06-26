-- 48-recommend.sql — the recommender's store.
-- recommendations: data-driven, per-tenant suggestions ("add a security pass", "use higher rigor")
-- distilled from accumulated build_outcomes (the recommender learns as more builds finish). dismissed_at
-- is the learning signal: was the suggestion useful (kept) or not (dismissed)?

CREATE TABLE IF NOT EXISTS recommendations (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    TEXT,
    org_id       TEXT,
    kind         TEXT,
    title        TEXT,
    body         TEXT,
    score        NUMERIC,
    evidence     JSONB DEFAULT '{}',
    dismissed_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ DEFAULT now()
);
