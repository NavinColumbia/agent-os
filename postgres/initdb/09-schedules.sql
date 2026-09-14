-- Scheduler: recurring autonomous jobs (e.g. periodic data ingestion, retention sweeps, monitors).
CREATE TABLE IF NOT EXISTS schedules (
    name        TEXT PRIMARY KEY,
    command     TEXT NOT NULL,          -- shell command run by the agent-os venv context
    interval_s  INTEGER NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    manual_only BOOLEAN NOT NULL DEFAULT false,
    last_run    TIMESTAMPTZ,
    next_run    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scheduler_runs (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    decision    TEXT NOT NULL,
    rc          INTEGER,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    detail      TEXT,
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS scheduler_runs_name_at_idx ON scheduler_runs (name, at DESC);
