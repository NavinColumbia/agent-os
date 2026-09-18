-- Authoritative one-recipient human request ledger. Notifications are delivery
-- hints; request state here decides whether a response is still valid.

CREATE TABLE IF NOT EXISTS public.aos_v2_human_requests (
    tenant_id text NOT NULL,
    request_id text NOT NULL,
    run_id text NOT NULL,
    notification_id text NOT NULL,
    source_id text NOT NULL,
    request_kind text NOT NULL
        CHECK (request_kind IN ('advisory', 'workflow_blocking')),
    recipient_id text NOT NULL,
    requested_by text NOT NULL,
    subject text NOT NULL,
    body text NOT NULL,
    correlation_id text,
    status text NOT NULL CHECK (status IN (
        'open', 'response_pending', 'answered', 'cancelled',
        'superseded', 'recovery_required'
    )),
    response jsonb,
    responded_by text,
    response_idempotency_key text,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    answered_at timestamptz,
    PRIMARY KEY (tenant_id, request_id),
    UNIQUE (tenant_id, notification_id),
    UNIQUE (tenant_id, source_id),
    FOREIGN KEY (tenant_id, notification_id)
        REFERENCES public.aos_v2_notifications (tenant_id, notification_id)
        ON DELETE CASCADE,
    CHECK (request_id ~ '^human-request-[0-9a-f]{64}$'),
    CHECK (length(run_id) BETWEEN 1 AND 256),
    CHECK (length(source_id) BETWEEN 1 AND 256),
    CHECK (length(recipient_id) BETWEEN 1 AND 256),
    CHECK (length(requested_by) BETWEEN 1 AND 256),
    CHECK (length(subject) BETWEEN 1 AND 500),
    CHECK (length(body) BETWEEN 1 AND 16384),
    CHECK (correlation_id IS NULL OR length(correlation_id) BETWEEN 1 AND 256),
    CHECK (
        (request_kind = 'workflow_blocking' AND correlation_id IS NOT NULL)
        OR (request_kind = 'advisory' AND correlation_id IS NULL)
    ),
    CHECK (
        (status IN ('response_pending', 'answered', 'superseded', 'recovery_required')
            AND response IS NOT NULL AND responded_by IS NOT NULL
            AND response_idempotency_key IS NOT NULL)
        OR
        (status IN ('open', 'cancelled')
            AND response IS NULL AND responded_by IS NULL
            AND response_idempotency_key IS NULL)
    ),
    CHECK ((status = 'answered') = (answered_at IS NOT NULL)),
    CHECK (response IS NULL OR (
        jsonb_typeof(response) = 'object'
        AND response <> '{}'::jsonb
        AND octet_length(response::text) <= 65536
    ))
);

CREATE UNIQUE INDEX IF NOT EXISTS aos_v2_human_requests_correlation_idx
    ON public.aos_v2_human_requests (tenant_id, correlation_id)
    WHERE correlation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS aos_v2_human_requests_recipient_open_idx
    ON public.aos_v2_human_requests (
        tenant_id, recipient_id, status, created_at DESC, request_id DESC
    );
CREATE INDEX IF NOT EXISTS aos_v2_human_requests_run_idx
    ON public.aos_v2_human_requests (
        tenant_id, run_id, created_at DESC, request_id DESC
    );

ALTER TABLE public.aos_v2_human_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_human_requests FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_human_requests FROM PUBLIC;
REVOKE ALL ON TABLE public.aos_v2_human_requests FROM agentos_app;
REVOKE ALL ON TABLE public.aos_v2_human_requests FROM agentos_worker;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_human_requests TO agentos_app;

DROP POLICY IF EXISTS aos_v2_human_requests_tenant_guc
    ON public.aos_v2_human_requests;
CREATE POLICY aos_v2_human_requests_tenant_guc
    ON public.aos_v2_human_requests
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
