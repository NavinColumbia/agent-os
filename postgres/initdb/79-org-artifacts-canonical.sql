-- Canonical org artifact ownership. Historically org_artifacts was created lazily
-- inside orgs.record_artifact(), which meant a tenant/app-role write attempted DDL
-- and a fresh install could create the table after the generic RLS migration.

CREATE TABLE IF NOT EXISTS org_artifacts (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT,
    org_id      BIGINT NOT NULL,
    kind        TEXT NOT NULL,
    product     TEXT,
    path        TEXT,
    ref         TEXT,
    summary     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE org_artifacts ADD COLUMN IF NOT EXISTS tenant_id TEXT;
UPDATE org_artifacts a
   SET tenant_id=o.tenant_id
  FROM orgs o
 WHERE a.org_id=o.id AND a.tenant_id IS NULL;

-- Legacy selftests and deleted organizations left artifact rows with neither a
-- tenant nor a surviving parent. They cannot be assigned without fabricating
-- ownership. Preserve their complete row as owner-only forensic evidence, then
-- remove them from the operational tenant relation.
CREATE TABLE IF NOT EXISTS orphaned_org_artifacts_archive (
    original_id     BIGINT PRIMARY KEY,
    payload         JSONB NOT NULL,
    archived_reason TEXT NOT NULL,
    archived_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO orphaned_org_artifacts_archive(original_id,payload,archived_reason)
SELECT a.id,to_jsonb(a),'missing parent organization; tenant ownership not recoverable'
  FROM org_artifacts a
 WHERE a.tenant_id IS NULL
   AND NOT EXISTS (SELECT 1 FROM orgs o WHERE o.id=a.org_id)
ON CONFLICT(original_id) DO NOTHING;
DELETE FROM org_artifacts a
 WHERE a.tenant_id IS NULL
   AND NOT EXISTS (SELECT 1 FROM orgs o WHERE o.id=a.org_id);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM org_artifacts WHERE tenant_id IS NULL) THEN
        RAISE EXCEPTION 'org_artifacts contains rows without resolvable tenant ownership';
    END IF;
END $$;

ALTER TABLE org_artifacts ALTER COLUMN tenant_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS org_artifacts_tenant_id_rls_idx
    ON org_artifacts (tenant_id);

DROP TRIGGER IF EXISTS org_artifacts_tenant_spine ON org_artifacts;
CREATE TRIGGER org_artifacts_tenant_spine
BEFORE INSERT OR UPDATE OF org_id,tenant_id ON org_artifacts
FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_org('org_id');

ALTER TABLE org_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE org_artifacts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS org_artifacts_tenant_isolation ON org_artifacts;
CREATE POLICY org_artifacts_tenant_isolation ON org_artifacts
    USING (tenant_id=current_setting('app.tenant_id',true))
    WITH CHECK (tenant_id=current_setting('app.tenant_id',true));

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentos_app') THEN
        GRANT SELECT,INSERT,UPDATE,DELETE ON org_artifacts TO agentos_app;
        GRANT USAGE,SELECT ON SEQUENCE org_artifacts_id_seq TO agentos_app;
        REVOKE ALL ON TABLE orphaned_org_artifacts_archive FROM agentos_app;
    END IF;
END $$;
