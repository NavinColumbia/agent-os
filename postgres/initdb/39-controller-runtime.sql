-- 39-controller-runtime.sql - durable legacy controller state and job queue.
--
-- These relations used to be created by loopcontroller.py on first use.  Later
-- recovery, isolation, and QA-accounting migrations alter them, so a clean
-- installation must establish their complete base schema first.
CREATE TABLE IF NOT EXISTS controller_state (
    thread_id             BIGINT PRIMARY KEY,
    tenant_id             TEXT,
    org_id                BIGINT,
    phase                 TEXT NOT NULL DEFAULT 'DISCOVER',
    brief                 JSONB,
    options               JSONB,
    chosen_option         JSONB,
    plan                  JSONB,
    research_run_id       BIGINT,
    product               TEXT,
    awaiting              TEXT,
    job_kind              TEXT,
    job_started_at        TIMESTAMPTZ,
    job_eta_min           INTEGER,
    job_status            TEXT,
    job_sla_warned        BOOLEAN DEFAULT false,
    job_sla_warned_at     TIMESTAMPTZ,
    job_sla_claimed_at    TIMESTAMPTZ,
    job_sla_claim_token   TEXT,
    execution_scope       TEXT NOT NULL DEFAULT 'production',
    pending_intent        TEXT,
    qa_checkpoint_count   INTEGER DEFAULT 0,
    qa_last_completed     INTEGER,
    qa_no_progress_count  INTEGER DEFAULT 0,
    qa_campaign_key       TEXT,
    updated_at            TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS controller_jobs (
    id                 BIGSERIAL PRIMARY KEY,
    thread_id          BIGINT,
    tenant_id          TEXT,
    phase              TEXT,
    kind               TEXT,
    status             TEXT DEFAULT 'running',
    result             JSONB,
    started_at         TIMESTAMPTZ DEFAULT now(),
    finished_at        TIMESTAMPTZ,
    heartbeat_at       TIMESTAMPTZ,
    lease_token        BIGINT DEFAULT 0,
    worker_pid         BIGINT,
    worker_start_ticks BIGINT,
    worker_boot_id     TEXT,
    progress_at        TIMESTAMPTZ,
    progress_signature TEXT,
    progress_meta      JSONB,
    execution_scope    TEXT NOT NULL DEFAULT 'production'
);

CREATE INDEX IF NOT EXISTS controller_jobs_thread_idx
    ON controller_jobs (thread_id, id DESC);
