-- Real economics on every traced step: the actual USD cost + token usage of each agent call
-- (captured from `claude -p --output-format json`). This turns build cost from a time proxy into
-- real money, per stage, per app.
ALTER TABLE traces ADD COLUMN IF NOT EXISTS cost_usd   NUMERIC NOT NULL DEFAULT 0;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS tokens_in  INT NOT NULL DEFAULT 0;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS tokens_out INT NOT NULL DEFAULT 0;
