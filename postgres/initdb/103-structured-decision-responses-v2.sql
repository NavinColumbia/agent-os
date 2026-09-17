-- A human response is first committed as durable intent, then applied to the
-- exact workflow wait by a lease-recoverable worker. This closes the crash
-- window between clicking a decision and resuming the graph.

CREATE TABLE IF NOT EXISTS public.aos_v2_decision_responses (
    tenant_id text NOT NULL,
    response_id text NOT NULL,
    notification_id text NOT NULL,
    run_id text NOT NULL,
    correlation_id text NOT NULL,
    event_id text NOT NULL,
    response jsonb NOT NULL,
    expected_version integer NOT NULL,
    actor_id text NOT NULL,
    idempotency_key text NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    status text NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_expires_at timestamptz,
    last_error jsonb,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    PRIMARY KEY (tenant_id, response_id),
    UNIQUE (tenant_id, notification_id),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, notification_id)
        REFERENCES public.aos_v2_notifications (tenant_id, notification_id)
        ON DELETE CASCADE,
    CHECK (response_id ~ '^decision-[0-9a-f]{64}$'),
    CHECK (event_id ~ '^decision-event-[0-9a-f]{64}$'),
    CHECK (
        jsonb_typeof(response) = 'object'
        AND response <> '{}'::jsonb
        AND octet_length(response::text) <= 131072
    ),
    CHECK (expected_version >= 0),
    CHECK (attempts >= 0),
    CHECK (status IN ('pending', 'executing', 'applied', 'superseded', 'failed')),
    CHECK (
        (status = 'executing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR
        (status <> 'executing' AND lease_owner IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK ((status IN ('applied', 'superseded')) = (completed_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS aos_v2_decision_responses_ready_idx
    ON public.aos_v2_decision_responses (tenant_id, available_at, response_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS aos_v2_decision_responses_expired_idx
    ON public.aos_v2_decision_responses (tenant_id, lease_expires_at, response_id)
    WHERE status = 'executing';
CREATE INDEX IF NOT EXISTS aos_v2_decision_responses_ready_global_idx
    ON public.aos_v2_decision_responses (available_at, tenant_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS aos_v2_decision_responses_expired_global_idx
    ON public.aos_v2_decision_responses (lease_expires_at, tenant_id)
    WHERE status = 'executing';

ALTER TABLE public.aos_v2_decision_responses ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_decision_responses FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_decision_responses FROM agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_decision_responses TO agentos_app;
DROP POLICY IF EXISTS aos_v2_decision_responses_tenant_guc
    ON public.aos_v2_decision_responses;
CREATE POLICY aos_v2_decision_responses_tenant_guc
    ON public.aos_v2_decision_responses
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- Discovery gets scheduling columns only; claim and payload reads switch to
-- agentos_app with the selected tenant GUC.
REVOKE ALL ON TABLE public.aos_v2_decision_responses FROM agentos_worker;
GRANT SELECT (tenant_id, status, available_at, lease_expires_at)
    ON TABLE public.aos_v2_decision_responses TO agentos_worker;
DROP POLICY IF EXISTS aos_v2_decision_responses_worker_discovery
    ON public.aos_v2_decision_responses;
CREATE POLICY aos_v2_decision_responses_worker_discovery
    ON public.aos_v2_decision_responses
    FOR SELECT TO agentos_worker
    USING (true);
