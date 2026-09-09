-- Extend the immutable company stream with governed AI staffing decisions.
-- Migration 95 remains immutable for already-provisioned environments.

ALTER TABLE public.aos_v2_company_events
    DROP CONSTRAINT IF EXISTS aos_v2_company_events_kind_check;
ALTER TABLE public.aos_v2_company_events
    ADD CONSTRAINT aos_v2_company_events_kind_check
    CHECK (kind IN ('agent_hired', 'agent_retired', 'hiring_proposal_decided'));
