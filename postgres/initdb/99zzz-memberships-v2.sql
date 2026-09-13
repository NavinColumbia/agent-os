-- Provider-neutral team access. Invitation capabilities are signed outside the
-- database; only their SHA-256 digests are retained here.

CREATE TABLE IF NOT EXISTS public.aos_v2_memberships (
    tenant_id text NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    subject_id text NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 255),
    roles jsonb NOT NULL CHECK (
        jsonb_typeof(roles) = 'array'
        AND jsonb_array_length(roles) BETWEEN 1 AND 3
        AND roles <@ '["owner", "operator", "viewer"]'::jsonb
    ),
    active boolean NOT NULL DEFAULT true,
    invitation_id text NOT NULL CHECK (invitation_id ~ '^invitation-[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revoked_by text,
    revoked_at timestamptz,
    revoked_reason text,
    revocation_key text,
    PRIMARY KEY (tenant_id, subject_id),
    CHECK (
        (active AND revoked_by IS NULL AND revoked_at IS NULL
            AND revoked_reason IS NULL AND revocation_key IS NULL)
        OR
        (NOT active AND revoked_by IS NOT NULL AND revoked_at IS NOT NULL
            AND length(revoked_reason) BETWEEN 1 AND 2000
            AND length(revocation_key) BETWEEN 8 AND 200)
    )
);

CREATE INDEX IF NOT EXISTS aos_v2_memberships_subject_active_idx
    ON public.aos_v2_memberships (subject_id, active, tenant_id);

CREATE TABLE IF NOT EXISTS public.aos_v2_invitations (
    tenant_id text NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    invitation_id text NOT NULL CHECK (invitation_id ~ '^invitation-[0-9a-f]{64}$'),
    token_digest text NOT NULL CHECK (token_digest ~ '^[0-9a-f]{64}$'),
    roles jsonb NOT NULL CHECK (
        jsonb_typeof(roles) = 'array'
        AND jsonb_array_length(roles) BETWEEN 1 AND 3
        AND roles <@ '["owner", "operator", "viewer"]'::jsonb
    ),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 200),
    expires_in_seconds integer NOT NULL CHECK (expires_in_seconds BETWEEN 300 AND 2592000),
    created_by text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 255),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    claimed_by text,
    claimed_at timestamptz,
    PRIMARY KEY (tenant_id, invitation_id),
    UNIQUE (tenant_id, idempotency_key),
    UNIQUE (tenant_id, token_digest),
    CHECK (expires_at > created_at),
    CHECK (
        (claimed_by IS NULL AND claimed_at IS NULL)
        OR
        (length(claimed_by) BETWEEN 1 AND 255 AND claimed_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS aos_v2_invitations_expiry_idx
    ON public.aos_v2_invitations (expires_at)
    WHERE claimed_at IS NULL;

ALTER TABLE public.aos_v2_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_memberships FORCE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_invitations FORCE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.aos_v2_memberships FROM agentos_app;
REVOKE ALL ON TABLE public.aos_v2_invitations FROM agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_memberships TO agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_invitations TO agentos_app;

DROP POLICY IF EXISTS aos_v2_memberships_read_scope ON public.aos_v2_memberships;
CREATE POLICY aos_v2_memberships_read_scope ON public.aos_v2_memberships
    FOR SELECT TO agentos_app
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        OR subject_id = current_setting('app.subject_id', true)
    );

DROP POLICY IF EXISTS aos_v2_memberships_tenant_insert ON public.aos_v2_memberships;
CREATE POLICY aos_v2_memberships_tenant_insert ON public.aos_v2_memberships
    FOR INSERT TO agentos_app
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

DROP POLICY IF EXISTS aos_v2_memberships_tenant_update ON public.aos_v2_memberships;
CREATE POLICY aos_v2_memberships_tenant_update ON public.aos_v2_memberships
    FOR UPDATE TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

DROP POLICY IF EXISTS aos_v2_invitations_tenant_guc ON public.aos_v2_invitations;
CREATE POLICY aos_v2_invitations_tenant_guc ON public.aos_v2_invitations
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
