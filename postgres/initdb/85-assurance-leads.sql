-- Public, pre-account sales intake for the managed AI Release Assurance service.
-- Contact data is deliberately separate from tenant operational data and expires unless a founder converts it.
CREATE TABLE IF NOT EXISTS assurance_pilot_leads (
    id TEXT PRIMARY KEY,
    dedupe_hash TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    company TEXT NOT NULL,
    app_url TEXT NOT NULL,
    concern TEXT NOT NULL,
    access_mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new'
      CHECK (status IN ('new','contacted','qualified','checkout_sent','paid','won','lost','deleted')),
    consent_version TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'assurance-landing',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delete_after TIMESTAMPTZ NOT NULL DEFAULT now() + interval '90 days'
);

CREATE INDEX IF NOT EXISTS assurance_pilot_leads_status_idx
    ON assurance_pilot_leads (status, created_at DESC);
CREATE INDEX IF NOT EXISTS assurance_pilot_leads_retention_idx
    ON assurance_pilot_leads (delete_after);

-- Founder-operated, pre-account outbound delivery ledger. It is deliberately
-- global because the curated prospect queue exists before an Agent OS tenant
-- or customer relationship does. Runtime tenant roles must never read the
-- recipients or delivery history.
CREATE TABLE IF NOT EXISTS assurance_outreach_delivery (
    target_id TEXT PRIMARY KEY,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('sending','sent','failed')),
    attempts INTEGER NOT NULL DEFAULT 1,
    intent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ,
    transport TEXT,
    transport_id TEXT,
    error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentos_app') THEN
        REVOKE ALL ON TABLE assurance_pilot_leads FROM agentos_app;
        REVOKE ALL ON TABLE assurance_outreach_delivery FROM agentos_app;
    END IF;
END $$;
