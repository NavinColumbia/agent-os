-- 50-orchestra.sql — the DURABLE ORG: actors, events, runs (REBUILD-PLAN A1, "ONE durable actor runtime").
-- Promotes scripts/orchestra/ from an in-memory demo to THE engine: agents + org trees as Postgres rows
-- (identity, tenure, assignment, memory — so "hiring" and the org chart are real), events on a persisted,
-- SKIP-LOCKED-claimable bus, runs as the unit of a vision's lifecycle. Everything crash-safe: an event
-- claimed by a worker that dies is lease-reclaimable; an actor's memory/result/status survive any restart.
-- Applied idempotently (CREATE ... IF NOT EXISTS) by scripts/orchestra/store.py at runtime AND by initdb
-- on a fresh database — this file is the single source of truth for the schema.

-- One orchestration run: a tenant's VISION moving through the org until it finishes.
CREATE TABLE IF NOT EXISTS orchestra_runs (
    run_id      BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    org_id      BIGINT,
    vision      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',   -- running | done | failed | halted
    result      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS orchestra_runs_tenant_idx ON orchestra_runs (tenant_id, status);

-- One AI employee. supervisor_id links form the ORG TREE (NULL = a root, normally the controller).
-- hired_at/last_active = tenure + liveness; memory = the actor's own durable working memory (JSONB,
-- shallow-merged on update so it accumulates); result = what it ultimately produced.
CREATE TABLE IF NOT EXISTS orchestra_actors (
    actor_id      BIGSERIAL PRIMARY KEY,
    run_id        BIGINT NOT NULL,
    tenant_id     TEXT NOT NULL,
    org_id        BIGINT,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'worker',  -- worker | supervisor | controller
    supervisor_id BIGINT,                          -- -> orchestra_actors.actor_id (NULL = root)
    status        TEXT NOT NULL DEFAULT 'idle',    -- idle | working | blocked | parked | done | dead
    assignment    TEXT,
    memory        JSONB NOT NULL DEFAULT '{}',
    result        JSONB,
    hire_key      TEXT,                             -- stable replay key for crash-safe hiring
    hired_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active   TIMESTAMPTZ NOT NULL DEFAULT now(),
    step_claimed_at TIMESTAMPTZ,
    step_claimed_by TEXT
);
ALTER TABLE orchestra_actors ADD COLUMN IF NOT EXISTS step_claimed_at TIMESTAMPTZ;
ALTER TABLE orchestra_actors ADD COLUMN IF NOT EXISTS step_claimed_by TEXT;
ALTER TABLE orchestra_actors ADD COLUMN IF NOT EXISTS hire_key TEXT;
CREATE INDEX IF NOT EXISTS orchestra_actors_run_idx    ON orchestra_actors (run_id);
CREATE INDEX IF NOT EXISTS orchestra_actors_tenant_idx ON orchestra_actors (tenant_id, status);
CREATE INDEX IF NOT EXISTS orchestra_actors_sup_idx    ON orchestra_actors (supervisor_id);
CREATE INDEX IF NOT EXISTS orchestra_actors_step_claim_idx
    ON orchestra_actors (step_claimed_at) WHERE step_claimed_at IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS orchestra_actors_hire_key_idx
    ON orchestra_actors (run_id, hire_key) WHERE hire_key IS NOT NULL;

-- The PERSISTED bus: every inter-actor event is a claimable row. A consumer claims its pending events
-- with FOR UPDATE SKIP LOCKED (concurrent-claimer-safe, same pattern as the tasks queue), stamps
-- claimed_at/claimed_by, and marks processed_at when handled. A claim whose holder crashed is
-- reclaimable once claimed_at is older than the lease — no event is ever silently lost.
CREATE TABLE IF NOT EXISTS orchestra_events (
    id           BIGSERIAL PRIMARY KEY,
    run_id       BIGINT NOT NULL,
    tenant_id    TEXT NOT NULL,
    frm          BIGINT,                           -- sending actor_id (NULL = system/human injection)
    to_actor     BIGINT NOT NULL,                  -- recipient actor_id (the inbox this row sits in)
    kind         TEXT NOT NULL,                    -- task|done|next|blocked|finding|question|need_agent|
                                                   -- resource_request|process_change|need_context|escalate|
                                                   -- resolve|context_update|broadcast
    payload      JSONB NOT NULL DEFAULT '{}',
    corr_id      TEXT,                             -- conversation/escalation-chain correlation
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at   TIMESTAMPTZ,
    claimed_by   TEXT,
    processed_at TIMESTAMPTZ
);
-- the hot claim path: an actor's unprocessed inbox, oldest first.
CREATE INDEX IF NOT EXISTS orchestra_events_inbox_idx
    ON orchestra_events (to_actor, id) WHERE processed_at IS NULL;
CREATE INDEX IF NOT EXISTS orchestra_events_run_idx  ON orchestra_events (run_id);
CREATE INDEX IF NOT EXISTS orchestra_events_corr_idx ON orchestra_events (corr_id);

-- Durable fencing for long-running tool workers.  Session advisory locks disappear when their
-- PostgreSQL connection is reset even though Chromium/a fixer may still be alive.  A lease row
-- survives that reset; every takeover increments fence_token, so a superseded worker can prove it
-- no longer owns the side-effect boundary and stop before its next action.
CREATE TABLE IF NOT EXISTS orchestra_tool_leases (
    lease_key      TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    run_id         BIGINT NOT NULL,
    actor_id       BIGINT NOT NULL,
    tool           TEXT NOT NULL,
    owner_id       TEXT NOT NULL,
    fence_token    BIGINT NOT NULL DEFAULT 1,
    lease_until    TIMESTAMPTZ NOT NULL,
    heartbeat_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS orchestra_tool_leases_expiry_idx
    ON orchestra_tool_leases (lease_until);
CREATE INDEX IF NOT EXISTS orchestra_tool_leases_run_idx
    ON orchestra_tool_leases (tenant_id, run_id);
