-- Feature flags / gradual rollout (for product experiments + safe launches).
CREATE TABLE IF NOT EXISTS flags (
    name        TEXT PRIMARY KEY,
    enabled     BOOLEAN NOT NULL DEFAULT false,
    rollout_pct INTEGER NOT NULL DEFAULT 100
);
