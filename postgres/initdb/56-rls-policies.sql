-- 56-rls-policies.sql - reviewed DB-enforced tenant isolation policy set.
--
-- This is the actual RLS policy migration shape after the app-role/GUC conversion work. It creates narrow
-- NOLOGIN group roles, enables+forces RLS on tenant-owned tables, grants the tenant app role table access
-- constrained by policies, and handles the two reviewed special cases:
--   * audit_log: tenant app reads only its tenant rows; a separate writer can read global tail + append.
--   * role_lessons: tenant app reads shared fleet lessons (tenant_id NULL) plus its own tenant rows, inserts
--     only its own tenant rows, and can update only the uses counter.
--
-- Do not use owner/superuser application connections for tenant request paths after applying this. Tenant
-- request transactions must run as, or inherit, agentos_app and set:
--   SELECT set_config('app.tenant_id', '<tenant>', true);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentos_app') THEN
        CREATE ROLE agentos_app NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentos_audit_writer') THEN
        CREATE ROLE agentos_audit_writer NOLOGIN;
    END IF;
END $$;

DO $$
DECLARE
    r record;
    tenant_col text;
    platform_exempt text[] := ARRAY[
        'agent_alerts', 'app_policies', 'blobs', 'browser_slots', 'budgets', 'claude_slots',
        'dbpool_selftest', 'email_codes', 'experiments', 'flags', 'heartbeats', 'kill_switch',
        'mem_edges', 'memories', 'proactive_sweep_state', 'scheduler_runs', 'schedules', 'sentinel_state',
        'skills', 'stripe_events', 'task_checkpoints', 'watchdog_alerts'
    ];
    special_tables text[] := ARRAY['audit_log', 'role_lessons'];
    seq_name text;
    seq_row record;
BEGIN
    FOR r IN
        SELECT c.relname AS table_name
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relkind = 'r'
           AND c.relname <> ALL(platform_exempt)
           AND c.relname <> ALL(special_tables)
           AND (
                 EXISTS (
                     SELECT 1 FROM pg_attribute a
                      WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND a.attnum > 0
                 )
                 OR c.relname = 'task_board'
               )
         ORDER BY c.relname
    LOOP
        tenant_col := CASE WHEN r.table_name = 'task_board' THEN 'tenant' ELSE 'tenant_id' END;

        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', r.table_name);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', r.table_name);
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM agentos_app', r.table_name);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO agentos_app', r.table_name);

        IF EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = r.table_name AND column_name = 'id'
        ) THEN
            SELECT pg_get_serial_sequence(format('public.%I', r.table_name), 'id') INTO seq_name;
            IF seq_name IS NOT NULL THEN
                EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %s TO agentos_app', seq_name);
            END IF;
        END IF;

        -- Serial/identity columns are not required to be named `id`.  The
        -- orchestra spine uses run_id/actor_id and conversations uses turn;
        -- table DML grants without the owned-sequence grants make the RLS
        -- role fail only at runtime on INSERT.  Grant every sequence owned by
        -- this tenant table, not merely pg_get_serial_sequence(table,'id').
        FOR seq_row IN
            SELECT sn.nspname AS schema_name, s.relname AS sequence_name
              FROM pg_class s
              JOIN pg_namespace sn ON sn.oid=s.relnamespace
              JOIN pg_depend d ON d.objid=s.oid AND d.deptype IN ('a','i')
             WHERE s.relkind='S' AND d.refobjid=format('public.%I', r.table_name)::regclass
        LOOP
            EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %I.%I TO agentos_app',
                           seq_row.schema_name, seq_row.sequence_name);
        END LOOP;

        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', r.table_name || '_tenant_guc', r.table_name);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL TO agentos_app ' ||
            'USING (%I = current_setting(''app.tenant_id'', true)) ' ||
            'WITH CHECK (%I = current_setting(''app.tenant_id'', true))',
            r.table_name || '_tenant_guc', r.table_name, tenant_col, tenant_col
        );
    END LOOP;
END $$;

DO $$
BEGIN
    IF to_regclass('public.audit_log') IS NOT NULL THEN
        ALTER TABLE public.audit_log ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.audit_log FORCE ROW LEVEL SECURITY;
        REVOKE ALL ON TABLE public.audit_log FROM agentos_app;
        REVOKE ALL ON TABLE public.audit_log FROM agentos_audit_writer;
        GRANT SELECT ON TABLE public.audit_log TO agentos_app;
        GRANT SELECT, INSERT ON TABLE public.audit_log TO agentos_audit_writer;
        IF to_regclass('public.audit_log_id_seq') IS NOT NULL THEN
            GRANT USAGE, SELECT ON SEQUENCE public.audit_log_id_seq TO agentos_audit_writer;
        END IF;

        DROP POLICY IF EXISTS audit_log_tenant_read ON public.audit_log;
        CREATE POLICY audit_log_tenant_read ON public.audit_log
          FOR SELECT TO agentos_app
          USING (tenant_id = current_setting('app.tenant_id', true));

        DROP POLICY IF EXISTS audit_log_writer_append ON public.audit_log;
        CREATE POLICY audit_log_writer_append ON public.audit_log
          FOR ALL TO agentos_audit_writer
          USING (true)
          WITH CHECK (true);
    END IF;

    IF to_regclass('public.role_lessons') IS NOT NULL THEN
        ALTER TABLE public.role_lessons ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.role_lessons FORCE ROW LEVEL SECURITY;
        REVOKE ALL ON TABLE public.role_lessons FROM agentos_app;
        GRANT SELECT, INSERT ON TABLE public.role_lessons TO agentos_app;
        GRANT UPDATE (uses) ON TABLE public.role_lessons TO agentos_app;
        IF to_regclass('public.role_lessons_id_seq') IS NOT NULL THEN
            GRANT USAGE, SELECT ON SEQUENCE public.role_lessons_id_seq TO agentos_app;
        END IF;

        DROP POLICY IF EXISTS role_lessons_shared_read ON public.role_lessons;
        CREATE POLICY role_lessons_shared_read ON public.role_lessons
          FOR SELECT TO agentos_app
          USING (tenant_id = current_setting('app.tenant_id', true) OR tenant_id IS NULL);

        DROP POLICY IF EXISTS role_lessons_tenant_insert ON public.role_lessons;
        CREATE POLICY role_lessons_tenant_insert ON public.role_lessons
          FOR INSERT TO agentos_app
          WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

        DROP POLICY IF EXISTS role_lessons_use_increment ON public.role_lessons;
        CREATE POLICY role_lessons_use_increment ON public.role_lessons
          FOR UPDATE TO agentos_app
          USING (tenant_id = current_setting('app.tenant_id', true) OR tenant_id IS NULL)
          WITH CHECK (tenant_id = current_setting('app.tenant_id', true) OR tenant_id IS NULL);
    END IF;
END $$;
