-- Deferred QA video encoding. Raw WebM is release evidence; MP4 is a review convenience processed after
-- the browser/explorer slot is released. Claims are leased and fenced so daemon restarts cannot double-finish.
CREATE TABLE IF NOT EXISTS qa_evidence_encoding_jobs (
  id BIGSERIAL PRIMARY KEY,
  source_path TEXT NOT NULL UNIQUE,
  output_path TEXT NOT NULL,
  receipt_path TEXT,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','retry','done','failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  claim_token TEXT,
  claimed_at TIMESTAMPTZ,
  lease_until TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  error TEXT,
  result JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS qa_evidence_encoding_jobs_claim_idx
  ON qa_evidence_encoding_jobs(status, lease_until, id);
