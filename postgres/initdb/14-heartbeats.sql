-- Liveness heartbeats + watchdog alert dedup.
-- Long-running loops (ticker, watchdog, dashboard) and the factory beat here each cycle; the watchdog
-- flags components whose heartbeat goes stale, and remembers which alerts it already pinged (cooldown)
-- so it warns you once per incident instead of every tick.
CREATE TABLE IF NOT EXISTS heartbeats (
    component TEXT PRIMARY KEY,
    ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    meta      JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS watchdog_alerts (
    signature  TEXT PRIMARY KEY,
    level      TEXT NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_sent  TIMESTAMPTZ
);
