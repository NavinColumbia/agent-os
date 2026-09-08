"""Adaptive mission intake, resource inventory, and human copilot contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class GapKind(str, Enum):
    HUMAN_ROLE = "human_role"
    AUTHORITY = "authority"
    CREDENTIAL = "credential"
    DATA = "data"
    BUDGET = "budget"
    LEGAL = "legal"
    INFRASTRUCTURE = "infrastructure"
    DECISION = "decision"
    PHYSICAL_ACTION = "physical_action"


class GapStatus(str, Enum):
    OPEN = "open"
    REQUESTED = "requested"
    RESOLVED = "resolved"
    WAIVED = "waived"


@dataclass(frozen=True)
class ResourceInventory:
    human_ids: frozenset[str] = field(default_factory=frozenset)
    agent_ids: frozenset[str] = field(default_factory=frozenset)
    service_ids: frozenset[str] = field(default_factory=frozenset)
    connector_ids: frozenset[str] = field(default_factory=frozenset)
    credential_refs: frozenset[str] = field(default_factory=frozenset)
    data_asset_ids: frozenset[str] = field(default_factory=frozenset)
    jurisdictions: frozenset[str] = field(default_factory=frozenset)
    available_budget_cents: int = 0

    def __post_init__(self) -> None:
        if self.available_budget_cents < 0:
            raise ValueError("available budget cannot be negative")


@dataclass(frozen=True)
class PrerequisiteGap:
    gap_id: str
    kind: GapKind
    question: str
    why_needed: str
    requested_from: str
    blocking: bool
    status: GapStatus = GapStatus.OPEN
    resolution: str | None = None

    def __post_init__(self) -> None:
        if not self.gap_id or not self.question or not self.why_needed or not self.requested_from:
            raise ValueError("gap identity, question, reason, and recipient are required")
        if self.status is GapStatus.RESOLVED and not self.resolution:
            raise ValueError("resolved prerequisite gaps require a resolution")


@dataclass(frozen=True)
class MissionCharter:
    mission_id: str
    tenant_id: str
    outcome: str
    requested_by: str
    budget_limit_cents: int
    success_measures: tuple[str, ...]
    constraints: tuple[str, ...] = ()
    prohibited_actions: tuple[str, ...] = ()
    desired_by: str | None = None
    revision: int = 1

    def __post_init__(self) -> None:
        if not self.mission_id or not self.tenant_id or not self.outcome or not self.requested_by:
            raise ValueError("mission identity, tenant, outcome, and requester are required")
        if self.budget_limit_cents < 0 or self.revision < 1 or not self.success_measures:
            raise ValueError("mission requires a budget, positive revision, and success measures")


@dataclass(frozen=True)
class IntakeSnapshot:
    charter: MissionCharter
    resources: ResourceInventory
    gaps: tuple[PrerequisiteGap, ...]
    revision: int

    def __post_init__(self) -> None:
        if self.revision < 1 or len({gap.gap_id for gap in self.gaps}) != len(self.gaps):
            raise ValueError("intake revision must be positive and gap IDs unique")

    def questions(self, *, limit: int = 5) -> tuple[PrerequisiteGap, ...]:
        """Return one consolidated, blocking-first adaptive question packet."""

        if limit < 1:
            raise ValueError("question limit must be positive")
        unresolved = [gap for gap in self.gaps if gap.status in {GapStatus.OPEN, GapStatus.REQUESTED}]
        unresolved.sort(key=lambda gap: (not gap.blocking, gap.kind.value, gap.gap_id))
        return tuple(unresolved[:limit])

    @property
    def can_execute(self) -> bool:
        return not any(gap.blocking and gap.status is not GapStatus.RESOLVED for gap in self.gaps)


@dataclass(frozen=True)
class OrganizationChangeProposal:
    proposal_id: str
    proposed_by: str
    summary: str
    operations: tuple[str, ...]
    reason: str
    estimated_cost_cents: int
    reversible: bool
    needs_human_approval: bool

    def __post_init__(self) -> None:
        if not self.proposal_id or not self.proposed_by or not self.summary or not self.operations or not self.reason:
            raise ValueError("organization proposal identity, author, operations, and reason are required")
        if self.estimated_cost_cents < 0:
            raise ValueError("proposal cost cannot be negative")


@dataclass(frozen=True)
class PersonalCopilotGrant:
    grant_id: str
    human_id: str
    assistant_agent_id: str
    scopes: frozenset[str]
    granted_by: str
    expires_at: str | None = None
    active: bool = True

    def __post_init__(self) -> None:
        if not self.grant_id or not self.human_id or not self.assistant_agent_id or not self.granted_by:
            raise ValueError("copilot grant identity, human, assistant, and grantor are required")
        if not self.scopes:
            raise ValueError("a personal copilot requires explicit permission scopes")
