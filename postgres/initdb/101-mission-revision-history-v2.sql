-- Immutable mission revisions. Authority records bind one revision in their
-- canonical JSON, so a material intent change fences prior delegation.

CREATE TABLE IF NOT EXISTS aos_v2_mission_revisions (
    tenant_id text NOT NULL,
    mission_id text NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    spec jsonb NOT NULL,
    revised_by text NOT NULL,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, mission_id, revision),
    FOREIGN KEY (tenant_id, mission_id)
        REFERENCES aos_v2_missions (tenant_id, mission_id) ON DELETE CASCADE,
    CHECK (spec->>'tenant_id' = tenant_id),
    CHECK (spec->>'mission_id' = mission_id),
    CHECK ((spec->>'revision')::integer = revision)
);

INSERT INTO aos_v2_mission_revisions (
    tenant_id, mission_id, revision, fingerprint, spec, revised_by, reason, created_at
)
SELECT
    tenant_id,
    mission_id,
    revision,
    fingerprint,
    spec,
    COALESCE(NULLIF(spec->>'principal_id', ''), 'system:migration'),
    'backfilled current mission revision',
    created_at
FROM aos_v2_missions
ON CONFLICT (tenant_id, mission_id, revision) DO NOTHING;

ALTER TABLE aos_v2_mission_authorities
    DROP CONSTRAINT IF EXISTS aos_v2_mission_authorities_revision_valid;
ALTER TABLE aos_v2_mission_authorities
    ADD CONSTRAINT aos_v2_mission_authorities_revision_valid
    CHECK (COALESCE((grant_record->>'mission_revision')::integer, 1) > 0) NOT VALID;
ALTER TABLE aos_v2_mission_authorities
    VALIDATE CONSTRAINT aos_v2_mission_authorities_revision_valid;

CREATE INDEX IF NOT EXISTS aos_v2_mission_revisions_recorded_idx
    ON aos_v2_mission_revisions (tenant_id, mission_id, created_at DESC);

ALTER TABLE aos_v2_mission_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE aos_v2_mission_revisions FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE aos_v2_mission_revisions FROM agentos_app;
GRANT SELECT, INSERT ON TABLE aos_v2_mission_revisions TO agentos_app;
DROP POLICY IF EXISTS aos_v2_mission_revisions_tenant_guc ON aos_v2_mission_revisions;
CREATE POLICY aos_v2_mission_revisions_tenant_guc
    ON aos_v2_mission_revisions
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
