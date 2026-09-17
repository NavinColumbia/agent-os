-- Subject-bound mission participation. Tenant membership is necessary but
-- client/reviewer/builder mission access is explicitly projected here.

CREATE TABLE IF NOT EXISTS public.aos_v2_mission_participants (
    tenant_id text NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    mission_id text NOT NULL CHECK (length(mission_id) BETWEEN 1 AND 256),
    subject_id text NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 255),
    participation_role text NOT NULL CHECK (
        participation_role IN ('builder', 'reviewer', 'client', 'viewer')
    ),
    active boolean NOT NULL DEFAULT true,
    version integer NOT NULL CHECK (version >= 1),
    granted_by text NOT NULL CHECK (length(granted_by) BETWEEN 1 AND 255),
    granted_at timestamptz NOT NULL DEFAULT now(),
    grant_key text NOT NULL CHECK (length(grant_key) BETWEEN 8 AND 200),
    revoked_by text,
    revoked_at timestamptz,
    revoked_reason text,
    revocation_key text,
    PRIMARY KEY (tenant_id, mission_id, subject_id),
    CHECK (
        (active AND revoked_by IS NULL AND revoked_at IS NULL
            AND revoked_reason IS NULL AND revocation_key IS NULL)
        OR
        (NOT active AND length(revoked_by) BETWEEN 1 AND 255
            AND revoked_at IS NOT NULL
            AND length(revoked_reason) BETWEEN 1 AND 2000
            AND length(revocation_key) BETWEEN 8 AND 200)
    )
);

CREATE INDEX IF NOT EXISTS aos_v2_mission_participants_subject_idx
    ON public.aos_v2_mission_participants (tenant_id, subject_id, active, mission_id);

ALTER TABLE public.aos_v2_mission_participants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_mission_participants FORCE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.aos_v2_mission_participants FROM agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_mission_participants TO agentos_app;

DROP POLICY IF EXISTS aos_v2_mission_participants_tenant_guc
    ON public.aos_v2_mission_participants;
CREATE POLICY aos_v2_mission_participants_tenant_guc
    ON public.aos_v2_mission_participants
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
