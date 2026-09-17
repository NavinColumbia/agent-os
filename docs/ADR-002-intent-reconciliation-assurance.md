# ADR-002: Intent model, bounded reconciliation, and deterministic assurance

**Status:** accepted; production vertical slice implemented 2026-09-17
**Decision owner:** Agent OS platform
**Scope:** every mission, agent/human delegation, material external effect, and completion claim

## Decision

The six-stage V2 lifecycle remains a customer-facing milestone projection and compatibility adapter. It no
longer governs the complete internal organization. Live work is governed by a tenant/cell/mission-scoped intent
model containing the objective, success measures, constraints, hazards, claims, evidence, authority, budget,
commitments, and desired/current-state observations.

Bounded specialist reconcilers observe that model and propose version-fenced changes. A reconciler cannot mutate
mission truth, increase authority, spend money, or perform an external effect. Material changes and every proposed
effect pass deterministic admission before execution.

The assurance kernel binds one proposed effect to:

- the tenant, mission, actor, accountable principal, and complete delegation chain;
- exact action and resource scopes, an expiry, policy version, and available authority budget;
- mission prohibitions, known hazards, safety constraints, reversibility, and a safe fallback;
- a local AuthZEN-shaped policy decision and an atomic double-entry budget reservation;
- an idempotency key, input digest, decision receipt, evidence, and eventual settlement.

An effect is `allowed`, `restricted`, `human_required`, or `denied`. Anything other than `allowed` carries an
explicit safe mode such as read-only, draft-only, rollback, freeze, or human review. Policy failure is fail-closed.

## State ownership

| Concern | Authoritative owner |
|---|---|
| Mission objective, constraints, hazards, authority and accountable owner | mission model |
| Current business claims and their historical validity | bitemporal claim/evidence projection |
| Milestone visible to the CEO | V2 lifecycle projection |
| Durable timers, waits, retries and execution history | selected `WorkflowEngine` adapter |
| Proposed desired/current-state convergence | bounded reconcilers |
| Effect admission and safe fallback | deterministic assurance kernel |
| Spendable, reserved and settled mission funds | balanced mission commitment ledger |
| Raw evidence and personal data | retention-controlled artifact/data store, never the permanent ledger |

The logical mission model is not a physically centralized global brain. It is sharded by cell, tenant, and
mission. Reconcilers use optimistic versions, idempotent operations, bounded retries, and local policy bundles.

## Compatibility and migration

- [`ADR-001`](ADR-001-single-authoritative-lifecycle.md) continues to govern the coarse customer milestone and
  its non-dual workflow-engine migration rules.
- Existing arbitrary workflow graphs remain execution plans. They are not promoted into business truth.
- Existing runs can drain unchanged. New directives create a canonical mission contract using the same run ID.
- DBOS remains the bootstrap adapter. Temporal and Restate must pass the same failure-injection and contract
  corpus before either becomes a growth default; the choice is no longer predetermined by architecture prose.
- AuthZEN is the stable PEP/PDP boundary. Cedar is the preferred future application/effect policy evaluator;
  OPA remains appropriate for infrastructure/admission policy. PostgreSQL RLS remains defense in depth.

## Implemented production slice

- `domain/mission_model.py`: mission, claim, evidence reference, hazard, authority, effect, and decision contracts.
- `application/assurance.py`: authority attenuation and deterministic effect admission.
- `application/reconciliation.py`: observation/proposal boundary and stale/conflict/evidence checks.
- `application/runtime_effects.py`: workflow effect firewall; every external tool call receives a
  version-bound, exact-input authority check before its handler runs and a durable settlement afterward.
- `infrastructure/authzen_policy.py`: local AuthZEN-shaped policy adapter and baseline policy.
- `infrastructure/sql_mission_control.py`: tenant-fenced repository, dependency invalidation projection,
  recursive revocation, privacy tombstones, decisions, reservations and settlement.
- Signed Ed25519 policy bundles with active windows, corrupt/stale-bundle rejection, and fail-closed policy
  unavailability behavior.
- Immutable mission revision history. Each revision uses optimistic concurrency, balances any budget delta,
  fences stale grants, and creates a new revision-bound runtime grant.
- Evidence-backed trajectory control for continue, diagnose, replan, context reset, escalation and freeze;
  progress is not terminated by a crude elapsed-time limit.
- Correlated request/response communications, surfaced decision conflicts, independent maker/checker claims,
  bounded human-attention modes, and governed improvement candidates with holdout evaluation, canary and rollback.
- Migrations `100-mission-assurance-kernel-v2.sql` and `101-mission-revision-history-v2.sql`, HTTP endpoints,
  directive compatibility wiring, CEO mission-control projection, and tenant RLS policies.

## Release validation

Architecture is not considered proven merely because its unit tests pass. The release gate includes the local
unit/integration/security/migration corpus plus environment-specific staging exercises for:

1. concurrent effect-admission and budget-overdraft tests on PostgreSQL;
2. policy unavailable/stale/corrupt-bundle tests;
3. authority expiry and revocation during execution;
4. crash between admission, effect execution, receipt recording, and settlement;
5. hazard-triggered safe-mode and human-release exercises;
6. DBOS/Temporal/Restate replay and failure-injection comparison;
7. multi-day trajectory, context-reset, provider-outage, contradictory-evidence, and delayed-human scenarios.
