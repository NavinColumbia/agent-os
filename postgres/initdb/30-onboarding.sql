-- Guided first-run wizard state. A non-technical CEO is walked through the journey: sign up -> connect a
-- model provider (Claude and/or Codex) -> give AI-processing consent -> ship a first build. This table only
-- records the FURTHEST step reached (a cursor) + a completed flag; the wizard ALWAYS recomputes per-step
-- status from real state (connected providers, consent ledger, tenant_products) rather than trusting it.
CREATE TABLE IF NOT EXISTS onboarding_state (
    tenant_id   TEXT PRIMARY KEY,
    step        TEXT DEFAULT 'welcome',         -- furthest step reached: welcome|provider|consent|first_build|done
    completed   BOOLEAN DEFAULT false,
    updated_at  TIMESTAMPTZ DEFAULT now()
);
