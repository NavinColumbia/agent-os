-- 33-task-board.sql - durable CEO-facing request and finding board.
--
-- This table used to be created lazily by scripts/taskboard.py.  It must be
-- part of the ordered schema because later tenant-isolation migrations add an
-- index and RLS policy before any application process has run.
CREATE TABLE IF NOT EXISTS task_board (
    id         SERIAL PRIMARY KEY,
    tenant     TEXT NOT NULL DEFAULT 'platform',
    title      TEXT NOT NULL,
    detail     TEXT DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'asked',
    source     TEXT NOT NULL DEFAULT 'ceo',
    notes      TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS task_board_status_updated_idx
    ON task_board (status, updated_at DESC);
