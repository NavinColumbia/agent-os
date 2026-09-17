-- Canonical mission intent, bitemporal provenance, effect-bound authority,
-- deterministic assurance decisions, and balanced mission budget entries.

CREATE TABLE IF NOT EXISTS aos_v2_missions (
    tenant_id text NOT NULL,
    mission_id text NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN ('active', 'waiting', 'contained', 'succeeded', 'cancelled')),
    spec jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, mission_id),
    CHECK (spec->>'tenant_id' = tenant_id),
    CHECK (spec->>'mission_id' = mission_id),
    CHECK ((spec->>'revision')::integer = revision)
);

CREATE TABLE IF NOT EXISTS aos_v2_mission_evidence (
    tenant_id text NOT NULL,
    mission_id text NOT NULL,
    evidence_id text NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    contains_personal_data boolean NOT NULL DEFAULT false,
    retention_until timestamptz,
    observed_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL,
    evidence jsonb NOT NULL,
    PRIMARY KEY (tenant_id, mission_id, evidence_id),
    FOREIGN KEY (tenant_id, mission_id)
        REFERENCES aos_v2_missions (tenant_id, mission_id) ON DELETE CASCADE,
    CHECK (evidence->>'mission_id' = mission_id),
    CHECK (evidence->>'evidence_id' = evidence_id),
    CHECK (NOT contains_personal_data OR retention_until IS NOT NULL),
    CHECK (retention_until IS NULL OR retention_until > recorded_at)
);

CREATE TABLE IF NOT EXISTS aos_v2_mission_claims (
    tenant_id text NOT NULL,
    mission_id text NOT NULL,
    claim_id text NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN ('proposed', 'accepted', 'disputed', 'superseded', 'invalidated')),
    valid_from timestamptz NOT NULL,
    valid_to timestamptz,
    recorded_at timestamptz NOT NULL,
    claim jsonb NOT NULL,
    PRIMARY KEY (tenant_id, mission_id, claim_id),
    FOREIGN KEY (tenant_id, mission_id)
        REFERENCES aos_v2_missions (tenant_id, mission_id) ON DELETE CASCADE,
    CHECK (claim->>'mission_id' = mission_id),
    CHECK (claim->>'claim_id' = claim_id),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE TABLE IF NOT EXISTS aos_v2_mission_evidence_tombstones (
    tenant_id text NOT NULL,
    mission_id text NOT NULL,
    evidence_id text NOT NULL,
    erased_at timestamptz NOT NULL DEFAULT now(),
    erased_by text NOT NULL,
    reason text NOT NULL,
    PRIMARY KEY (tenant_id, mission_id, evidence_id),
    FOREIGN KEY (tenant_id, mission_id, evidence_id)
        REFERENCES aos_v2_mission_evidence (tenant_id, mission_id, evidence_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS aos_v2_mission_hazards (
    tenant_id text NOT NULL,
    mission_id text NOT NULL,
    hazard_id text NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    severity text NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    hazard jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, mission_id, hazard_id),
    FOREIGN KEY (tenant_id, mission_id)
        REFERENCES aos_v2_missions (tenant_id, mission_id) ON DELETE CASCADE,
    CHECK (hazard->>'mission_id' = mission_id),
    CHECK (hazard->>'hazard_id' = hazard_id)
);

CREATE TABLE IF NOT EXISTS aos_v2_mission_authorities (
    tenant_id text NOT NULL,
    mission_id text NOT NULL,
    grant_id text NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    delegate_id text NOT NULL,
    parent_grant_id text,
    expires_at timestamptz NOT NULL,
    revoked boolean NOT NULL DEFAULT false,
    revoked_at timestamptz,
    revoked_by text,
    revocation_reason text,
    grant_record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, mission_id, grant_id),
    FOREIGN KEY (tenant_id, mission_id)
        REFERENCES aos_v2_missions (tenant_id, mission_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, mission_id, parent_grant_id)
        REFERENCES aos_v2_mission_authorities (tenant_id, mission_id, grant_id),
    CHECK (grant_record->>'tenant_id' = tenant_id),
    CHECK (grant_record->>'mission_id' = mission_id),
    CHECK (grant_record->>'grant_id' = grant_id),
    CHECK (grant_record->>'delegate_id' = delegate_id),
    CHECK (NOT revoked OR (revoked_at IS NOT NULL AND revoked_by IS NOT NULL AND revocation_reason IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS aos_v2_mission_effects (
    tenant_id text NOT NULL,
    mission_id text NOT NULL,
    effect_id text NOT NULL,
    idempotency_key text NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN (
        'admitted', 'restricted', 'human_required', 'denied', 'succeeded', 'failed'
    )),
    request jsonb NOT NULL,
    reservation_id text,
    reserved_cents bigint NOT NULL DEFAULT 0 CHECK (reserved_cents >= 0),
    actual_cents bigint CHECK (actual_cents >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    PRIMARY KEY (tenant_id, mission_id, effect_id),
    UNIQUE (tenant_id, mission_id, idempotency_key),
    FOREIGN KEY (tenant_id, mission_id)
        REFERENCES aos_v2_missions (tenant_id, mission_id) ON DELETE CASCADE,
    CHECK (request->>'tenant_id' = tenant_id),
    CHECK (request->>'mission_id' = mission_id),
    CHECK (request->>'effect_id' = effect_id),
    CHECK ((status = 'admitted') = (reservation_id IS NOT NULL)
           OR status IN ('succeeded', 'failed'))
);

CREATE TABLE IF NOT EXISTS aos_v2_assurance_decisions (
    tenant_id text NOT NULL,
    mission_id text NOT NULL,
    decision_id text NOT NULL,
    effect_id text NOT NULL,
    disposition text NOT NULL CHECK (disposition IN ('allowed', 'restricted', 'human_required', 'denied')),
    decision jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, mission_id, decision_id),
    UNIQUE (tenant_id, mission_id, effect_id),
    FOREIGN KEY (tenant_id, mission_id, effect_id)
        REFERENCES aos_v2_mission_effects (tenant_id, mission_id, effect_id) ON DELETE CASCADE,
    CHECK (decision->>'decision_id' = decision_id),
    CHECK (decision->>'effect_id' = effect_id),
    CHECK (decision->>'disposition' = disposition)
);

CREATE TABLE IF NOT EXISTS aos_v2_mission_budget_entries (
    tenant_id text NOT NULL,
    mission_id text NOT NULL,
    transaction_id text NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    account text NOT NULL CHECK (account IN ('authorized', 'available', 'reserved', 'spent')),
    amount_cents bigint NOT NULL,
    effect_id text,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, mission_id, transaction_id, position),
    FOREIGN KEY (tenant_id, mission_id)
        REFERENCES aos_v2_missions (tenant_id, mission_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, mission_id, effect_id)
        REFERENCES aos_v2_mission_effects (tenant_id, mission_id, effect_id)
);

CREATE INDEX IF NOT EXISTS aos_v2_mission_claims_temporal_idx
    ON aos_v2_mission_claims (tenant_id, mission_id, valid_from, recorded_at);
CREATE INDEX IF NOT EXISTS aos_v2_mission_evidence_retention_idx
    ON aos_v2_mission_evidence (tenant_id, retention_until)
    WHERE retention_until IS NOT NULL;
CREATE INDEX IF NOT EXISTS aos_v2_mission_authorities_delegate_idx
    ON aos_v2_mission_authorities (tenant_id, delegate_id, expires_at)
    WHERE NOT revoked;
CREATE INDEX IF NOT EXISTS aos_v2_mission_effects_status_idx
    ON aos_v2_mission_effects (tenant_id, mission_id, status, created_at);
CREATE INDEX IF NOT EXISTS aos_v2_mission_budget_account_idx
    ON aos_v2_mission_budget_entries (tenant_id, mission_id, account);

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'aos_v2_missions',
        'aos_v2_mission_evidence',
        'aos_v2_mission_evidence_tombstones',
        'aos_v2_mission_claims',
        'aos_v2_mission_hazards',
        'aos_v2_mission_authorities',
        'aos_v2_mission_effects',
        'aos_v2_assurance_decisions',
        'aos_v2_mission_budget_entries'
    ]
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM agentos_app', table_name);
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', table_name || '_tenant_guc', table_name);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL TO agentos_app '
            'USING (tenant_id = current_setting(''app.tenant_id'', true)) '
            'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))',
            table_name || '_tenant_guc', table_name
        );
    END LOOP;
END $$;

-- The runtime role gets only the mutations exercised by the repository. In
-- particular, evidence, claims, decisions, revisions, and budget entries are
-- append-only even if application code is compromised; privacy erasure is an
-- explicit tombstone rather than destruction of audit history.
GRANT SELECT, INSERT, UPDATE ON TABLE aos_v2_missions TO agentos_app;
GRANT SELECT, INSERT ON TABLE aos_v2_mission_evidence TO agentos_app;
GRANT SELECT, INSERT ON TABLE aos_v2_mission_evidence_tombstones TO agentos_app;
GRANT SELECT, INSERT ON TABLE aos_v2_mission_claims TO agentos_app;
GRANT SELECT, INSERT ON TABLE aos_v2_mission_hazards TO agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE aos_v2_mission_authorities TO agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE aos_v2_mission_effects TO agentos_app;
GRANT SELECT, INSERT ON TABLE aos_v2_assurance_decisions TO agentos_app;
GRANT SELECT, INSERT ON TABLE aos_v2_mission_budget_entries TO agentos_app;
