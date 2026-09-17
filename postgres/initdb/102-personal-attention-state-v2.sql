-- Per-person attention state is mutable presentation data layered over the
-- immutable notification ledger.  Dismissal and snoozing never delete the
-- notification or its external-delivery audit trail.

CREATE TABLE IF NOT EXISTS public.aos_v2_notification_states (
    tenant_id text NOT NULL,
    subject_id text NOT NULL,
    notification_id text NOT NULL,
    status text NOT NULL,
    snoozed_until timestamptz,
    version integer NOT NULL DEFAULT 1,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    idempotency_key text NOT NULL,
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, subject_id, notification_id),
    FOREIGN KEY (tenant_id, notification_id)
        REFERENCES public.aos_v2_notifications (tenant_id, notification_id)
        ON DELETE CASCADE,
    CHECK (status IN ('unread', 'read', 'dismissed', 'snoozed', 'resolved')),
    CHECK (version >= 1),
    CHECK ((status = 'snoozed') = (snoozed_until IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS aos_v2_notification_states_inbox_idx
    ON public.aos_v2_notification_states
    (tenant_id, subject_id, status, snoozed_until);

CREATE TABLE IF NOT EXISTS public.aos_v2_notification_preferences (
    tenant_id text NOT NULL,
    subject_id text NOT NULL,
    record jsonb NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    version integer NOT NULL DEFAULT 1,
    idempotency_key text NOT NULL,
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, subject_id),
    CHECK (version >= 1),
    CHECK (record->>'tenant_id' = tenant_id),
    CHECK (record->>'subject_id' = subject_id),
    CHECK (record->>'mode' IN ('focused', 'balanced', 'all'))
);

ALTER TABLE public.aos_v2_notification_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_notification_states FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_notification_states FROM agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_notification_states TO agentos_app;
DROP POLICY IF EXISTS aos_v2_notification_states_tenant_guc
    ON public.aos_v2_notification_states;
CREATE POLICY aos_v2_notification_states_tenant_guc
    ON public.aos_v2_notification_states
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE public.aos_v2_notification_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_notification_preferences FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_notification_preferences FROM agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_notification_preferences TO agentos_app;
DROP POLICY IF EXISTS aos_v2_notification_preferences_tenant_guc
    ON public.aos_v2_notification_preferences;
CREATE POLICY aos_v2_notification_preferences_tenant_guc
    ON public.aos_v2_notification_preferences
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
