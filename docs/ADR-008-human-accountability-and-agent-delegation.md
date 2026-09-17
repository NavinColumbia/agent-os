# ADR-008: Human Accountability and Agent Delegation

- **Status:** accepted and implemented
- **Date:** 2026-09-17
- **Owners:** product architecture, identity, runtime assurance

## Context

Mission execution already materialized durable work with an agent or service as its executor. That was not a
complete organizational contract: a person could participate in a mission, but could not be made explicitly
accountable for a particular work item or enter a personal work queue. Replacing the executor with the person's
identity would also be false—the agent still performs the delegated work and produces the execution receipts.

## Decision

Add a tenant- and mission-scoped human accountability overlay to durable graph work. A work projection now
separates:

- `accountable_owner_id`: the person who accepted responsibility for the work and its outcome, falling back to
  the mission's accountable owner or `role:manager` while a request is pending or invalid;
- `delegate_id`: the agent or service that actually executes the graph work;
- `human_assignment`: the versioned assignment record, including its mission participation role.

Only an authenticated principal with `work.assign` may request, replace, or remove responsibility. A
`responsible` duty requires an active builder; an independent `reviewer` duty requires an active reviewer, so
maker and checker can coexist. The person must explicitly accept before becoming the projected accountable
owner and may decline with a reason. Replacement is atomic and compare-and-swap fenced by the current version.
Every command has an immutable, payload-fingerprinted receipt, so a delayed retry returns its original result
and cannot reverse a newer decision.

Builders and reviewers receive a personal `GET /v2/me/work` projection and a **My Work** landing surface. It
joins active assignments to current lifecycle and management truth after rechecking mission access. A missing
current work item is classified separately as superseded, revision-changed, not materialized, conflicted, or
temporarily unavailable rather than silently treated as completed. The overlay
does not mutate graph state, fabricate progress, or authorize the human to perform an agent effect.

## Invariants

1. Assignment identity is `(tenant, mission, work, duty)` and every read/write is tenant fenced; PostgreSQL
   forces RLS.
2. Tenant role grants a capability; a separate active mission-participant grant grants resource access.
3. Only an active mission builder can accept responsible ownership; an active reviewer remains a separate
   checker and never replaces the responsible owner.
4. The authenticated assigning/revoking principal is server derived; actor identity is never accepted in JSON.
5. Every mutation supplies `expected_version`; a different person replaces the current duty atomically with an
   explicit reason, while a stale writer fails without changing current state.
6. Human accountability and agent delegation remain distinct in every projection.
7. Every assignment is bound to execution run, workflow identity/version, work identity, objective, and
   delegate through a contract fingerprint; a changed contract requires reconfirmation.
8. Personal work never broadens mission visibility and disappears when the subject lacks active mission access.
9. Assignment notifications contain identifiers and a generic action prompt, not mission or work content.
10. Builder/reviewer projections omit portfolio-wide hiring, internal communication, effect-authority, and raw
   execution-timeline data even when they can inspect their assigned work.
11. The graph remains the source of status and completion truth; the accountability overlay cannot complete work.
12. Current rows are projections; immutable assignment events retain subject, duty, actor, reason, work
    fingerprint, timestamps, request fingerprint, and response for every revision.
13. Database triggers lock and recheck active mission participation and organization membership on assignment,
    and prevent either grant from being revoked while active responsibility would be stranded.

## Consequences

- Managers can make responsibility explicit without pretending a human executed an agent action.
- Builders and reviewers enter through an actionable personal queue rather than the executive portfolio.
- Mission participation or organization membership cannot be revoked while it would strand active
  responsibility; the API returns the exact duties that must first be removed or reassigned.
- Assignment history is reconstructable through immutable command receipts plus safe live experience events.
- Bulk assignment, SLA/escalation policy, workload balancing, and cursor pagination remain compatible extensions.

## Acceptance

- A manager can assign a current work item only to an active builder/reviewer in the same mission.
- Builder/reviewer sees only their active assignments and can open only missions they may access.
- Client/viewer cannot read personal work or become an assignee.
- Management shows the human accountable owner and the agent delegate as separate identities.
- Assignment retry is idempotent; key reuse with different parameters and stale expected versions fail.
- Atomic replacement advances the durable version without a responsibility gap; delayed retries cannot undo it.
- The assignee can accept or reasonedly decline, and only acceptance changes projected accountability.
- A foreign tenant cannot list, assign, revoke, or infer another tenant's assignment.
- A non-executive can sign in without an executive-only company-directory request blocking the workspace.
