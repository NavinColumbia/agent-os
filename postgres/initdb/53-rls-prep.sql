-- 53-rls-prep.sql - low-risk preparation for DB-enforced tenant isolation.
--
-- RLS policies must use tenant_id (or an audited legacy tenant alias) without turning every tenant-facing
-- query into a table scan. This creates tenant indexes on every existing tenant-scoped table. It does NOT
-- enable RLS; that requires a separate app-role/GUC rollout so current owner/admin connections do not
-- accidentally bypass or break policies.
DO $$
DECLARE
    r record;
    idx text;
BEGIN
    FOR r IN
        SELECT table_schema, table_name
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND column_name = 'tenant_id'
         GROUP BY table_schema, table_name
    LOOP
        idx := r.table_name || '_tenant_id_rls_idx';
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.%I (tenant_id)', idx, r.table_schema, r.table_name);
    END LOOP;
END $$;

-- Legacy CEO task board rows are tenant-owned through task_board.tenant. New tenant-facing tables should
-- use tenant_id, but this alias is indexed so the generated RLS policy can be applied safely.
CREATE INDEX IF NOT EXISTS task_board_tenant_rls_idx ON task_board (tenant);
