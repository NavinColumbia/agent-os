-- Org self-measurement (ADR 0002/0004): append-only metrics events -> KPIs in the monthly retro.
CREATE TABLE IF NOT EXISTS org_metrics (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    product     TEXT,
    task_id     TEXT,
    event       TEXT NOT NULL,      -- state_change | cr_filed | cr_decided | approval | breaker_trip
    from_state  TEXT,
    to_state    TEXT,
    tokens_in   INT DEFAULT 0,
    tokens_out  INT DEFAULT 0,
    model       TEXT,
    wall_clock_s NUMERIC,
    outcome     TEXT                -- success | rework | reject | kill
);
CREATE INDEX IF NOT EXISTS org_metrics_product_idx ON org_metrics (product, ts);
