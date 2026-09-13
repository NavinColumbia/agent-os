"""Turn one CEO directive into a bounded, agent-designed executable mission graph."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Collection, Mapping

from agent_os.application.command_worker import FatalCommandError
from agent_os.application.lifecycle import CommandEnvelope
from agent_os.application.mission import mission_planning_run_id
from agent_os.application.ports import (
    ArtifactStore,
    GraphWorkflowEngine,
    WorkflowEngine,
)
from agent_os.domain.lifecycle import Event, EventKind, LifecycleStatus
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import (
    TokenStatus,
    WorkflowAction,
    WorkflowActionKind,
    WorkflowEvent,
    WorkflowEventKind,
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowTransitionRejected,
)
from agent_os.infrastructure.graph_output_refs import resolve_prior_output
from agent_os.infrastructure.mission_programs import (
    MissionProgramPlan,
    MissionWorkflowPlan,
    validate_program_graph,
)


MISSION_BOOTSTRAP_WORKFLOW_ID = "agent-os-mission-bootstrap"
MISSION_PLAN_ARTIFACT_LABEL = "mission-workflow"
_MISSION_PLAN_MAX_NODES = 64
_MISSION_PLAN_MAX_EDGES = 256
_MISSION_PLAN_MAX_ITERATIONS_PER_NODE = 16
_MISSION_PLAN_MAX_TOTAL_ITERATIONS = 128
_MISSION_INTERNAL_TOOLS = frozenset({"workflow.revise"})
_MISSION_TOOL_ALLOWLIST = frozenset({
    "deploy.preview", "deploy.service", "deploy.static", "sandbox.run", "workflow.revise",
})


def _mission_tools(configured: Collection[str] | None) -> frozenset[str]:
    tools = (
        _MISSION_TOOL_ALLOWLIST
        if configured is None
        else frozenset(configured) | _MISSION_INTERNAL_TOOLS
    )
    unsupported = tools - _MISSION_TOOL_ALLOWLIST
    if unsupported:
        raise ValueError(f"unsupported mission tools: {sorted(unsupported)}")
    return tools


def _workflow_id(tenant_id: str, planning_run_id: str, artifact_id: str) -> str:
    material = f"agent-os:mission-workflow:v1:{tenant_id}:{planning_run_id}:{artifact_id}"
    return "mission-" + hashlib.sha256(material.encode()).hexdigest()[:32]


def _child_run_id(planning_run_id: str, workflow_id: str) -> str:
    material = f"agent-os:mission-run:v1:{planning_run_id}:{workflow_id}"
    return "mission-run-" + hashlib.sha256(material.encode()).hexdigest()[:32]


def _cancel_graph_run(
    graph: GraphWorkflowEngine,
    *,
    tenant_id: str,
    run_id: str,
    reason: str,
    source_id: str,
) -> str:
    """Cancel one correlated graph despite bounded concurrent progress."""

    event_id = "mission-cancel-" + hashlib.sha256(
        f"{source_id}:{run_id}".encode()
    ).hexdigest()
    for _ in range(8):
        state = graph.get_graph_run(tenant_id, run_id)
        if state is None:
            return "missing"
        if state.status is WorkflowRunStatus.CANCELLED:
            return "cancelled"
        if state.status in {WorkflowRunStatus.SUCCEEDED, WorkflowRunStatus.FAILED}:
            return state.status.value
        try:
            graph.submit_graph_event(
                tenant_id,
                run_id,
                WorkflowEvent(
                    event_id,
                    WorkflowEventKind.RUN_CANCELLED,
                    state.version,
                    {"reason": reason},
                ),
            )
            return "cancelled"
        except WorkflowTransitionRejected as exc:
            if "stale" not in str(exc):
                raise FatalCommandError(f"mission graph cancellation was rejected: {exc}") from exc
    raise FatalCommandError("mission graph cancellation exceeded its concurrency retry bound")


def mission_bootstrap_definition(
    tenant_id: str, *, available_tools: Collection[str] | None = None,
) -> WorkflowDefinition:
    tools = _mission_tools(available_tools)
    requirements = {
        "deliverable": (
            "Propose exactly one application/json artifact labeled mission-workflow. Use json_value, "
            "not a JSON-escaped content string. Its value is a complete mission program, not merely a task "
            "graph, and must match the supplied JSON schema. Assess feasibility honestly with ranges and "
            "assumptions; ask only material questions; scope each question to the workstreams it blocks so "
            "independent work proceeds; design accountable human/agent/service roles; inventory every "
            "resource and capability and give missing ones an acquisition/expansion path; map execution, "
            "evidence-based verification and repair; and include a recurring replan decision with both "
            "continue and replan paths. Never claim an unverified capability is available. External spend, "
            "hiring, credentials, legal authority, production release, and irreversible decisions remain "
            "human-governed. A material-replan path must enter workflow.revise and source a newly proposed "
            "complete mission-program JSON artifact from its replan agent; that atomic authority replaces "
            "obsolete live work in the same run. Design the smallest sufficient non-linear program for the "
            "CEO directive."
        ),
        "plan_schema": MissionProgramPlan.model_json_schema(),
        "available_tools": sorted(tools),
        **({"sandbox_tool_configuration": {
            "tool": "sandbox.run",
            "source": {
                "node_id": "successful earlier agent node",
                "output_path": ["artifact_ids", "artifact-label"],
            },
            "command": ["direct", "argument", "vector"],
            "success_condition": "outgoing condition for exit code zero",
            "failure_condition": "optional outgoing repair condition",
        }} if "sandbox.run" in tools else {}),
        **({"preview_deployment_configuration": {
            "tool": "deploy.preview",
            "source": {
                "node_id": "successful earlier agent node",
                "output_path": ["artifact_ids", "html-preview-artifact-label"],
            },
            "success_condition": "outgoing condition after publication",
        }} if "deploy.preview" in tools else {}),
        **({"production_static_deployment_configuration": {
            "tool": "deploy.static",
            "source": {
                "node_id": "successful earlier agent or sandbox node",
                "output_path": ["artifact_ids", "tested-source-bundle-label"],
            },
            "approval": {
                "node_id": "successful earlier human approval node",
                "output_path": ["human_response", "approved"],
            },
            "app_slug": "stable lowercase DNS label",
            "success_condition": "outgoing condition after production publication",
        }} if "deploy.static" in tools else {}),
        **({"production_service_deployment_configuration": {
            "tool": "deploy.service",
            "source": {
                "node_id": "successful earlier sandbox node",
                "output_path": ["output_artifact_id"],
            },
            "approval": {
                "node_id": "successful earlier human approval node",
                "output_path": ["human_response", "approved"],
            },
            "app_slug": "stable lowercase DNS label",
            "health_path": "/health",
            "success_condition": "outgoing condition after production promotion",
            "source_contract": (
                "Produce an Agent OS source bundle containing a root Dockerfile. Every FROM image and the "
                "trusted builder are digest-pinned, the final stage declares a numeric non-root USER, the "
                "container listens on PORT 8080, and health_path returns a 2xx response. Do not embed secrets."
            ),
        }} if "deploy.service" in tools else {}),
        "limits": {
            "nodes": _MISSION_PLAN_MAX_NODES,
            "edges": _MISSION_PLAN_MAX_EDGES,
            "max_iterations_per_node": _MISSION_PLAN_MAX_ITERATIONS_PER_NODE,
            "total_node_iterations": _MISSION_PLAN_MAX_TOTAL_ITERATIONS,
        },
    }
    return WorkflowDefinition(
        workflow_id=MISSION_BOOTSTRAP_WORKFLOW_ID,
        tenant_id=tenant_id,
        name="Autonomous mission planning",
        version=1,
        entry_node_id="plan",
        nodes=(
            WorkflowNode(
                "plan",
                NodeKind.AGENT,
                "Design a safe executable mission graph from the authoritative CEO directive.",
                "mission-architect",
                {"agent_context": requirements, "max_iterations": 1},
            ),
            WorkflowNode(
                "launch",
                NodeKind.TOOL,
                "Validate, register, and launch the proposed mission graph.",
                configuration={
                    "tool": "workflow.launch",
                    "source": {
                        "node_id": "plan",
                        "output_path": ["artifact_ids", MISSION_PLAN_ARTIFACT_LABEL],
                    },
                    "success_condition": "launched",
                    "max_iterations": 1,
                },
            ),
            WorkflowNode(
                "done",
                NodeKind.TERMINAL,
                "Accept durable proof that the detailed mission graph was launched.",
                configuration={"max_iterations": 1},
            ),
        ),
        edges=(
            WorkflowEdge("plan", "launch", "planned"),
            WorkflowEdge("launch", "done", "launched"),
        ),
        created_by="system:mission-bootstrap",
    )


def materialize_mission_workflow(
    raw: Mapping[str, Any],
    *,
    tenant_id: str,
    planning_run_id: str,
    artifact_id: str,
    allowed_tools: Collection[str] | None = None,
    workflow_id: str | None = None,
    workflow_version: int = 1,
    supersedes_version: int | None = None,
) -> WorkflowDefinition:
    tools = _mission_tools(allowed_tools)
    try:
        if raw.get("format") is not None:
            program = MissionProgramPlan.model_validate(raw)
            validate_program_graph(program, available_tools=tools)
            plan = program.workflow
        else:
            # Compatibility for explicitly registered pre-program graphs.  The
            # autonomous bootstrap path below calls materialize_mission_program
            # and therefore cannot bypass the full command contract.
            plan = MissionWorkflowPlan.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise FatalCommandError(f"mission workflow proposal is invalid: {exc}") from exc

    nodes: list[WorkflowNode] = []
    tool_sources: list[tuple[str, str]] = []
    production_approvals: list[tuple[str, str]] = []
    configured_conditions: list[tuple[str, str, str]] = []
    total_iterations = 0
    for proposed in plan.nodes:
        configuration = dict(proposed.configuration)
        raw_iterations = configuration.get("max_iterations", 2)
        if (
            isinstance(raw_iterations, bool)
            or not isinstance(raw_iterations, int)
            or not 1 <= raw_iterations <= _MISSION_PLAN_MAX_ITERATIONS_PER_NODE
        ):
            raise FatalCommandError("planned node max_iterations must be an integer from 1 to 16")
        configuration["max_iterations"] = raw_iterations
        total_iterations += raw_iterations
        kind = NodeKind(proposed.kind)
        agent_context = configuration.get("agent_context", {})
        if kind in {NodeKind.AGENT, NodeKind.DECISION}:
            unexpected = set(configuration) - {"max_iterations", "agent_context"}
            if unexpected:
                raise FatalCommandError(
                    f"planned {kind.value} node has unsupported configuration: {sorted(unexpected)}"
                )
            if not isinstance(agent_context, Mapping):
                raise FatalCommandError("planned agent_context must be an object")
        elif kind is NodeKind.HUMAN:
            unexpected = set(configuration) - {
                "max_iterations", "recipient_ids", "response_condition", "rejection_condition",
                "correlation_id",
            }
            if unexpected:
                raise FatalCommandError(
                    f"planned human node has unsupported configuration: {sorted(unexpected)}"
                )
            recipients = configuration.get("recipient_ids", ["human:ceo"])
            if (
                not isinstance(recipients, (list, tuple))
                or not 1 <= len(recipients) <= 32
                or any(not isinstance(item, str) or not item.strip() for item in recipients)
            ):
                raise FatalCommandError("planned human node requires 1..32 recipient IDs")
            configuration["recipient_ids"] = list(dict.fromkeys(recipients))
            response_condition = configuration.get("response_condition")
            if response_condition is not None:
                if not isinstance(response_condition, str) or not response_condition:
                    raise FatalCommandError("planned human response_condition must be nonempty")
                configured_conditions.append((
                    proposed.node_id, "response_condition", response_condition,
                ))
            rejection_condition = configuration.get("rejection_condition")
            if rejection_condition is not None:
                if not isinstance(rejection_condition, str) or not rejection_condition:
                    raise FatalCommandError("planned human rejection_condition must be nonempty")
                if rejection_condition == response_condition:
                    raise FatalCommandError(
                        "planned human response and rejection conditions must differ"
                    )
                configured_conditions.append((
                    proposed.node_id, "rejection_condition", rejection_condition,
                ))
        elif kind is NodeKind.TERMINAL:
            unexpected = set(configuration) - {"max_iterations"}
            if unexpected:
                raise FatalCommandError(
                    f"planned terminal node has unsupported configuration: {sorted(unexpected)}"
                )
        elif kind is NodeKind.TOOL:
            tool = configuration.get("tool")
            allowed_configuration = {
                "tool", "source", "success_condition", "max_iterations",
            }
            if tool == "sandbox.run":
                allowed_configuration.update({"command", "failure_condition"})
            elif tool in {"deploy.static", "deploy.service"}:
                allowed_configuration.update({"approval", "app_slug"})
                if tool == "deploy.service":
                    allowed_configuration.add("health_path")
            unexpected = set(configuration) - allowed_configuration
            if unexpected:
                raise FatalCommandError(
                    f"planned tool node has unsupported configuration: {sorted(unexpected)}"
                )
            if tool not in tools:
                raise FatalCommandError(f"planned workflow requested unavailable tool: {tool}")
            source = configuration.get("source")
            if not isinstance(source, Mapping):
                raise FatalCommandError("planned tool requires a prior-node source")
            source_node_id = source.get("node_id")
            output_path = source.get("output_path")
            if (
                not isinstance(source_node_id, str)
                or not source_node_id
                or not isinstance(output_path, (list, tuple))
                or not output_path
                or len(output_path) > 16
                or any(
                    isinstance(part, bool)
                    or not isinstance(part, (str, int))
                    or (isinstance(part, str) and not part)
                    or (isinstance(part, int) and part < 0)
                    for part in output_path
                )
            ):
                raise FatalCommandError("planned tool source reference is invalid")
            if tool == "sandbox.run":
                command = configuration.get("command")
                if (
                    not isinstance(command, (list, tuple))
                    or not 1 <= len(command) <= 64
                    or any(
                        not isinstance(item, str)
                        or not item
                        or "\0" in item
                        or len(item) > 4_096
                        for item in command
                    )
                ):
                    raise FatalCommandError("planned sandbox command must be bounded direct argv")
            elif tool in {"deploy.static", "deploy.service"}:
                deployment_kind = "static deployment" if tool == "deploy.static" else "service deployment"
                app_slug = configuration.get("app_slug")
                if (
                    not isinstance(app_slug, str)
                    or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", app_slug)
                ):
                    raise FatalCommandError(
                        f"planned {deployment_kind} app_slug must be a lowercase DNS label"
                    )
                if tool == "deploy.service":
                    health_path = configuration.get("health_path", "/health")
                    if (
                        not isinstance(health_path, str)
                        or not re.fullmatch(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,255}", health_path)
                        or "//" in health_path
                        or ".." in health_path
                    ):
                        raise FatalCommandError(
                            "planned service deployment health_path must be a bounded absolute path"
                        )
                    configuration["health_path"] = health_path
                approval = configuration.get("approval")
                if not isinstance(approval, Mapping):
                    raise FatalCommandError(
                        f"planned {deployment_kind} requires a prior human approval"
                    )
                approval_node_id = approval.get("node_id")
                approval_output_path = approval.get("output_path")
                if (
                    not isinstance(approval_node_id, str)
                    or not approval_node_id
                    or approval_output_path != ["human_response", "approved"]
                ):
                    raise FatalCommandError(
                        f"planned {deployment_kind} approval reference is invalid"
                    )
                tool_sources.append((proposed.node_id, approval_node_id))
                production_approvals.append((proposed.node_id, approval_node_id))
            success_condition = configuration.get("success_condition")
            if not isinstance(success_condition, str) or not success_condition:
                raise FatalCommandError("planned tool requires a success_condition")
            configured_conditions.append((
                proposed.node_id, "success_condition", success_condition,
            ))
            failure_condition = configuration.get("failure_condition")
            if tool == "sandbox.run" and failure_condition is not None:
                if not isinstance(failure_condition, str) or not failure_condition:
                    raise FatalCommandError("planned sandbox failure_condition must be nonempty")
                configured_conditions.append((
                    proposed.node_id, "failure_condition", failure_condition,
                ))
            tool_sources.append((proposed.node_id, source_node_id))
        nodes.append(WorkflowNode(
            node_id=proposed.node_id,
            kind=kind,
            purpose=proposed.purpose,
            owner_role=proposed.owner_role,
            configuration=configuration,
        ))
    if total_iterations > _MISSION_PLAN_MAX_TOTAL_ITERATIONS:
        raise FatalCommandError("planned workflow exceeds its total iteration budget")

    workflow_identity = workflow_id or _workflow_id(tenant_id, planning_run_id, artifact_id)
    try:
        definition = WorkflowDefinition(
            workflow_id=workflow_identity,
            tenant_id=tenant_id,
            name=plan.name,
            version=workflow_version,
            entry_node_id=plan.entry_node_id,
            nodes=tuple(nodes),
            edges=tuple(WorkflowEdge(
                edge.source, edge.target, edge.condition, edge.priority,
            ) for edge in plan.edges),
            created_by="agent:mission-architect",
            supersedes_version=supersedes_version,
        )
    except (TypeError, ValueError) as exc:
        raise FatalCommandError(f"mission workflow structure is invalid: {exc}") from exc

    terminals = {node.node_id for node in definition.nodes if node.kind is NodeKind.TERMINAL}
    can_reach_terminal = set(terminals)
    changed = True
    while changed:
        changed = False
        for edge in definition.edges:
            if edge.target in can_reach_terminal and edge.source not in can_reach_terminal:
                can_reach_terminal.add(edge.source)
                changed = True
    trapped = {node.node_id for node in definition.nodes} - can_reach_terminal
    if trapped:
        raise FatalCommandError(
            f"planned workflow has nodes with no terminal path: {sorted(trapped)}"
        )
    outgoing_by_node = {
        node.node_id: definition.outgoing(node.node_id) for node in definition.nodes
    }
    edge_identities = {
        (edge.source, edge.target, edge.condition) for edge in definition.edges
    }
    if len(edge_identities) != len(definition.edges):
        raise FatalCommandError("planned workflow contains duplicate edges")
    terminal_with_edges = [
        node_id for node_id in terminals if outgoing_by_node[node_id]
    ]
    if terminal_with_edges:
        raise FatalCommandError(
            f"planned terminal nodes cannot have outgoing edges: {sorted(terminal_with_edges)}"
        )
    for node_id, configuration_key, condition in configured_conditions:
        available = {
            edge.condition for edge in outgoing_by_node[node_id]
            if edge.condition != "always"
        }
        if condition not in available:
            raise FatalCommandError(
                f"planned {configuration_key} is not an outgoing condition for {node_id}"
            )
    for tool_node_id, source_node_id in tool_sources:
        frontier = [source_node_id]
        reachable = set(frontier)
        while frontier:
            current = frontier.pop()
            for edge in outgoing_by_node.get(current, ()):
                if edge.target not in reachable:
                    reachable.add(edge.target)
                    frontier.append(edge.target)
        if tool_node_id not in reachable or tool_node_id == source_node_id:
            raise FatalCommandError(
                f"planned tool {tool_node_id} does not follow its source node {source_node_id}"
            )
    nodes_by_id = {node.node_id: node for node in definition.nodes}
    for tool_node_id, approval_node_id in production_approvals:
        approval_node = nodes_by_id.get(approval_node_id)
        if approval_node is None or approval_node.kind is not NodeKind.HUMAN:
            raise FatalCommandError(
                f"planned production deployment {tool_node_id} approval must reference a human node"
            )
    return definition


def materialize_mission_program(
    raw: Mapping[str, Any],
    *,
    tenant_id: str,
    planning_run_id: str,
    artifact_id: str,
    allowed_tools: Collection[str] | None = None,
    workflow_id: str | None = None,
    workflow_version: int = 1,
    supersedes_version: int | None = None,
    authorized_budget_cents: int | None = None,
) -> tuple[MissionProgramPlan, WorkflowDefinition]:
    """Validate the complete north-star contract and materialize its graph."""

    try:
        program = MissionProgramPlan.model_validate(raw)
        validate_program_graph(program, available_tools=_mission_tools(allowed_tools))
    except (TypeError, ValueError) as exc:
        raise FatalCommandError(f"mission program proposal is invalid: {exc}") from exc
    if (
        authorized_budget_cents is not None
        and program.authorized_budget_cents > authorized_budget_cents
    ):
        raise FatalCommandError("mission program exceeds the CEO-authorized budget")
    definition = materialize_mission_workflow(
        raw,
        tenant_id=tenant_id,
        planning_run_id=planning_run_id,
        artifact_id=artifact_id,
        allowed_tools=allowed_tools,
        workflow_id=workflow_id,
        workflow_version=workflow_version,
        supersedes_version=supersedes_version,
    )
    return program, definition


class MissionBootstrapHandler:
    """Idempotently enqueue the built-in planner graph for a lifecycle command."""

    def __init__(
        self,
        graph_engine: GraphWorkflowEngine,
        lifecycle_engine: WorkflowEngine | None = None,
        available_tools: Collection[str] | None = None,
    ) -> None:
        self._graph = graph_engine
        self._lifecycle = lifecycle_engine
        self._available_tools = _mission_tools(available_tools)

    def execute(self, item: CommandEnvelope) -> Mapping[str, Any]:
        if self._lifecycle is not None:
            lifecycle = self._lifecycle.get_run(item.organization_id, item.run_id)
            if lifecycle is None:
                raise FatalCommandError("mission lifecycle disappeared before planning")
            if lifecycle.status is LifecycleStatus.CANCELLED:
                return {"planning_started": False, "reason": "lifecycle_cancelled"}
        definition = mission_bootstrap_definition(
            item.organization_id, available_tools=self._available_tools,
        )
        self._graph.register_workflow(definition)
        planning_run_id = mission_planning_run_id(item.run_id)
        context = {
            **dict(item.command.payload),
            "lifecycle_run_id": item.run_id,
            "lifecycle_expected_version": item.aggregate_version,
            "mission_bootstrap": True,
        }
        receipt = self._graph.start_graph_run(
            item.organization_id,
            definition.workflow_id,
            definition.version,
            run_id=planning_run_id,
            request_id=f"mission-bootstrap-{item.command_id}",
            context=context,
        )
        return {
            "planning_run_id": planning_run_id,
            "workflow_id": definition.workflow_id,
            "duplicate": receipt.duplicate,
        }


class WorkflowLaunchToolNodeHandlers:
    """Validate an agent-authored plan and launch it through the graph authority."""

    def __init__(
        self,
        graph_engine: GraphWorkflowEngine,
        artifact_store: ArtifactStore,
        lifecycle_engine: WorkflowEngine | None = None,
        available_tools: Collection[str] | None = None,
    ) -> None:
        self._graph = graph_engine
        self._artifacts = artifact_store
        self._lifecycle = lifecycle_engine
        self._available_tools = _mission_tools(available_tools)

    def _cancelled_lifecycle(self, tenant_id: str, state: WorkflowRunState) -> bool:
        if self._lifecycle is None:
            return False
        lifecycle_run_id = str(state.context.get("lifecycle_run_id") or "")
        lifecycle = self._lifecycle.get_run(tenant_id, lifecycle_run_id)
        if lifecycle is None:
            raise FatalCommandError("mission lifecycle disappeared before child launch")
        return lifecycle.status is LifecycleStatus.CANCELLED

    def named_handlers(self):
        return {"workflow.launch": self.execute, "workflow.revise": self.revise}

    def _program_artifact(self, tenant_id: str, artifact_id: str) -> Mapping[str, Any]:
        record = self._artifacts.describe(tenant_id, artifact_id)
        content = self._artifacts.get(tenant_id, artifact_id)
        if record is None or content is None:
            raise FatalCommandError("mission program artifact does not exist in this tenant")
        if record.get("media_type") != "application/json":
            raise FatalCommandError("mission program artifact must use application/json")
        try:
            raw = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise FatalCommandError("mission program artifact is invalid JSON") from exc
        if not isinstance(raw, Mapping):
            raise FatalCommandError("mission program artifact must contain one object")
        return raw

    def execute(
        self,
        tenant_id: str,
        run_id: str,
        definition: WorkflowDefinition,
        state: WorkflowRunState,
        action: WorkflowAction,
        node: WorkflowNode,
    ) -> Mapping[str, Any]:
        del definition
        if node.kind is not NodeKind.TOOL or node.configuration.get("tool") != "workflow.launch":
            raise FatalCommandError("workflow launch handler received the wrong tool node")
        if self._cancelled_lifecycle(tenant_id, state):
            _cancel_graph_run(
                self._graph,
                tenant_id=tenant_id,
                run_id=run_id,
                reason="CEO lifecycle was cancelled before child launch",
                source_id=action.action_id,
            )
            return {"disposition": "fail", "reason": "mission was cancelled"}
        artifact_id = resolve_prior_output(
            state, node.configuration.get("source"), subject="workflow launch",
        )
        if not isinstance(artifact_id, str) or not artifact_id:
            raise FatalCommandError("workflow launch source is not an artifact ID")
        raw = self._program_artifact(tenant_id, artifact_id)

        raw_budget = state.context.get("budget_limit_cents", 0)
        if isinstance(raw_budget, bool) or not isinstance(raw_budget, int):
            raise FatalCommandError("CEO-authorized mission budget is invalid")
        program, child_definition = materialize_mission_program(
            raw,
            tenant_id=tenant_id,
            planning_run_id=run_id,
            artifact_id=artifact_id,
            allowed_tools=self._available_tools,
            authorized_budget_cents=raw_budget,
        )
        self._graph.register_workflow(child_definition)
        child_run_id = _child_run_id(run_id, child_definition.workflow_id)
        child_context = {
            key: value for key, value in state.context.items() if key != "mission_bootstrap"
        }
        child_context.update({
            "mission_execution": True,
            "planning_run_id": run_id,
            "workflow_plan_artifact_id": artifact_id,
            "mission_program": program.model_dump(mode="json", exclude={"workflow"}),
            "mission_program_revision": program.revision,
        })
        self._graph.start_graph_run(
            tenant_id,
            child_definition.workflow_id,
            child_definition.version,
            run_id=child_run_id,
            request_id="mission-launch-" + hashlib.sha256(action.action_id.encode()).hexdigest(),
            context=child_context,
        )
        if self._cancelled_lifecycle(tenant_id, state):
            _cancel_graph_run(
                self._graph,
                tenant_id=tenant_id,
                run_id=child_run_id,
                reason="CEO lifecycle was cancelled during child launch",
                source_id=action.action_id,
            )
            _cancel_graph_run(
                self._graph,
                tenant_id=tenant_id,
                run_id=run_id,
                reason="CEO lifecycle was cancelled during child launch",
                source_id=action.action_id,
            )
            return {"disposition": "fail", "reason": "mission was cancelled"}
        launch_record = {
            "format": "agent-os.mission-launch.v1",
            "tenant_id": tenant_id,
            "planning_run_id": run_id,
            "workflow_plan_artifact_id": artifact_id,
            "mission_program_format": program.format,
            "mission_program_revision": program.revision,
            "workflow_id": child_definition.workflow_id,
            "workflow_version": child_definition.version,
            "child_run_id": child_run_id,
        }
        launch_artifact_id = self._artifacts.put(
            organization_id=tenant_id,
            content=json.dumps(
                launch_record, allow_nan=False, separators=(",", ":"), sort_keys=True,
            ).encode(),
            media_type="application/json",
            idempotency_key=f"{action.action_id}:mission-launch-receipt",
        )
        configured_condition = node.configuration.get("success_condition", "launched")
        return {
            "disposition": "complete",
            "satisfied_conditions": [str(configured_condition)],
            "evidence_ids": [artifact_id, launch_artifact_id],
            "output": {
                **launch_record,
                "launch_artifact_id": launch_artifact_id,
                "admitted_program": program.model_dump(mode="json", exclude={"workflow"}),
            },
        }

    def revise(
        self,
        tenant_id: str,
        run_id: str,
        definition: WorkflowDefinition,
        state: WorkflowRunState,
        action: WorkflowAction,
        node: WorkflowNode,
    ) -> Mapping[str, Any]:
        del definition
        if node.kind is not NodeKind.TOOL or node.configuration.get("tool") != "workflow.revise":
            raise FatalCommandError("workflow revision handler received the wrong tool node")
        if state.context.get("mission_execution") is not True:
            raise FatalCommandError("only an admitted mission execution may revise its workflow")
        artifact_id = resolve_prior_output(
            state, node.configuration.get("source"), subject="workflow revision",
        )
        if not isinstance(artifact_id, str) or not artifact_id:
            raise FatalCommandError("workflow revision source is not an artifact ID")
        raw = self._program_artifact(tenant_id, artifact_id)
        current_revision = state.context.get("mission_program_revision", 1)
        current_program = state.context.get("mission_program", {})
        if isinstance(current_revision, bool) or not isinstance(current_revision, int):
            raise FatalCommandError("current mission program revision is invalid")
        if not isinstance(current_program, Mapping):
            raise FatalCommandError("current mission program is missing")
        replanning = current_program.get("replanning", {})
        max_revisions = replanning.get("max_revisions", 16) if isinstance(replanning, Mapping) else 16
        if isinstance(max_revisions, bool) or not isinstance(max_revisions, int):
            raise FatalCommandError("mission program revision bound is invalid")
        if current_revision >= max_revisions:
            raise FatalCommandError("mission program reached its admitted revision bound")

        program, replacement = materialize_mission_program(
            raw,
            tenant_id=tenant_id,
            planning_run_id=str(state.context.get("planning_run_id") or run_id),
            artifact_id=artifact_id,
            allowed_tools=self._available_tools,
            workflow_id=state.workflow_id,
            workflow_version=state.workflow_version + 1,
            supersedes_version=state.workflow_version,
            authorized_budget_cents=int(current_program.get("authorized_budget_cents", 0)),
        )
        if program.revision != current_revision + 1:
            raise FatalCommandError("replacement mission program must advance exactly one revision")
        if program.replanning.max_revisions > max_revisions:
            raise FatalCommandError("replacement program cannot expand its admitted revision authority")
        if program.authorized_budget_cents > int(current_program.get("authorized_budget_cents", 0)):
            raise FatalCommandError("replacement program cannot expand its admitted budget authority")
        self._graph.register_workflow(replacement)
        event_id = "mission-revision-" + hashlib.sha256(action.action_id.encode()).hexdigest()
        receipt = self._graph.submit_graph_event(
            tenant_id,
            run_id,
            WorkflowEvent(
                event_id,
                WorkflowEventKind.RUN_REVISED,
                state.version,
                {
                    "replacement_workflow": replacement.to_dict(),
                    "mission_program": program.model_dump(mode="json", exclude={"workflow"}),
                    "evidence_ids": [artifact_id],
                    "reason": node.purpose,
                },
            ),
        )
        return {
            "disposition": "complete",
            "satisfied_conditions": [str(node.configuration.get("success_condition") or "revised")],
            "evidence_ids": [artifact_id],
            "output": {
                "mission_program_revision": program.revision,
                "workflow_version": replacement.version,
                "state_version": receipt.state.version,
            },
        }


class MissionCancellationHandler:
    """Propagate a terminal CEO cancellation into planning and child graphs."""

    def __init__(
        self,
        graph_engine: GraphWorkflowEngine,
        artifact_store: ArtifactStore,
    ) -> None:
        self._graph = graph_engine
        self._artifacts = artifact_store

    def _derived_child_run_id(
        self, tenant_id: str, planning_run_id: str, state: WorkflowRunState,
    ) -> str | None:
        artifact_ids = {
            str(token.output.get("artifact_ids", {}).get(MISSION_PLAN_ARTIFACT_LABEL) or "")
            for token in state.tokens
            if token.node_id == "plan" and isinstance(token.output.get("artifact_ids"), Mapping)
        }
        artifact_ids.discard("")
        if len(artifact_ids) != 1:
            return None
        artifact_id = next(iter(artifact_ids))
        record = self._artifacts.describe(tenant_id, artifact_id)
        content = self._artifacts.get(tenant_id, artifact_id)
        if record is None or record.get("media_type") != "application/json" or content is None:
            return None
        try:
            proposal = json.loads(content)
            definition = materialize_mission_workflow(
                proposal,
                tenant_id=tenant_id,
                planning_run_id=planning_run_id,
                artifact_id=artifact_id,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, FatalCommandError, ValueError):
            return None
        return _child_run_id(planning_run_id, definition.workflow_id)

    def execute(self, item: CommandEnvelope) -> Mapping[str, Any]:
        reason = str(item.command.payload.get("reason") or "CEO cancelled the mission")
        planning_run_id = mission_planning_run_id(item.run_id)
        planning = self._graph.get_graph_run(item.organization_id, planning_run_id)
        child_run_ids = set()
        if planning is not None:
            child_run_ids.update(
                str(token.output["child_run_id"])
                for token in planning.tokens
                if token.output.get("child_run_id")
            )
            derived = self._derived_child_run_id(
                item.organization_id, planning_run_id, planning,
            )
            if derived is not None:
                child_run_ids.add(derived)

        results = {
            child_run_id: _cancel_graph_run(
                self._graph,
                tenant_id=item.organization_id,
                run_id=child_run_id,
                reason=reason,
                source_id=item.command_id,
            )
            for child_run_id in sorted(child_run_ids)
        }
        results[planning_run_id] = _cancel_graph_run(
            self._graph,
            tenant_id=item.organization_id,
            run_id=planning_run_id,
            reason=reason,
            source_id=item.command_id,
        )
        # Re-read after cancelling planning to catch a launch result that
        # committed immediately before our cancellation version fence.
        refreshed = self._graph.get_graph_run(item.organization_id, planning_run_id)
        if refreshed is not None:
            for child_run_id in sorted({
                str(token.output["child_run_id"])
                for token in refreshed.tokens
                if token.output.get("child_run_id")
            }):
                results[child_run_id] = _cancel_graph_run(
                    self._graph,
                    tenant_id=item.organization_id,
                    run_id=child_run_id,
                    reason=reason,
                    source_id=item.command_id,
                )
        return {"cancelled_graphs": results}


class MissionGraphEffectHandlers:
    """Project detailed mission terminal outcomes onto the coarse CEO lifecycle."""

    def __init__(
        self,
        *,
        lifecycle_engine: WorkflowEngine,
        graph_engine: GraphWorkflowEngine,
        notification_handlers: Mapping[WorkflowActionKind, Any],
    ) -> None:
        self._lifecycle = lifecycle_engine
        self._graph = graph_engine
        self._notifications = dict(notification_handlers)

    def graph_handlers(self):
        handlers = dict(self._notifications)
        handlers[WorkflowActionKind.RUN_SUCCEEDED] = self._succeeded
        handlers[WorkflowActionKind.RUN_FAILED] = self._failed
        return handlers

    def _state(self, envelope: Mapping[str, Any]) -> WorkflowRunState:
        tenant_id = str(envelope.get("tenant_id") or "")
        run_id = str(envelope.get("run_id") or "")
        state = self._graph.get_graph_run(tenant_id, run_id)
        if state is None:
            raise FatalCommandError("mission graph disappeared before terminal projection")
        return state

    def _project(
        self,
        envelope: Mapping[str, Any],
        action: WorkflowAction,
        *,
        succeeded: bool,
    ) -> Mapping[str, Any]:
        state = self._state(envelope)
        context = state.context
        lifecycle_run_id = str(context.get("lifecycle_run_id") or "")
        is_bootstrap = context.get("mission_bootstrap") is True
        is_execution = context.get("mission_execution") is True
        if not lifecycle_run_id or (not is_bootstrap and not is_execution):
            handler = self._notifications.get(action.kind)
            if handler is None:
                raise FatalCommandError(f"no notification handler for {action.kind.value}")
            return dict(handler(envelope, action))
        if is_bootstrap and succeeded:
            child_runs = [
                str(token.output.get("child_run_id"))
                for token in state.tokens
                if token.output.get("child_run_id")
            ]
            if len(set(child_runs)) != 1:
                raise FatalCommandError("planning completed without exactly one launched mission graph")
            return {"planning_completed": True, "child_run_id": child_runs[0]}

        tenant_id = state.tenant_id
        lifecycle = self._lifecycle.get_run(tenant_id, lifecycle_run_id)
        if lifecycle is None:
            raise FatalCommandError("mission lifecycle disappeared before terminal projection")
        if lifecycle.status is LifecycleStatus.CANCELLED:
            return {"projected": False, "reason": "lifecycle_cancelled"}
        raw_expected = context.get("lifecycle_expected_version")
        if isinstance(raw_expected, bool):
            raise FatalCommandError("mission lifecycle projection version is invalid")
        try:
            expected_version = int(raw_expected)
        except (TypeError, ValueError) as exc:
            raise FatalCommandError("mission lifecycle projection version is missing") from exc
        event_id = "mission-result-" + hashlib.sha256(action.action_id.encode()).hexdigest()
        if succeeded:
            evidence_ids = list(dict.fromkeys(
                evidence_id
                for token in state.tokens
                if token.status is TokenStatus.SUCCEEDED
                for evidence_id in token.evidence_ids
            ))
            if not evidence_ids:
                raise FatalCommandError("successful mission graph has no durable evidence")
            event = Event(
                event_id,
                EventKind.MISSION_COMPLETED,
                expected_version,
                {
                    "summary": f"Mission graph {state.workflow_id} completed.",
                    "evidence_ids": evidence_ids,
                    "graph_run_id": state.run_id,
                },
            )
        else:
            event = Event(
                event_id,
                EventKind.OPERATION_FAILED,
                expected_version,
                {
                    "operation": "mission_graph",
                    "reason": str(action.payload.get("reason") or state.failure or "mission failed"),
                    "recoverable": True,
                    "retryable": False,
                    "graph_run_id": state.run_id,
                },
            )
        receipt = self._lifecycle.submit_event(tenant_id, lifecycle_run_id, event)
        return {
            "projected": True,
            "lifecycle_run_id": lifecycle_run_id,
            "workflow_id": receipt.workflow_id,
            "duplicate": receipt.duplicate,
        }

    def _succeeded(
        self, envelope: Mapping[str, Any], action: WorkflowAction,
    ) -> Mapping[str, Any]:
        return self._project(envelope, action, succeeded=True)

    def _failed(
        self, envelope: Mapping[str, Any], action: WorkflowAction,
    ) -> Mapping[str, Any]:
        return self._project(envelope, action, succeeded=False)
