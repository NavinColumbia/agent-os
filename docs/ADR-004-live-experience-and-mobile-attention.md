# ADR-004: Live Experience Transport and Mobile Attention

- **Status:** accepted; durable catch-up, streaming, and AG-UI edge adapter implemented; push staged
- **Date:** 2026-09-17
- **Owners:** experience, runtime assurance, platform operations

## Context

The workspace currently uses bounded REST projections and a quiet ten-second polling fallback. Cursor-paginated
attention history is durable, but it is not a live event plane. Native `EventSource` also cannot attach the
in-memory bearer and selected-organization headers used by this workspace. Browser notification calls while a
page is open are not background mobile push.

AG-UI is useful as an agent-to-frontend compatibility vocabulary: it defines lifecycle, activity,
snapshot/delta, subagent, tool, message, and custom events. Its own guidance requires ordered processing and
snapshot recovery when deltas diverge. Some interrupt behavior remains draft, and event names have continued to
evolve, so it must not become Agent OS's database schema or approval truth.
[AG-UI events](https://github.com/ag-ui-protocol/ag-ui/blob/main/docs/concepts/events.mdx),
[AG-UI state](https://github.com/ag-ui-protocol/ag-ui/blob/main/docs/concepts/state.mdx)

The HTML standard gives SSE reconnect and `Last-Event-ID` semantics. The Push API is a different mechanism: a
per-service-worker subscription lets a push service wake the worker and carries endpoint plus P-256/auth key
material. Conflating the two would either lose foreground recovery or falsely claim background delivery.
[WHATWG server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html),
[W3C Push API](https://www.w3.org/TR/push-api/)

## Decision

1. **Agent OS owns a durable experience-event log.** A tenant-monotonic cursor records projection invalidations
   and safe structured activity. The source mutation and event append share a transaction/outbox boundary.
   Telemetry and process logs never masquerade as user-visible business events.
2. **REST snapshots remain authoritative.** On first connection, cursor expiry, detected divergence, or client
   schema mismatch, the client reloads a bounded snapshot. Deltas apply only to a known snapshot revision.
3. **SSE is the first foreground transport.** The browser uses streaming `fetch`, not native `EventSource`, so
   bearer and active-organization headers remain memory-only. The stream supports a tenant-bound cursor,
   heartbeat comments, bounded connection lifetime, backoff, and the current polling fallback.
4. **AG-UI is a version-pinned edge adapter.** Agent run/activity/subagent events may be translated to a tested
   AG-UI contract for third-party clients. Internal approvals, authority, costs, evidence, and workflow state
   retain Agent OS schemas. Draft AG-UI interrupt events do not replace the durable decision-response protocol.
5. **No private reasoning stream.** User-facing events expose status, action, evidence, and diagnostic facts;
   chain-of-thought, raw prompts, secrets, lease owners, and unrestricted tool payloads remain private.
6. **Web Push is a separate attention delivery adapter.** A person explicitly enrolls each device. Subscription
   endpoints and authentication material receive tenant/person/device scoping, encryption/secret handling,
   revocation, expiry refresh, redacted payloads, retries, receipts, quiet-hour policy, and an in-app fallback.
   VAPID/public-key provisioning is an activation dependency, not a reason to weaken the design.
7. **PWA first, native app by evidence.** Installability, responsive decision flows, and background push cover the
   first mobile use case. A native app requires measured PWA limitations.

## Event contract direction

```text
ExperienceEvent {
  tenant_sequence, event_id, occurred_at,
  resource_type, resource_id, projection_revision,
  kind, audience, safe_summary, trace_id
}

GET /v2/events?cursor=...                 # bounded catch-up
GET /v2/events/stream?cursor=...          # authenticated SSE
GET /v2/...                               # authoritative snapshots
POST/DELETE /v2/me/push-subscriptions/... # explicit device enrollment
```

The event log carries enough information to refetch or safely update a projection, not a duplicate unbounded
copy of every domain object. Retention exposes an explicit minimum cursor; an older cursor receives a snapshot
reset response rather than silent loss.

## Implementation order

1. tenant-sequenced experience-event store, RLS, retention floor, and transactionally appended notification/
   mission/decision events;
2. bounded catch-up API and projection-revision contract;
3. fetch-SSE client with reconnect, visibility handling, resnapshot, and polling fallback;
4. pinned AG-UI compatibility tests at the public adapter boundary;
5. push-subscription metadata/secret boundary, service-worker handlers, governed delivery worker, and live-device
   receipts after VAPID provisioning.

## Current implementation

Migration 105 and `SQLExperienceEventLog` now provide the tenant-monotonic stream, forced-RLS audience rows,
retention floor, idempotent source keys, and bounded catch-up API. Notification publication, personal attention
state/preferences, and structured decision admission/completion append safe events in the same database
transaction as their source mutation. Cursor tokens are bound to the selected organization; an expired cursor
returns an explicit snapshot-reset requirement. The migration is included in both local Compose and the
production migration image.

Authenticated foreground SSE is also implemented for that stream. It accepts the opaque cursor or
`Last-Event-ID`, uses bounded connections and heartbeat comments, emits explicit reset/cursor events, and is
consumed through streaming `fetch` so bearer and organization headers stay in memory. The browser pauses the
stream while hidden, reconnects with backoff, coalesces invalidations, preserves typed input, and retains the
ten-second polling path as a degradation fallback.

Authoritative lifecycle transitions, arbitrary-workflow starts/events, and mission-assurance mutations now
append to the same stream inside their version-fenced source transactions. Mission creation/revision, evidence,
claims, hazards, delegated authority, assurance decisions, and effect settlement therefore produce durable live
invalidations without copying private record content into the stream. Company-roster and external-connector
changes use that same transactional path, as do membership/invitation changes, model-policy changes, billing
projection changes, notification-route changes, and preview publication/revocation. Individual artifact writes
are deliberately coalesced behind their lifecycle, evidence, or deployment event instead of flooding an open
browser with internal output churn.

The public catch-up and SSE endpoints now accept `protocol=ag-ui`. The adapter uses the exact-pinned official
Python package `ag-ui-protocol==0.1.22` and emits disclosure-narrowing `CUSTOM` events; official Pydantic models
validate the boundary in regression tests. The default Agent OS contract is unchanged. Tenant identity, audience
membership, private payloads, approval authority, and workflow internals are not copied into the AG-UI value.
AG-UI remains an edge representation rather than persistence or authorization truth. Provisioned Web Push remains
staged, so the endpoint does not yet claim that every background device is live.

## Acceptance

- reconnect from the last acknowledged cursor neither loses nor duplicates a business transition;
- an expired cursor forces a visible snapshot reset;
- stream loss never blocks mission execution or destroys draft input;
- tenant/audience authorization is applied before an event leaves the server;
- a slow client cannot create unbounded server memory or database work;
- foreground SSE, background push, and polling yield the same authoritative state;
- push opt-out/revocation stops future delivery without deleting the audit record.
