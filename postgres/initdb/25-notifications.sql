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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS notifications_feed_idx ON notifications (tenant_id, read_at, id DESC);

-- Per-tenant per-category channel preferences (opt-out / quiet a category). Absent row = defaults.
CREATE TABLE IF NOT EXISTS notification_prefs (
    tenant_id  TEXT NOT NULL,
    category   TEXT NOT NULL,
    in_app     BOOLEAN NOT NULL DEFAULT true,
    email      BOOLEAN NOT NULL DEFAULT true,
    push       BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_id, category)
);
