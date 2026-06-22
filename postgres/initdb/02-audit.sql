-- Append-only, hash-chained, tamper-evident audit log for agent decisions/actions.
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor       TEXT NOT NULL,        -- agent role / id
    action      TEXT NOT NULL,        -- tool / op
    resource    TEXT,                 -- path / target / args digest
    decision    TEXT NOT NULL,        -- allow | deny | ask | executed
    payload     JSONB NOT NULL DEFAULT '{}',
    prev_hash   TEXT NOT NULL,        -- hash of previous entry ('' for genesis)
    entry_hash  TEXT NOT NULL         -- HMAC-SHA256(key, canonical(business fields) || prev_hash)
);
CREATE INDEX IF NOT EXISTS audit_log_ts_idx ON audit_log (ts);
