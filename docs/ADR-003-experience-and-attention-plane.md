# ADR-003: Persona-Aware Experience and Attention Plane

- **Status:** accepted incrementally
- **Date:** 2026-09-17
- **Owners:** product architecture, identity, runtime assurance

## Context

The durable V2 workflow and mission-assurance kernel can plan, execute, wait, recover, verify, govern effects,
and retain evidence. The product surface still projects nearly every authenticated person as a CEO, polls and
rebuilds screens, treats immutable notification history as an unread inbox, and hides much of the decision and
evidence truth already returned by the API.

Replacing the runtime would discard the strongest part of the system. Adding a separate application per
persona would create competing truth and authorization models.

## Decision

Add an Experience and Attention plane inside the current modular Python/PostgreSQL application. All personas
consume projections over the same mission, organization, authority, decision, evidence, and audit records.

The first records are:

- per-person notification preference;
- per-person notification state layered over immutable notification truth;
- an authenticated session/capability projection;
- mission-level collaboration mode and interruption budget surfaced at intake;
- a durable, idempotent decision-response intent and recovery worker bound to an exact notification,
  workflow wait, actor, and deterministic graph event.

The next compatible records are structured `AttentionItem`, `RecipientReceipt`, `NotificationSubscription`,
and immutable `DeliveryPlan`. Structured decision responses already hide workflow correlation/version mechanics
from clients; the raw graph event route refuses to bypass that durable boundary when this capability is present.

UI defaults to outcome, owner, decision, progress, evidence, and next action. Agent graph, prompts, raw model
output, and traces are advanced diagnostic views.

## Invariants

1. Preference, read, dismiss, and snooze state never mutate or delete notification/audit truth.
2. Server authorization is authoritative; capability-driven UI is explanatory only.
3. Quiet hours and digests cannot suppress a mandatory safety gate; bypass is recorded and narrowly scoped.
4. Human accountability remains explicit when execution is delegated to an agent.
5. Refresh and live updates cannot destroy unsubmitted human input or focus.
6. An unavailable projection is shown as unavailable/stale, never as empty completion.
7. Tenant-facing roles, platform-operator roles, and runtime service identities remain distinct.
8. The first scale path remains PostgreSQL, durable outboxes, stateless APIs/workers, bounded projections, and
   tenant home cells. Kafka, active-active writes, and a language rewrite require measured evidence.

## API direction

```text
GET /v2/me
GET/PUT /v2/notification-preferences
PUT /v2/notifications/{notification_id}/state
POST /v2/decisions/{decision_id}/responses
POST /v2/decisions/{decision_id}/redrive             # owner/operator recovery

GET /v2/inbox?status=open&cursor=...             # implemented seek pagination
GET /v2/events?cursor=...                        # ADR-004 durable catch-up
POST /v2/me/push-subscriptions                   # after VAPID provisioning
```

Live transport, recovery cursors, AG-UI compatibility, and background mobile delivery are governed by
[ADR-004](ADR-004-live-experience-and-mobile-attention.md). They deliberately remain separate from the
decision-response truth defined here.

## Consequences

- Existing mission/runtime/assurance ports remain stable.
- Mutable attention projections require RLS-protected tables and idempotent updates.
- Human responses add a small durable queue so a process crash cannot lose accepted intent or apply it twice.
- An exhausted response keeps its original human intent and deterministic event ID; authorized redrive starts
  a fresh bounded retry cycle while an append-only redrive ledger retains every idempotency key, lifetime
  attempt boundary, and recovery actor.
- Owners/operators receive bounded payload-redacted action timelines; raw payloads, secrets, and lease-owner
  internals remain outside the experience projection.
- External delivery routes must evolve from tenant-wide category matching to recipient/audience-aware policy.
- Read models and cursor pagination become necessary before very large mission histories.
- Persona UX can evolve independently without forking workflow semantics.

## Acceptance

- Viewer cannot create, cancel, revise, settle, or approve a mission.
- Personal dismissal removes an item from open attention without deleting the ledger record.
- A background update never destroys a typed response or configuration draft.
- Every interrupt explains its urgency/routing rationale.
- CEO, builder, reviewer, operator, and administrator land on useful authorized projections of the same mission.
- Keyboard and screen-reader users can complete intake, decision, review, and recovery.
