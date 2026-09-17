"""Bounded, optimistic-concurrency reconciliation contracts.

Reconcilers may observe and propose.  They never mutate mission truth directly;
the deterministic admission function rejects stale, conflicting, unsupported,
or authority-changing proposals before a repository applies them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Protocol


class ReconciliationOperationKind(str, Enum):
    RECORD_CLAIM = "record_claim"
    DISPUTE_CLAIM = "dispute_claim"
    ADD_HAZARD = "add_hazard"
    PROPOSE_EFFECT = "propose_effect"
    UPDATE_COMMITMENT = "update_commitment"
    REQUEST_HUMAN = "request_human"
    SET_MILESTONE_PROJECTION = "set_milestone_projection"


@dataclass(frozen=True)
class MissionObservation:
    observation_id: str
    tenant_id: str
    mission_id: str
    source_id: str
    observed_at: str
    valid_at: str
    mission_revision: int
    facts: Mapping[str, Any]
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(not value.strip() for value in (
            self.observation_id, self.tenant_id, self.mission_id, self.source_id,
        )):
            raise ValueError("observation identity, tenant, mission, and source are required")
        if self.mission_revision < 1 or not self.facts:
            raise ValueError("observation needs a positive mission revision and facts")
        for value in (self.observed_at, self.valid_at):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("observation timestamps must include a timezone")


@dataclass(frozen=True)
class ReconciliationOperation:
    kind: ReconciliationOperationKind
    target_id: str
    value: Mapping[str, Any]
    material: bool = False

    def __post_init__(self) -> None:
        if not self.target_id.strip() or not self.value:
            raise ValueError("reconciliation operation needs a target and value")


@dataclass(frozen=True)
class ReconciliationProposal:
    proposal_id: str
    tenant_id: str
    mission_id: str
    reconciler_id: str
    observation_ids: tuple[str, ...]
    expected_revision: int
    operations: tuple[ReconciliationOperation, ...]
    rationale: str
    confidence: float
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(not value.strip() for value in (
            self.proposal_id, self.tenant_id, self.mission_id, self.reconciler_id,
            self.rationale,
        )):
            raise ValueError("reconciliation proposal identity and rationale are required")
        if self.expected_revision < 1 or not self.observation_ids or not self.operations:
            raise ValueError("proposal requires a revision, observations, and operations")
        if not 0 <= self.confidence <= 1:
            raise ValueError("proposal confidence must be between zero and one")
        targets = [(item.kind, item.target_id) for item in self.operations]
        if len(set(targets)) != len(targets):
            raise ValueError("proposal cannot mutate the same target twice")


@dataclass(frozen=True)
class ReconciliationAdmission:
    admitted: bool
    requires_human: bool
    reasons: tuple[str, ...]
    obligations: tuple[str, ...] = field(default_factory=tuple)


class MissionReconciler(Protocol):
    reconciler_id: str

    def observe(self, tenant_id: str, mission_id: str) -> MissionObservation: ...

    def propose(
        self,
        observation: MissionObservation,
    ) -> ReconciliationProposal | None: ...


def admit_reconciliation(
    proposal: ReconciliationProposal,
    *,
    tenant_id: str,
    mission_id: str,
    current_revision: int,
    known_observation_ids: frozenset[str],
    known_evidence_ids: frozenset[str],
) -> ReconciliationAdmission:
    """Validate a proposal without executing its requested effects."""

    reasons: list[str] = []
    obligations: list[str] = []
    admitted = True
    requires_human = False
    if (proposal.tenant_id, proposal.mission_id) != (tenant_id, mission_id):
        reasons.append("proposal crosses its tenant or mission boundary")
        admitted = False
    if proposal.expected_revision != current_revision:
        reasons.append("proposal observed a stale mission revision")
        admitted = False
    if set(proposal.observation_ids) - known_observation_ids:
        reasons.append("proposal cites an unknown observation")
        admitted = False
    if set(proposal.evidence_ids) - known_evidence_ids:
        reasons.append("proposal cites unknown evidence")
        admitted = False
    if len(proposal.operations) > 32:
        reasons.append("proposal exceeds the bounded operation limit")
        admitted = False

    for operation in proposal.operations:
        if operation.kind is ReconciliationOperationKind.PROPOSE_EFFECT:
            obligations.append("route proposed effect through the assurance kernel")
        if operation.kind in {
            ReconciliationOperationKind.ADD_HAZARD,
            ReconciliationOperationKind.UPDATE_COMMITMENT,
        } and operation.material:
            requires_human = True
        if operation.kind is ReconciliationOperationKind.SET_MILESTONE_PROJECTION:
            obligations.append("projection must not overwrite mission truth")
    if proposal.confidence < 0.5 and any(item.material for item in proposal.operations):
        requires_human = True
        reasons.append("low-confidence material change requires human review")
    if admitted and not reasons:
        reasons.append("proposal passed version, evidence, scope, and conflict checks")
    if requires_human:
        obligations.append("obtain attributable human decision before applying material change")
    return ReconciliationAdmission(
        admitted=admitted,
        requires_human=requires_human,
        reasons=tuple(dict.fromkeys(reasons)),
        obligations=tuple(dict.fromkeys(obligations)),
    )
