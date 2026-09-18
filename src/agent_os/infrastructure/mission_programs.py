"""Validated, provider-independent contract for commanding an arbitrary mission.

The language model proposes this document, but it does not get to decide whether
the proposal is executable.  These models and ``validate_program_graph`` form a
deterministic admission boundary in front of the durable workflow runtime.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Collection, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MISSION_PROGRAM_FORMAT = "agent-os.mission-program.v1"


class HumanDecisionBrief(BaseModel):
    """Bounded context a person needs before responding to a workflow wait."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["input", "approval", "choice"]
    request: str = Field(min_length=1, max_length=4_000)
    requesting_role: str | None = Field(default=None, min_length=1, max_length=256)
    recommendation: str | None = Field(default=None, max_length=4_000)
    alternatives: list[str] = Field(default_factory=list, max_length=8)
    consequences: list[str] = Field(min_length=1, max_length=8)
    reversibility: Literal[
        "reversible", "partially_reversible", "irreversible", "unknown",
    ] = "unknown"
    safe_default: str = Field(min_length=1, max_length=4_000)
    allow_request_changes: bool = False
    estimated_cost_cents: int | None = Field(
        default=None, ge=0, le=100_000_000_000,
    )
    deadline_at: str | None = Field(default=None, max_length=64)

    @field_validator("alternatives", "consequences")
    @classmethod
    def bounded_nonempty_items(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 2_000 for value in values):
            raise ValueError("decision brief list items must contain 1 to 2000 characters")
        return values

    @field_validator("deadline_at")
    @classmethod
    def deadline_is_zoned_rfc3339(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("decision deadline must be RFC 3339") from exc
        if parsed.tzinfo is None:
            raise ValueError("decision deadline must include a timezone")
        return value


class PlannedNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    kind: Literal["agent", "decision", "human", "tool", "subworkflow", "terminal"]
    purpose: str = Field(min_length=1, max_length=4_000)
    owner_role: str | None = Field(default=None, max_length=256)
    configuration: dict[str, Any] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def agent_has_owner(self) -> "PlannedNode":
        if self.kind in {"agent", "subworkflow"} and not self.owner_role:
            raise ValueError("planned agent and subworkflow nodes require owner_role")
        return self


class PlannedEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    target: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    condition: str = Field(default="always", min_length=1, max_length=128)
    priority: int = Field(default=0, ge=-10_000, le=10_000)


class MissionWorkflowPlan(BaseModel):
    """Identity-free graph proposal; authority supplies tenant and creator."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    entry_node_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    nodes: list[PlannedNode] = Field(min_length=2, max_length=64)
    edges: list[PlannedEdge] = Field(min_length=1, max_length=256)


class EstimateRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    optimistic: int = Field(ge=0)
    likely: int = Field(ge=0)
    pessimistic: int = Field(ge=0)
    unit: Literal["hours", "days", "weeks", "months", "years", "usd_cents"]
    basis: str = Field(min_length=1, max_length=4_000)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def ordered(self) -> "EstimateRange":
        if not self.optimistic <= self.likely <= self.pessimistic:
            raise ValueError("estimate ranges must be ordered optimistic <= likely <= pessimistic")
        return self


class FeasibilityAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal[
        "viable", "viable_with_conditions", "discovery_required", "not_viable"
    ]
    rationale: str = Field(min_length=1, max_length=8_000)
    delivery_estimate: EstimateRange
    cost_estimate: EstimateRange
    assumptions: list[str] = Field(min_length=1, max_length=64)
    risks: list[str] = Field(default_factory=list, max_length=64)
    excluded_outcomes: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def cost_is_money(self) -> "FeasibilityAssessment":
        if self.cost_estimate.unit != "usd_cents":
            raise ValueError("feasibility cost estimate must use usd_cents")
        return self


class SuccessMeasure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    measure_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    description: str = Field(min_length=1, max_length=4_000)


class ClarificationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    question: str = Field(min_length=1, max_length=4_000)
    why_material: str = Field(min_length=1, max_length=4_000)
    requested_from: list[str] = Field(min_length=1, max_length=32)
    status: Literal["open", "resolved", "assumed"]
    blocking_workstream_ids: list[str] = Field(default_factory=list, max_length=64)
    human_node_id: str | None = Field(default=None, max_length=64)
    resolution: str | None = Field(default=None, max_length=8_000)
    default_assumption: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def resolution_matches_status(self) -> "ClarificationPlan":
        if self.status == "resolved" and not self.resolution:
            raise ValueError("resolved clarification requires a resolution")
        if self.status == "assumed" and not self.default_assumption:
            raise ValueError("assumed clarification requires a default assumption")
        if self.status == "open" and not self.human_node_id:
            raise ValueError("open clarification requires a correlated human workflow node")
        return self


class RolePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    title: str = Field(min_length=1, max_length=256)
    participant_kind: Literal["agent", "human", "service", "vendor"]
    responsibilities: list[str] = Field(min_length=1, max_length=64)
    manager_role_id: str | None = Field(default=None, max_length=64)
    required_count: int = Field(default=1, ge=1, le=100_000)
    authority: list[str] = Field(default_factory=list, max_length=64)


class ResourcePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    kind: Literal[
        "human_role", "authority", "credential", "data", "budget", "legal",
        "infrastructure", "decision", "physical_action", "service"
    ]
    purpose: str = Field(min_length=1, max_length=4_000)
    status: Literal["verified", "available_unverified", "missing", "requested"]
    owner_role_id: str = Field(min_length=1, max_length=64)
    acquisition_mode: Literal[
        "use_existing", "verify", "request_human", "configure", "build", "procure", "hire"
    ]
    acquisition_node_ids: list[str] = Field(default_factory=list, max_length=64)
    depends_on_resource_ids: list[str] = Field(default_factory=list, max_length=64)
    evidence_ids: list[str] = Field(default_factory=list, max_length=64)
    budget_cents: int = Field(default=0, ge=0)
    needs_human_approval: bool = False

    @model_validator(mode="after")
    def acquisition_is_truthful(self) -> "ResourcePlan":
        if self.status == "verified" and not self.evidence_ids:
            raise ValueError("verified resources require evidence IDs")
        if self.status != "verified" and not self.acquisition_node_ids:
            raise ValueError("unverified or missing resources require acquisition workflow nodes")
        if self.acquisition_mode in {"procure", "hire"} and not self.needs_human_approval:
            raise ValueError("procurement and hiring require human approval")
        return self


class CapabilityPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    purpose: str = Field(min_length=1, max_length=4_000)
    status: Literal["verified", "available_unverified", "missing", "requested"]
    owner_role_id: str = Field(min_length=1, max_length=64)
    expansion_mode: Literal[
        "use_existing", "verify", "configure_integration", "build_adapter",
        "build_capability", "procure_service", "engage_specialist"
    ]
    expansion_node_ids: list[str] = Field(default_factory=list, max_length=64)
    required_tool_ids: list[str] = Field(default_factory=list, max_length=64)
    evidence_ids: list[str] = Field(default_factory=list, max_length=64)
    acceptance_checks: list[str] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def expansion_is_truthful(self) -> "CapabilityPlan":
        if self.status == "verified" and not self.evidence_ids:
            raise ValueError("verified capabilities require evidence IDs")
        if self.status != "verified" and not self.expansion_node_ids:
            raise ValueError("unverified or missing capabilities require expansion workflow nodes")
        return self


class CoordinationPlan(BaseModel):
    """Admitted reason for using a particular execution topology.

    Multi-agent fan-out is not intrinsically better than a direct tool call or
    one agent.  The planner must name the simpler comparison, predict the
    bounded cost/latency, and tie the extra coordination to mission measures.
    Defaults preserve old admitted programs as a conservative single-agent
    topology; newly proposed programs are instructed to fill this explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    strategy: Literal[
        "deterministic_workflow", "single_agent", "parallel_agents",
        "evaluator_optimizer", "subworkflow",
    ] = "single_agent"
    rationale: str = Field(
        default="Legacy program: retain one bounded execution thread.",
        min_length=1,
        max_length=4_000,
    )
    coupling: Literal["low", "medium", "high"] = "high"
    parallelism: int = Field(default=1, ge=1, le=32)
    comparison_baseline: Literal[
        "direct_model", "single_agent", "current_system", "human_workflow",
        "not_applicable",
    ] = "current_system"
    expected_benefit: str = Field(
        default="Preserve the behavior of an already admitted program.",
        min_length=1,
        max_length=4_000,
    )
    measure_ids: list[str] = Field(default_factory=list, max_length=64)
    estimated_model_cost_cents: int = Field(default=0, ge=0, le=100_000_000_000)
    latency_budget_seconds: int | None = Field(default=None, ge=1, le=31_536_000)
    fallback: str = Field(
        default="Return to the admitted single execution thread.",
        min_length=1,
        max_length=4_000,
    )

    @model_validator(mode="after")
    def topology_is_coherent(self) -> "CoordinationPlan":
        governed_topologies = {
            "parallel_agents", "evaluator_optimizer", "subworkflow",
        }
        if self.strategy in governed_topologies:
            if not self.measure_ids:
                raise ValueError(f"{self.strategy.replace('_', '-')} requires a measured benefit")
            if self.comparison_baseline == "not_applicable":
                raise ValueError(
                    f"{self.strategy.replace('_', '-')} requires a simpler comparison baseline"
                )
            if self.latency_budget_seconds is None:
                raise ValueError(f"{self.strategy.replace('_', '-')} requires a latency budget")
        if self.strategy == "parallel_agents":
            if self.coupling != "low":
                raise ValueError("parallel agents require low-coupling work")
            if self.parallelism < 2:
                raise ValueError("parallel agents require parallelism of at least two")
        elif self.parallelism != 1:
            raise ValueError("only the parallel-agents strategy may request parallelism above one")
        return self


class WorkstreamPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workstream_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    objective: str = Field(min_length=1, max_length=4_000)
    accountable_role_id: str = Field(min_length=1, max_length=64)
    workflow_node_ids: list[str] = Field(min_length=1, max_length=64)
    depends_on_workstream_ids: list[str] = Field(default_factory=list, max_length=64)
    required_resource_ids: list[str] = Field(default_factory=list, max_length=64)
    required_capability_ids: list[str] = Field(default_factory=list, max_length=64)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=64)
    coordination: CoordinationPlan = Field(default_factory=CoordinationPlan)


class VerificationClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    claim: str = Field(min_length=1, max_length=4_000)
    success_measure_ids: list[str] = Field(min_length=1, max_length=64)
    reviewer_role_id: str = Field(min_length=1, max_length=64)
    maker_role_ids: list[str] = Field(default_factory=list, max_length=64)
    independence_required: bool = False
    verification_node_ids: list[str] = Field(min_length=1, max_length=64)
    required_evidence: list[str] = Field(min_length=1, max_length=64)
    failure_routes_to_node_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def independent_checker_is_explicit(self) -> "VerificationClaim":
        if self.independence_required and not self.maker_role_ids:
            raise ValueError("independent verification requires explicit maker roles")
        return self


class ReplanningPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_role_id: str = Field(min_length=1, max_length=64)
    review_cadence: str = Field(min_length=1, max_length=1_000)
    triggers: list[str] = Field(min_length=1, max_length=64)
    replan_node_ids: list[str] = Field(min_length=1, max_length=32)
    continue_condition: str = Field(min_length=1, max_length=128)
    replan_condition: str = Field(min_length=1, max_length=128)
    material_change_requires_new_revision: bool = True
    max_revisions: int = Field(default=16, ge=1, le=128)
    notify_role_ids: list[str] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def distinct_paths(self) -> "ReplanningPolicy":
        if self.continue_condition == self.replan_condition:
            raise ValueError("replanning continue and replan conditions must differ")
        if not self.material_change_requires_new_revision:
            raise ValueError("material program changes must create a new revision")
        return self


class MissionProgramPlan(BaseModel):
    """Complete command contract proposed before mission execution starts."""

    model_config = ConfigDict(extra="forbid")

    format: Literal[MISSION_PROGRAM_FORMAT]
    revision: int = Field(default=1, ge=1)
    objective: str = Field(min_length=1, max_length=50_000)
    authorized_budget_cents: int = Field(ge=0, le=100_000_000_000)
    success_measures: list[SuccessMeasure] = Field(min_length=1, max_length=64)
    feasibility: FeasibilityAssessment
    clarifications: list[ClarificationPlan] = Field(default_factory=list, max_length=64)
    roles: list[RolePlan] = Field(min_length=1, max_length=128)
    resources: list[ResourcePlan] = Field(default_factory=list, max_length=128)
    capabilities: list[CapabilityPlan] = Field(min_length=1, max_length=128)
    workstreams: list[WorkstreamPlan] = Field(min_length=1, max_length=128)
    verification: list[VerificationClaim] = Field(min_length=1, max_length=128)
    replanning: ReplanningPolicy
    workflow: MissionWorkflowPlan


def _unique(items: list[BaseModel], field: str, label: str) -> dict[str, BaseModel]:
    values = [str(getattr(item, field)) for item in items]
    if len(set(values)) != len(values):
        raise ValueError(f"mission program {label} IDs must be unique")
    return dict(zip(values, items, strict=True))


def _require_known(values: list[str], known: set[str], label: str) -> None:
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"mission program {label} references unknown IDs: {sorted(unknown)}")


def _require_acyclic(dependencies: dict[str, list[str]], label: str) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            raise ValueError(f"mission program {label} contains a dependency cycle")
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in dependencies.get(identifier, ()):
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in dependencies:
        visit(identifier)


def validate_program_graph(
    program: MissionProgramPlan,
    *,
    available_tools: Collection[str] | None = None,
) -> None:
    """Reject semantic holes between the charter and executable graph."""

    node_by_id = _unique(program.workflow.nodes, "node_id", "workflow node")
    role_by_id = _unique(program.roles, "role_id", "role")
    workstream_by_id = _unique(program.workstreams, "workstream_id", "workstream")
    resource_by_id = _unique(program.resources, "resource_id", "resource")
    capability_by_id = _unique(program.capabilities, "capability_id", "capability")
    measure_by_id = _unique(program.success_measures, "measure_id", "success measure")
    _unique(program.clarifications, "question_id", "clarification")
    _unique(program.verification, "claim_id", "verification claim")

    node_ids = set(node_by_id)
    role_ids = set(role_by_id)
    workstream_ids = set(workstream_by_id)
    resource_ids = set(resource_by_id)
    capability_ids = set(capability_by_id)
    measure_ids = set(measure_by_id)
    tools = None if available_tools is None else frozenset(available_tools)
    child_budget = sum(
        int(node.configuration.get("budget_limit_cents", 0))
        for node in program.workflow.nodes
        if node.kind == "subworkflow"
        and isinstance(node.configuration.get("budget_limit_cents", 0), int)
        and not isinstance(node.configuration.get("budget_limit_cents", 0), bool)
    )
    if child_budget > program.authorized_budget_cents:
        raise ValueError("mission child-program budgets exceed admitted budget authority")
    coordination_budget = sum(
        workstream.coordination.estimated_model_cost_cents
        for workstream in program.workstreams
    )
    if coordination_budget > program.authorized_budget_cents:
        raise ValueError("mission coordination estimates exceed admitted budget authority")
    if program.feasibility.cost_estimate.likely > program.authorized_budget_cents and not any(
        resource.kind == "budget" and resource.status in {"missing", "requested"}
        for resource in program.resources
    ):
        raise ValueError(
            "mission program cost exceeds authority without an explicit budget acquisition gap"
        )

    for role in program.roles:
        if role.manager_role_id is not None:
            _require_known([role.manager_role_id], role_ids, "role manager")
            if role.manager_role_id == role.role_id:
                raise ValueError("mission program role cannot manage itself")
    _require_acyclic({
        role.role_id: [] if role.manager_role_id is None else [role.manager_role_id]
        for role in program.roles
    }, "role management")

    for question in program.clarifications:
        _require_known(question.blocking_workstream_ids, workstream_ids, "clarification blocker")
        if question.human_node_id is not None:
            _require_known([question.human_node_id], node_ids, "clarification human node")
            if node_by_id[question.human_node_id].kind != "human":
                raise ValueError("mission program clarification must reference a human node")
            recipients = node_by_id[question.human_node_id].configuration.get("recipient_ids", ())
            if not isinstance(recipients, (list, tuple)) or not set(question.requested_from) <= {
                str(item) for item in recipients
            }:
                raise ValueError("mission program clarification node omits a requested recipient")

    for resource in program.resources:
        _require_known([resource.owner_role_id], role_ids, "resource owner")
        _require_known(resource.acquisition_node_ids, node_ids, "resource acquisition")
        _require_known(resource.depends_on_resource_ids, resource_ids, "resource dependency")
        if resource.resource_id in resource.depends_on_resource_ids:
            raise ValueError("mission program resource cannot depend on itself")
        if resource.acquisition_mode in {"request_human", "procure", "hire"} and not any(
            node_by_id[node_id].kind == "human" for node_id in resource.acquisition_node_ids
        ):
            raise ValueError("human-governed resource acquisition requires a human node")
    _require_acyclic({
        resource.resource_id: resource.depends_on_resource_ids for resource in program.resources
    }, "resource graph")

    for capability in program.capabilities:
        _require_known([capability.owner_role_id], role_ids, "capability owner")
        _require_known(capability.expansion_node_ids, node_ids, "capability expansion")
        if (
            tools is not None
            and capability.status in {"verified", "available_unverified"}
            and set(capability.required_tool_ids) - tools
        ):
            raise ValueError(
                f"capability {capability.capability_id} claims unavailable runtime tools"
            )
        if tools is not None and capability.status in {"verified", "available_unverified"}:
            represented_tools = {
                str(node_by_id[node_id].configuration.get("tool") or "")
                for node_id in capability.expansion_node_ids
            }
            if set(capability.required_tool_ids) - represented_tools:
                raise ValueError(
                    f"capability {capability.capability_id} does not verify its runtime tools"
                )
        if capability.expansion_mode in {"procure_service", "engage_specialist"} and not any(
            node_by_id[node_id].kind == "human" for node_id in capability.expansion_node_ids
        ):
            raise ValueError("external capability expansion requires a human node")

    covered_nodes: set[str] = set()
    for workstream in program.workstreams:
        _require_known([workstream.accountable_role_id], role_ids, "workstream owner")
        _require_known(workstream.workflow_node_ids, node_ids, "workstream workflow")
        _require_known(workstream.depends_on_workstream_ids, workstream_ids, "workstream dependency")
        _require_known(workstream.required_resource_ids, resource_ids, "workstream resource")
        _require_known(workstream.required_capability_ids, capability_ids, "workstream capability")
        _require_known(
            workstream.coordination.measure_ids,
            measure_ids,
            "workstream coordination measure",
        )
        if workstream.workstream_id in workstream.depends_on_workstream_ids:
            raise ValueError("mission program workstream cannot depend on itself")
        topology = workstream.coordination
        workstream_nodes = [node_by_id[node_id] for node_id in workstream.workflow_node_ids]
        parallel_workers = [
            node for node in workstream_nodes if node.kind in {"agent", "subworkflow"}
        ]
        if topology.strategy == "parallel_agents" and len(parallel_workers) < topology.parallelism:
            raise ValueError(
                f"workstream {workstream.workstream_id} has fewer parallel workers than admitted"
            )
        if topology.strategy == "parallel_agents":
            parallel_ids = {node.node_id for node in parallel_workers}
            maximum_branch_width = max((
                len({edge.target for edge in program.workflow.edges if edge.source == source}
                    & parallel_ids)
                for source in node_ids
            ), default=0)
            if maximum_branch_width < topology.parallelism:
                raise ValueError(
                    f"workstream {workstream.workstream_id} does not expose its admitted parallel branch"
                )
        if topology.strategy == "subworkflow" and not any(
            node.kind == "subworkflow" for node in workstream_nodes
        ):
            raise ValueError(
                f"workstream {workstream.workstream_id} declares subworkflow without a subworkflow node"
            )
        if topology.strategy == "evaluator_optimizer" and len(workstream.workflow_node_ids) < 2:
            raise ValueError(
                f"workstream {workstream.workstream_id} evaluator-optimizer needs maker and evaluator nodes"
            )
        if topology.strategy == "deterministic_workflow" and parallel_workers:
            raise ValueError(
                f"workstream {workstream.workstream_id} deterministic workflow contains an "
                "agent or subworkflow"
            )
        covered_nodes.update(workstream.workflow_node_ids)
        for node_id in workstream.workflow_node_ids:
            owner = node_by_id[node_id].owner_role
            if owner is not None and owner != workstream.accountable_role_id:
                raise ValueError(
                    f"workflow node {node_id} owner does not match accountable workstream role"
                )
    _require_acyclic({
        workstream.workstream_id: workstream.depends_on_workstream_ids
        for workstream in program.workstreams
    }, "workstream graph")

    covered_measures: set[str] = set()
    for claim in program.verification:
        _require_known(claim.success_measure_ids, measure_ids, "verification success measure")
        covered_measures.update(claim.success_measure_ids)
        _require_known([claim.reviewer_role_id], role_ids, "verification reviewer")
        _require_known(claim.maker_role_ids, role_ids, "verification maker")
        if claim.independence_required and claim.reviewer_role_id in claim.maker_role_ids:
            raise ValueError("independent verification reviewer cannot be a maker")
        _require_known(claim.verification_node_ids, node_ids, "verification node")
        _require_known([claim.failure_routes_to_node_id], node_ids, "verification repair route")
        for node_id in claim.verification_node_ids:
            if node_by_id[node_id].kind not in {"agent", "decision", "tool"}:
                raise ValueError("verification claims require agent, decision, or tool nodes")
            owner = node_by_id[node_id].owner_role
            if owner is not None and owner != claim.reviewer_role_id:
                raise ValueError(
                    f"verification node {node_id} is not owned by its declared reviewer"
                )
    if covered_measures != measure_ids:
        raise ValueError(
            f"mission program has success measures without verification: {sorted(measure_ids - covered_measures)}"
        )

    policy = program.replanning
    _require_known([policy.owner_role_id, *policy.notify_role_ids], role_ids, "replanning role")
    _require_known(policy.replan_node_ids, node_ids, "replanning node")
    outgoing = {
        node_id: {
            edge.condition: edge.target
            for edge in program.workflow.edges
            if edge.source == node_id
        }
        for node_id in policy.replan_node_ids
    }
    for node_id, paths in outgoing.items():
        if node_by_id[node_id].kind not in {"agent", "decision"}:
            raise ValueError("replanning must be owned by an agent or decision node")
        if policy.continue_condition not in paths or policy.replan_condition not in paths:
            raise ValueError(
                f"replanning node {node_id} must expose both declared replan paths"
            )
        if node_by_id[paths[policy.replan_condition]].kind == "terminal":
            raise ValueError("a replan path cannot terminate the mission")
        replan_target = node_by_id[paths[policy.replan_condition]]
        if not (
            replan_target.kind == "tool"
            and replan_target.configuration.get("tool") == "workflow.revise"
        ):
            raise ValueError(
                "a material replan path must enter the atomic workflow.revise authority"
            )

    open_blockers = {
        workstream_id
        for question in program.clarifications
        if question.status == "open"
        for workstream_id in question.blocking_workstream_ids
    }
    if open_blockers and open_blockers != workstream_ids:
        independent_nodes = {
            node_id
            for workstream_id, workstream in workstream_by_id.items()
            if workstream_id not in open_blockers
            for node_id in workstream.workflow_node_ids
        }
        human_nodes = {
            question.human_node_id
            for question in program.clarifications
            if question.status == "open" and question.human_node_id
        }
        if not independent_nodes - human_nodes:
            raise ValueError(
                "open scoped questions must leave independent workflow work available"
            )

    terminal_nodes = {
        node.node_id for node in program.workflow.nodes if node.kind == "terminal"
    }
    required_coverage = node_ids - terminal_nodes
    governed_nodes = covered_nodes | {
        node_id for resource in program.resources for node_id in resource.acquisition_node_ids
    } | {
        node_id for capability in program.capabilities for node_id in capability.expansion_node_ids
    } | {
        node_id for claim in program.verification for node_id in claim.verification_node_ids
    } | set(policy.replan_node_ids) | {
        outgoing[node_id][policy.replan_condition] for node_id in policy.replan_node_ids
    } | {
        question.human_node_id for question in program.clarifications if question.human_node_id
    }
    ungoverned = required_coverage - governed_nodes
    if ungoverned:
        raise ValueError(f"mission program has ungoverned workflow nodes: {sorted(ungoverned)}")
