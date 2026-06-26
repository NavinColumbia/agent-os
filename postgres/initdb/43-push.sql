-- 43-push.sql — per-tenant push transport (fills the dead 'push' notification channel).
-- push_targets: one ntfy topic per tenant. send() looks up the topic and POSTs to the local ntfy;
-- with no topic on file it falls back to paging the founder. One row per tenant (the device/feed binding).

CREATE TABLE IF NOT EXISTS push_targets (
    tenant_id     TEXT PRIMARY KEY,
    topic         TEXT,
    registered_at TIMESTAMPTZ DEFAULT now()
);
