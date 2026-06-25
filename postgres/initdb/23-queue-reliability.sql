-- Queue reliability: dead-letter, retry/backoff, and stuck-task lease reclaim for the agent task queue.
-- Before this, dispatcher.process() marked a task 'done' even when the agent FAILED (rc!=0) — failed work
-- was silently dropped — and a task left 'active' by a crashed dispatcher was orphaned forever. These
-- columns make the queue durable: a failed task is retried with backoff, then dead-lettered (visible +
-- paged), and a stuck 'active' task past its lease is reclaimed to 'pending'. All idempotent.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attempts    INT DEFAULT 0;          -- delivery attempts so far
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS max_retry   INT DEFAULT 3;          -- dead-letter after this many
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS locked_at   TIMESTAMPTZ;            -- when claimed 'active' (lease)
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS not_before  TIMESTAMPTZ;            -- backoff: don't run before this
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_error  TEXT;                   -- blocker from the last failure
-- status now ranges over: pending | active | done | failed (retrying) | dead (gave up, needs human)
CREATE INDEX IF NOT EXISTS tasks_active_lease_idx ON tasks (status, locked_at);
CREATE INDEX IF NOT EXISTS tasks_dead_idx         ON tasks (status) WHERE status = 'dead';
