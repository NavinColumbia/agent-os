-- 42-qualityloop.sql — the "iterate until perfect" engine's state.
-- quality_runs: one climb-to-bar loop per build; quality_measurements: one row per round (the evidence
-- trail of how quality climbed); build_outcomes: the terminal record per build (feeds the recommender).

CREATE TABLE IF NOT EXISTS quality_runs (
    id          BIGSERIAL PRIMARY KEY,
    product     TEXT,
    org_id      TEXT,
    bar         TEXT,
    rounds      INT DEFAULT 0,
    status      TEXT DEFAULT 'running',
    result      TEXT,
    started_at  TIMESTAMPTZ DEFAULT now(),
    finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS quality_measurements (
    id              BIGSERIAL PRIMARY KEY,
    run_id          BIGINT,
    round           INT,
    tests_pass      BOOLEAN,
    security_clean  BOOLEAN,
    verified        BOOLEAN,
    score           NUMERIC,
    note            TEXT,
    at              TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS build_outcomes (
    id           BIGSERIAL PRIMARY KEY,
    product      TEXT,
    kind         TEXT,
    rounds       INT,
    final_score  NUMERIC,
    shipped      BOOLEAN,
    cost_usd     NUMERIC,
    at           TIMESTAMPTZ DEFAULT now()
);
