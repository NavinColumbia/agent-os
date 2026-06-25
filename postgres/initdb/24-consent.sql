-- AI-consent ledger. Apple Guideline 5.1.2(i) (eff. 2025-11-13), Google Play AI policy, and EU AI Act
-- Art. 50 (applies 2026-08-02) all require a NAMED, explicit, revocable consent BEFORE any user data is
-- sent to a third-party AI provider. This table is the auditable record of that consent per tenant +
-- provider + disclosure version (re-consent is required when the disclosure text/version changes).
CREATE TABLE IF NOT EXISTS ai_consent (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    provider          TEXT NOT NULL,                 -- e.g. 'Anthropic Claude'
    disclosure_version TEXT NOT NULL,                -- bump to force re-consent
    accepted_at       TIMESTAMPTZ,
    revoked_at        TIMESTAMPTZ,
    UNIQUE (tenant_id, provider, disclosure_version)
);
CREATE INDEX IF NOT EXISTS ai_consent_tenant_idx ON ai_consent (tenant_id, provider);
