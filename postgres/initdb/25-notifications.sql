-- Tenant-facing notification taxonomy. Until now the only notify path was notify.py -> ntfy to the
-- FOUNDER's phone (operator pager). Products that ship to real users need a per-tenant feed with a
-- channel (in_app | email | push), a category (build | billing | changelog | incident | security), and a
-- level (silent | passive | standard | urgent) that decides how loudly it surfaces — exactly the
-- taxonomy the launch research calls for. In-app notifications are stored here and read by the front door.
CREATE TABLE IF NOT EXISTS notifications (
    id         BIGSERIAL PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    channel    TEXT NOT NULL DEFAULT 'in_app',   -- in_app | email | push
    category   TEXT NOT NULL DEFAULT 'build',     -- build | billing | changelog | incident | security
    level      TEXT NOT NULL DEFAULT 'standard',  -- silent | passive | standard | urgent
    title      TEXT NOT NULL,
    body       TEXT,
    url        TEXT,
    context_key TEXT,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS notifications_feed_idx ON notifications (tenant_id, read_at, id DESC);
CREATE INDEX IF NOT EXISTS notifications_context_idx ON notifications (tenant_id, context_key)
    WHERE context_key IS NOT NULL;

-- Honest external-delivery telemetry. An in-app row proves persistence, not that email/ntfy/the operator
-- accepted the message. Keep those facts separate so the UI and audit trail never claim an unobserved send.
CREATE TABLE IF NOT EXISTS notification_deliveries (
    notification_id BIGINT NOT NULL,
    tenant_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,                    -- accepted | failed | unavailable
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT,
    attempted_at TIMESTAMPTZ,
    accepted_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ,
    PRIMARY KEY (notification_id, channel)
);
CREATE INDEX IF NOT EXISTS notification_deliveries_retry_idx
    ON notification_deliveries (next_attempt_at) WHERE status = 'failed';

-- Per-tenant per-category channel preferences (opt-out / quiet a category). Absent row = defaults.
CREATE TABLE IF NOT EXISTS notification_prefs (
    tenant_id  TEXT NOT NULL,
    category   TEXT NOT NULL,
    in_app     BOOLEAN NOT NULL DEFAULT true,
    email      BOOLEAN NOT NULL DEFAULT true,
    push       BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_id, category)
);
