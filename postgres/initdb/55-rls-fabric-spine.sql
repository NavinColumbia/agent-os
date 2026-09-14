-- 55-rls-fabric-spine.sql - nullable tenant spine for legacy durable messaging fabric.
--
-- This is preparation only: it does NOT enable RLS. The comms fabric is addressed by agent/workflow ids.
-- Where those ids are registered in directory, derive tenant_id from directory. Rows that cannot be resolved
-- remain NULL and will be visible only to operator/admin paths once RLS is enabled.

CREATE OR REPLACE FUNCTION aos_set_tenant_from_directory() RETURNS trigger AS $$
DECLARE
    i int;
    actor text;
BEGIN
    IF NEW.tenant_id IS NOT NULL THEN
        RETURN NEW;
    END IF;
    FOR i IN 0..TG_NARGS - 1 LOOP
        actor := NULLIF(to_jsonb(NEW)->>TG_ARGV[i], '');
        IF actor IS NOT NULL THEN
            SELECT tenant_id INTO NEW.tenant_id
              FROM directory
             WHERE agent_id = actor AND tenant_id IS NOT NULL
             LIMIT 1;
            IF NEW.tenant_id IS NOT NULL THEN
                RETURN NEW;
            END IF;
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF to_regclass('public.conversations') IS NOT NULL THEN
        ALTER TABLE conversations ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE conversations c SET tenant_id = d.tenant_id
          FROM directory d
         WHERE c.tenant_id IS NULL
           AND d.tenant_id IS NOT NULL
           AND (d.agent_id = c.sender OR d.agent_id = c.recipient);
        CREATE INDEX IF NOT EXISTS conversations_tenant_id_rls_idx ON conversations (tenant_id);
        DROP TRIGGER IF EXISTS conversations_tenant_spine ON conversations;
        CREATE TRIGGER conversations_tenant_spine BEFORE INSERT OR UPDATE OF sender, recipient, tenant_id ON conversations
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_directory('sender', 'recipient');
    END IF;

    IF to_regclass('public.inbox') IS NOT NULL THEN
        ALTER TABLE inbox ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE inbox i SET tenant_id = d.tenant_id
          FROM directory d
         WHERE i.tenant_id IS NULL
           AND d.tenant_id IS NOT NULL
           AND d.agent_id = i.subscriber;
        CREATE INDEX IF NOT EXISTS inbox_tenant_id_rls_idx ON inbox (tenant_id);
        DROP TRIGGER IF EXISTS inbox_tenant_spine ON inbox;
        CREATE TRIGGER inbox_tenant_spine BEFORE INSERT OR UPDATE OF subscriber, tenant_id ON inbox
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_directory('subscriber');
    END IF;

    IF to_regclass('public.waits') IS NOT NULL THEN
        ALTER TABLE waits ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE waits w SET tenant_id = d.tenant_id
          FROM directory d
         WHERE w.tenant_id IS NULL
           AND d.tenant_id IS NOT NULL
           AND (d.agent_id = w.waiter OR d.agent_id = w.awaited);
        CREATE INDEX IF NOT EXISTS waits_tenant_id_rls_idx ON waits (tenant_id);
        DROP TRIGGER IF EXISTS waits_tenant_spine ON waits;
        CREATE TRIGGER waits_tenant_spine BEFORE INSERT OR UPDATE OF waiter, awaited, tenant_id ON waits
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_directory('waiter', 'awaited');
    END IF;

    IF to_regclass('public.orchestra_messages') IS NOT NULL THEN
        ALTER TABLE orchestra_messages ADD COLUMN IF NOT EXISTS tenant_id text;
        UPDATE orchestra_messages m SET tenant_id = d.tenant_id
          FROM directory d
         WHERE m.tenant_id IS NULL
           AND d.tenant_id IS NOT NULL
           AND (d.agent_id = m.frm OR d.agent_id = m.to_actor);
        CREATE INDEX IF NOT EXISTS orchestra_messages_tenant_id_rls_idx ON orchestra_messages (tenant_id);
        DROP TRIGGER IF EXISTS orchestra_messages_tenant_spine ON orchestra_messages;
        CREATE TRIGGER orchestra_messages_tenant_spine BEFORE INSERT OR UPDATE OF frm, to_actor, tenant_id ON orchestra_messages
          FOR EACH ROW EXECUTE FUNCTION aos_set_tenant_from_directory('frm', 'to_actor');
    END IF;
END $$;
