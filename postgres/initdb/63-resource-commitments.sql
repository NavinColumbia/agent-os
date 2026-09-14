-- Durable resource commitments and actuals.
--
-- Authority says whether an actor may make a commitment; this ledger records what
-- was reserved, for which objective/work contract, under which authority, and what
-- was eventually consumed. Oversubscription is retained as management evidence --
-- it is not silently hidden by a scheduler heuristic.

-- Existing identifiers are globally unique, but the composite index also lets
-- commitment foreign keys prove tenant ownership at the database boundary.
CREATE UNIQUE INDEX IF NOT EXISTS work_contracts_tenant_contract_uidx
    ON work_contracts (tenant_id, contract_id);

CREATE TABLE IF NOT EXISTS resource_pools (
    pool_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    org_id TEXT,
    name TEXT NOT NULL,
    resource_kind TEXT NOT NULL,
    unit TEXT NOT NULL,
    capacity NUMERIC(20,6) NOT NULL CHECK (capacity >= 0),
    window_start TIMESTAMPTZ,
    window_end TIMESTAMPTZ,
    accountable_owner TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active','frozen','closed')),
    version INT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, pool_id),
    CHECK (window_end IS NULL OR window_start IS NULL OR window_end > window_start)
);
CREATE INDEX IF NOT EXISTS resource_pools_active_idx
    ON resource_pools (tenant_id, resource_kind, state);

CREATE TABLE IF NOT EXISTS resource_commitments (
    commitment_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    pool_id TEXT NOT NULL,
    objective_id TEXT,
    work_contract_id TEXT,
    description TEXT NOT NULL,
    expected_amount NUMERIC(20,6) NOT NULL CHECK (expected_amount >= 0),
    reserved_amount NUMERIC(20,6) NOT NULL CHECK (reserved_amount > 0),
    actual_amount NUMERIC(20,6) NOT NULL DEFAULT 0 CHECK (actual_amount >= 0),
    authority_kind TEXT NOT NULL
        CHECK (authority_kind IN ('standing','manager','human','contractual')),
    authority_reference TEXT NOT NULL,
    authority_snapshot JSONB NOT NULL,
    approval_reference TEXT,
    approved_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved','committed','released','expired','reconciling','reconciled','cancelled')),
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    release_reason TEXT,
    released_at TIMESTAMPTZ,
    reconciliation_due_at TIMESTAMPTZ,
    reconciled_at TIMESTAMPTZ,
    version INT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, commitment_id),
    FOREIGN KEY (tenant_id, pool_id) REFERENCES resource_pools(tenant_id, pool_id),
    FOREIGN KEY (tenant_id, objective_id) REFERENCES strategic_objectives(tenant_id, objective_id),
    FOREIGN KEY (tenant_id, work_contract_id)
        REFERENCES work_contracts(tenant_id, contract_id),
    CHECK (objective_id IS NOT NULL OR work_contract_id IS NOT NULL),
    CHECK (authority_snapshot <> '{}'::jsonb),
    CHECK (expires_at IS NULL OR expires_at > valid_from)
);
CREATE INDEX IF NOT EXISTS resource_commitments_pool_live_idx
    ON resource_commitments (tenant_id, pool_id, status, expires_at);
CREATE INDEX IF NOT EXISTS resource_commitments_objective_idx
    ON resource_commitments (tenant_id, objective_id) WHERE objective_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS resource_commitments_contract_idx
    ON resource_commitments (tenant_id, work_contract_id) WHERE work_contract_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS resource_commitments_reconcile_idx
    ON resource_commitments (tenant_id, reconciliation_due_at)
    WHERE status IN ('released','expired','reconciling');

CREATE TABLE IF NOT EXISTS resource_actuals (
    actual_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    commitment_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    delta_amount NUMERIC(20,6) NOT NULL CHECK (delta_amount <> 0),
    kind TEXT NOT NULL CHECK (kind IN ('usage','refund','correction')),
    source_reference TEXT NOT NULL,
    evidence JSONB NOT NULL,
    recorded_by TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, commitment_id)
        REFERENCES resource_commitments(tenant_id, commitment_id),
    CHECK (evidence <> '{}'::jsonb),
    CHECK ((kind='usage' AND delta_amount > 0) OR
           (kind='refund' AND delta_amount < 0) OR kind='correction')
);
CREATE INDEX IF NOT EXISTS resource_actuals_commitment_idx
    ON resource_actuals (tenant_id, commitment_id, actual_id);

CREATE TABLE IF NOT EXISTS resource_reconciliations (
    reconciliation_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    commitment_id TEXT NOT NULL,
    expected_amount NUMERIC(20,6) NOT NULL,
    reserved_amount NUMERIC(20,6) NOT NULL,
    actual_amount NUMERIC(20,6) NOT NULL,
    variance_amount NUMERIC(20,6) NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('accepted','adjusted','disputed')),
    rationale TEXT NOT NULL,
    evidence JSONB NOT NULL,
    reconciled_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, commitment_id)
        REFERENCES resource_commitments(tenant_id, commitment_id),
    CHECK (evidence <> '{}'::jsonb),
    CHECK (variance_amount = actual_amount - expected_amount)
);
CREATE INDEX IF NOT EXISTS resource_reconciliations_commitment_idx
    ON resource_reconciliations (tenant_id, commitment_id, reconciliation_id DESC);

CREATE TABLE IF NOT EXISTS resource_commitment_events (
    event_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    commitment_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    facts JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, commitment_id)
        REFERENCES resource_commitments(tenant_id, commitment_id)
);
CREATE INDEX IF NOT EXISTS resource_commitment_events_idx
    ON resource_commitment_events (tenant_id, commitment_id, event_id DESC);

DO $$
DECLARE
    tbl text;
    seq_name text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentos_app') THEN
        FOREACH tbl IN ARRAY ARRAY[
            'resource_pools','resource_commitments','resource_actuals',
            'resource_reconciliations','resource_commitment_events'
        ] LOOP
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
            seq_name := pg_get_serial_sequence(format('public.%I', tbl),
                CASE WHEN tbl='resource_actuals' THEN 'actual_id'
                     WHEN tbl='resource_reconciliations' THEN 'reconciliation_id'
                     WHEN tbl='resource_commitment_events' THEN 'event_id' END);
            IF seq_name IS NOT NULL THEN
                EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %s TO agentos_app', seq_name);
            END IF;
        END LOOP;
    END IF;
END $$;
