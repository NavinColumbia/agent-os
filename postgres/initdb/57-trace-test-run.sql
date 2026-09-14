-- Keep synthetic/offline verification telemetry available for debugging without allowing it to trip
-- production provider, spend, stuck-build, or recovery signals.
ALTER TABLE traces ADD COLUMN IF NOT EXISTS test_run BOOLEAN NOT NULL DEFAULT FALSE;
