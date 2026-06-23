-- Orchestration: per-agent prioritized task queues + hire requests.
-- An agent that needs a collaborator it cannot spawn files a hire_request (only the controller spawns);
-- work routed to an agent lands in its task queue, which it drains highest-priority-first.
CREATE TABLE IF NOT EXISTS tasks (
    id         BIGSERIAL PRIMARY KEY,
    assignee   TEXT NOT NULL,
    requester  TEXT,
    role       TEXT,
    title      TEXT NOT NULL,
    priority   INT  NOT NULL DEFAULT 5,        -- 1 (highest) .. 9 (lowest)
    status     TEXT NOT NULL DEFAULT 'pending', -- pending | active | done
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tasks_queue_idx ON tasks (assignee, status, priority, id);

CREATE TABLE IF NOT EXISTS hire_requests (
    id         BIGSERIAL PRIMARY KEY,
    requester  TEXT NOT NULL,
    need_role  TEXT NOT NULL,
    reason     TEXT,
    status     TEXT NOT NULL DEFAULT 'open',   -- open | fulfilled | denied
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
