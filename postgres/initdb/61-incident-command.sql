-- Durable incident command: accountable roles, evidence-gated recovery, and learning.
-- A timer creates attention; it never declares an incident failed or resolved.

CREATE TABLE IF NOT EXISTS company_incidents (
    incident_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    title TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('SEV1','SEV2','SEV3','SEV4')),
    status TEXT NOT NULL DEFAULT 'declared'
        CHECK (status IN ('declared','triage','contained','recovering','resolved','closed')),
    commander TEXT NOT NULL,
    deputy TEXT NOT NULL,
    source_ref TEXT,
    impact JSONB NOT NULL,
    communication_cadence_s INT NOT NULL CHECK (communication_cadence_s >= 60),
    next_communication_at TIMESTAMPTZ NOT NULL,
    declared_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    contained_at TIMESTAMPTZ,
    recovered_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    postmortem_id TEXT,
    created_by TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (commander <> deputy)
);
CREATE INDEX IF NOT EXISTS company_incidents_attention_idx
    ON company_incidents (next_communication_at, severity)
    WHERE status NOT IN ('resolved','closed');
CREATE INDEX IF NOT EXISTS company_incidents_tenant_idx
    ON company_incidents (tenant_id, status, declared_at DESC);

CREATE TABLE IF NOT EXISTS incident_responders (
    assignment_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    incident_id TEXT NOT NULL REFERENCES company_incidents(incident_id),
    responder TEXT NOT NULL,
    role TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'assigned'
        CHECK (status IN ('assigned','acknowledged','active','released')),
    assigned_by TEXT NOT NULL,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ,
    UNIQUE (incident_id, responder, role)
);
CREATE INDEX IF NOT EXISTS incident_responders_incident_idx
    ON incident_responders (incident_id, status);
CREATE INDEX IF NOT EXISTS incident_responders_tenant_idx
    ON incident_responders (tenant_id, incident_id, status);

-- This is an append-only factual log: commands, observations, hypotheses, decisions,
-- handoffs, stakeholder updates, and changes in impact all live on one clock.
CREATE TABLE IF NOT EXISTS incident_timeline (
    event_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    incident_id TEXT NOT NULL REFERENCES company_incidents(incident_id),
    actor TEXT NOT NULL,
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}',
    audience TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS incident_timeline_incident_idx
    ON incident_timeline (incident_id, occurred_at, event_id);
CREATE INDEX IF NOT EXISTS incident_timeline_tenant_idx
    ON incident_timeline (tenant_id, incident_id, occurred_at, event_id);

CREATE TABLE IF NOT EXISTS incident_verifications (
    verification_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    incident_id TEXT NOT NULL REFERENCES company_incidents(incident_id),
    phase TEXT NOT NULL CHECK (phase IN ('containment','recovery','recurrence')),
    assertion TEXT NOT NULL,
    method TEXT NOT NULL,
    evidence JSONB NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('passed','failed','inconclusive')),
    verified_by TEXT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS incident_verifications_incident_idx
    ON incident_verifications (incident_id, phase, verified_at DESC);
CREATE INDEX IF NOT EXISTS incident_verifications_tenant_idx
    ON incident_verifications (tenant_id, incident_id, phase, verified_at DESC);

CREATE TABLE IF NOT EXISTS incident_corrective_actions (
    action_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    incident_id TEXT NOT NULL REFERENCES company_incidents(incident_id),
    title TEXT NOT NULL,
    owner TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'medium'
        CHECK (priority IN ('low','medium','high','critical')),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','in_progress','implemented','verified','closed','cancelled')),
    due_at TIMESTAMPTZ NOT NULL,
    recurrence_key TEXT,
    implementation_evidence JSONB NOT NULL DEFAULT '{}',
    verification_evidence JSONB NOT NULL DEFAULT '{}',
    verified_by TEXT,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS incident_actions_attention_idx
    ON incident_corrective_actions (due_at, priority)
    WHERE status NOT IN ('closed','cancelled');
CREATE INDEX IF NOT EXISTS incident_corrective_actions_tenant_idx
    ON incident_corrective_actions (tenant_id, incident_id, status);

CREATE TABLE IF NOT EXISTS incident_postmortems (
    postmortem_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    incident_id TEXT NOT NULL UNIQUE REFERENCES company_incidents(incident_id),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','review','published')),
    owner TEXT NOT NULL,
    root_causes JSONB NOT NULL,
    contributing_factors JSONB NOT NULL DEFAULT '[]',
    lessons JSONB NOT NULL DEFAULT '[]',
    recurrence_assessment JSONB NOT NULL DEFAULT '{}',
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS incident_postmortems_tenant_idx
    ON incident_postmortems (tenant_id, incident_id);
ALTER TABLE company_incidents DROP CONSTRAINT IF EXISTS company_incidents_postmortem_fk;
ALTER TABLE company_incidents ADD CONSTRAINT company_incidents_postmortem_fk
    FOREIGN KEY (postmortem_id) REFERENCES incident_postmortems(postmortem_id)
    DEFERRABLE INITIALLY DEFERRED;

DO $$
DECLARE
    tbl text;
    seq_name text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentos_app') THEN
        FOREACH tbl IN ARRAY ARRAY['company_incidents','incident_responders',
            'incident_verifications','incident_corrective_actions','incident_postmortems']
        LOOP
            EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tbl);
            EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', tbl);
            EXECUTE format('REVOKE ALL ON TABLE public.%I FROM agentos_app', tbl);
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO agentos_app', tbl);
            EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_tenant_guc', tbl);
            EXECUTE format(
                'CREATE POLICY %I ON public.%I FOR ALL TO agentos_app ' ||
                'USING (tenant_id = current_setting(''app.tenant_id'', true)) ' ||
                'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))',
                tbl || '_tenant_guc', tbl);
        END LOOP;

        ALTER TABLE incident_timeline ENABLE ROW LEVEL SECURITY;
        ALTER TABLE incident_timeline FORCE ROW LEVEL SECURITY;
        REVOKE ALL ON TABLE incident_timeline FROM agentos_app;
        GRANT SELECT, INSERT ON TABLE incident_timeline TO agentos_app;
        DROP POLICY IF EXISTS incident_timeline_tenant_read ON incident_timeline;
        CREATE POLICY incident_timeline_tenant_read ON incident_timeline FOR SELECT TO agentos_app
            USING (tenant_id = current_setting('app.tenant_id', true));
        DROP POLICY IF EXISTS incident_timeline_tenant_append ON incident_timeline;
        CREATE POLICY incident_timeline_tenant_append ON incident_timeline FOR INSERT TO agentos_app
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
        SELECT pg_get_serial_sequence('public.incident_timeline', 'event_id') INTO seq_name;
        IF seq_name IS NOT NULL THEN
            EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %s TO agentos_app', seq_name);
        END IF;
    END IF;
END $$;
