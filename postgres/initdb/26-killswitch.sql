-- Runtime human-oversight kill-switch. EU AI Act Art. 14 (human oversight, enforced 2026-08-02) requires
-- the operator to be able to stop/override autonomous agents at runtime. factory.agent() checks this
-- table before every spawn; a row halts that scope ('global' = whole fleet, or a product/tenant id) at the
-- next turn boundary. Resume = delete the row.
CREATE TABLE IF NOT EXISTS kill_switch (
    scope   TEXT PRIMARY KEY,                 -- 'global' or a product/tenant id
    reason  TEXT,
    set_by  TEXT,
    set_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
