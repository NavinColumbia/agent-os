"""Turn one CEO directive into a bounded, agent-designed executable mission graph."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    WorkflowRunState,
)
from agent_os.infrastructure.graph_output_refs import resolve_prior_output


MISSION_BOOTSTRAP_WORKFLOW_ID = "agent-os-mission-bootstrap"
MISSION_PLAN_ARTIFACT_LABEL = "mission-workflow"
_MISSION_PLAN_MAX_NODES = 32
_MISSION_PLAN_MAX_EDGES = 128
_MISSION_PLAN_MAX_ITERATIONS_PER_NODE = 16
_MISSION_PLAN_MAX_TOTAL_ITERATIONS = 128
_MISSION_TOOL_ALLOWLIST = frozenset({"deploy.preview", "sandbox.run"})


class PlannedNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    kind: Literal["agent", "decision", "human", "tool", "terminal"]
    purpose: str = Field(min_length=1, max_length=4_000)
    owner_role: str | None = Field(default=None, max_length=256)
    configuration: dict[str, Any] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def agent_has_owner(self) -> "PlannedNode":
        if self.kind == "agent" and not self.owner_role:
            raise ValueError("planned agent nodes require owner_role")
        return self


class PlannedEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    target: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    condition: str = Field(default="always", min_length=1, max_length=128)
    priority: int = Field(default=0, ge=-10_000, le=10_000)


class MissionWorkflowPlan(BaseModel):
    """Identity-free model proposal; authority supplies tenant, version, and creator."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    entry_node_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    nodes: list[PlannedNode] = Field(min_length=2, max_length=_MISSION_PLAN_MAX_NODES)
    edges: list[PlannedEdge] = Field(min_length=1, max_length=_MISSION_PLAN_MAX_EDGES)


def _workflow_id(tenant_id: str, planning_run_id: str, artifact_id: str) -> str:
    material = f"agent-os:mission-workflow:v1:{tenant_id}:{planning_run_id}:{artifact_id}"
    return "mission-" + hashlib.sha256(material.encode()).hexdigest()[:32]


def _child_run_id(planning_run_id: str, workflow_id: str) -> str:
    material = f"agent-os:mission-run:v1:{planning_run_id}:{workflow_id}"
    return "mission-run-" + hashlib.sha256(material.encode()).hexdigest()[:32]


def mission_bootstrap_definition(tenant_id: str) -> WorkflowDefinition:
    requirements = {
        "deliverable": (
            "Propose exactly one application/json artifact labeled mission-workflow. Use json_value, "
            "not a JSON-escaped content string. The value must match the supplied schema. Design the "
            "smallest sufficient non-linear team workflow for the CEO directive; include evidence gates, "
            "repair paths, and correlated human nodes only where judgment or authority is truly needed."
        ),
        "plan_schema": {
            "name": "string",
            "entry_node_id": "node id",
            "nodes": [{
                "node_id": "lowercase stable id",
                "kind": "agent | decision | human | tool | terminal",
                "purpose": "specific accountable outcome",
                "owner_role": "required for agent nodes",
                "configuration": {
                    "max_iterations": "integer 1..16",
                    "agent_context": "optional bounded instructions for that node",
                },
            }],
            "edges": [{
                "source": "node id",
                "target": "node id",
                "condition": "always or a condition the source node will satisfy",
                "priority": 0,
            }],
        },
        "available_tools": sorted(_MISSION_TOOL_ALLOWLIST),
        "sandbox_tool_configuration": {
            "tool": "sandbox.run",
            "source": {
                "node_id": "successful earlier agent node",
                "output_path": ["artifact_ids", "artifact-label"],
            },
            "command": ["direct", "argument", "vector"],
            "success_condition": "outgoing condition for exit code zero",
            "failure_condition": "optional outgoing repair condition",
        },
        "preview_deployment_configuration": {
            "tool": "deploy.preview",
            "source": {
                "node_id": "successful earlier agent node",
                "output_path": ["artifact_ids", "html-preview-artifact-label"],
            },
            "success_condition": "outgoing condition after publication",
        },
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
) -> WorkflowDefinition:
    try:
        plan = MissionWorkflowPlan.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise FatalCommandError(f"mission workflow proposal is invalid: {exc}") from exc

    nodes: list[WorkflowNode] = []
    tool_sources: list[tuple[str, str]] = []
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
                "max_iterations", "recipient_ids", "response_condition", "correlation_id",
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
            unexpected = set(configuration) - allowed_configuration
            if unexpected:
                raise FatalCommandError(
                    f"planned tool node has unsupported configuration: {sorted(unexpected)}"
                )
            if tool not in _MISSION_TOOL_ALLOWLIST:
                raise FatalCommandError(f"planned workflow requested unregistered tool: {tool}")
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

    workflow_id = _workflow_id(tenant_id, planning_run_id, artifact_id)
    try:
        definition = WorkflowDefinition(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            name=plan.name,
            version=1,
            entry_node_id=plan.entry_node_id,
            nodes=tuple(nodes),
            edges=tuple(WorkflowEdge(
                edge.source, edge.target, edge.condition, edge.priority,
            ) for edge in plan.edges),
            created_by="agent:mission-architect",
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
    return definition


class MissionBootstrapHandler:
    """Idempotently enqueue the built-in planner graph for a lifecycle command."""

    def __init__(self, graph_engine: GraphWorkflowEngine) -> None:
        self._graph = graph_engine

    def execute(self, item: CommandEnvelope) -> Mapping[str, Any]:
        definition = mission_bootstrap_definition(item.organization_id)
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

    def __init__(self, graph_engine: GraphWorkflowEngine, artifact_store: ArtifactStore) -> None:
        self._graph = graph_engine
        self._artifacts = artifact_store

    def named_handlers(self):
        return {"workflow.launch": self.execute}

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
        artifact_id = resolve_prior_output(
            state, node.configuration.get("source"), subject="workflow launch",
        )
        if not isinstance(artifact_id, str) or not artifact_id:
            raise FatalCommandError("workflow launch source is not an artifact ID")
        record = self._artifacts.describe(tenant_id, artifact_id)
        content = self._artifacts.get(tenant_id, artifact_id)
        if record is None or content is None:
            raise FatalCommandError("mission workflow artifact does not exist in this tenant")
        if record.get("media_type") != "application/json":
            raise FatalCommandError("mission workflow artifact must use application/json")
        try:
            raw = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise FatalCommandError("mission workflow artifact is invalid JSON") from exc
        if not isinstance(raw, Mapping):
            raise FatalCommandError("mission workflow artifact must contain one object")

        child_definition = materialize_mission_workflow(
            raw,
            tenant_id=tenant_id,
            planning_run_id=run_id,
            artifact_id=artifact_id,
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
        })
        self._graph.start_graph_run(
            tenant_id,
            child_definition.workflow_id,
            child_definition.version,
            run_id=child_run_id,
            request_id="mission-launch-" + hashlib.sha256(action.action_id.encode()).hexdigest(),
            context=child_context,
        )
        launch_record = {
            "format": "agent-os.mission-launch.v1",
            "tenant_id": tenant_id,
            "planning_run_id": run_id,
            "workflow_plan_artifact_id": artifact_id,
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
            },
        }


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
