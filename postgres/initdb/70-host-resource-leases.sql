-- Host-wide weighted admission is control-plane state, not tenant application data.
-- It fences concurrent model, browser and media launches against one RAM/CPU envelope.
CREATE TABLE IF NOT EXISTS host_resource_leases (
    lease_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    resource_kind TEXT NOT NULL,
    memory_mb INT NOT NULL CHECK (memory_mb >= 0),
    cpu_millis INT NOT NULL CHECK (cpu_millis >= 0),
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_until TIMESTAMPTZ NOT NULL,
    owner_token TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS host_resource_leases_lease_until_idx
    ON host_resource_leases(lease_until);

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentos_app') THEN
        REVOKE ALL ON TABLE host_resource_leases FROM agentos_app;
    END IF;
END $$;
