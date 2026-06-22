-- Scoped secrets vault: secrets encrypted at rest, scoped by (product, environment, allowed roles).
-- Test/QA gets TEST secrets only; prod secrets never leak to test. Access is role+env checked + audited.
CREATE TABLE IF NOT EXISTS secrets (
    name         TEXT NOT NULL,
    product      TEXT NOT NULL,
    environment  TEXT NOT NULL,          -- dev | test | staging | prod
    allowed_roles TEXT[] NOT NULL,        -- which agent roles may read it
    value_enc    BYTEA NOT NULL,          -- Fernet-encrypted
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ,
    PRIMARY KEY (name, product, environment)
);
