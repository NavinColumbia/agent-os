-- 35-legacy-tenant-runtime.sql - canonical schemas for the strangler runtime.
--
-- These relations were historically created lazily by imported Python modules.
-- That made clean installs depend on import order and meant tables created after
-- migration 56 missed its tenant grants and RLS policies.  Establish every
-- production relation before the tenant-spine and RLS migrations run.

CREATE TABLE IF NOT EXISTS accounts (
    email      TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    name       TEXT,
    pw_salt    TEXT NOT NULL,
    pw_hash    TEXT NOT NULL,
    verified   BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS email_codes (
    email      TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    purpose    TEXT NOT NULL DEFAULT 'verify',
    attempts   INT NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (email, purpose)
);

CREATE TABLE IF NOT EXISTS auth_rate_limits (
    action     TEXT NOT NULL,
    key_hash   TEXT NOT NULL,
    bucket     BIGINT NOT NULL,
    attempts   INT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (action, key_hash, bucket)
);

CREATE TABLE IF NOT EXISTS ceo_vision (
    tenant_id    TEXT NOT NULL,
    scope        TEXT NOT NULL,
    vision       TEXT NOT NULL DEFAULT '',
    requirements JSONB,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, scope)
);

CREATE TABLE IF NOT EXISTS proactive_sent (
    tenant_id TEXT NOT NULL,
    sig       TEXT NOT NULL,
    last_sent TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, sig)
);

CREATE TABLE IF NOT EXISTS proactive_sweep_state (
    name             TEXT PRIMARY KEY,
    cursor_tenant_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS brief_cache (
    tenant_id  TEXT NOT NULL,
    org_id     INT NOT NULL DEFAULT 0,
    data       JSONB NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, org_id)
);

CREATE TABLE IF NOT EXISTS tenant_providers (
    tenant_id TEXT NOT NULL,
    provider  TEXT NOT NULL,
    has_key   BOOLEAN NOT NULL DEFAULT false,
    priority  INT NOT NULL DEFAULT 5,
    auth_mode TEXT DEFAULT 'api_key',
    added_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (tenant_id, provider)
);

CREATE TABLE IF NOT EXISTS product_registry (
    product_id    TEXT PRIMARY KEY,
    tenant_id     TEXT,
    org_id        TEXT,
    repo_path     TEXT NOT NULL,
    plan          JSONB,
    current_phase TEXT,
    phases        JSONB NOT NULL DEFAULT '{}',
    attempts      JSONB NOT NULL DEFAULT '{}',
    ts            TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS findings (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT,
    org_id          BIGINT,
    source          TEXT NOT NULL,
    title           TEXT NOT NULL,
    detail          TEXT,
    severity        TEXT NOT NULL DEFAULT 'med',
    need_role       TEXT NOT NULL DEFAULT 'builder',
    status          TEXT NOT NULL DEFAULT 'open',
    owner           TEXT,
    task_id         BIGINT,
    hire_id         BIGINT,
    board_id        BIGINT,
    verify_check    JSONB,
    verification_id BIGINT,
    escalated_at    TIMESTAMPTZ,
    escalated_to    TEXT,
    dedupe_key      TEXT,
    drop_reason     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    resolved_by     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS findings_dedupe_key_uq
    ON findings (tenant_id, source, dedupe_key)
    WHERE dedupe_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS finding_verifications (
    id         BIGSERIAL PRIMARY KEY,
    tenant_id  TEXT,
    finding_id BIGINT NOT NULL,
    kind       TEXT NOT NULL,
    passed     BOOLEAN NOT NULL,
    evidence   JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS qa_runs (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT,
    product     TEXT NOT NULL,
    rounds      INT NOT NULL,
    passed      BOOLEAN NOT NULL,
    clean       BOOLEAN NOT NULL,
    verdict     JSONB NOT NULL,
    report_md   TEXT,
    report_json TEXT,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS story_corpus (
    id               BIGSERIAL PRIMARY KEY,
    tenant_id        TEXT,
    product          TEXT NOT NULL,
    story_id         TEXT NOT NULL,
    title            TEXT NOT NULL,
    persona          TEXT NOT NULL DEFAULT 'user',
    category         TEXT NOT NULL DEFAULT '',
    steps            JSONB NOT NULL DEFAULT '[]',
    expected_outcome TEXT NOT NULL DEFAULT '',
    source           TEXT NOT NULL DEFAULT 'generated',
    bug_ref          TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (product, title)
);
CREATE INDEX IF NOT EXISTS story_corpus_product_idx ON story_corpus (product);

CREATE TABLE IF NOT EXISTS sentinel_state (
    key TEXT PRIMARY KEY,
    val TEXT,
    ts  TIMESTAMPTZ DEFAULT now()
);
