-- MEMORY LAYER (IMPROVEMENTS-PLAN item 9) — the org's durable, provenanced, permissioned memory.
-- The design: docs/MEMORY-LAYER-DESIGN.md. Extends the pre-existing "memory spine" (company_memory +
-- role_lessons, formerly created inline by companymemory._ensure) with the SOTA-research dimensions —
-- two-tier visibility, per-role scope, provenance into the tamper-evident audit chain — plus coordinator
-- PLAN / phase-SUMMARY checkpoints (item 10, the compaction unit item 11 reuses). scripts/companymemory.py
-- also creates/upgrades these on demand (_ensure with ADD COLUMN IF NOT EXISTS), so an existing DB needs no
-- re-init; this file is the source of truth for a FRESH install. Working memory stays where it belongs —
-- orchestra_actors.memory (per-actor, run-scoped, resume-safe) — and is deliberately NOT duplicated here.

-- FACTUAL memory: durable facts about a company (decisions, preferences, product history, constraints),
-- injected into every agent's brief so a spawned agent is not a blank slate.
CREATE TABLE IF NOT EXISTS company_memory (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    org_id        TEXT,
    kind          TEXT NOT NULL,                       -- decision | preference | product | context | constraint
    text          TEXT NOT NULL,
    weight        INT  DEFAULT 1,                      -- recall orders by weight DESC, ts DESC
    visibility    TEXT NOT NULL DEFAULT 'shared',      -- 'shared' | 'private' (two-tier; private = author-only)
    role_scope    TEXT,                                -- NULL = all roles; else only this role may recall it
    author_actor  TEXT,                                -- provenance: which agent/actor wrote it
    run_id        TEXT,                                -- provenance: which run
    sources       JSONB,                               -- provenance: events/resources consulted
    audit_id      BIGINT,                              -- link into the tamper-evident audit_log chain
    ts            TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS company_memory_scope ON company_memory (tenant_id, org_id, ts DESC);

-- EXPERIENTIAL memory: hard-won lessons distilled from failures/fix-loops, loaded into a role's brief so the
-- fleet stops repeating mistakes. tenant_id NULL = a cross-tenant fleet lesson (default); a value scopes it.
CREATE TABLE IF NOT EXISTS role_lessons (
    id            BIGSERIAL PRIMARY KEY,
    role          TEXT NOT NULL,
    lesson        TEXT NOT NULL UNIQUE,
    source        TEXT,
    uses          INT  DEFAULT 0,                      -- read-count; popular lessons float up (self-reinforcing)
    tenant_id     TEXT,
    run_id        TEXT,
    author_actor  TEXT,
    audit_id      BIGINT,
    ts            TIMESTAMPTZ DEFAULT now()
);

-- Coordinator PLAN + per-phase SUMMARY checkpoints (item 10): durable, distinct from event history so a
-- context truncation can't lose the plan. Single-writer = the coordinator actor; history is immutable
-- (a revised plan is a new row; readers take the latest). phase_summary rows are item 11's compaction unit.
CREATE TABLE IF NOT EXISTS memory_checkpoints (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    actor_id      TEXT,
    kind          TEXT NOT NULL,                       -- 'plan' | 'phase_summary'
    phase         TEXT,
    seq           INT,
    content       TEXT NOT NULL,
    superseded_by BIGINT,
    audit_id      BIGINT,
    ts            TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS memory_checkpoints_run ON memory_checkpoints (run_id, kind, seq);
