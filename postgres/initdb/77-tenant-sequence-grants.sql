-- Repair/continuation migration for RLS tenant-role sequence privileges.
--
-- Migration 56 originally granted a sequence only when the tenant table's
-- generated column was literally named `id`.  INSERTs through agentos_app
-- therefore failed for valid tenant tables using run_id, actor_id, or turn.
-- Discover ownership from pg_depend so future non-id identity columns are
-- covered automatically.  Special audit/role-lessons grants remain governed
-- by their narrower policies in migration 56.

DO $$
DECLARE
    row record;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentos_app') THEN
        RAISE EXCEPTION 'agentos_app role is missing; apply migration 56 first';
    END IF;

    FOR row IN
        SELECT DISTINCT sn.nspname AS schema_name, s.relname AS sequence_name
          FROM pg_class s
          JOIN pg_namespace sn ON sn.oid=s.relnamespace
          JOIN pg_depend d ON d.objid=s.oid AND d.deptype IN ('a','i')
          JOIN pg_class t ON t.oid=d.refobjid AND t.relkind IN ('r','p')
          JOIN pg_namespace tn ON tn.oid=t.relnamespace
         WHERE s.relkind='S'
           AND sn.nspname='public'
           AND tn.nspname='public'
           AND t.relname NOT IN ('audit_log','role_lessons')
           AND (
                EXISTS (SELECT 1 FROM pg_attribute a
                         WHERE a.attrelid=t.oid AND a.attname='tenant_id'
                           AND a.attnum>0 AND NOT a.attisdropped)
                OR t.relname='task_board'
           )
    LOOP
        EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %I.%I TO agentos_app',
                       row.schema_name, row.sequence_name);
    END LOOP;
END $$;
