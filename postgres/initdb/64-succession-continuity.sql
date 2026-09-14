-- Human-company succession, absence cover and safe operational takeover.
-- Current projections make duty-manager reads cheap; append-only evidence tables
-- retain the human/agent judgments behind every appointment and handoff.

CREATE TABLE IF NOT EXISTS continuity_roles (
    role_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    org_id TEXT,
    role_name TEXT NOT NULL,
    scope_ref TEXT NOT NULL,
    criticality TEXT NOT NULL DEFAULT 'important'
        CHECK (criticality IN ('routine','important','critical')),
    minimum_backups INT NOT NULL DEFAULT 1 CHECK (minimum_backups >= 0),
    duty_manager TEXT NOT NULL,
    readiness_max_age_s INT NOT NULL DEFAULT 2592000 CHECK (readiness_max_age_s >= 60),
    takeover_policy JSONB NOT NULL DEFAULT '{}',
    current_holder TEXT,
    fence_epoch BIGINT NOT NULL DEFAULT 0 CHECK (fence_epoch >= 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, role_id),
    UNIQUE (tenant_id, scope_ref, role_name)
);
CREATE INDEX IF NOT EXISTS continuity_roles_manager_idx
    ON continuity_roles (tenant_id, duty_manager, criticality) WHERE status='active';

CREATE TABLE IF NOT EXISTS continuity_appointments (
    appointment_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    appointee TEXT NOT NULL,
    appointment_kind TEXT NOT NULL CHECK (appointment_kind IN ('primary','backup','acting')),
    priority INT NOT NULL DEFAULT 1 CHECK (priority > 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('planned','active','ended','revoked')),
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until TIMESTAMPTZ,
    appointed_by TEXT NOT NULL,
    decision_evidence JSONB NOT NULL,
    ended_by TEXT,
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, appointment_id),
    FOREIGN KEY (tenant_id, role_id) REFERENCES continuity_roles(tenant_id, role_id),
    CHECK (decision_evidence <> '{}'::jsonb),
    CHECK (valid_until IS NULL OR valid_until > valid_from),
    CHECK ((status IN ('ended','revoked')) = (ended_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS continuity_one_active_primary_idx
    ON continuity_appointments (tenant_id, role_id)
    WHERE appointment_kind='primary' AND status='active';
CREATE INDEX IF NOT EXISTS continuity_appointments_cover_idx
    ON continuity_appointments (tenant_id, role_id, appointment_kind, priority)
    WHERE status='active';

CREATE TABLE IF NOT EXISTS continuity_availability_events (
    availability_event_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('available','degraded','unavailable','unknown')),
    reason TEXT NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expected_until TIMESTAMPTZ,
    evidence JSONB NOT NULL,
    reported_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (evidence <> '{}'::jsonb),
    CHECK (expected_until IS NULL OR expected_until > effective_at)
);
CREATE INDEX IF NOT EXISTS continuity_availability_latest_idx
    ON continuity_availability_events (tenant_id, subject, effective_at DESC, availability_event_id DESC);

CREATE TABLE IF NOT EXISTS continuity_knowledge_transfers (
    transfer_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    from_subject TEXT NOT NULL,
    to_subject TEXT NOT NULL,
    artifact_kind TEXT NOT NULL CHECK (artifact_kind IN
        ('runbook','walkthrough','access_validation','simulation','decision_log','other')),
    artifact_ref TEXT NOT NULL,
    evidence JSONB NOT NULL,
    readiness_state TEXT NOT NULL DEFAULT 'submitted'
        CHECK (readiness_state IN ('submitted','verified','failed','superseded')),
    verified_by TEXT,
    verified_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, transfer_id),
    FOREIGN KEY (tenant_id, role_id) REFERENCES continuity_roles(tenant_id, role_id),
    CHECK (from_subject <> to_subject),
    CHECK (evidence <> '{}'::jsonb),
    CHECK ((readiness_state='verified') = (verified_by IS NOT NULL AND verified_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS continuity_transfer_readiness_idx
    ON continuity_knowledge_transfers (tenant_id, role_id, to_subject, readiness_state, verified_at DESC);

CREATE TABLE IF NOT EXISTS continuity_takeovers (
    takeover_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    predecessor TEXT,
    successor TEXT NOT NULL,
    trigger_kind TEXT NOT NULL CHECK (trigger_kind IN
        ('absence','unresponsive','planned_handoff','incident','manager_judgment')),
    trigger_evidence JSONB NOT NULL,
    manager_decision TEXT NOT NULL,
    decision_evidence JSONB NOT NULL,
    authorized_by TEXT NOT NULL,
    fence_epoch BIGINT NOT NULL CHECK (fence_epoch > 0),
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','released','revoked')),
    acting_appointment_id TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    ended_by TEXT,
    release_evidence JSONB NOT NULL DEFAULT '{}',
    UNIQUE (tenant_id, takeover_id),
    UNIQUE (tenant_id, role_id, fence_epoch),
    FOREIGN KEY (tenant_id, role_id) REFERENCES continuity_roles(tenant_id, role_id),
    FOREIGN KEY (tenant_id, acting_appointment_id)
        REFERENCES continuity_appointments(tenant_id, appointment_id),
    CHECK (trigger_evidence <> '{}'::jsonb),
    CHECK (decision_evidence <> '{}'::jsonb),
    CHECK ((state='active') = (ended_at IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS continuity_one_active_takeover_idx
    ON continuity_takeovers (tenant_id, role_id) WHERE state='active';

CREATE TABLE IF NOT EXISTS continuity_fence_events (
    fence_event_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    takeover_id TEXT,
    previous_holder TEXT,
    resulting_holder TEXT,
    previous_epoch BIGINT NOT NULL,
    resulting_epoch BIGINT NOT NULL,
    event_kind TEXT NOT NULL CHECK (event_kind IN ('takeover','release','revoke','appointment')),
    actor TEXT NOT NULL,
    decision_evidence JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, role_id) REFERENCES continuity_roles(tenant_id, role_id),
    FOREIGN KEY (tenant_id, takeover_id) REFERENCES continuity_takeovers(tenant_id, takeover_id),
    CHECK (resulting_epoch = previous_epoch + 1),
    CHECK (decision_evidence <> '{}'::jsonb)
);
CREATE INDEX IF NOT EXISTS continuity_fence_events_tenant_idx
    ON continuity_fence_events (tenant_id, role_id, fence_event_id DESC);

DO $$
DECLARE
    tbl text;
    seq_name text;
    mutable boolean;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentos_app') THEN
        FOREACH tbl IN ARRAY ARRAY[
            'continuity_roles','continuity_appointments','continuity_availability_events',
            'continuity_knowledge_transfers','continuity_takeovers','continuity_fence_events'
        ] LOOP
            EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tbl);
            EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', tbl);
            EXECUTE format('REVOKE ALL ON TABLE public.%I FROM agentos_app', tbl);
            mutable := tbl IN ('continuity_roles','continuity_appointments',
                               'continuity_knowledge_transfers','continuity_takeovers');
            IF mutable THEN
                EXECUTE format('GRANT SELECT, INSERT, UPDATE ON TABLE public.%I TO agentos_app', tbl);
            ELSE
                EXECUTE format('GRANT SELECT, INSERT ON TABLE public.%I TO agentos_app', tbl);
            END IF;
            EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_tenant_guc', tbl);
            EXECUTE format(
                'CREATE POLICY %I ON public.%I FOR ALL TO agentos_app ' ||
                'USING (tenant_id = current_setting(''app.tenant_id'', true)) ' ||
                'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))',
                tbl || '_tenant_guc', tbl);
            seq_name := pg_get_serial_sequence(format('public.%I', tbl),
                CASE WHEN tbl='continuity_availability_events' THEN 'availability_event_id'
                     WHEN tbl='continuity_fence_events' THEN 'fence_event_id' END);
            IF seq_name IS NOT NULL THEN
                EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %s TO agentos_app', seq_name);
            END IF;
        END LOOP;
    END IF;
END $$;
