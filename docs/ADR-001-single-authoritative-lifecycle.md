# ADR-001: Replace overlapping controllers with one authoritative lifecycle

**Status:** accepted; implementation started 2026-09-07
**Decision owner:** Agent OS platform
**Scope:** CEO prompt through deployed product

## Decision

Replace the current controller/orchestra/queue state machinery rather than extending it.

Agent OS will have one framework-neutral product lifecycle aggregate:

```text
INTAKE -> RESEARCH -> SPECIFY -> BUILD -> VERIFY -> RELEASE
```

Its execution condition is a separate dimension:

```text
ACTIVE | WAITING | FAILED | SUCCEEDED | CANCELLED
```

Temporal will persist and replay lifecycle execution. It will not define the business transitions. Pure domain code in [`lifecycle.py`](../src/agent_os/domain/lifecycle.py) owns valid transitions and emits typed application commands. PydanticAI performs bounded model/tool work inside activities. PostgreSQL stores product projections, audit records and idempotent external side-effect receipts; it is not another workflow engine.

## Why a replacement is warranted

The current primary paths contain more than 11,000 lines across `loopcontroller.py`, `orchestra/runtime.py`, `orchestra/store.py`, `controller.py`, `orchestrator.py` and `orchestrate.py`. Product phase, human gates, actor status, queue claims, process health, QA continuation, retries and recovery are coupled through direct SQL updates and large conditional functions.

Examples of the resulting ambiguity:

- `controller_state.phase`, `controller_state.awaiting`, `controller_jobs.status`, `orchestra_runs.status`, `orchestra_actors.status`, QA story status and management cases can all describe the same product's progress differently.
- A process heartbeat, meaningful progress lease and customer-visible phase are reconciled by separate sweepers.
- Worker time limits can become apparent product failures even when durable business work is incomplete.
- QA continuation, repair and evidence review add special branches to a 4,000-line actor runtime instead of using one explicit verification iteration.
- Recovery code changes authoritative rows after workers fail, creating races that require more fencing, reapers and cleanup paths.

The system has valuable tests and product semantics, but the runtime shape makes every reliability fix increase the next failure's state space.

## State ownership

| Concern | Authoritative owner | Not allowed to own it |
|---|---|---|
| Product phase and execution condition | `LifecycleState` replayed by Temporal | UI, watchdog, worker heartbeat, QA actor, model output |
| Workflow history, timers, signals and activity retries | Temporal | PostgreSQL lease/sweeper code |
| Business records, tenancy, approvals, budgets and audit projections | PostgreSQL application repositories | Temporal payloads as the only copy |
| Agent/model/tool loop | `AgentRuntime`/PydanticAI activity | Workflow definitions or UI |
| Model selection and normalized I/O | `ModelGateway` | Domain state or provider SDK objects |
| QA story execution/evidence | QA application module | Product phase |
| Public status | Projection of lifecycle plus current activity attempt | Independently editable status columns |

## Transition rules

1. Product phases move forward only. A repair does not move `VERIFY` back to `BUILD`; it creates another verification cycle and a new immutable artifact revision.
2. Waiting is not a phase. A wait carries a typed reason, correlation ID and exact resume command.
3. A deadline or worker slice is not a product event. It may checkpoint or replace an activity attempt while the lifecycle remains active/waiting.
4. Only explicit non-retryable failures produce `FAILED`. Recoverable failures can receive a typed recovery event. Cancellation and success are terminal.
5. Every event has an immutable ID and expected aggregate version. The workflow/event adapter enforces uniqueness; stale writers cannot advance state.
6. State contains no framework, database, provider or UI object. It serializes to stable primitive records.
7. External side effects require idempotency keys and durable receipts. Workflow replay never repeats a payment, notification, deployment promotion or publication.
8. Humans are contacted only for an authority boundary or requested steering—not because an internal worker reached an arbitrary time limit.

## Mapping from the legacy phases

| Legacy | V2 |
|---|---|
| `DISCOVER` | `INTAKE` |
| `RESEARCH` | `RESEARCH` |
| `OPTIONS`, `DEEP_DESIGN`, `PLAN_APPROVAL` | `SPECIFY` |
| `PROTOTYPE`, `IMPLEMENT` | `BUILD` |
| `TESTQA` plus repair continuations | `VERIFY` plus `verification_cycle` |
| `DELIVER` | `RELEASE`; completion is `status=SUCCEEDED` |
| `awaiting=fleet/user_feedback/...` | typed `WaitState`, independent of phase |
| actor `idle/working/blocked/parked/done/dead` | activity-attempt telemetry, not product state |

## Migration

This is a strangler replacement, not a dual-authority rewrite.

1. Freeze new features in the legacy controllers; urgent correctness/security fixes only.
2. Capture golden histories from the existing critical tests and map them to V2 events/outcomes.
3. Complete the pure lifecycle contract and property/transition tests.
4. Add application ports and an in-memory workflow harness; no database or model imports in the domain.
5. Implement one Temporal workflow shell that replays the pure transition function and schedules idempotent activities.
6. Build projection writers for the existing console/API. V2 lifecycle remains the sole new-run authority.
7. Run a shadow comparison using recorded inputs; never let shadow execution perform external side effects.
8. Canary new internal runs, then selected tenants. Existing legacy runs drain on the old runtime.
9. After parity and recovery tests, route all new runs to V2 and delete the legacy scheduler/lease/reaper paths once no old run remains.

Do not dual-write authoritative state between the old controller and Temporal. A run is born on exactly one engine and stays there until terminal.

## First implementation slice

Implemented now:

- Pure enums and immutable lifecycle state.
- Explicit forward-transition table.
- Orthogonal correlated waits.
- Retryable versus recoverable failure semantics.
- QA repair iterations within `VERIFY`.
- Optimistic event-version fencing, immediate duplicate handling and primitive serialization.
- Contract tests for happy path, invalid phase skips, waits, retry, recovery, repair, cancellation, idempotency and serialization.
- Framework-neutral application seam that turns transitions into replay-stable, idempotently identified
  command envelopes.
- In-memory full-history harness proving deterministic replay, old-event deduplication, conflicting-ID
  rejection, ordered version fencing, and JSON-safe event payloads before the Temporal adapter exists.

Not implemented yet:

- Temporal workflow/activity adapters.
- V2 event store uniqueness and product projections.
- Legacy-history translation and shadow comparison.
- API/console routing to V2.
- Deletion of legacy runtime code.

## Acceptance gates

- Every valid transition is enumerated and every invalid transition fails closed.
- Temporal replay remains deterministic across a worker upgrade.
- Killing workers during every activity boundary produces no lost progress or duplicate side effects.
- A multi-hour genuine operation remains healthy while activity progress is observable; no product failure is synthesized from elapsed time alone.
- Human waits survive restarts and resume only on a matching correlation ID.
- QA repair can iterate without stale evidence becoming authoritative.
- Public status is derived from V2 history/projections and cannot disagree with worker truth.
- All new lifecycle runs use V2 before any legacy controller deletion begins.

## Consequences

The migration is substantial, but continuing the current design would spend more effort reconciling controllers than building the product. Temporal and PydanticAI may still change over time; keeping lifecycle semantics pure makes those migrations bounded. The old test suite remains a behavioral specification, not a mandate to preserve its implementation structure.
