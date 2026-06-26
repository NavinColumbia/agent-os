-- 41-research.sql — STATE + OPTIONS layer over the research engine.
-- A controller starts a research run ("give me time to research"), the fleet produces a cited
-- report asynchronously, and we distill it into a few SELECTABLE strategic option cards the CEO
-- can pick from. research_runs tracks the async run; research_options holds the distilled choices.
CREATE TABLE IF NOT EXISTS research_runs (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT,
    org_id      TEXT,
    thread_id   BIGINT,
    question    TEXT,
    status      TEXT DEFAULT 'running',
    report_path TEXT,
    started_at  TIMESTAMPTZ DEFAULT now(),
    finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS research_options (
    id          BIGSERIAL PRIMARY KEY,
    run_id      BIGINT,
    title       TEXT,
    summary     TEXT,
    recommended BOOLEAN DEFAULT false,
    chosen      BOOLEAN DEFAULT false
);
