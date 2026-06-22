-- Scheduler: recurring autonomous jobs (e.g. periodic data ingestion, retention sweeps, monitors).
CREATE TABLE IF NOT EXISTS schedules (
    name        TEXT PRIMARY KEY,
    command     TEXT NOT NULL,          -- shell command run by the agent-os venv context
    interval_s  INTEGER NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    last_run    TIMESTAMPTZ,
    next_run    TIMESTAMPTZ NOT NULL DEFAULT now()
);
