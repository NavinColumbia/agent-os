-- Authoritative V2 product lifecycle projection, event log, and command outbox.
-- One organization is one tenant for the first customer vertical.  Every key
-- and index retains tenant_id so this can be cell-sharded without rewriting
-- lifecycle semantics.

CREATE TABLE IF NOT EXISTS aos_v2_lifecycle_runs (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    version integer NOT NULL CHECK (version >= 0),
    state jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id),
    CHECK (state->>'organization_id' = tenant_id),
    CHECK (state->>'run_id' = run_id),
    CHECK ((state->>'version')::integer = version)
);

CREATE TABLE IF NOT EXISTS aos_v2_lifecycle_events (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    event_id text NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    aggregate_version integer NOT NULL CHECK (aggregate_version > 0),
    event jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id, event_id),
    UNIQUE (tenant_id, run_id, aggregate_version),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES aos_v2_lifecycle_runs (tenant_id, run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS aos_v2_lifecycle_commands (
    command_id text PRIMARY KEY CHECK (command_id ~ '^[0-9a-f]{64}$'),
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    event_id text NOT NULL,
    aggregate_version integer NOT NULL CHECK (aggregate_version > 0),
    position integer NOT NULL CHECK (position >= 0),
    envelope jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'executing', 'succeeded', 'failed', 'cancelled')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    result jsonb,
    last_error jsonb,
    UNIQUE (tenant_id, run_id, aggregate_version, position),
    FOREIGN KEY (tenant_id, run_id, event_id)
        REFERENCES aos_v2_lifecycle_events (tenant_id, run_id, event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS aos_v2_organization_streams (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    version integer NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES aos_v2_lifecycle_runs (tenant_id, run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS aos_v2_organization_events (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    event_id text NOT NULL,
    stream_version integer NOT NULL CHECK (stream_version > 0),
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    kind text NOT NULL,
    actor_id text NOT NULL,
    occurred_at text NOT NULL,
    causation_id text,
    correlation_id text,
    payload jsonb NOT NULL,
    event jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id, event_id),
    UNIQUE (tenant_id, run_id, stream_version),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES aos_v2_organization_streams (tenant_id, run_id) ON DELETE CASCADE,
    CHECK (event->>'tenant_id' = tenant_id),
    CHECK (event->>'run_id' = run_id),
    CHECK (event->>'event_id' = event_id),
    CHECK (event->>'kind' = kind)
);

INSERT INTO aos_v2_organization_streams (tenant_id, run_id, version, created_at, updated_at)
SELECT tenant_id, run_id, 0, created_at, updated_at
FROM aos_v2_lifecycle_runs
ON CONFLICT (tenant_id, run_id) DO NOTHING;

-- Keep this bootstrap migration safe when upgrading a database that received
-- an earlier V2 preview schema.
ALTER TABLE aos_v2_lifecycle_commands
    ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS available_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS lease_owner text,
    ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_error jsonb;

CREATE INDEX IF NOT EXISTS aos_v2_lifecycle_runs_tenant_updated_idx
    ON aos_v2_lifecycle_runs (tenant_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS aos_v2_lifecycle_events_tenant_run_version_idx
    ON aos_v2_lifecycle_events (tenant_id, run_id, aggregate_version);
CREATE INDEX IF NOT EXISTS aos_v2_lifecycle_commands_recoverable_idx
    ON aos_v2_lifecycle_commands (tenant_id, status, available_at, created_at)
    WHERE status IN ('pending', 'executing');
CREATE INDEX IF NOT EXISTS aos_v2_organization_events_stream_idx
    ON aos_v2_organization_events (tenant_id, run_id, stream_version);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentos_app') THEN
        CREATE ROLE agentos_app NOLOGIN;
    END IF;
END $$;

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'aos_v2_lifecycle_runs',
        'aos_v2_lifecycle_events',
        'aos_v2_lifecycle_commands',
        'aos_v2_organization_streams',
        'aos_v2_organization_events'
    ]
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM agentos_app', table_name);
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO agentos_app',
            table_name
        );
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', table_name || '_tenant_guc', table_name);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL TO agentos_app ' ||
            'USING (tenant_id = current_setting(''app.tenant_id'', true)) ' ||
            'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))',
            table_name || '_tenant_guc', table_name
        );
    END LOOP;
END $$;
