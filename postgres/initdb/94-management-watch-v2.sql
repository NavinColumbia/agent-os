-- Durable, deduplicated scheduling for proactive mission-manager health checks.
-- A review interval produces diagnosis/escalation; it never terminates work.

CREATE TABLE IF NOT EXISTS aos_v2_management_watches (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'executing', 'retired')),
    next_check_at timestamptz NOT NULL DEFAULT now(),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_owner text,
    lease_expires_at timestamptz,
    last_checked_at timestamptz,
    last_signal_fingerprint text CHECK (
        last_signal_fingerprint IS NULL OR last_signal_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    consecutive_signal_checks integer NOT NULL DEFAULT 0
        CHECK (consecutive_signal_checks >= 0),
    notified_level integer NOT NULL DEFAULT 0 CHECK (notified_level >= 0),
    last_result jsonb,
    last_error jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES aos_v2_workflow_runs (tenant_id, run_id) ON DELETE CASCADE
);

INSERT INTO aos_v2_management_watches (tenant_id, run_id, status, next_check_at, created_at)
SELECT tenant_id, run_id, 'pending', now(), now()
FROM aos_v2_workflow_runs
WHERE state->>'status' IN ('active', 'waiting')
ON CONFLICT (tenant_id, run_id) DO NOTHING;

CREATE INDEX IF NOT EXISTS aos_v2_management_watches_due_idx
    ON aos_v2_management_watches (tenant_id, status, next_check_at)
    WHERE status IN ('pending', 'executing');
CREATE INDEX IF NOT EXISTS aos_v2_management_watches_ready_global_idx
    ON aos_v2_management_watches (next_check_at, tenant_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS aos_v2_management_watches_expired_global_idx
    ON aos_v2_management_watches (lease_expires_at, tenant_id)
    WHERE status = 'executing';

ALTER TABLE public.aos_v2_management_watches ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_management_watches FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_management_watches FROM agentos_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.aos_v2_management_watches TO agentos_app;
DROP POLICY IF EXISTS aos_v2_management_watches_tenant_guc
    ON public.aos_v2_management_watches;
CREATE POLICY aos_v2_management_watches_tenant_guc
    ON public.aos_v2_management_watches
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- Cross-tenant discovery sees scheduling metadata only. The management worker
-- enters the tenant-scoped app role before reading mission state or payloads.
REVOKE ALL ON TABLE public.aos_v2_management_watches FROM agentos_worker;
GRANT SELECT (tenant_id, status, next_check_at, lease_expires_at)
    ON TABLE public.aos_v2_management_watches TO agentos_worker;
DROP POLICY IF EXISTS aos_v2_management_watches_worker_discovery
    ON public.aos_v2_management_watches;
CREATE POLICY aos_v2_management_watches_worker_discovery
    ON public.aos_v2_management_watches
    FOR SELECT TO agentos_worker
    USING (true);
