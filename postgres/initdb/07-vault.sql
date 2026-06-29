-- Scoped secrets vault: secrets encrypted at rest, scoped by (tenant, product, environment, roles).
-- Test/QA gets TEST secrets only; prod secrets never leak to test. Access is tenant+role+env checked + audited.
-- #52: tenant_id is part of the PK so a per-tenant BYO key (product='tenant:<tid>') is isolated in
-- storage; vault.get_secret additionally binds the *requester's* tenant before decrypting.
CREATE TABLE IF NOT EXISTS secrets (
    name         TEXT NOT NULL,
    product      TEXT NOT NULL,
    environment  TEXT NOT NULL,          -- dev | test | staging | prod
    tenant_id    TEXT NOT NULL DEFAULT '',-- owning tenant ('' = shared/infra secret)
    allowed_roles TEXT[] NOT NULL,        -- which agent roles may read it
    value_enc    BYTEA NOT NULL,          -- Fernet-encrypted
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ,
    PRIMARY KEY (name, product, environment, tenant_id)
);
