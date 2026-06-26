-- 47-design.sql — PROTOTYPE phase: per-org design artifacts.
-- The design fleet generates a self-contained prototype HTML screen for each of three audiences
-- (the CEO cockpit, the internal team, the external users) from a product plan; each produced screen
-- is recorded here so the gallery view (designview.py) can list it and a human can approve/reject it.
-- surface ∈ {cockpit, team, external}; status ∈ {draft, review, approved}.
CREATE TABLE IF NOT EXISTS design_artifacts (
    id         BIGSERIAL PRIMARY KEY,
    org_id     TEXT,
    product    TEXT,
    kind       TEXT DEFAULT 'screen',
    surface    TEXT,
    title      TEXT,
    html_path  TEXT,
    status     TEXT DEFAULT 'draft',
    created_at TIMESTAMPTZ DEFAULT now()
);
