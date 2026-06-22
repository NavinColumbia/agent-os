-- Communication fabric state (ADR 0005): durable conversation log + wait-for graph.
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT NOT NULL,
    turn            BIGSERIAL,
    message_id      TEXT NOT NULL,
    in_reply_to     TEXT,
    intent          TEXT NOT NULL,
    sender          TEXT NOT NULL,
    recipient       TEXT NOT NULL,
    content         JSONB NOT NULL DEFAULT '{}',
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (conversation_id, turn)
);
CREATE INDEX IF NOT EXISTS conv_msgid_idx ON conversations (message_id);

-- Wait-for graph: an edge waiter -> awaited means "waiter is suspended awaiting awaited".
-- The deadlock detector runs Tarjan SCC over these rows.
CREATE TABLE IF NOT EXISTS waits (
    waiter   TEXT NOT NULL,         -- workflow/agent id that is blocked
    awaited  TEXT NOT NULL,         -- workflow/agent id it is waiting on
    since    TIMESTAMPTZ NOT NULL DEFAULT now(),
    reply_by TIMESTAMPTZ,           -- SLA; past-due => timeout/escalate
    PRIMARY KEY (waiter, awaited)
);

-- Idempotent-consumer inbox (exactly-once effect): a (subscriber,message_id) seen once.
CREATE TABLE IF NOT EXISTS inbox (
    subscriber TEXT NOT NULL,
    message_id TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (subscriber, message_id)
);
