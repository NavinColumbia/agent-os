-- Durable strategic planning above work contracts.
--
-- Work contracts remain the source of truth for accepted operational ownership.
-- This layer records why the work matters, how success is measured, how objectives
-- compose, and what management judgment changed the plan.  Measurements and state
-- transitions are append-only evidence; current columns are projections for fast reads.

CREATE TABLE IF NOT EXISTS objective_portfolios (
    portfolio_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    org_id TEXT,
    title TEXT NOT NULL,
    purpose TEXT NOT NULL,
    horizon_start DATE,
    horizon_end DATE,
    accountable_owner TEXT NOT NULL,
    manager_owner TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('draft','active','completed','cancelled')),
    planning_revision INT NOT NULL DEFAULT 1 CHECK (planning_revision > 0),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, portfolio_id),
    CHECK (horizon_end IS NULL OR horizon_start IS NULL OR horizon_end >= horizon_start)
);

CREATE TABLE IF NOT EXISTS strategic_objectives (
    objective_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    portfolio_id TEXT NOT NULL,
    parent_objective_id TEXT,
    work_contract_id TEXT REFERENCES work_contracts(contract_id),
    title TEXT NOT NULL,
    outcome TEXT NOT NULL,
    accountable_owner TEXT NOT NULL,
    manager_owner TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'proposed'
        CHECK (state IN ('proposed','active','at_risk','blocked','achieved','abandoned')),
    priority INT NOT NULL DEFAULT 3 CHECK (priority BETWEEN 0 AND 5),
    weight NUMERIC(8,4) NOT NULL DEFAULT 1 CHECK (weight > 0),
    confidence NUMERIC(5,4) NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
    starts_on DATE,
    due_on DATE,
    version INT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, objective_id),
    UNIQUE (tenant_id, portfolio_id, objective_id),
    UNIQUE (work_contract_id),
    FOREIGN KEY (tenant_id, portfolio_id)
        REFERENCES objective_portfolios(tenant_id, portfolio_id),
    FOREIGN KEY (tenant_id, portfolio_id, parent_objective_id)
        REFERENCES strategic_objectives(tenant_id, portfolio_id, objective_id),
    CHECK (parent_objective_id IS NULL OR parent_objective_id <> objective_id),
    CHECK (due_on IS NULL OR starts_on IS NULL OR due_on >= starts_on)
);
CREATE INDEX IF NOT EXISTS strategic_objectives_tree_idx
    ON strategic_objectives (tenant_id, portfolio_id, parent_objective_id);
CREATE INDEX IF NOT EXISTS strategic_objectives_manager_idx
    ON strategic_objectives (tenant_id, manager_owner, state);

CREATE TABLE IF NOT EXISTS objective_key_results (
    key_result_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    objective_id TEXT NOT NULL,
    title TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    unit TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('increase','decrease','binary')),
    baseline NUMERIC NOT NULL,
    target NUMERIC NOT NULL,
    current_value NUMERIC NOT NULL,
    weight NUMERIC(8,4) NOT NULL DEFAULT 1 CHECK (weight > 0),
    owner TEXT NOT NULL,
    due_on DATE,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','achieved','missed','cancelled')),
    version INT NOT NULL DEFAULT 1 CHECK (version > 0),
    last_evidence JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, key_result_id),
    FOREIGN KEY (tenant_id, objective_id)
        REFERENCES strategic_objectives(tenant_id, objective_id),
    CHECK ((direction='increase' AND target > baseline) OR
           (direction='decrease' AND target < baseline) OR
           (direction='binary' AND baseline=0 AND target=1 AND current_value IN (0,1)))
);
CREATE INDEX IF NOT EXISTS objective_key_results_objective_idx
    ON objective_key_results (tenant_id, objective_id);

CREATE TABLE IF NOT EXISTS objective_measurements (
    measurement_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    key_result_id TEXT NOT NULL,
    previous_value NUMERIC NOT NULL,
    measured_value NUMERIC NOT NULL,
    evidence JSONB NOT NULL,
    measured_by TEXT NOT NULL,
    measured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, key_result_id)
        REFERENCES objective_key_results(tenant_id, key_result_id),
    CHECK (evidence <> '{}'::jsonb)
);
CREATE INDEX IF NOT EXISTS objective_measurements_kr_idx
    ON objective_measurements (tenant_id, key_result_id, measurement_id DESC);

CREATE TABLE IF NOT EXISTS objective_dependencies (
    dependency_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    objective_id TEXT NOT NULL,
    depends_on_objective_id TEXT NOT NULL,
    required_condition TEXT NOT NULL,
    dependency_owner TEXT NOT NULL,
    criticality TEXT NOT NULL DEFAULT 'blocking'
        CHECK (criticality IN ('informational','important','blocking')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','satisfied','failed','waived')),
    evidence JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, objective_id, depends_on_objective_id),
    FOREIGN KEY (tenant_id, objective_id)
        REFERENCES strategic_objectives(tenant_id, objective_id),
    FOREIGN KEY (tenant_id, depends_on_objective_id)
        REFERENCES strategic_objectives(tenant_id, objective_id),
    CHECK (objective_id <> depends_on_objective_id)
);

CREATE TABLE IF NOT EXISTS objective_tradeoffs (
    tradeoff_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    portfolio_id TEXT NOT NULL,
    objective_id TEXT,
    chosen_option TEXT NOT NULL,
    forgone_options JSONB NOT NULL,
    rationale TEXT NOT NULL,
    assumptions JSONB NOT NULL DEFAULT '[]',
    decided_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'decided'
        CHECK (status IN ('decided','reopened','superseded')),
    revisit_trigger TEXT NOT NULL,
    supersedes_tradeoff_id TEXT,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, portfolio_id)
        REFERENCES objective_portfolios(tenant_id, portfolio_id),
    FOREIGN KEY (tenant_id, objective_id)
        REFERENCES strategic_objectives(tenant_id, objective_id),
    FOREIGN KEY (supersedes_tradeoff_id) REFERENCES objective_tradeoffs(tradeoff_id),
    CHECK (jsonb_typeof(forgone_options)='array' AND jsonb_array_length(forgone_options)>0)
);
CREATE INDEX IF NOT EXISTS objective_tradeoffs_tenant_idx
    ON objective_tradeoffs (tenant_id, portfolio_id, decided_at DESC);

CREATE TABLE IF NOT EXISTS objective_plan_revisions (
    plan_revision_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    portfolio_id TEXT NOT NULL,
    revision INT NOT NULL,
    change_kind TEXT NOT NULL,
    rationale TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    previous_snapshot JSONB NOT NULL,
    resulting_snapshot JSONB NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, portfolio_id, revision),
    FOREIGN KEY (tenant_id, portfolio_id)
        REFERENCES objective_portfolios(tenant_id, portfolio_id)
);

CREATE TABLE IF NOT EXISTS objective_state_changes (
    state_change_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    objective_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    rationale TEXT NOT NULL,
    evidence JSONB NOT NULL,
    changed_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, objective_id)
        REFERENCES strategic_objectives(tenant_id, objective_id),
    CHECK (from_state <> to_state),
    CHECK (evidence <> '{}'::jsonb)
);
CREATE INDEX IF NOT EXISTS objective_state_changes_idx
    ON objective_state_changes (tenant_id, objective_id, state_change_id DESC);

DO $$
DECLARE
    tbl text;
    seq_name text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentos_app') THEN
        FOREACH tbl IN ARRAY ARRAY[
            'objective_portfolios','strategic_objectives','objective_key_results',
            'objective_measurements','objective_dependencies','objective_tradeoffs',
            'objective_plan_revisions','objective_state_changes'
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
                CASE WHEN tbl='objective_measurements' THEN 'measurement_id'
                     WHEN tbl='objective_plan_revisions' THEN 'plan_revision_id'
                     WHEN tbl='objective_state_changes' THEN 'state_change_id' END);
            IF seq_name IS NOT NULL THEN
                EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %s TO agentos_app', seq_name);
            END IF;
        END LOOP;
    END IF;
END $$;
