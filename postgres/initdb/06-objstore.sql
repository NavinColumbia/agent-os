-- Content-addressed object/blob store (ADR 0005 FilePart). Agents share images/PDFs/any object
-- BY REFERENCE: put() -> sha256 id; the id travels in a message; get(id) retrieves. Content-addressing
-- gives automatic dedup. Optional per-blob TTL + GC for expiry.
CREATE TABLE IF NOT EXISTS blobs (
    id          TEXT PRIMARY KEY,        -- sha256 hex of the content (content-addressed, dedup)
    mime        TEXT,
    size_bytes  INTEGER NOT NULL,
    data        BYTEA NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ              -- NULL = keep forever; else GC removes after this
);
CREATE INDEX IF NOT EXISTS blobs_expiry_idx ON blobs (expires_at);
