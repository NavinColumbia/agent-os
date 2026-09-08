-- Versioned customer/agent-designed graph definitions, token-run state, event
-- history, and action outbox. The six product phases remain a separate coarse
-- projection; these graphs may branch, loop, wait for humans, and fan out.

CREATE TABLE IF NOT EXISTS aos_v2_workflow_definitions (
    tenant_id text NOT NULL,
    workflow_id text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    definition jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workflow_id, version),
    CHECK (definition->>'tenant_id' = tenant_id),
    CHECK (definition->>'workflow_id' = workflow_id),
    CHECK ((definition->>'version')::integer = version)
);

CREATE TABLE IF NOT EXISTS aos_v2_workflow_runs (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    workflow_id text NOT NULL,
    workflow_version integer NOT NULL,
    state_version integer NOT NULL CHECK (state_version >= 0),
    start_fingerprint text NOT NULL CHECK (start_fingerprint ~ '^[0-9a-f]{64}$'),
    state jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id),
    FOREIGN KEY (tenant_id, workflow_id, workflow_version)
        REFERENCES aos_v2_workflow_definitions (tenant_id, workflow_id, version),
    CHECK (state->>'tenant_id' = tenant_id),
    CHECK (state->>'run_id' = run_id),
    CHECK ((state->>'version')::integer = state_version)
);

CREATE TABLE IF NOT EXISTS aos_v2_workflow_events (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    event_id text NOT NULL,
    state_version integer NOT NULL CHECK (state_version >= 0),
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    event jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id, event_id),
    UNIQUE (tenant_id, run_id, state_version),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES aos_v2_workflow_runs (tenant_id, run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS aos_v2_workflow_actions (
    action_id text PRIMARY KEY CHECK (action_id ~ '^[0-9a-f]{64}$'),
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    source_event_id text NOT NULL,
    state_version integer NOT NULL CHECK (state_version >= 0),
    position integer NOT NULL CHECK (position >= 0),
    action jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'executing', 'succeeded', 'failed', 'cancelled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, run_id, state_version, position),
    FOREIGN KEY (tenant_id, run_id, source_event_id)
        REFERENCES aos_v2_workflow_events (tenant_id, run_id, event_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS aos_v2_workflow_actions_pending_idx
    ON aos_v2_workflow_actions (tenant_id, status, created_at)
    WHERE status = 'pending';

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'aos_v2_workflow_definitions',
        'aos_v2_workflow_runs',
        'aos_v2_workflow_events',
        'aos_v2_workflow_actions'
    ]
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM agentos_app', table_name);
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO agentos_app', table_name
        );
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', table_name || '_tenant_guc', table_name);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL TO agentos_app '
            'USING (tenant_id = current_setting(''app.tenant_id'', true)) '
            'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))',
            table_name || '_tenant_guc', table_name
        );
    END LOOP;
END $$;
