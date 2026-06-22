-- Capability/skills registry: what the org can DO, which roles have it, which tools it uses, and
-- whether it's ready or needs setup/a paid key. Roles query this to know their capabilities.
CREATE TABLE IF NOT EXISTS skills (
    name        TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    description TEXT NOT NULL,
    roles       TEXT[] NOT NULL,
    tools       TEXT[] NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ready'   -- ready | needs_key | needs_setup
);
