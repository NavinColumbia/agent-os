-- Immutable in-product notifications. External transports consume this truth;
-- they never replace it or infer success from a model message.

CREATE TABLE IF NOT EXISTS aos_v2_notifications (
    tenant_id text NOT NULL,
    notification_id text NOT NULL,
    run_id text NOT NULL,
    category text NOT NULL,
    recipient_ids jsonb NOT NULL,
    subject text NOT NULL,
    body text NOT NULL,
    source_id text NOT NULL,
    correlation_id text,
    payload jsonb NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, notification_id),
    CHECK (record->>'tenant_id' = tenant_id),
    CHECK (record->>'notification_id' = notification_id),
    CHECK (record->>'run_id' = run_id),
    CHECK (record->>'category' = category)
);

CREATE INDEX IF NOT EXISTS aos_v2_notifications_tenant_created_idx
    ON aos_v2_notifications (tenant_id, created_at DESC, notification_id DESC);
CREATE INDEX IF NOT EXISTS aos_v2_notifications_tenant_run_idx
    ON aos_v2_notifications (tenant_id, run_id, created_at DESC);

-- Recipient membership is normalized so an individual's inbox remains an
-- indexed query even when a tenant produces a high volume of notifications
-- for other people and agents.
CREATE TABLE IF NOT EXISTS aos_v2_notification_recipients (
    tenant_id text NOT NULL,
    notification_id text NOT NULL,
    recipient_id text NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, notification_id, recipient_id),
    FOREIGN KEY (tenant_id, notification_id)
        REFERENCES aos_v2_notifications (tenant_id, notification_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS aos_v2_notification_recipients_inbox_idx
    ON aos_v2_notification_recipients
    (tenant_id, recipient_id, created_at DESC, notification_id DESC);

ALTER TABLE public.aos_v2_notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_notifications FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_notifications FROM agentos_app;
GRANT SELECT, INSERT ON TABLE public.aos_v2_notifications TO agentos_app;
DROP POLICY IF EXISTS aos_v2_notifications_tenant_guc ON public.aos_v2_notifications;
CREATE POLICY aos_v2_notifications_tenant_guc ON public.aos_v2_notifications
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE public.aos_v2_notification_recipients ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_notification_recipients FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_notification_recipients FROM agentos_app;
GRANT SELECT, INSERT ON TABLE public.aos_v2_notification_recipients TO agentos_app;
DROP POLICY IF EXISTS aos_v2_notification_recipients_tenant_guc
    ON public.aos_v2_notification_recipients;
CREATE POLICY aos_v2_notification_recipients_tenant_guc
    ON public.aos_v2_notification_recipients
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
