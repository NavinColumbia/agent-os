-- Durable objective ownership and acknowledged delegation.
--
-- A conversation message is not an assignment.  These tables make the human-org
-- invariant explicit: one accountable owner keeps the outcome until a proposed
-- successor affirmatively accepts the complete work contract.  Managers receive
-- evidence about silence, missing coverage and rejected/overdue handoffs; they
-- still make the operational decision agentically.

CREATE TABLE IF NOT EXISTS work_contracts (
    contract_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    org_id TEXT,
    parent_contract_id TEXT,
    objective TEXT NOT NULL,
    acceptance_contract JSONB NOT NULL,
    constraints JSONB NOT NULL DEFAULT '{}',
    dependencies JSONB NOT NULL DEFAULT '[]',
    accountable_owner TEXT NOT NULL,
    manager_owner TEXT NOT NULL,
    backup_owner TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','blocked','completed','cancelled')),
    priority INT NOT NULL DEFAULT 3 CHECK (priority BETWEEN 0 AND 5),
    risk TEXT NOT NULL DEFAULT 'medium'
        CHECK (risk IN ('low','medium','high','critical')),
    capacity_units NUMERIC(8,2) NOT NULL DEFAULT 1 CHECK (capacity_units > 0),
    update_cadence_s INT NOT NULL DEFAULT 900 CHECK (update_cadence_s >= 30),
    last_substantive_update_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_checkin_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ownership_revision INT NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS work_contracts_owner_idx
    ON work_contracts (tenant_id, accountable_owner, status);
CREATE INDEX IF NOT EXISTS work_contracts_checkin_idx
    ON work_contracts (next_checkin_at) WHERE status IN ('active','blocked');

CREATE TABLE IF NOT EXISTS work_delegations (
    delegation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    contract_id TEXT NOT NULL REFERENCES work_contracts(contract_id),
    from_owner TEXT NOT NULL,
    proposed_owner TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    brief JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'offered'
        CHECK (status IN ('offered','accepted','rejected','countered','withdrawn','expired')),
    reply_due_at TIMESTAMPTZ NOT NULL,
    response JSONB,
    ownership_revision INT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    responded_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS work_delegations_one_offer_idx
    ON work_delegations (contract_id) WHERE status='offered';
CREATE INDEX IF NOT EXISTS work_delegations_due_idx
    ON work_delegations (reply_due_at) WHERE status='offered';
CREATE INDEX IF NOT EXISTS work_delegations_tenant_idx
    ON work_delegations (tenant_id, contract_id, status);

CREATE TABLE IF NOT EXISTS work_contract_updates (
    update_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    contract_id TEXT NOT NULL REFERENCES work_contracts(contract_id),
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    substantive BOOLEAN NOT NULL DEFAULT false,
    evidence JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS work_contract_updates_contract_idx
    ON work_contract_updates (contract_id, update_id DESC);
CREATE INDEX IF NOT EXISTS work_contract_updates_tenant_idx
    ON work_contract_updates (tenant_id, contract_id, update_id DESC);

DO $$
DECLARE
    tbl text;
    seq_name text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentos_app') THEN
        FOREACH tbl IN ARRAY ARRAY['work_contracts','work_delegations','work_contract_updates']
        LOOP
            EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tbl);
            EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', tbl);
            EXECUTE format('REVOKE ALL ON TABLE public.%I FROM agentos_app', tbl);
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO agentos_app', tbl);
            seq_name := NULL;
            IF tbl = 'work_contract_updates' THEN
                SELECT pg_get_serial_sequence(format('public.%I', tbl), 'update_id') INTO seq_name;
            END IF;
            IF seq_name IS NOT NULL THEN
                EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %s TO agentos_app', seq_name);
            END IF;
            EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_tenant_guc', tbl);
            EXECUTE format(
                'CREATE POLICY %I ON public.%I FOR ALL TO agentos_app ' ||
                'USING (tenant_id = current_setting(''app.tenant_id'', true)) ' ||
                'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))',
                tbl || '_tenant_guc', tbl);
        END LOOP;
    END IF;
END $$;
