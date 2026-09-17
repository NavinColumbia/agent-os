# ADR-007: Mission-Scoped Multiway Conversation

- **Status:** accepted and implemented
- **Date:** 2026-09-17
- **Owners:** product architecture, identity, runtime assurance

## Context

A mission could previously be created, inspected, revised by a manager, or resumed through a structured human
decision. A participant could not ask a durable question, clarify a requirement, or provide an ordinary update
inside the mission. Using the approval channel as chat would weaken exact-action approval semantics; using an
external chat product as truth would lose tenant fencing, mission history, and recovery.

## Decision

Add an immutable `MissionMessage` ledger inside the existing PostgreSQL experience plane. Every message is
bound to tenant, mission, authenticated sender, server-derived persona, channel, kind, idempotency key, and
server timestamp. The initial channels are:

- `shared`: visible to every active participant assigned to the mission and portfolio-wide mission managers;
- `internal`: visible only to builders/reviewers and principals with portfolio-wide mission authority.

The initial kinds are `comment`, `question`, and `update`. External clients/viewers may comment or ask on the
shared channel; they cannot publish authoritative status updates or enter the internal delivery channel.

A question creates a separate safe notification addressed to the exact active mission participants plus the
accountable management audience. The notification includes only mission/message identifiers and a generic
prompt to open the authorized conversation. The question body remains behind the mission authorization check
and is never copied into push/webhook payloads.

Conversation and approval remain distinct. A comment such as “looks good” cannot approve an effect, resume a
workflow wait, revise authority, or release a product. Consequential intent still uses the structured,
parameter-bound decision API.

## Invariants

1. Tenant RLS and active mission participation are enforced before reading or appending a message.
2. Sender identity and persona are derived from authentication, never accepted from message content.
3. Idempotency-key reuse with changed content is rejected; retries return the original immutable message.
4. Client/viewer projections never include internal messages, including through live invalidation events.
5. Message bodies never enter safe experience summaries or background notification payloads.
6. Questions fail visibly through the durable conversation even if optional external delivery is unavailable.
7. Conversation text never carries approval authority or bypasses mission revision/effect policy.
8. Bounded reads return the latest 200 messages at most; larger-history pagination is a compatible next step.

## Consequences

- CEO, manager, builder, reviewer, and client can communicate around one durable mission without exposing the
  workflow graph as the primary interaction model.
- Draft text survives ambient live refresh in the browser and is cleared only after a successful append.
- Shared questions reach exact participants and managers while retaining a privacy-minimized notification.
- Agent and integration adapters can append through the same authenticated boundary in a later increment;
  they must not write directly to a separate chat truth.
- Retention/redaction policy, attachments, mentions, direct messages, and cursor pagination can extend this
  ledger without changing approval semantics.

## Acceptance

- An assigned client sees shared messages, cannot see/post internal messages, and cannot publish a status update.
- A reviewer sees shared and internal messages for an assigned mission only.
- An unassigned participant receives 404 for both list and append.
- Repeating the same message request is idempotent; changing its content under the same key is rejected.
- A shared client question produces one generic notification for authorized participants/management and does
  not expose its body in notification or experience-event data.
- Keyboard users can read and post from the mission drawer without ambient refresh destroying the draft.
