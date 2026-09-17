-- Durable, tenant-monotonic user-visible event log.  These records are safe
-- projection invalidations and summaries, not raw prompts, reasoning, secrets,
-- tool payloads, or an alternative workflow/audit authority.

CREATE TABLE IF NOT EXISTS public.aos_v2_experience_streams (
    tenant_id text PRIMARY KEY,
    next_sequence bigint NOT NULL DEFAULT 1,
    retained_from_sequence bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (next_sequence >= 1),
    CHECK (retained_from_sequence >= 1),
    CHECK (retained_from_sequence <= next_sequence)
);

CREATE TABLE IF NOT EXISTS public.aos_v2_experience_events (
    tenant_id text NOT NULL,
    tenant_sequence bigint NOT NULL,
    event_id text NOT NULL,
    source_key text NOT NULL,
    resource_type text NOT NULL,
    resource_id text NOT NULL,
    projection_revision bigint NOT NULL,
    kind text NOT NULL,
    audience_ids jsonb NOT NULL,
    safe_summary text NOT NULL,
    trace_id text,
    fingerprint text NOT NULL,
    record jsonb NOT NULL,
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, tenant_sequence),
    UNIQUE (tenant_id, event_id),
    UNIQUE (tenant_id, source_key),
    FOREIGN KEY (tenant_id)
        REFERENCES public.aos_v2_experience_streams (tenant_id),
    CHECK (tenant_sequence >= 1),
    CHECK (length(event_id) BETWEEN 1 AND 96),
    CHECK (length(source_key) BETWEEN 1 AND 512),
    CHECK (length(resource_type) BETWEEN 1 AND 64),
    CHECK (length(resource_id) BETWEEN 1 AND 256),
    CHECK (projection_revision >= 0),
    CHECK (length(kind) BETWEEN 1 AND 128),
    CHECK (jsonb_typeof(audience_ids) = 'array'),
    CHECK (jsonb_array_length(audience_ids) BETWEEN 1 AND 128),
    CHECK (length(safe_summary) BETWEEN 1 AND 500),
    CHECK (trace_id IS NULL OR length(trace_id) BETWEEN 1 AND 256),
    CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    CHECK (record->>'tenant_id' = tenant_id),
    CHECK ((record->>'tenant_sequence')::bigint = tenant_sequence),
    CHECK (record->>'event_id' = event_id)
);

CREATE INDEX IF NOT EXISTS aos_v2_experience_events_tenant_time_idx
    ON public.aos_v2_experience_events
    (tenant_id, occurred_at DESC, tenant_sequence DESC);

CREATE TABLE IF NOT EXISTS public.aos_v2_experience_event_audiences (
    tenant_id text NOT NULL,
    tenant_sequence bigint NOT NULL,
    audience_id text NOT NULL,
    PRIMARY KEY (tenant_id, tenant_sequence, audience_id),
    FOREIGN KEY (tenant_id, tenant_sequence)
        REFERENCES public.aos_v2_experience_events (tenant_id, tenant_sequence)
        ON DELETE CASCADE,
    CHECK (length(audience_id) BETWEEN 1 AND 256)
);

CREATE INDEX IF NOT EXISTS aos_v2_experience_event_audiences_lookup_idx
    ON public.aos_v2_experience_event_audiences
    (tenant_id, audience_id, tenant_sequence);

ALTER TABLE public.aos_v2_experience_streams ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_experience_streams FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_experience_streams FROM agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_experience_streams TO agentos_app;
DROP POLICY IF EXISTS aos_v2_experience_streams_tenant_guc
    ON public.aos_v2_experience_streams;
CREATE POLICY aos_v2_experience_streams_tenant_guc
    ON public.aos_v2_experience_streams
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE public.aos_v2_experience_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_experience_events FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_experience_events FROM agentos_app;
GRANT SELECT, INSERT ON TABLE public.aos_v2_experience_events TO agentos_app;
DROP POLICY IF EXISTS aos_v2_experience_events_tenant_guc
    ON public.aos_v2_experience_events;
CREATE POLICY aos_v2_experience_events_tenant_guc
    ON public.aos_v2_experience_events
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE public.aos_v2_experience_event_audiences ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_experience_event_audiences FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_experience_event_audiences FROM agentos_app;
GRANT SELECT, INSERT ON TABLE public.aos_v2_experience_event_audiences TO agentos_app;
DROP POLICY IF EXISTS aos_v2_experience_event_audiences_tenant_guc
    ON public.aos_v2_experience_event_audiences;
CREATE POLICY aos_v2_experience_event_audiences_tenant_guc
    ON public.aos_v2_experience_event_audiences
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

REVOKE ALL ON TABLE public.aos_v2_experience_streams FROM agentos_worker;
REVOKE ALL ON TABLE public.aos_v2_experience_events FROM agentos_worker;
REVOKE ALL ON TABLE public.aos_v2_experience_event_audiences FROM agentos_worker;
