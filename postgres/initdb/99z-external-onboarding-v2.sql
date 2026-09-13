-- Human and vendor capacity is admitted only after an authorized owner records
-- the external identity/legal/access attestations. The append-only event remains
-- the source of truth; no mutable row can silently turn a proposal into a hire.

ALTER TABLE public.aos_v2_company_events
    DROP CONSTRAINT IF EXISTS aos_v2_company_events_kind_check;
ALTER TABLE public.aos_v2_company_events
    ADD CONSTRAINT aos_v2_company_events_kind_check
    CHECK (kind IN (
        'agent_hired',
        'agent_retired',
        'hiring_proposal_decided',
        'external_participant_onboarded'
    ));
