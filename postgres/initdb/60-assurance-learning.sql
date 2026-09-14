-- Independent assurance and blameless organizational learning.
--
-- Assurance is deliberately separate from execution and line management.  A
-- terminal verdict is impossible without both criterion results and durable
-- evidence.  Learning records system conditions and owned countermeasures; the
-- coaching tables intentionally contain no score/rank/compensation fields.

CREATE TABLE IF NOT EXISTS assurance_reviews (
    review_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    work_contract_id TEXT REFERENCES work_contracts(contract_id),
    executor_id TEXT NOT NULL,
    manager_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    acceptance_contract JSONB NOT NULL,
    independence_basis JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'awaiting_evidence'
        CHECK (status IN ('awaiting_evidence','ready','accepted','rejected','changes_requested','withdrawn')),
    submitted_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at TIMESTAMPTZ,
    CHECK (executor_id <> manager_id),
    CHECK (reviewer_id <> executor_id AND reviewer_id <> manager_id),
    CHECK ((independence_basis @> '{"separate_reporting_line":true}'::jsonb
         OR independence_basis @> '{"external_assurance":true}'::jsonb
         OR independence_basis @> '{"cross_functional_mandate":true}'::jsonb)
       AND NOT independence_basis @> '{"reports_to_executor":true}'::jsonb
       AND NOT independence_basis @> '{"review_incentive_owned_by_executor":true}'::jsonb)
);
CREATE INDEX IF NOT EXISTS assurance_reviews_subject_idx
    ON assurance_reviews (tenant_id, subject_type, subject_id);

CREATE TABLE IF NOT EXISTS assurance_evidence (
    evidence_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    review_id TEXT NOT NULL REFERENCES assurance_reviews(review_id),
    kind TEXT NOT NULL,
    uri TEXT NOT NULL,
    digest TEXT NOT NULL,
    claims JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    submitted_by TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (review_id, digest)
);

CREATE TABLE IF NOT EXISTS assurance_verdicts (
    verdict_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    review_id TEXT NOT NULL UNIQUE REFERENCES assurance_reviews(review_id),
    verdict TEXT NOT NULL CHECK (verdict IN ('accepted','rejected','changes_requested')),
    decided_by TEXT NOT NULL,
    rationale TEXT NOT NULL,
    criterion_results JSONB NOT NULL,
    evidence_ids JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS assurance_evidence_tenant_idx
    ON assurance_evidence (tenant_id, review_id);
CREATE INDEX IF NOT EXISTS assurance_verdicts_tenant_idx
    ON assurance_verdicts (tenant_id, review_id);

CREATE OR REPLACE FUNCTION enforce_assurance_verdict() RETURNS trigger AS $$
DECLARE assigned_reviewer text; review_tenant text; evidence_count int;
BEGIN
    SELECT reviewer_id, tenant_id INTO assigned_reviewer, review_tenant
      FROM assurance_reviews WHERE review_id=NEW.review_id FOR UPDATE;
    IF assigned_reviewer IS NULL OR assigned_reviewer <> NEW.decided_by THEN
        RAISE EXCEPTION 'only the independent assigned reviewer may decide';
    END IF;
    IF review_tenant <> NEW.tenant_id THEN RAISE EXCEPTION 'tenant mismatch'; END IF;
    IF jsonb_typeof(NEW.criterion_results) <> 'object' OR NEW.criterion_results = '{}'::jsonb
       OR jsonb_typeof(NEW.evidence_ids) <> 'array' OR jsonb_array_length(NEW.evidence_ids)=0 THEN
        RAISE EXCEPTION 'verdict requires criterion results and evidence ids';
    END IF;
    SELECT count(*) INTO evidence_count FROM assurance_evidence e
      WHERE e.review_id=NEW.review_id AND e.evidence_id IN
        (SELECT jsonb_array_elements_text(NEW.evidence_ids));
    IF evidence_count <> jsonb_array_length(NEW.evidence_ids) THEN
        RAISE EXCEPTION 'verdict cites missing or foreign evidence';
    END IF;
    IF NEW.verdict='accepted' AND EXISTS
       (SELECT 1 FROM jsonb_each(NEW.criterion_results) c
        WHERE c.value NOT IN ('true'::jsonb, '"pass"'::jsonb, '"met"'::jsonb)) THEN
        RAISE EXCEPTION 'acceptance requires every criterion to be met';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS assurance_verdict_guard ON assurance_verdicts;
CREATE TRIGGER assurance_verdict_guard BEFORE INSERT ON assurance_verdicts
    FOR EACH ROW EXECUTE FUNCTION enforce_assurance_verdict();

CREATE TABLE IF NOT EXISTS learning_incidents (
    incident_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    trigger_type TEXT NOT NULL CHECK (trigger_type IN
      ('assurance_rejection','repeat_failure','customer_impact','security','reliability','manual')),
    trigger_ref TEXT,
    severity TEXT NOT NULL CHECK (severity IN ('low','medium','high','critical')),
    summary TEXT NOT NULL,
    detected_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','learning','monitoring','closed')),
    recurrence_of TEXT REFERENCES learning_incidents(incident_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS learning_incidents_tenant_idx
    ON learning_incidents (tenant_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS postmortems (
    postmortem_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    incident_id TEXT NOT NULL UNIQUE REFERENCES learning_incidents(incident_id),
    facilitator_id TEXT NOT NULL,
    impact JSONB NOT NULL,
    timeline JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','review','closed')),
    blameless BOOLEAN NOT NULL DEFAULT true CHECK (blameless),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS postmortems_tenant_idx
    ON postmortems (tenant_id, incident_id);

CREATE TABLE IF NOT EXISTS learning_findings (
    finding_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    postmortem_id TEXT NOT NULL REFERENCES postmortems(postmortem_id),
    system_condition TEXT NOT NULL,
    contributing_factors JSONB NOT NULL,
    evidence_refs JSONB NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS learning_findings_tenant_idx
    ON learning_findings (tenant_id, postmortem_id);

CREATE TABLE IF NOT EXISTS corrective_actions (
    action_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    postmortem_id TEXT NOT NULL REFERENCES postmortems(postmortem_id),
    finding_id TEXT REFERENCES learning_findings(finding_id),
    action TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    verifier_id TEXT NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    success_measure JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','in_progress','implemented','verified','ineffective')),
    implementation_evidence JSONB NOT NULL DEFAULT '[]',
    verification_evidence JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at TIMESTAMPTZ,
    CHECK (owner_id <> verifier_id)
);
CREATE INDEX IF NOT EXISTS corrective_actions_tenant_idx
    ON corrective_actions (tenant_id, postmortem_id, status);

CREATE OR REPLACE FUNCTION enforce_corrective_verification() RETURNS trigger AS $$
BEGIN
    IF NEW.status='verified' AND OLD.status IS DISTINCT FROM 'verified' THEN
        IF current_setting('app.actor_id', true) IS DISTINCT FROM NEW.verifier_id THEN
            RAISE EXCEPTION 'only the independent verifier may verify a corrective action';
        END IF;
        IF jsonb_typeof(NEW.implementation_evidence) <> 'array'
           OR jsonb_array_length(NEW.implementation_evidence)=0
           OR jsonb_typeof(NEW.verification_evidence) <> 'array'
           OR jsonb_array_length(NEW.verification_evidence)=0 THEN
            RAISE EXCEPTION 'verification requires implementation and outcome evidence';
        END IF;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS corrective_verification_guard ON corrective_actions;
CREATE TRIGGER corrective_verification_guard BEFORE UPDATE ON corrective_actions
    FOR EACH ROW EXECUTE FUNCTION enforce_corrective_verification();

CREATE TABLE IF NOT EXISTS recurrence_checks (
    recurrence_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    incident_id TEXT NOT NULL REFERENCES learning_incidents(incident_id),
    prior_incident_id TEXT NOT NULL REFERENCES learning_incidents(incident_id),
    related_action_id TEXT REFERENCES corrective_actions(action_id),
    detected_by TEXT NOT NULL,
    evidence JSONB NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('new_pattern','same_pattern','countermeasure_failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (incident_id <> prior_incident_id)
);
CREATE INDEX IF NOT EXISTS recurrence_checks_tenant_idx
    ON recurrence_checks (tenant_id, incident_id, created_at DESC);

CREATE TABLE IF NOT EXISTS coaching_observations (
    observation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    observer_id TEXT NOT NULL,
    work_context TEXT NOT NULL,
    observed_behavior TEXT NOT NULL,
    impact TEXT NOT NULL,
    suggested_practice TEXT NOT NULL,
    evidence_refs JSONB NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'developmental' CHECK (purpose='developmental'),
    subject_visible BOOLEAN NOT NULL DEFAULT true CHECK (subject_visible),
    punitive_use BOOLEAN NOT NULL DEFAULT false CHECK (NOT punitive_use),
    status TEXT NOT NULL DEFAULT 'shared' CHECK (status IN ('shared','acknowledged','rebutted','expired')),
    rebuttal TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS coaching_observations_subject_idx
    ON coaching_observations (tenant_id, subject_id, expires_at);

DO $$
DECLARE tbl text; seq_name text;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentos_app') THEN
    FOREACH tbl IN ARRAY ARRAY['assurance_reviews','assurance_evidence','assurance_verdicts',
      'learning_incidents','postmortems','learning_findings','corrective_actions',
      'recurrence_checks','coaching_observations']
    LOOP
      EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tbl);
      EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', tbl);
      EXECUTE format('REVOKE ALL ON TABLE public.%I FROM agentos_app', tbl);
      EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO agentos_app', tbl);
      EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_tenant_guc', tbl);
      EXECUTE format('CREATE POLICY %I ON public.%I FOR ALL TO agentos_app USING '
        || '(tenant_id=current_setting(''app.tenant_id'',true)) WITH CHECK '
        || '(tenant_id=current_setting(''app.tenant_id'',true))', tbl || '_tenant_guc', tbl);
    END LOOP;
  END IF;
END $$;
