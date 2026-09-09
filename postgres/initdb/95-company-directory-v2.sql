-- Standing company identities survive individual mission runs. Changes are
-- immutable tenant events; the default organization remains the bootstrap base.

CREATE TABLE IF NOT EXISTS aos_v2_companies (
    tenant_id text PRIMARY KEY,
    name text NOT NULL,
    version integer NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS aos_v2_company_events (
    tenant_id text NOT NULL,
    event_id text NOT NULL,
    stream_version integer NOT NULL CHECK (stream_version > 0),
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    kind text NOT NULL CHECK (kind IN ('agent_hired', 'agent_retired')),
    actor_id text NOT NULL,
    payload jsonb NOT NULL,
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, event_id),
    UNIQUE (tenant_id, stream_version),
    FOREIGN KEY (tenant_id) REFERENCES aos_v2_companies (tenant_id) ON DELETE CASCADE,
    CHECK (record->>'tenant_id' = tenant_id),
    CHECK (record->>'event_id' = event_id),
    CHECK (record->>'kind' = kind),
    CHECK ((record->>'stream_version')::integer = stream_version)
);

CREATE INDEX IF NOT EXISTS aos_v2_company_events_stream_idx
    ON aos_v2_company_events (tenant_id, stream_version);

ALTER TABLE public.aos_v2_companies ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_companies FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_companies FROM agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_companies TO agentos_app;
DROP POLICY IF EXISTS aos_v2_companies_tenant_guc ON public.aos_v2_companies;
CREATE POLICY aos_v2_companies_tenant_guc ON public.aos_v2_companies
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE public.aos_v2_company_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_company_events FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_company_events FROM agentos_app;
GRANT SELECT, INSERT ON TABLE public.aos_v2_company_events TO agentos_app;
DROP POLICY IF EXISTS aos_v2_company_events_tenant_guc ON public.aos_v2_company_events;
CREATE POLICY aos_v2_company_events_tenant_guc ON public.aos_v2_company_events
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
