-- Release-aware execution-plane health. This deliberately global projection
-- contains no tenant identifiers, queue payloads, model content, or error text.

CREATE TABLE IF NOT EXISTS public.aos_v2_worker_health (
    cell_id text NOT NULL CHECK (length(cell_id) BETWEEN 1 AND 128),
    worker_id text NOT NULL CHECK (length(worker_id) BETWEEN 1 AND 256),
    generation_id uuid NOT NULL,
    application_version text NOT NULL
        CHECK (length(application_version) BETWEEN 1 AND 128),
    state text NOT NULL CONSTRAINT aos_v2_worker_health_state_check
        CHECK (state IN ('starting', 'standby', 'running', 'stopping', 'stopped')),
    started_at timestamptz NOT NULL,
    heartbeat_at timestamptz NOT NULL,
    last_discovery_at timestamptz,
    last_successful_discovery_at timestamptz,
    discovery_error_streak integer NOT NULL DEFAULT 0
        CHECK (discovery_error_streak >= 0),
    busy_since timestamptz,
    last_main_progress_at timestamptz NOT NULL,
    queue_probe_at timestamptz,
    ready_count_capped integer CHECK (ready_count_capped >= 0),
    ready_count_truncated boolean,
    oldest_ready_at timestamptz,
    oldest_queue_kind text CHECK (
        oldest_queue_kind IS NULL OR oldest_queue_kind IN (
            'lifecycle', 'workflow', 'management', 'notification', 'web_push', 'decision'
        )
    ),
    last_probe_error_type text CHECK (
        last_probe_error_type IS NULL OR length(last_probe_error_type) BETWEEN 1 AND 128
    ),
    stopped_at timestamptz,
    PRIMARY KEY (cell_id, worker_id),
    CHECK (
        (queue_probe_at IS NULL AND ready_count_capped IS NULL
            AND ready_count_truncated IS NULL AND oldest_ready_at IS NULL
            AND oldest_queue_kind IS NULL)
        OR
        (queue_probe_at IS NOT NULL AND ready_count_capped IS NOT NULL
            AND ready_count_truncated IS NOT NULL
            AND ((ready_count_capped = 0 AND oldest_ready_at IS NULL
                    AND oldest_queue_kind IS NULL)
                OR (ready_count_capped > 0 AND oldest_ready_at IS NOT NULL
                    AND oldest_queue_kind IS NOT NULL)))
    ),
    CHECK (
        (state = 'stopped' AND stopped_at IS NOT NULL)
        OR (state <> 'stopped' AND stopped_at IS NULL)
    )
);

-- Preserve idempotency for pre-release environments that exercised an earlier
-- draft of migration 108 before this progress fence was added.
ALTER TABLE public.aos_v2_worker_health
    ADD COLUMN IF NOT EXISTS last_main_progress_at timestamptz;
UPDATE public.aos_v2_worker_health
   SET last_main_progress_at = COALESCE(last_main_progress_at, busy_since, heartbeat_at, started_at)
 WHERE last_main_progress_at IS NULL;
ALTER TABLE public.aos_v2_worker_health
    ALTER COLUMN last_main_progress_at SET NOT NULL;

ALTER TABLE public.aos_v2_worker_health
    DROP CONSTRAINT IF EXISTS aos_v2_worker_health_state_check;
ALTER TABLE public.aos_v2_worker_health
    ADD CONSTRAINT aos_v2_worker_health_state_check
    CHECK (state IN ('starting', 'standby', 'running', 'stopping', 'stopped'));

CREATE INDEX IF NOT EXISTS aos_v2_worker_health_active_release_idx
    ON public.aos_v2_worker_health (
        cell_id, application_version, heartbeat_at DESC
    )
    WHERE state IN ('starting', 'running');

ALTER TABLE public.aos_v2_worker_health ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_worker_health FORCE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.aos_v2_worker_health FROM PUBLIC;
REVOKE ALL ON TABLE public.aos_v2_worker_health FROM agentos_app;
REVOKE ALL ON TABLE public.aos_v2_worker_health FROM agentos_worker;
GRANT SELECT ON TABLE public.aos_v2_worker_health TO agentos_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.aos_v2_worker_health TO agentos_worker;

DROP POLICY IF EXISTS aos_v2_worker_health_app_read
    ON public.aos_v2_worker_health;
CREATE POLICY aos_v2_worker_health_app_read
    ON public.aos_v2_worker_health
    FOR SELECT TO agentos_app
    USING (true);

DROP POLICY IF EXISTS aos_v2_worker_health_worker_read
    ON public.aos_v2_worker_health;
CREATE POLICY aos_v2_worker_health_worker_read
    ON public.aos_v2_worker_health
    FOR SELECT TO agentos_worker
    USING (true);

DROP POLICY IF EXISTS aos_v2_worker_health_worker_insert
    ON public.aos_v2_worker_health;
CREATE POLICY aos_v2_worker_health_worker_insert
    ON public.aos_v2_worker_health
    FOR INSERT TO agentos_worker
    WITH CHECK (true);

DROP POLICY IF EXISTS aos_v2_worker_health_worker_update
    ON public.aos_v2_worker_health;
CREATE POLICY aos_v2_worker_health_worker_update
    ON public.aos_v2_worker_health
    FOR UPDATE TO agentos_worker
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS aos_v2_worker_health_worker_prune
    ON public.aos_v2_worker_health;
CREATE POLICY aos_v2_worker_health_worker_prune
    ON public.aos_v2_worker_health
    FOR DELETE TO agentos_worker
    USING (heartbeat_at < now() - interval '7 days');

-- One release per execution cell may claim new work. Candidate workers remain
-- read-only standby probes until the deployment coordinator flips this fence.
CREATE TABLE IF NOT EXISTS public.aos_v2_execution_release (
    cell_id text PRIMARY KEY CHECK (length(cell_id) BETWEEN 1 AND 128),
    active_application_version text NOT NULL
        CHECK (length(active_application_version) BETWEEN 1 AND 128),
    activation_generation uuid NOT NULL,
    activated_at timestamptz NOT NULL
);

ALTER TABLE public.aos_v2_execution_release ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_execution_release FORCE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.aos_v2_execution_release FROM PUBLIC;
REVOKE ALL ON TABLE public.aos_v2_execution_release FROM agentos_app;
REVOKE ALL ON TABLE public.aos_v2_execution_release FROM agentos_worker;
GRANT SELECT ON TABLE public.aos_v2_execution_release TO agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_execution_release TO agentos_worker;

DROP POLICY IF EXISTS aos_v2_execution_release_app_read
    ON public.aos_v2_execution_release;
CREATE POLICY aos_v2_execution_release_app_read
    ON public.aos_v2_execution_release
    FOR SELECT TO agentos_app
    USING (true);

DROP POLICY IF EXISTS aos_v2_execution_release_worker_read
    ON public.aos_v2_execution_release;
CREATE POLICY aos_v2_execution_release_worker_read
    ON public.aos_v2_execution_release
    FOR SELECT TO agentos_worker
    USING (true);

DROP POLICY IF EXISTS aos_v2_execution_release_worker_insert
    ON public.aos_v2_execution_release;
CREATE POLICY aos_v2_execution_release_worker_insert
    ON public.aos_v2_execution_release
    FOR INSERT TO agentos_worker
    WITH CHECK (true);

DROP POLICY IF EXISTS aos_v2_execution_release_worker_update
    ON public.aos_v2_execution_release;
CREATE POLICY aos_v2_execution_release_worker_update
    ON public.aos_v2_execution_release
    FOR UPDATE TO agentos_worker
    USING (true)
    WITH CHECK (true);
