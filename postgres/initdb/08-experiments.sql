-- Experiment tracking (minimal MLflow-style) for data-scientist / ml-engineer roles.
CREATE TABLE IF NOT EXISTS experiments (
    run_id      TEXT PRIMARY KEY,
    experiment  TEXT NOT NULL,
    product     TEXT,
    params      JSONB NOT NULL DEFAULT '{}',
    metrics     JSONB NOT NULL DEFAULT '{}',
    tags        TEXT[] DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS experiments_exp_idx ON experiments (experiment, created_at);
