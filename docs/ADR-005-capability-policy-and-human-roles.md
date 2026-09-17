# ADR-005: Capability Policy and Human Delivery Roles

- **Status:** accepted and implemented incrementally
- **Date:** 2026-09-17
- **Owners:** identity, product architecture, runtime assurance

## Context

Role-name checks had accumulated independently in API routes and the browser. That creates two unsafe failure
modes: a screen can promise an action that the server rejects, or a hidden endpoint can accept authority that
the session does not disclose. It also makes new human personas cosmetic unless every route is edited correctly.

## Decision

Keep coarse, auditable tenant membership roles while deriving named capabilities from one policy in
`agent_os.domain.access`. Server authorization remains authoritative. The session publishes the same capability
union for clients, and persona is only a presentation preset.

Human membership roles are:

| Role | Default responsibility | Material authority |
|---|---|---|
| owner | accountable company principal | all tenant capabilities and ownership delegation |
| admin | identity, model, integration, and policy administration | manage ordinary members and platform configuration, never owners/admins |
| manager | mission/program delivery | create, steer, cancel, staff, assign, and release work |
| operator | runtime reliability and recovery | operational audit/recovery plus mission operations, never billing |
| builder | assigned implementation | execute work, report hazards, request scoped effects, publish artifacts |
| reviewer | independent verification | read work/evidence, report hazards, answer action-bound decisions |
| billing | subscription and cost administration | billing changes and usage visibility, no execution authority |
| client | external stakeholder | assigned-mission milestones, review, and explicitly addressed decisions |
| viewer | internal read-only observer | directory, assigned-mission status, and explicitly addressed decisions |

Runtime `agent` and `system` roles are service identities and cannot be invited as human memberships.

## Invariants

1. API routes authorize named capabilities, not UI visibility or user-supplied role names.
2. Multiple roles union capabilities; persona priority changes presentation only.
3. Unknown identity-provider roles receive the viewer-equivalent baseline and no mutation capability.
4. A tenant administrator cannot grant/revoke owner or administrator authority. Only an owner can cross that
   boundary.
5. Operator authority never implies billing authority.
6. Builder authority never implies mission creation, cancellation, membership, integration, or billing power.
7. Reviewer authority never implies artifact publication or runtime mutation.
8. Direct notification response remains an action-bound capability: the exact server-authorized recipient and
   decision brief still constrain the action.
9. Low-level graph creation, evidence/claim publication, external effects, and recovery require explicit
   capabilities even if a client hides those endpoints.
10. Tenant and project/resource scoping remain additional constraints; a capability is necessary, never
    sufficient to cross a tenant or resource boundary.

## Consequences

- Adding a role or changing authority is reviewable in one bounded policy and its matrix tests.
- The browser can adapt landing views and controls without reconstructing security logic.
- Existing owner/operator behavior is retained except where it contradicted the published capabilities.
- Resource-scoped mission participation is implemented by
  [ADR-006](ADR-006-mission-scoped-participation.md). Tenant role grants a capability; an active assignment is
  additionally required for builder, reviewer, client, and viewer access to a specific mission.

## Acceptance

- Every human role has a distinct tested persona/capability projection.
- Manager can create a mission; administrator, billing, builder, reviewer, client, and viewer cannot.
- Billing can manage subscriptions; operator cannot.
- Administrator can invite/revoke ordinary members but cannot grant or revoke owner/admin authority.
- Viewer cannot publish claims/evidence or start low-level graph execution.
- Capability-gated browser controls never advertise forbidden membership, model, staffing, integration, or
  billing actions.
- Unassigned builders, reviewers, clients, and viewers cannot enumerate or retrieve a mission, its attention,
  artifacts, or internal execution projection.
