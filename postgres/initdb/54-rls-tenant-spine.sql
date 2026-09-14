-- 54-rls-tenant-spine.sql - nullable tenant spine for parent-scoped evidence tables.
--
-- This is still preparation only: it does NOT enable RLS. It adds tenant_id where tenant ownership can be
-- derived mechanically from existing parent rows, backfills current rows, indexes the spine, and installs
-- lightweight triggers so new writes stay populated while Python callers are migrated incrementally.

CREATE OR REPLACE FUNCTION aos_set_tenant_from_product() RETURNS trigger AS $$
DECLARE
    prod text;
BEGIN
    prod := to_jsonb(NEW)->>TG_ARGV[0];
    IF NEW.tenant_id IS NULL AND prod IS NOT NULL AND prod <> '' THEN
        SELECT tenant_id INTO NEW.tenant_id FROM tenant_products WHERE product = prod LIMIT 1;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION aos_set_tenant_from_org() RETURNS trigger AS $$
DECLARE
    oid bigint;
    raw text;
BEGIN
    raw := NULLIF(to_jsonb(NEW)->>TG_ARGV[0], '');
    IF NEW.tenant_id IS NULL AND raw ~ '^[0-9]+$' THEN
        oid := raw::bigint;
        SELECT tenant_id INTO NEW.tenant_id FROM orgs WHERE id = oid LIMIT 1;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION aos_set_tenant_from_product_or_org() RETURNS trigger AS $$
DECLARE
    prod text;
    oid bigint;
    raw text;
BEGIN
    prod := to_jsonb(NEW)->>TG_ARGV[0];
    IF NEW.tenant_id IS NULL AND prod IS NOT NULL AND prod <> '' THEN
        SELECT tenant_id INTO NEW.tenant_id FROM tenant_products WHERE product = prod LIMIT 1;
    END IF;
    IF NEW.tenant_id IS NULL THEN
        raw := NULLIF(to_jsonb(NEW)->>TG_ARGV[1], '');
        IF raw ~ '^[0-9]+$' THEN
            oid := raw::bigint;
            SELECT tenant_id INTO NEW.tenant_id FROM orgs WHERE id = oid LIMIT 1;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION aos_set_tenant_from_research_run() RETURNS trigger AS $$
BEGIN
    IF NEW.tenant_id IS NULL AND NEW.run_id IS NOT NULL THEN
        SELECT tenant_id INTO NEW.tenant_id FROM research_runs WHERE id = NEW.run_id LIMIT 1;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION aos_set_tenant_from_quality_run() RETURNS trigger AS $$
BEGIN
    IF NEW.tenant_id IS NULL AND NEW.run_id IS NOT NULL THEN
        SELECT tenant_id INTO NEW.tenant_id FROM quality_runs WHERE id = NEW.run_id LIMIT 1;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION aos_set_tenant_from_finding() RETURNS trigger AS $$
BEGIN
    IF NEW.tenant_id IS NULL AND NEW.finding_id IS NOT NULL THEN
        SELECT tenant_id INTO NEW.tenant_id FROM findings WHERE id = NEW.finding_id LIMIT 1;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF to_regclass('public.app_registry') IS NOT NULL THEN
        ALTER TABLE app_registry ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE app_registry a SET tenant_id = tp.tenant_id
          FROM tenant_products tp WHERE a.tenant_id IS NULL AND tp.product = a.name;
        CREATE INDEX IF NOT EXISTS app_registry_tenant_id_rls_idx ON app_registry (tenant_id);
        DROP TRIGGER IF EXISTS app_registry_tenant_spine ON app_registry;
        CREATE TRIGGER app_registry_tenant_spine BEFORE INSERT OR UPDATE OF name, tenant_id ON app_registry
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_product('name');
    END IF;

    IF to_regclass('public.build_outcomes') IS NOT NULL THEN
        ALTER TABLE build_outcomes ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE build_outcomes b SET tenant_id = tp.tenant_id
          FROM tenant_products tp WHERE b.tenant_id IS NULL AND tp.product = b.product;
        CREATE INDEX IF NOT EXISTS build_outcomes_tenant_id_rls_idx ON build_outcomes (tenant_id);
        DROP TRIGGER IF EXISTS build_outcomes_tenant_spine ON build_outcomes;
        CREATE TRIGGER build_outcomes_tenant_spine BEFORE INSERT OR UPDATE OF product, tenant_id ON build_outcomes
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_product('product');
    END IF;

    IF to_regclass('public.directory') IS NOT NULL THEN
        ALTER TABLE directory ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE directory d SET tenant_id = tp.tenant_id
          FROM tenant_products tp WHERE d.tenant_id IS NULL AND tp.product = d.product;
        CREATE INDEX IF NOT EXISTS directory_tenant_id_rls_idx ON directory (tenant_id);
        DROP TRIGGER IF EXISTS directory_tenant_spine ON directory;
        CREATE TRIGGER directory_tenant_spine BEFORE INSERT OR UPDATE OF product, tenant_id ON directory
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_product('product');
    END IF;

    IF to_regclass('public.findings') IS NOT NULL THEN
        ALTER TABLE findings ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE findings f SET tenant_id = o.tenant_id
          FROM orgs o WHERE f.tenant_id IS NULL AND o.id = f.org_id;
        CREATE INDEX IF NOT EXISTS findings_tenant_id_rls_idx ON findings (tenant_id);
        DROP TRIGGER IF EXISTS findings_tenant_spine ON findings;
        CREATE TRIGGER findings_tenant_spine BEFORE INSERT OR UPDATE OF org_id, tenant_id ON findings
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_org('org_id');
    END IF;

    IF to_regclass('public.org_artifacts') IS NOT NULL THEN
        ALTER TABLE org_artifacts ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE org_artifacts a SET tenant_id = o.tenant_id
          FROM orgs o WHERE a.tenant_id IS NULL AND o.id = a.org_id;
        CREATE INDEX IF NOT EXISTS org_artifacts_tenant_id_rls_idx ON org_artifacts (tenant_id);
        DROP TRIGGER IF EXISTS org_artifacts_tenant_spine ON org_artifacts;
        CREATE TRIGGER org_artifacts_tenant_spine BEFORE INSERT OR UPDATE OF org_id, tenant_id ON org_artifacts
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_org('org_id');
    END IF;

    IF to_regclass('public.org_lineage') IS NOT NULL THEN
        ALTER TABLE org_lineage ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE org_lineage l SET tenant_id = o.tenant_id
          FROM orgs o WHERE l.tenant_id IS NULL AND (o.id = l.org_id OR o.id = l.derived_from);
        CREATE INDEX IF NOT EXISTS org_lineage_tenant_id_rls_idx ON org_lineage (tenant_id);
        DROP TRIGGER IF EXISTS org_lineage_tenant_spine ON org_lineage;
        CREATE TRIGGER org_lineage_tenant_spine BEFORE INSERT OR UPDATE OF org_id, derived_from, tenant_id ON org_lineage
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_org('org_id');
    END IF;

    IF to_regclass('public.org_metrics') IS NOT NULL THEN
        ALTER TABLE org_metrics ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE org_metrics m SET tenant_id = tp.tenant_id
          FROM tenant_products tp WHERE m.tenant_id IS NULL AND tp.product = m.product;
        CREATE INDEX IF NOT EXISTS org_metrics_tenant_id_rls_idx ON org_metrics (tenant_id);
        DROP TRIGGER IF EXISTS org_metrics_tenant_spine ON org_metrics;
        CREATE TRIGGER org_metrics_tenant_spine BEFORE INSERT OR UPDATE OF product, tenant_id ON org_metrics
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_product('product');
    END IF;

    IF to_regclass('public.qa_runs') IS NOT NULL THEN
        ALTER TABLE qa_runs ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE qa_runs q SET tenant_id = tp.tenant_id
          FROM tenant_products tp WHERE q.tenant_id IS NULL AND tp.product = q.product;
        CREATE INDEX IF NOT EXISTS qa_runs_tenant_id_rls_idx ON qa_runs (tenant_id);
        DROP TRIGGER IF EXISTS qa_runs_tenant_spine ON qa_runs;
        CREATE TRIGGER qa_runs_tenant_spine BEFORE INSERT OR UPDATE OF product, tenant_id ON qa_runs
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_product('product');
    END IF;

    IF to_regclass('public.quality_runs') IS NOT NULL THEN
        ALTER TABLE quality_runs ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE quality_runs q SET tenant_id = tp.tenant_id
          FROM tenant_products tp WHERE q.tenant_id IS NULL AND tp.product = q.product;
        UPDATE quality_runs q SET tenant_id = o.tenant_id
          FROM orgs o WHERE q.tenant_id IS NULL AND q.org_id ~ '^[0-9]+$' AND o.id = q.org_id::bigint;
        CREATE INDEX IF NOT EXISTS quality_runs_tenant_id_rls_idx ON quality_runs (tenant_id);
        DROP TRIGGER IF EXISTS quality_runs_tenant_spine ON quality_runs;
        CREATE TRIGGER quality_runs_tenant_spine BEFORE INSERT OR UPDATE OF product, org_id, tenant_id ON quality_runs
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_product_or_org('product', 'org_id');
    END IF;

    IF to_regclass('public.story_corpus') IS NOT NULL THEN
        ALTER TABLE story_corpus ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE story_corpus s SET tenant_id = tp.tenant_id
          FROM tenant_products tp WHERE s.tenant_id IS NULL AND tp.product = s.product;
        CREATE INDEX IF NOT EXISTS story_corpus_tenant_id_rls_idx ON story_corpus (tenant_id);
        DROP TRIGGER IF EXISTS story_corpus_tenant_spine ON story_corpus;
        CREATE TRIGGER story_corpus_tenant_spine BEFORE INSERT OR UPDATE OF product, tenant_id ON story_corpus
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_product('product');
    END IF;

    IF to_regclass('public.traces') IS NOT NULL THEN
        ALTER TABLE traces ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE traces t SET tenant_id = tp.tenant_id
          FROM tenant_products tp WHERE t.tenant_id IS NULL AND tp.product = t.product;
        CREATE INDEX IF NOT EXISTS traces_tenant_id_rls_idx ON traces (tenant_id);
        DROP TRIGGER IF EXISTS traces_tenant_spine ON traces;
        CREATE TRIGGER traces_tenant_spine BEFORE INSERT OR UPDATE OF product, tenant_id ON traces
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_product('product');
    END IF;

    IF to_regclass('public.research_options') IS NOT NULL THEN
        ALTER TABLE research_options ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE research_options o SET tenant_id = r.tenant_id
          FROM research_runs r WHERE o.tenant_id IS NULL AND r.id = o.run_id;
        CREATE INDEX IF NOT EXISTS research_options_tenant_id_rls_idx ON research_options (tenant_id);
        DROP TRIGGER IF EXISTS research_options_tenant_spine ON research_options;
        CREATE TRIGGER research_options_tenant_spine BEFORE INSERT OR UPDATE OF run_id, tenant_id ON research_options
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_research_run();
    END IF;

    IF to_regclass('public.quality_measurements') IS NOT NULL THEN
        ALTER TABLE quality_measurements ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE quality_measurements m SET tenant_id = q.tenant_id
          FROM quality_runs q WHERE m.tenant_id IS NULL AND q.id = m.run_id;
        CREATE INDEX IF NOT EXISTS quality_measurements_tenant_id_rls_idx ON quality_measurements (tenant_id);
        DROP TRIGGER IF EXISTS quality_measurements_tenant_spine ON quality_measurements;
        CREATE TRIGGER quality_measurements_tenant_spine BEFORE INSERT OR UPDATE OF run_id, tenant_id ON quality_measurements
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_quality_run();
    END IF;

    IF to_regclass('public.finding_verifications') IS NOT NULL THEN
        ALTER TABLE finding_verifications ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE finding_verifications v SET tenant_id = f.tenant_id
          FROM findings f WHERE v.tenant_id IS NULL AND f.id = v.finding_id;
        CREATE INDEX IF NOT EXISTS finding_verifications_tenant_id_rls_idx ON finding_verifications (tenant_id);
        DROP TRIGGER IF EXISTS finding_verifications_tenant_spine ON finding_verifications;
        CREATE TRIGGER finding_verifications_tenant_spine BEFORE INSERT OR UPDATE OF finding_id, tenant_id ON finding_verifications
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_finding();
    END IF;
END $$;
