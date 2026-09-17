-- Revision-bound human responsibility and independent review over graph work.
-- Current rows are CAS projections; immutable mutation receipts retain every decision.

CREATE TABLE IF NOT EXISTS public.aos_v2_mission_work_assignments (
    tenant_id text NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    mission_id text NOT NULL CHECK (length(mission_id) BETWEEN 1 AND 256),
    work_id text NOT NULL CHECK (length(work_id) BETWEEN 1 AND 256),
    duty text NOT NULL CHECK (duty IN ('responsible', 'reviewer')),
    work_fingerprint text NOT NULL CHECK (work_fingerprint ~ '^[0-9a-f]{64}$'),
    subject_id text NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 255),
    participation_role text NOT NULL CHECK (participation_role IN ('builder', 'reviewer')),
    status text NOT NULL CHECK (status IN ('pending', 'accepted', 'declined', 'revoked')),
    active boolean NOT NULL,
    version integer NOT NULL CHECK (version >= 1),
    assigned_by text NOT NULL CHECK (length(assigned_by) BETWEEN 1 AND 255),
    assigned_at timestamptz NOT NULL,
    assignment_reason text NOT NULL CHECK (length(assignment_reason) BETWEEN 1 AND 2000),
    responded_by text,
    responded_at timestamptz,
    response_reason text,
    revoked_by text,
    revoked_at timestamptz,
    revoked_reason text,
    PRIMARY KEY (tenant_id, mission_id, work_id, duty),
    FOREIGN KEY (tenant_id, mission_id, subject_id)
        REFERENCES public.aos_v2_mission_participants (tenant_id, mission_id, subject_id),
    CHECK (
        (duty = 'responsible' AND participation_role = 'builder')
        OR (duty = 'reviewer' AND participation_role = 'reviewer')
    ),
    CHECK (active = (status IN ('pending', 'accepted'))),
    CHECK (
        (status = 'pending' AND responded_by IS NULL AND responded_at IS NULL)
        OR
        (status IN ('accepted', 'declined')
            AND length(responded_by) BETWEEN 1 AND 255 AND responded_at IS NOT NULL)
        OR status = 'revoked'
    ),
    CHECK (
        (status <> 'revoked' AND revoked_by IS NULL AND revoked_at IS NULL
            AND revoked_reason IS NULL)
        OR
        (status = 'revoked' AND length(revoked_by) BETWEEN 1 AND 255
            AND revoked_at IS NOT NULL AND length(revoked_reason) BETWEEN 1 AND 2000)
    )
);

CREATE INDEX IF NOT EXISTS aos_v2_mission_work_assignments_subject_idx
    ON public.aos_v2_mission_work_assignments (
        tenant_id, subject_id, active, assigned_at DESC
    );

CREATE TABLE IF NOT EXISTS public.aos_v2_mission_work_assignment_events (
    tenant_id text NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    mutation_key text NOT NULL CHECK (length(mutation_key) BETWEEN 8 AND 200),
    request_fingerprint text NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    mission_id text NOT NULL CHECK (length(mission_id) BETWEEN 1 AND 256),
    work_id text NOT NULL CHECK (length(work_id) BETWEEN 1 AND 256),
    duty text NOT NULL CHECK (duty IN ('responsible', 'reviewer')),
    assignment_version integer NOT NULL CHECK (assignment_version >= 1),
    event_kind text NOT NULL CHECK (
        event_kind IN ('assigned', 'reassigned', 'accepted', 'declined', 'revoked')
    ),
    subject_id text NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 255),
    actor_id text NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 255),
    reason text NOT NULL CHECK (length(reason) <= 2000),
    snapshot jsonb NOT NULL,
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, mutation_key),
    UNIQUE (tenant_id, mission_id, work_id, duty, assignment_version)
);

CREATE INDEX IF NOT EXISTS aos_v2_mission_work_assignment_events_history_idx
    ON public.aos_v2_mission_work_assignment_events (
        tenant_id, mission_id, occurred_at DESC
    );

CREATE OR REPLACE FUNCTION public.aos_v2_validate_work_assignment_subject()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM public.aos_v2_mission_participants p
         WHERE p.tenant_id = NEW.tenant_id
           AND p.mission_id = NEW.mission_id
           AND p.subject_id = NEW.subject_id
           AND p.participation_role = NEW.participation_role
           AND p.active
         FOR NO KEY UPDATE
    ) THEN
        RAISE EXCEPTION 'work assignment requires an active matching mission participant'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM public.aos_v2_memberships m
         WHERE m.tenant_id = NEW.tenant_id
           AND m.subject_id = NEW.subject_id
           AND m.active
           AND m.roles ? NEW.participation_role
         FOR NO KEY UPDATE
    ) THEN
        RAISE EXCEPTION 'work assignment requires an active matching organization membership'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS aos_v2_work_assignment_subject_guard
    ON public.aos_v2_mission_work_assignments;
CREATE TRIGGER aos_v2_work_assignment_subject_guard
BEFORE INSERT OR UPDATE OF subject_id, participation_role, active
ON public.aos_v2_mission_work_assignments
FOR EACH ROW WHEN (NEW.active)
EXECUTE FUNCTION public.aos_v2_validate_work_assignment_subject();

CREATE OR REPLACE FUNCTION public.aos_v2_guard_participant_responsibility()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.active AND NOT NEW.active AND EXISTS (
        SELECT 1
          FROM public.aos_v2_mission_work_assignments a
         WHERE a.tenant_id = OLD.tenant_id
           AND a.mission_id = OLD.mission_id
           AND a.subject_id = OLD.subject_id
           AND a.active
    ) THEN
        RAISE EXCEPTION 'active work responsibility must be removed or reassigned first'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS aos_v2_participant_responsibility_guard
    ON public.aos_v2_mission_participants;
CREATE TRIGGER aos_v2_participant_responsibility_guard
BEFORE UPDATE OF active ON public.aos_v2_mission_participants
FOR EACH ROW EXECUTE FUNCTION public.aos_v2_guard_participant_responsibility();

CREATE OR REPLACE FUNCTION public.aos_v2_guard_membership_participation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.active AND NOT NEW.active AND EXISTS (
        SELECT 1
          FROM public.aos_v2_mission_participants p
         WHERE p.tenant_id = OLD.tenant_id
           AND p.subject_id = OLD.subject_id
           AND p.active
    ) THEN
        RAISE EXCEPTION 'active mission participation must be revoked first'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS aos_v2_membership_participation_guard
    ON public.aos_v2_memberships;
CREATE TRIGGER aos_v2_membership_participation_guard
BEFORE UPDATE OF active ON public.aos_v2_memberships
FOR EACH ROW EXECUTE FUNCTION public.aos_v2_guard_membership_participation();

ALTER TABLE public.aos_v2_mission_work_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_mission_work_assignments FORCE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_mission_work_assignment_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_mission_work_assignment_events FORCE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.aos_v2_mission_work_assignments FROM agentos_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.aos_v2_mission_work_assignments TO agentos_app;
REVOKE ALL ON TABLE public.aos_v2_mission_work_assignment_events FROM agentos_app;
GRANT SELECT, INSERT ON TABLE public.aos_v2_mission_work_assignment_events TO agentos_app;

DROP POLICY IF EXISTS aos_v2_mission_work_assignments_tenant_guc
    ON public.aos_v2_mission_work_assignments;
CREATE POLICY aos_v2_mission_work_assignments_tenant_guc
    ON public.aos_v2_mission_work_assignments
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

DROP POLICY IF EXISTS aos_v2_mission_work_assignment_events_tenant_guc
    ON public.aos_v2_mission_work_assignment_events;
CREATE POLICY aos_v2_mission_work_assignment_events_tenant_guc
    ON public.aos_v2_mission_work_assignment_events
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
