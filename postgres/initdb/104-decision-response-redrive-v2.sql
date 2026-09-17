-- Operator recovery gives a failed durable human response a new bounded retry
-- cycle without losing its immutable response, deterministic event ID, or
-- lifetime attempt accounting.

ALTER TABLE public.aos_v2_decision_responses
    ADD COLUMN IF NOT EXISTS total_attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS redrive_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS redrive_idempotency_key text,
    ADD COLUMN IF NOT EXISTS redriven_by text;

UPDATE public.aos_v2_decision_responses
   SET total_attempts = attempts
 WHERE total_attempts < attempts;

ALTER TABLE public.aos_v2_decision_responses
    DROP CONSTRAINT IF EXISTS aos_v2_decision_responses_total_attempts_check,
    ADD CONSTRAINT aos_v2_decision_responses_total_attempts_check
        CHECK (total_attempts >= attempts AND total_attempts >= 0),
    DROP CONSTRAINT IF EXISTS aos_v2_decision_responses_redrive_count_check,
    ADD CONSTRAINT aos_v2_decision_responses_redrive_count_check
        CHECK (redrive_count >= 0),
    DROP CONSTRAINT IF EXISTS aos_v2_decision_responses_redrive_identity_check,
    ADD CONSTRAINT aos_v2_decision_responses_redrive_identity_check CHECK (
        (redrive_idempotency_key IS NULL AND redriven_by IS NULL)
        OR
        (
            redrive_idempotency_key IS NOT NULL
            AND length(redrive_idempotency_key) BETWEEN 8 AND 200
            AND redriven_by IS NOT NULL
            AND length(redriven_by) BETWEEN 1 AND 256
        )
    );

CREATE TABLE IF NOT EXISTS public.aos_v2_decision_response_redrives (
    tenant_id text NOT NULL,
    notification_id text NOT NULL,
    idempotency_key text NOT NULL,
    response_id text NOT NULL,
    redrive_number integer NOT NULL,
    actor_id text NOT NULL,
    previous_attempts integer NOT NULL,
    total_attempts_at_redrive integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, notification_id, idempotency_key),
    UNIQUE (tenant_id, notification_id, redrive_number),
    FOREIGN KEY (tenant_id, response_id)
        REFERENCES public.aos_v2_decision_responses (tenant_id, response_id)
        ON DELETE CASCADE,
    CHECK (length(idempotency_key) BETWEEN 8 AND 200),
    CHECK (length(actor_id) BETWEEN 1 AND 256),
    CHECK (redrive_number > 0),
    CHECK (previous_attempts > 0),
    CHECK (total_attempts_at_redrive >= previous_attempts)
);

ALTER TABLE public.aos_v2_decision_response_redrives ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.aos_v2_decision_response_redrives FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.aos_v2_decision_response_redrives FROM agentos_app;
GRANT SELECT, INSERT ON TABLE public.aos_v2_decision_response_redrives TO agentos_app;
DROP POLICY IF EXISTS aos_v2_decision_response_redrives_tenant_guc
    ON public.aos_v2_decision_response_redrives;
CREATE POLICY aos_v2_decision_response_redrives_tenant_guc
    ON public.aos_v2_decision_response_redrives
    FOR ALL TO agentos_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

REVOKE ALL ON TABLE public.aos_v2_decision_response_redrives FROM agentos_worker;
