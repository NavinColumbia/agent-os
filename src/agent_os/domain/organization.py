"""Domain vocabulary for a non-linear, human-governed AI organization.

These types are deliberately independent of model and workflow frameworks.
They describe the organization that executes inside a product lifecycle: many
teams, a dependency graph of work, direct/peer/upward communication, explicit
decisions, and proactive escalation.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Mapping


class AgentStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


class WorkStatus(str, Enum):
    BACKLOG = "backlog"
    READY = "ready"
    ACTIVE = "active"
    WAITING = "waiting"
    BLOCKED = "blocked"
    REVIEW = "review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MessageKind(str, Enum):
    UPDATE = "update"
    REQUEST = "request"
    RESPONSE = "response"
    OBSERVATION = "observation"
    RISK = "risk"
    ESCALATION = "escalation"
    CHALLENGE = "challenge"
    DECISION = "decision"


class Audience(str, Enum):
    DIRECT = "direct"
    TEAM = "team"
    WORKSTREAM = "workstream"
    ORGANIZATION = "organization"
    HUMAN = "human"


class DecisionStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    REVERSED = "reversed"


class ParticipantKind(str, Enum):
    AGENT = "agent"
    HUMAN = "human"
    SERVICE = "service"


class EscalationAction(str, Enum):
    FOLLOW_UP = "follow_up"
    NOTIFY_MANAGER = "notify_manager"
    REASSIGN = "reassign"
    ENGAGE_VENDOR = "engage_vendor"
    REQUEST_EXECUTIVE_DECISION = "request_executive_decision"


@dataclass(frozen=True)
class Team:
    team_id: str
    name: str
    purpose: str
    manager_id: str | None = None

    def __post_init__(self) -> None:
        if not self.team_id.strip() or not self.name.strip() or not self.purpose.strip():
            raise ValueError("team_id, name, and purpose are required")


@dataclass(frozen=True)
class AgentProfile:
    agent_id: str
    role: str
    team_id: str
    manager_id: str | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)
    tool_grants: frozenset[str] = field(default_factory=frozenset)
    hiring_authority: bool = False
    spending_limit_cents: int = 0
    status: AgentStatus = AgentStatus.ACTIVE

    def __post_init__(self) -> None:
        if not self.agent_id.strip() or not self.role.strip() or not self.team_id.strip():
            raise ValueError("agent_id, role, and team_id are required")
        if self.manager_id == self.agent_id:
            raise ValueError("an agent cannot manage itself")
        if self.spending_limit_cents < 0:
            raise ValueError("spending_limit_cents cannot be negative")


@dataclass(frozen=True)
class HumanParticipant:
    participant_id: str
    display_name: str
    team_id: str
    responsibilities: tuple[str, ...]
    manager_id: str | None = None
    response_sla_seconds: int = 86_400
    quality_criteria: tuple[str, ...] = ()
    active: bool = True

    def __post_init__(self) -> None:
        if not self.participant_id or not self.display_name or not self.team_id:
            raise ValueError("human participant identity, name, and team are required")
        if not self.responsibilities or self.response_sla_seconds <= 0:
            raise ValueError("human participant responsibilities and a positive response SLA are required")


@dataclass(frozen=True)
class ServiceParticipant:
    participant_id: str
    name: str
    capabilities: frozenset[str]
    owner_id: str
    active: bool = True

    def __post_init__(self) -> None:
        if not self.participant_id or not self.name or not self.owner_id or not self.capabilities:
            raise ValueError("service identity, owner, and capabilities are required")


@dataclass(frozen=True)
class WorkContract:
    work_id: str
    objective: str
    requested_by: str
    accountable_agent_id: str
    assignee_ids: tuple[str, ...]
    status: WorkStatus = WorkStatus.BACKLOG
    dependency_ids: frozenset[str] = field(default_factory=frozenset)
    acceptance_criteria: tuple[str, ...] = ()
    progress_percent: int = 0
    last_progress_at: str | None = None
    next_update_at: str | None = None
    wait_correlation_id: str | None = None
    blocker: str | None = None
    evidence_ids: tuple[str, ...] = ()
    revision: int = 0

    def __post_init__(self) -> None:
        if not self.work_id.strip() or not self.objective.strip() or not self.requested_by.strip():
            raise ValueError("work_id, objective, and requested_by are required")
        if not self.accountable_agent_id.strip() or not self.assignee_ids:
            raise ValueError("work requires one accountable agent and at least one assignee")
        if len(set(self.assignee_ids)) != len(self.assignee_ids):
            raise ValueError("work assignees must be unique")
        if self.work_id in self.dependency_ids:
            raise ValueError("work cannot depend on itself")
        if not 0 <= self.progress_percent <= 100 or self.revision < 0:
            raise ValueError("invalid progress or revision")
        waiting = self.status in {WorkStatus.WAITING, WorkStatus.BLOCKED}
        if waiting and not (self.wait_correlation_id or self.blocker):
            raise ValueError("waiting/blocked work must explain what it needs")
        if self.status is WorkStatus.SUCCEEDED:
            if self.progress_percent != 100 or not self.evidence_ids:
                raise ValueError("succeeded work requires 100% progress and evidence")

    def report_progress(
        self,
        *,
        percent: int,
        observed_at: str,
        next_update_at: str | None,
    ) -> "WorkContract":
        if self.status in {WorkStatus.SUCCEEDED, WorkStatus.CANCELLED}:
            raise ValueError("terminal work cannot report progress")
        if percent < self.progress_percent:
            raise ValueError("progress cannot move backwards; record a revision or finding instead")
        return replace(
            self,
            status=WorkStatus.ACTIVE,
            progress_percent=percent,
            last_progress_at=observed_at,
            next_update_at=next_update_at,
            wait_correlation_id=None,
            blocker=None,
            revision=self.revision + 1,
        )


@dataclass(frozen=True)
class Message:
    message_id: str
    conversation_id: str
    sender_id: str
    recipient_ids: tuple[str, ...]
    audience: Audience
    kind: MessageKind
    subject: str
    body: str
    created_at: str
    related_work_id: str | None = None
    requires_response: bool = False
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.message_id,
            self.conversation_id,
            self.sender_id,
            self.subject,
            self.body,
            self.created_at,
        )
        if not all(value.strip() for value in required):
            raise ValueError("message identity, sender, content, and timestamp are required")
        if self.audience in {Audience.DIRECT, Audience.HUMAN} and not self.recipient_ids:
            raise ValueError("direct/human messages require recipients")
        if self.requires_response and not self.correlation_id:
            raise ValueError("a requested response requires a correlation_id")


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    actor_id: str
    intent: str
    considered_options: tuple[str, ...]
    chosen_option: str
    rationale: str
    evidence_ids: tuple[str, ...]
    confidence: float
    reversible: bool
    status: DecisionStatus = DecisionStatus.PROPOSED
    approval_id: str | None = None

    def __post_init__(self) -> None:
        if not self.decision_id or not self.actor_id or not self.intent or not self.rationale:
            raise ValueError("decision identity, actor, intent, and rationale are required")
        if self.chosen_option not in self.considered_options:
            raise ValueError("chosen option must be among considered options")
        if not 0 <= self.confidence <= 1:
            raise ValueError("decision confidence must be between zero and one")


@dataclass(frozen=True)
class ReviewRecord:
    review_id: str
    work_id: str
    reviewer_id: str
    score: float
    accepted: bool
    findings: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.review_id or not self.work_id or not self.reviewer_id:
            raise ValueError("review identity, work, and reviewer are required")
        if not 0 <= self.score <= 1:
            raise ValueError("review score must be between zero and one")
        if self.accepted and (self.score < 0.5 or not self.evidence_ids):
            raise ValueError("accepted work requires adequate score and evidence")


@dataclass(frozen=True)
class EscalationLevel:
    level: int
    after_seconds: int
    action: EscalationAction
    recipient_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.level < 1 or self.after_seconds < 0 or not self.recipient_ids:
            raise ValueError("escalation level, delay, and recipients are required")


@dataclass(frozen=True)
class EscalationPolicy:
    policy_id: str
    levels: tuple[EscalationLevel, ...]

    def __post_init__(self) -> None:
        if not self.policy_id or not self.levels:
            raise ValueError("escalation policy identity and levels are required")
        if tuple(sorted(level.level for level in self.levels)) != tuple(range(1, len(self.levels) + 1)):
            raise ValueError("escalation levels must be contiguous starting at one")
        delays = tuple(level.after_seconds for level in sorted(self.levels, key=lambda item: item.level))
        if delays != tuple(sorted(delays)) or len(set(delays)) != len(delays):
            raise ValueError("escalation delays must increase at every level")

    def due(self, *, elapsed_seconds: int, completed_levels: frozenset[int]) -> EscalationLevel | None:
        return next((
            level for level in sorted(self.levels, key=lambda item: item.level)
            if level.level not in completed_levels and elapsed_seconds >= level.after_seconds
        ), None)


@dataclass(frozen=True)
class Organization:
    tenant_id: str
    organization_id: str
    name: str
    teams: Mapping[str, Team]
    agents: Mapping[str, AgentProfile]
    humans: Mapping[str, HumanParticipant] = field(default_factory=dict)
    services: Mapping[str, ServiceParticipant] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not self.organization_id.strip() or not self.name.strip():
            raise ValueError("tenant_id, organization_id, and name are required")
        for team_id, team in self.teams.items():
            if team_id != team.team_id:
                raise ValueError("team map key does not match team identity")
            if team.manager_id is not None and team.manager_id not in self.agents:
                raise ValueError("team manager must exist in the organization")
        for agent_id, agent in self.agents.items():
            if agent_id != agent.agent_id or agent.team_id not in self.teams:
                raise ValueError("agent identity/team is not part of the organization")
            if agent.manager_id is not None and agent.manager_id not in self.agents:
                raise ValueError("agent manager must exist in the organization")
        identities = set(self.agents)
        for participant_id, human in self.humans.items():
            if participant_id != human.participant_id or human.team_id not in self.teams:
                raise ValueError("human identity/team is not part of the organization")
            if participant_id in identities:
                raise ValueError("participant identities must be globally unique")
            identities.add(participant_id)
        for participant_id, service in self.services.items():
            if participant_id != service.participant_id or service.owner_id not in identities:
                raise ValueError("service identity/owner is not part of the organization")
            if participant_id in identities:
                raise ValueError("participant identities must be globally unique")
            identities.add(participant_id)
        self._assert_acyclic_management()

    def _assert_acyclic_management(self) -> None:
        for agent in self.agents.values():
            seen = {agent.agent_id}
            current = agent
            while current.manager_id is not None:
                if current.manager_id in seen:
                    raise ValueError("management hierarchy contains a cycle")
                seen.add(current.manager_id)
                current = self.agents[current.manager_id]

    def direct_reports(self, manager_id: str) -> tuple[AgentProfile, ...]:
        return tuple(agent for agent in self.agents.values() if agent.manager_id == manager_id)

    def can_hire(self, actor_id: str) -> bool:
        actor = self.agents.get(actor_id)
        return bool(actor and actor.status is AgentStatus.ACTIVE and actor.hiring_authority)

    def route(self, message: Message) -> tuple[str, ...]:
        """Resolve a message without imposing a chain-of-command bottleneck."""

        sender = self.agents.get(message.sender_id)
        if sender is None or sender.status is not AgentStatus.ACTIVE:
            raise ValueError("only an active organization agent may send this message")
        if message.audience is Audience.ORGANIZATION:
            return tuple(sorted(a.agent_id for a in self.agents.values() if a.status is AgentStatus.ACTIVE))
        if message.audience is Audience.TEAM:
            return tuple(sorted(
                a.agent_id for a in self.agents.values()
                if a.team_id == sender.team_id and a.status is AgentStatus.ACTIVE
            ))
        participants = set(self.agents) | set(self.humans) | set(self.services)
        if message.audience is Audience.HUMAN:
            missing_humans = [item for item in message.recipient_ids if item not in self.humans]
            if missing_humans:
                raise ValueError("human message recipient is outside the organization")
            return message.recipient_ids
        missing = [agent_id for agent_id in message.recipient_ids if agent_id not in participants]
        if missing:
            raise ValueError("message recipient is outside the organization")
        return message.recipient_ids

    def validate_work_participants(self, contract: WorkContract) -> None:
        participants = set(self.agents) | set(self.humans) | set(self.services)
        if contract.accountable_agent_id not in self.agents:
            raise ValueError("work accountability must remain with an AI agent manager")
        unknown = set(contract.assignee_ids) - participants
        if unknown:
            raise ValueError(f"work assignees are outside the organization: {sorted(unknown)}")


@dataclass(frozen=True)
class WorkGraph:
    contracts: Mapping[str, WorkContract]

    def __post_init__(self) -> None:
        for work_id, contract in self.contracts.items():
            if work_id != contract.work_id:
                raise ValueError("work map key does not match work identity")
            unknown = contract.dependency_ids - self.contracts.keys()
            if unknown:
                raise ValueError(f"unknown work dependencies: {sorted(unknown)}")
        self._assert_acyclic_dependencies()

    def _assert_acyclic_dependencies(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(work_id: str) -> None:
            if work_id in visiting:
                raise ValueError("work dependency graph contains a cycle")
            if work_id in visited:
                return
            visiting.add(work_id)
            for dependency in self.contracts[work_id].dependency_ids:
                visit(dependency)
            visiting.remove(work_id)
            visited.add(work_id)

        for work_id in self.contracts:
            visit(work_id)

    def ready(self) -> tuple[WorkContract, ...]:
        complete = {
            work_id for work_id, contract in self.contracts.items()
            if contract.status is WorkStatus.SUCCEEDED
        }
        return tuple(
            contract for contract in self.contracts.values()
            if contract.status in {WorkStatus.BACKLOG, WorkStatus.READY}
            and contract.dependency_ids <= complete
        )
