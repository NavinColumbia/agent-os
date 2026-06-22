-- Per-product token/cost budgets (runtime governor enforcement).
CREATE TABLE IF NOT EXISTS budgets (
    product      TEXT PRIMARY KEY,
    token_budget BIGINT NOT NULL,
    hard_stop    BOOLEAN NOT NULL DEFAULT true
);
