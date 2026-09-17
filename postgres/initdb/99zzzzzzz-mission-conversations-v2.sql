-- Immutable mission-scoped human/agent conversation. Message bodies remain
-- behind tenant RLS and mission authorization; experience events carry only
-- safe invalidation summaries.

CREATE TABLE IF NOT EXISTS public.aos_v2_mission_messages (
    tenant_id text NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    mission_id text NOT NULL CHECK (length(mission_id) BETWEEN 1 AND 256),
    message_id text NOT NULL CHECK (length(message_id) BETWEEN 1 AND 96),
    sender_id text NOT NULL CHECK (length(sender_id) BETWEEN 1 AND 255),
    sender_persona text NOT NULL CHECK (length(sender_persona) BETWEEN 1 AND 32),
    channel text NOT NULL CHECK (channel IN ('shared', 'internal')),
    kind text NOT NULL CHECK (kind IN ('comment', 'question', 'update')),
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 8000),
    reply_to_message_id text CHECK (
        reply_to_message_id IS NULL OR length(reply_to_message_id) BETWEEN 1 AND 96
    ),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 200),
    fingerprint text NOT NULL CHECK (length(fingerprint) = 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, mission_id, message_id),
    UNIQUE (tenant_id, mission_id, sender_id, idempotency_key),
    FOREIGN KEY (tenant_id, mission_id, reply_to_message_id)
        REFERENCES public.aos_v2_mission_messages (tenant_id, mission_id, message_id)
        DEFERRABLE INITIALLY IMMEDIATE
);

CREATE INDEX IF NOT EXISTS aos_v2_mission_messages_timeline_idx
    ON public.aos_v2_mission_messages (
        tenant_id, mission_id, created_at DESC, message_id DESC
    );

ALTER TABLE public.aos_v2_mission_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_mission_messages FORCE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.aos_v2_mission_messages FROM agentos_app;
GRANT SELECT, INSERT ON TABLE public.aos_v2_mission_messages TO agentos_app;

DROP POLICY IF EXISTS aos_v2_mission_messages_tenant_guc
    ON public.aos_v2_mission_messages;
CREATE POLICY aos_v2_mission_messages_tenant_guc
    ON public.aos_v2_mission_messages
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
