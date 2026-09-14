-- Debug traces: the full, persisted I/O of every run. This is what turns the dashboard from a
-- live monitor into a debugger — each agent stage's actual prompt + response, each test run's output,
-- with timing, keyed by run_id so a whole build can be replayed step by step long after it finished.
CREATE TABLE IF NOT EXISTS traces (
    id        BIGSERIAL PRIMARY KEY,
    run_id    TEXT NOT NULL,
    product   TEXT,
    stage     TEXT,
    role      TEXT,
    kind      TEXT NOT NULL DEFAULT 'agent',   -- agent | test | event
    prompt    TEXT,
    output    TEXT,
    rc        INT,
    elapsed_s REAL,
    test_run  BOOLEAN NOT NULL DEFAULT FALSE, -- synthetic/offline verification; excluded from live health
    ts        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS traces_run_idx ON traces (run_id, id);
CREATE INDEX IF NOT EXISTS traces_product_idx ON traces (product, ts DESC);
