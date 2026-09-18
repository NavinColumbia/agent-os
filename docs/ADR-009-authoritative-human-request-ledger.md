# ADR-009: Authoritative Human-Request Ledger

- Status: accepted
- Date: 2026-09-17
- Owners: product architecture, identity/security, workflow runtime

## Context

Agent OS already had immutable notifications, structured workflow decisions, mission messages, and external
delivery. A notification, however, is a delivery hint: it can fan out to aliases and fallbacks, be snoozed,
arrive late, or fail to reach a device. It cannot authoritatively answer who owns a request, whether it still
blocks work, whether a response is durably pending, or whether the owning run made the request obsolete.

Treating every question as a workflow pause was also incorrect. A manager must be able to ask a person for
input while independent work continues. Conversely, a workflow-blocking decision must not be answerable by an
observer merely because escalation routing made the notification visible to them.

## Decision

Add `aos_v2_human_requests` as the tenant-fenced source of truth for requests to people. Notifications remain
immutable delivery and attention records linked one-to-one to a request; they never define request authority.

Every new request has exactly one authoritative recipient address. Delivery may expand a role address to
mission participants or add management fallbacks, but those observers cannot answer for the authoritative
recipient. Legacy `human_action_required` notifications remain readable and do not silently create ledger
state unless the producer explicitly supplies the request contract.

Two request kinds are supported:

- `workflow_blocking`: has a durable correlation ID, is created only from a persisted workflow wait, and is
  answered through the governed structured-decision worker;
- `advisory`: has no wait correlation, never changes workflow state, and can be answered directly by the exact
  recipient.

The lifecycle is `open → response_pending → answered`. Failed response application enters
`recovery_required` and an authorized redrive returns it to `response_pending`. A run ending with no response
changes `open → cancelled`; a recorded response that can no longer apply changes to `superseded`. Terminal
closure prevents a failed response from being redriven against dead work.

Request creation, answers, closure, decision admission, recovery, and completion are idempotent and append
privacy-safe experience events. Raw request bodies and responses stay in the authenticated in-app boundary.
External email/chat/webhook/push delivery contains an opaque hint even if a route normally permits full
notification bodies.

## Required invariants

1. Tenant identity is present in every key, query, uniqueness constraint, and row-level policy.
2. One notification, source action, or workflow correlation cannot create two request identities.
3. A workflow wait has exactly one recipient before `NODE_WAITED` is committed.
4. Visibility does not confer response authority; both mission access and the exact recipient address are
   checked at response admission.
5. Advisory requests do not pause, resume, revise, or otherwise mutate the mission lifecycle.
6. A response is never discarded after admission: it is applied, recoverable, or explicitly superseded.
7. Presentation state such as read/snoozed does not change request truth.
8. Delivery failure does not change request truth, and external delivery never contains request content.
9. Terminal runs cannot accept new requests and close all active requests with an auditable reason.
10. `agentos_worker` has no direct table privilege; mutation remains in the tenant app boundary and governed
    decision worker contract.

## Consequences

The CEO workspace and API can now distinguish “ask while work continues” from “a decision blocks progress,”
show the durable owner/status, preserve drafts through refresh, and recover response execution without asking a
person to repeat intent. Managers and escalation fallbacks can monitor a request without impersonating its
recipient.

This does not yet implement delegation, expiry/SLA escalation, email reply ingestion, or a complete request
queue with opaque cursor pagination. Those extend this ledger; they must not create a second authority model.

## Rejected alternatives

- **Notifications as request state:** conflates delivery with authority and fails under fan-out/retry.
- **Conversation messages as approvals:** lacks exact authority, correlation, and deterministic response
  application.
- **All questions pause workflows:** destroys useful parallelism and encourages fake blocking states.
- **Multiple authoritative recipients:** creates racing or ambiguous decisions; quorum/multi-approval must be
  modeled as several exact requests plus an explicit aggregation node.
- **External channels carry full bodies:** leaks proprietary or personal context into systems with different
  retention and authorization boundaries.

