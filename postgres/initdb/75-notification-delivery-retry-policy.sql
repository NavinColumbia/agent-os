-- Permanently unavailable transports (for example a tenant without a registered push topic)
-- cannot become deliverable through time-based retries. Keep them dormant until the configuration
-- action explicitly changes them back to failed/pending; only transient failures belong in the retry index.
UPDATE notification_deliveries
SET next_attempt_at = NULL
WHERE status = 'unavailable' AND next_attempt_at IS NOT NULL;

DROP INDEX IF EXISTS notification_deliveries_retry_idx;
CREATE INDEX notification_deliveries_retry_idx
    ON notification_deliveries (next_attempt_at, notification_id)
    WHERE status = 'failed';
