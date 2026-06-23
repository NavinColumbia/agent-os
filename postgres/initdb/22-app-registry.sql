-- App lifecycle registry: source of truth for every app the factory builds.
-- Complements portfolio.py (P&L view) with kind/version/deps/README + repo & dev/prod URLs.
CREATE TABLE IF NOT EXISTS app_registry (
  name         text PRIMARY KEY,
  kind         text NOT NULL,                          -- lib | web | service | extension | project
  status       text NOT NULL DEFAULT 'built',          -- built | launched | blocked | published | deployed
  version      text NOT NULL DEFAULT '0.1.0',
  repo_url     text,                                    -- private GitHub repo
  dev_url      text,                                    -- local/dev runtime URL
  prod_url     text,                                    -- production URL once deployed
  dependencies jsonb NOT NULL DEFAULT '[]',             -- tracked deps + versions (or "none"/"stdlib")
  has_readme   boolean NOT NULL DEFAULT false,
  last_commit  text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
