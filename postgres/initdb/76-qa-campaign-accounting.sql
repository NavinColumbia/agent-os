-- QA progress counters are meaningful only within one evidence/revision campaign.
-- A new orchestra run, evidence-policy revision, or post-fix coverage generation
-- legitimately restarts completed-story accounting and must not inherit a false
-- no-progress streak from its predecessor.

ALTER TABLE controller_state
  ADD COLUMN IF NOT EXISTS qa_campaign_key TEXT;
