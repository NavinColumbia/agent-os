-- Cross-tenant queue discovery for the dedicated V2 worker role. The role can
-- see only scheduling columns; command/action payloads remain tenant-fenced.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentos_worker') THEN
        CREATE ROLE agentos_worker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END $$;

ALTER ROLE agentos_worker
    NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM pg_auth_members membership
          JOIN pg_roles granted_role ON granted_role.oid = membership.roleid
          JOIN pg_roles member_role ON member_role.oid = membership.member
         WHERE granted_role.rolname = 'agentos_app'
           AND member_role.rolname = 'agentos_worker'
    ) THEN
        REVOKE agentos_app FROM agentos_worker;
    END IF;
END $$;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM agentos_worker;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM agentos_worker;
REVOKE ALL ON TABLE aos_v2_lifecycle_commands FROM agentos_worker;
REVOKE ALL ON TABLE aos_v2_workflow_actions FROM agentos_worker;
GRANT SELECT (tenant_id, status, available_at, lease_expires_at)
    ON TABLE aos_v2_lifecycle_commands TO agentos_worker;
GRANT SELECT (tenant_id, status, available_at, lease_expires_at)
    ON TABLE aos_v2_workflow_actions TO agentos_worker;

DROP POLICY IF EXISTS aos_v2_lifecycle_commands_worker_discovery
    ON aos_v2_lifecycle_commands;
CREATE POLICY aos_v2_lifecycle_commands_worker_discovery
    ON aos_v2_lifecycle_commands
    FOR SELECT TO agentos_worker
    USING (true);

DROP POLICY IF EXISTS aos_v2_workflow_actions_worker_discovery
    ON aos_v2_workflow_actions;
CREATE POLICY aos_v2_workflow_actions_worker_discovery
    ON aos_v2_workflow_actions
    FOR SELECT TO agentos_worker
    USING (true);

CREATE INDEX IF NOT EXISTS aos_v2_lifecycle_commands_ready_global_idx
    ON aos_v2_lifecycle_commands (available_at, tenant_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS aos_v2_lifecycle_commands_expired_global_idx
    ON aos_v2_lifecycle_commands (lease_expires_at, tenant_id)
    WHERE status = 'executing';
CREATE INDEX IF NOT EXISTS aos_v2_workflow_actions_ready_global_idx
    ON aos_v2_workflow_actions (available_at, tenant_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS aos_v2_workflow_actions_expired_global_idx
    ON aos_v2_workflow_actions (lease_expires_at, tenant_id)
    WHERE status = 'executing';
