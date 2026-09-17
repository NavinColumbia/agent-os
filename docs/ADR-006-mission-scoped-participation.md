# ADR-006: Mission-Scoped Human Participation and Projections

- **Status:** accepted and implemented
- **Date:** 2026-09-17
- **Owners:** identity, product architecture, runtime assurance

## Context

A tenant membership role answers what a person may do in principle. It does not answer which client engagement,
review, or implementation assignment that person may see. Tenant-wide builder, reviewer, client, or viewer
visibility would leak unrelated mission status, evidence, artifacts, and human questions. Hiding navigation in
the browser is not an authorization boundary, and a generic role-addressed notification can create the same
cross-mission leak through an inbox or push channel.

## Decision

Add a durable `MissionParticipant` record keyed by tenant, mission, and subject. Its participation role is one
of `builder`, `reviewer`, `client`, or `viewer`. Grant, revoke, and regrant are idempotent, versioned, attributed
to an actor, and retained rather than deleted. PostgreSQL row-level security applies the existing tenant fence.

Authorization is conjunctive:

```text
tenant membership capability AND active mission participation
```

Owners, managers, operators, and runtime service identities retain portfolio-wide access through the explicit
`mission.read.all` capability or the service-identity boundary. An assignment never widens the capabilities of
the person's tenant role. A reviewer assigned as a reviewer cannot publish an artifact; a client assignment does
not grant internal review or execution authority.

The same mission truth is projected by audience:

- builder: assigned execution and artifact publication within existing capability gates;
- reviewer: claims, bounded evidence, hazards, work/readiness, and action-bound review decisions;
- client/viewer: outcome, progress, milestones, approved deliverables, and stakeholder-safe claim summaries;
- manager/owner/operator: full authorized management and assurance projections.

Raw planning/execution identifiers, internal communications, staffing details, authority/effect ledgers, and
arbitrary artifact or global release inventory are not included in stakeholder projections.

New mission-scoped role notifications are expanded at publication to exact active subject IDs. If no eligible
participant exists, the runtime escalates to the manager/CEO audience and records the fallback instead of
broadcasting to every person with that tenant role. Push subscriptions do not subscribe to mission-scoped role
topics. A read-time assignment check remains as defense for legacy role-addressed records.

## Invariants

1. Tenant role is necessary but not sufficient for scoped mission access.
2. Assignment is tenant-bound and cannot authorize a foreign tenant, even for the same subject or mission ID.
3. Revocation immediately removes list, detail, notification, and projection visibility without deleting audit
   history.
4. Changing an active participation role requires explicit revoke and regrant; it cannot happen silently.
5. A manager may assign only a subject whose active tenant membership includes the matching role.
6. Client/viewer projections never expose internal execution or assurance detail merely because the backing
   endpoint contains it.
7. New role-addressed mission attention resolves to exact subjects before durable publication and external
   delivery.
8. No eligible recipient fails safe by escalating to accountable management; it never broadens to all members.

## Consequences

- Portfolio and detail queries for scoped roles use the participant index instead of retrieving every tenant
  mission and filtering in the browser.
- Mission participant management appears only to principals with mission-steering authority.
- Stakeholder access to a release is through the assigned mission's approved deliverable projection. Global
  deployment inventory and arbitrary artifact endpoints require separate capabilities.
- Existing legacy role notifications remain readable only after the read-time participant check. New runtime
  emissions use exact recipients, avoiding role-topic leakage in push and external delivery.
- Mission-level collaboration can now be extended with task-level ownership without changing the tenant role
  model or creating separate workflow implementations per persona.

## Acceptance

- An unassigned builder, reviewer, client, or viewer sees an empty mission portfolio and receives 404 for direct
  access.
- Assignment reveals exactly that mission; revocation removes it; regrant increments the durable version.
- A foreign tenant cannot observe or alter the assignment under API checks or PostgreSQL RLS.
- Reviewer projections include review evidence but omit effects, budgets, and internal communication.
- Client/viewer projections include status and approved deliverables but omit raw execution IDs and arbitrary
  artifacts.
- A legacy `role:reviewer` notification for an unassigned mission is not returned or retrievable.
- A newly published `role:reviewer` mission notification is materialized to active reviewer subject IDs, with
  a management fallback if no reviewer is assigned.
