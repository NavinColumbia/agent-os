-- Per-app financial circuit-breaker. Each app has a hard spend cap and a loss limit; when operating
-- spend exceeds the cap, or (revenue - spend) drops below -loss_limit, the guard auto-PAUSES the app
-- (a safe, protective action — it only stops spend). Resuming / raising limits is a spend decision and
-- requires human approval.
CREATE TABLE IF NOT EXISTS app_policies (
    app        TEXT PRIMARY KEY,
    spend_cap  NUMERIC NOT NULL DEFAULT 100,   -- hard $ ceiling on cumulative operating spend
    loss_limit NUMERIC NOT NULL DEFAULT 20,    -- auto-pause if (revenue - spend) <= -loss_limit
    status     TEXT NOT NULL DEFAULT 'active', -- active | paused
    reason     TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
