-- Durable, platform-owned keyset progress for the global accountability duty.
--
-- This is operational control state, not tenant data: one scheduler pass walks
-- every tenant's communication fabric without exposing any row through the
-- tenant application role.  Keeping both stream cursors in one versioned row
-- also lets a concurrent/manual invocation fail its compare-and-swap instead of
-- moving either cursor backwards.
CREATE TABLE IF NOT EXISTS accountability_sweep_state (
    name                    TEXT PRIMARY KEY,
    handoff_conversation_id TEXT NOT NULL DEFAULT '',
    handoff_turn            BIGINT NOT NULL DEFAULT 0,
    wait_waiter             TEXT NOT NULL DEFAULT '',
    wait_awaited            TEXT NOT NULL DEFAULT '',
    next_stream             TEXT NOT NULL DEFAULT 'handoffs'
                                CHECK (next_stream IN ('handoffs', 'waits')),
    version                 BIGINT NOT NULL DEFAULT 0,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO accountability_sweep_state(name) VALUES ('scheduled')
ON CONFLICT (name) DO NOTHING;

-- The request keyset should not walk seven days of non-request chatter before
-- finding its next bounded page.  Resolution context still uses the existing
-- (conversation_id, turn) primary key and is capped by the caller.
CREATE INDEX IF NOT EXISTS conversations_accountability_request_idx
    ON conversations (conversation_id, turn)
    WHERE intent IN ('ask','task','delegate','review_request','test_request',
                     'escalate','conflict','spec_ready');

-- Explicitly owner-only.  A tenant session must never see global cursor
-- positions, because their ordering leaks the existence of other tenants.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentos_app') THEN
        REVOKE ALL ON TABLE accountability_sweep_state FROM agentos_app;
    END IF;
END $$;
