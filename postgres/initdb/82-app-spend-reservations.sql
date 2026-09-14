-- Atomic pre-provider cost reservations. Completed spend remains authoritative in traces; these rows
-- cover only calls that are currently in flight (plus a few seconds while their trace is committed).
CREATE TABLE IF NOT EXISTS app_spend_reservations (
  token TEXT PRIMARY KEY,
  app TEXT NOT NULL,
  amount NUMERIC NOT NULL CHECK (amount >= 0),
  owner TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS app_spend_reservations_app_idx
  ON app_spend_reservations(app, expires_at);
