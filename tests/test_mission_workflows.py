from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from agent_os.application.command_worker import CommandRunStatus, FatalCommandError
from agent_os.application.graph_action_worker import DurableGraphActionWorker
from agent_os.application.lifecycle import CommandEnvelope
from agent_os.domain.lifecycle import (
    Command,
    CommandKind,
    Event,
    EventKind,
    LifecycleState,
    LifecycleStatus,
)
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowNode
from agent_os.domain.workflow_runtime import (
    NodeToken,
    TokenStatus,
    WorkflowAction,
    WorkflowActionKind,
    WorkflowEvent,
    WorkflowEventKind,
    WorkflowRunState,
    WorkflowRunStatus,
)
from agent_os.infrastructure.graph_action_executor import DurableGraphActionExecutor
from agent_os.infrastructure.memory import InMemoryWorkflowEngine
from agent_os.infrastructure.mission_workflows import (
    MISSION_BOOTSTRAP_WORKFLOW_ID,
    MissionBootstrapHandler,
    MissionCancellationHandler,
    MissionGraphEffectHandlers,
    WorkflowLaunchToolNodeHandlers,
    materialize_mission_program,
    materialize_mission_workflow,
    mission_bootstrap_definition,
    mission_planning_run_id,
)
from agent_os.infrastructure.pydantic_graph_nodes import PydanticGraphNodeRuntime
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore
from agent_os.infrastructure.sql_workflow_graph import SQLGraphWorkflowEngine
from agent_os.infrastructure.tool_node_router import GraphToolNodeRouter


def proposed_workflow() -> dict:
    return {
        "name": "Implement the directive",
        "entry_node_id": "build",
        "nodes": [
            {
                "node_id": "build",
                "kind": "agent",
                "purpose": "Produce a verified implementation artifact.",
                "owner_role": "engineer",
                "configuration": {
                    "max_iterations": 2,
                    "agent_context": {"required_artifact": "application-source"},
                },
            },
            {
                "node_id": "replan",
                "kind": "decision",
                "purpose": "Check new evidence and revise the plan when material facts changed.",
                "owner_role": "mission-manager",
                "configuration": {"max_iterations": 2},
            },
            {
                "node_id": "revise-program", "kind": "tool",
                "purpose": "Atomically admit the next mission program revision.",
                "configuration": {
                    "tool": "workflow.revise",
                    "source": {"node_id": "replan",
                               "output_path": ["artifact_ids", "mission-program-revision"]},
                    "success_condition": "revised", "max_iterations": 2,
                },
            },
            {
                "node_id": "done",
                "kind": "terminal",
                "purpose": "Accept the implementation evidence.",
                "configuration": {"max_iterations": 1},
            },
        ],
        "edges": [
            {"source": "build", "target": "replan", "condition": "ready"},
            {"source": "replan", "target": "done", "condition": "stable"},
            {"source": "replan", "target": "revise-program", "condition": "changed"},
            {"source": "revise-program", "target": "build", "condition": "revised"},
        ],
    }


def proposed_program() -> dict:
    return {
        "format": "agent-os.mission-program.v1",
        "revision": 1,
        "objective": "Build a tested application.",
        "authorized_budget_cents": 0,
        "success_measures": [{"measure_id": "implementation-ready",
                              "description": "A tested implementation artifact is accepted."}],
        "feasibility": {
            "verdict": "viable",
            "rationale": "The bounded application can be implemented with configured capabilities.",
            "delivery_estimate": {
                "optimistic": 1, "likely": 2, "pessimistic": 5, "unit": "days",
                "basis": "One implementation and review loop.", "confidence": 0.7,
            },
            "cost_estimate": {
                "optimistic": 0, "likely": 0, "pessimistic": 100,
                "unit": "usd_cents", "basis": "Local runtime with a bounded model turn.",
                "confidence": 0.7,
            },
            "assumptions": ["The directive is within the configured application sandbox."],
        },
        "clarifications": [],
        "roles": [
            {"role_id": "mission-manager", "title": "Mission Manager",
             "participant_kind": "agent", "responsibilities": ["Replan from evidence"]},
            {"role_id": "engineer", "title": "Engineer", "participant_kind": "agent",
             "responsibilities": ["Build tested application"],
             "manager_role_id": "mission-manager"},
        ],
        "resources": [],
        "capabilities": [{
            "capability_id": "application-delivery", "purpose": "Build the requested application",
            "status": "missing", "owner_role_id": "engineer",
            "expansion_mode": "build_capability", "expansion_node_ids": ["build"],
            "acceptance_checks": ["Implementation evidence exists."],
        }],
        "workstreams": [
            {"workstream_id": "delivery", "objective": "Build and verify the application",
             "accountable_role_id": "engineer", "workflow_node_ids": ["build"],
             "required_capability_ids": ["application-delivery"],
             "acceptance_criteria": ["Implementation artifact is tested."]},
            {"workstream_id": "command", "objective": "Review evidence and replan",
             "accountable_role_id": "mission-manager",
             "workflow_node_ids": ["replan", "revise-program"],
             "acceptance_criteria": ["Material changes cause replanning."]},
        ],
        "verification": [{
            "claim_id": "implementation-ready", "claim": "Implementation meets the directive.",
            "success_measure_ids": ["implementation-ready"],
            "reviewer_role_id": "mission-manager", "verification_node_ids": ["replan"],
            "required_evidence": ["Implementation artifact"],
            "failure_routes_to_node_id": "build",
        }],
        "replanning": {
            "owner_role_id": "mission-manager", "review_cadence": "After each delivery attempt",
            "triggers": ["Verification fails", "requirements change"],
            "replan_node_ids": ["replan"], "continue_condition": "stable",
            "replan_condition": "changed", "material_change_requires_new_revision": True,
            "notify_role_ids": ["mission-manager"],
        },
        "workflow": proposed_workflow(),
    }


def test_materialized_plan_is_tenant_owned_bounded_and_cannot_request_unknown_tools():
    definition = materialize_mission_workflow(
        proposed_workflow(),
        tenant_id="tenant-a",
        planning_run_id="plan-a",
        artifact_id="artifact-plan",
    )

    assert definition.tenant_id == "tenant-a"
    assert definition.created_by == "agent:mission-architect"
    assert definition.version == 1
    assert definition.workflow_id.startswith("mission-")

    bad = proposed_workflow()
    bad["nodes"][0] = {
        "node_id": "build",
        "kind": "tool",
        "purpose": "Escape the authority layer.",
        "configuration": {"tool": "host.shell", "max_iterations": 1},
    }
    with pytest.raises(FatalCommandError, match="unavailable tool"):
        materialize_mission_workflow(
            bad,
            tenant_id="tenant-a",
            planning_run_id="plan-b",
            artifact_id="artifact-bad",
        )


def test_planner_advertises_and_enforces_only_runtime_available_tools():
    definition = mission_bootstrap_definition(
        "tenant-a", available_tools={"deploy.preview", "sandbox.run"},
    )
    planner = next(node for node in definition.nodes if node.node_id == "plan")
    context = planner.configuration["agent_context"]
    assert context["available_tools"] == ["deploy.preview", "sandbox.run", "workflow.revise"]
    assert "preview_deployment_configuration" in context
    assert "sandbox_tool_configuration" in context
    assert "production_static_deployment_configuration" not in context
    assert "production_service_deployment_configuration" not in context

    plan = proposed_workflow()
    plan["nodes"][0] = {
        "node_id": "build",
        "kind": "tool",
        "purpose": "Attempt a cloud-only deployment.",
        "configuration": {
            "tool": "deploy.static",
            "source": {"node_id": "done", "output_path": ["artifact_ids", "source"]},
            "approval": {"node_id": "done", "output_path": ["human_response", "approved"]},
            "app_slug": "blocked-app", "success_condition": "ready", "max_iterations": 1,
        },
    }
    with pytest.raises(FatalCommandError, match="unavailable tool"):
        materialize_mission_workflow(
            plan, tenant_id="tenant-a", planning_run_id="plan-local",
            artifact_id="artifact-local", allowed_tools={"deploy.preview", "sandbox.run"},
        )


def test_production_publication_plan_requires_a_preceding_human_approval():
    plan = {
        "name": "Build, approve, and publish",
        "entry_node_id": "build",
        "nodes": [
            {
                "node_id": "build", "kind": "agent", "purpose": "Build tested source",
                "owner_role": "engineer", "configuration": {"max_iterations": 2},
            },
            {
                "node_id": "approve", "kind": "human", "purpose": "Approve production",
                "configuration": {
                    "max_iterations": 1, "recipient_ids": ["human:ceo"],
                    "response_condition": "approved",
                },
            },
            {
                "node_id": "publish", "kind": "tool", "purpose": "Publish production app",
                "configuration": {
                    "tool": "deploy.static",
                    "source": {
                        "node_id": "build", "output_path": ["artifact_ids", "source"],
                    },
                    "approval": {
                        "node_id": "approve", "output_path": ["human_response", "approved"],
                    },
                    "app_slug": "customer-portal", "success_condition": "published",
                    "max_iterations": 1,
                },
            },
            {
                "node_id": "done", "kind": "terminal", "purpose": "Accept release",
                "configuration": {"max_iterations": 1},
            },
        ],
        "edges": [
            {"source": "build", "target": "approve", "condition": "built"},
            {"source": "approve", "target": "publish", "condition": "approved"},
            {"source": "publish", "target": "done", "condition": "published"},
        ],
    }
    definition = materialize_mission_workflow(
        plan, tenant_id="tenant-a", planning_run_id="plan", artifact_id="artifact",
    )
    publish = next(item for item in definition.nodes if item.node_id == "publish")
    assert publish.configuration["tool"] == "deploy.static"

    plan["nodes"][1]["kind"] = "decision"
    plan["nodes"][1]["owner_role"] = "manager"
    plan["nodes"][1]["configuration"] = {"max_iterations": 1}
    with pytest.raises(FatalCommandError, match="approval must reference a human node"):
        materialize_mission_workflow(
            plan, tenant_id="tenant-a", planning_run_id="bad", artifact_id="artifact-bad",
        )


def test_plan_rejects_a_reachable_trap_with_no_terminal_path():
    bad = proposed_workflow()
    bad["nodes"] = [node for node in bad["nodes"] if node["node_id"] != "revise-program"]
    bad["nodes"].append({
        "node_id": "loop",
        "kind": "decision",
        "purpose": "Loop forever.",
        "configuration": {"max_iterations": 2},
    })
    bad["edges"] = [
        {"source": "build", "target": "replan", "condition": "ready"},
        {"source": "replan", "target": "loop", "condition": "stable"},
        {"source": "replan", "target": "done", "condition": "skip"},
        {"source": "loop", "target": "loop", "condition": "again"},
    ]
    with pytest.raises(FatalCommandError, match="no terminal path"):
        materialize_mission_workflow(
            bad,
            tenant_id="tenant-a",
            planning_run_id="plan-trap",
            artifact_id="artifact-trap",
        )


def test_start_mission_plans_validates_and_launches_a_child_graph(tmp_path: Path):
    graph = SQLGraphWorkflowEngine(
        f"sqlite:///{tmp_path / 'mission-graph.sqlite3'}", create_schema=True,
    )
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'mission-artifacts.sqlite3'}", create_schema=True,
    )
    try:
        envelope = CommandEnvelope(
            "command-start-mission",
            "lifecycle-run",
            "tenant-a",
            "scope-accepted",
            1,
            0,
            Command(CommandKind.START_MISSION, {
                "prompt": "Build a tested application",
                "requested_by": "human:ceo",
            }),
        )
        started = MissionBootstrapHandler(graph).execute(envelope)
        planning_run_id = mission_planning_run_id("lifecycle-run")
        assert started["planning_run_id"] == planning_run_id
        assert graph.get_workflow_definition(
            "tenant-a", MISSION_BOOTSTRAP_WORKFLOW_ID, 1,
        ) is not None

        planner_output = {
            "summary": "Designed the bounded mission team.",
            "disposition": "complete",
            "satisfied_conditions": ["planned"],
            "evidence_ids": [],
            "artifacts": [{
                "label": "mission-workflow",
                "media_type": "application/json",
                "json_value": proposed_program(),
            }],
            "output": {},
            "recipient_ids": [],
            "correlation_id": None,
            "reason": None,
            "retryable": False,
        }
        launch = WorkflowLaunchToolNodeHandlers(graph, artifacts)
        runtime = PydanticGraphNodeRuntime(
            TestModel(custom_output_args=planner_output),
            handlers=GraphToolNodeRouter(launch.named_handlers()).handlers(),
            artifact_store=artifacts,
            max_turn_budget_cents=1,
        )
        worker = DurableGraphActionWorker(
            outbox=graph,
            executor=DurableGraphActionExecutor(engine=graph, node_runtime=runtime),
            worker_id="mission-planner-worker",
            lease_seconds=30,
        )

        reports = [worker.run_one("tenant-a"), worker.run_one("tenant-a")]

        assert [report.status for report in reports] == [
            CommandRunStatus.SUCCEEDED, CommandRunStatus.SUCCEEDED,
        ]
        planning = graph.get_graph_run("tenant-a", planning_run_id)
        launch_token = next(token for token in planning.tokens if token.node_id == "launch")
        child_run_id = launch_token.output["child_run_id"]
        child = graph.get_graph_run("tenant-a", child_run_id)
        assert child is not None and child.status is WorkflowRunStatus.ACTIVE
        assert child.context["lifecycle_run_id"] == "lifecycle-run"
        assert child.context["mission_execution"] is True
        plan_id = launch_token.output["workflow_plan_artifact_id"]
        assert artifacts.describe("tenant-a", plan_id)["media_type"] == "application/json"
        assert artifacts.describe(
            "tenant-a", launch_token.output["launch_artifact_id"],
        ) is not None

        cancelled = MissionCancellationHandler(graph, artifacts).execute(CommandEnvelope(
            "command-cancel-mission",
            "lifecycle-run",
            "tenant-a",
            "cancel-requested",
            2,
            0,
            Command(CommandKind.CANCEL_ACTIVE_OPERATION, {"reason": "CEO stopped the run"}),
        ))
        assert cancelled["cancelled_graphs"][planning_run_id] == "cancelled"
        assert cancelled["cancelled_graphs"][child_run_id] == "cancelled"
        replay = MissionCancellationHandler(graph, artifacts).execute(CommandEnvelope(
            "command-cancel-mission",
            "lifecycle-run",
            "tenant-a",
            "cancel-requested",
            2,
            0,
            Command(CommandKind.CANCEL_ACTIVE_OPERATION, {"reason": "CEO stopped the run"}),
        ))
        assert replay == cancelled
        assert graph.get_graph_run(
            "tenant-a", planning_run_id,
        ).status is WorkflowRunStatus.CANCELLED
        assert graph.get_graph_run(
            "tenant-a", child_run_id,
        ).status is WorkflowRunStatus.CANCELLED
    finally:
        graph.close()
        artifacts.close()


def test_cancelled_lifecycle_cannot_start_a_late_planning_graph(tmp_path: Path):
    lifecycle = InMemoryWorkflowEngine()
    lifecycle.start_run(
        LifecycleState("late-run", "tenant-a"),
        Event("scope", EventKind.SCOPE_ACCEPTED, 0, {"prompt": "Build"}),
    )
    lifecycle.submit_event(
        "tenant-a",
        "late-run",
        Event("cancel", EventKind.CANCEL_REQUESTED, 1, {"reason": "Stop"}),
    )
    graph = SQLGraphWorkflowEngine(
        f"sqlite:///{tmp_path / 'late-cancel.sqlite3'}", create_schema=True,
    )
    try:
        result = MissionBootstrapHandler(graph, lifecycle).execute(CommandEnvelope(
            "late-start-command",
            "late-run",
            "tenant-a",
            "scope",
            1,
            0,
            Command(CommandKind.START_MISSION, {"prompt": "Build"}),
        ))
        assert result == {"planning_started": False, "reason": "lifecycle_cancelled"}
        assert graph.get_graph_run(
            "tenant-a", mission_planning_run_id("late-run"),
        ) is None
    finally:
        graph.close()


def test_material_replan_supersedes_the_graph_in_place_without_restarting_the_mission(
    tmp_path: Path,
):
    graph = SQLGraphWorkflowEngine(
        f"sqlite:///{tmp_path / 'revision-graph.sqlite3'}", create_schema=True,
    )
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'revision-artifacts.sqlite3'}", create_schema=True,
    )
    try:
        original = proposed_program()
        _, definition = materialize_mission_program(
            original, tenant_id="tenant-a", planning_run_id="planning",
            artifact_id="program-one",
        )
        graph.register_workflow(definition)
        admitted = {key: value for key, value in original.items() if key != "workflow"}
        graph.start_graph_run(
            "tenant-a", definition.workflow_id, 1, run_id="same-mission-run",
            request_id="start-revisable-mission", context={
                "mission_execution": True, "planning_run_id": "planning",
                "mission_program": admitted, "mission_program_revision": 1,
            },
        )

        state = graph.get_graph_run("tenant-a", "same-mission-run")
        build = state.tokens[0]
        state = graph.submit_graph_event("tenant-a", state.run_id, WorkflowEvent(
            "begin-build", WorkflowEventKind.NODE_BEGAN, state.version,
            {"token_id": build.token_id},
        )).state
        state = graph.submit_graph_event("tenant-a", state.run_id, WorkflowEvent(
            "complete-build", WorkflowEventKind.NODE_COMPLETED, state.version,
            {"token_id": build.token_id, "satisfied_conditions": ["ready"],
             "evidence_ids": ["artifact-build"], "output": {}},
        )).state
        replan = next(token for token in state.tokens if token.node_id == "replan")
        state = graph.submit_graph_event("tenant-a", state.run_id, WorkflowEvent(
            "begin-replan", WorkflowEventKind.NODE_BEGAN, state.version,
            {"token_id": replan.token_id},
        )).state

        replacement = copy.deepcopy(original)
        replacement["revision"] = 2
        replacement["workflow"]["name"] = "Revised implementation after new evidence"
        replacement_id = artifacts.put(
            organization_id="tenant-a",
            content=json.dumps(replacement).encode(), media_type="application/json",
            idempotency_key="program-revision-two",
        )
        state = graph.submit_graph_event("tenant-a", state.run_id, WorkflowEvent(
            "complete-replan", WorkflowEventKind.NODE_COMPLETED, state.version,
            {"token_id": replan.token_id, "satisfied_conditions": ["changed"],
             "evidence_ids": [replacement_id],
             "output": {"artifact_ids": {"mission-program-revision": replacement_id}}},
        )).state
        revise_token = next(
            token for token in state.tokens
            if token.node_id == "revise-program" and token.status is TokenStatus.READY
        )
        state = graph.submit_graph_event("tenant-a", state.run_id, WorkflowEvent(
            "begin-revision", WorkflowEventKind.NODE_BEGAN, state.version,
            {"token_id": revise_token.token_id},
        )).state
        action = WorkflowAction(
            "atomic-revision-action", WorkflowActionKind.EXECUTE_NODE,
            revise_token.token_id, revise_token.node_id,
        )
        revision_node = next(
            node for node in definition.nodes if node.node_id == "revise-program"
        )

        WorkflowLaunchToolNodeHandlers(graph, artifacts).revise(
            "tenant-a", state.run_id, definition, state, action, revision_node,
        )

        revised = graph.get_graph_run("tenant-a", "same-mission-run")
        assert revised.run_id == "same-mission-run"
        assert revised.workflow_id == definition.workflow_id
        assert revised.workflow_version == 2
        assert revised.context["mission_program_revision"] == 2
        assert revised.context["prior_workflow_version"] == 1
        assert revised.token(revise_token.token_id).status is TokenStatus.CANCELLED
        assert any(
            token.node_id == replacement["workflow"]["entry_node_id"]
            and token.status is TokenStatus.READY
            for token in revised.tokens
        )
        stored = graph.get_workflow_definition("tenant-a", definition.workflow_id, 2)
        assert stored.name == "Revised implementation after new evidence"
        assert stored.supersedes_version == 1
    finally:
        artifacts.close()
        graph.close()


class OneStateGraph:
    def __init__(self, state: WorkflowRunState) -> None:
        self.state = state

    def get_graph_run(self, tenant_id: str, run_id: str):
        if tenant_id == self.state.tenant_id and run_id == self.state.run_id:
            return self.state
        return None


def test_successful_child_graph_projects_once_to_the_coarse_ceo_lifecycle():
    lifecycle = InMemoryWorkflowEngine()
    lifecycle.start_run(
        LifecycleState("lifecycle-run", "tenant-a"),
        Event("scope", EventKind.SCOPE_ACCEPTED, 0, {"prompt": "Build"}),
    )
    token = NodeToken(
        "terminal-token", "done", TokenStatus.SUCCEEDED, 1,
        evidence_ids=("artifact-release", "artifact-tests"),
    )
    graph_state = WorkflowRunState(
        "mission-run", "tenant-a", "mission-workflow", 1, 2,
        WorkflowRunStatus.SUCCEEDED,
        (token,),
        {
            "mission_execution": True,
            "lifecycle_run_id": "lifecycle-run",
            "lifecycle_expected_version": 1,
        },
        (token.token_id,),
    )
    action = WorkflowAction(
        "mission-succeeded-action", WorkflowActionKind.RUN_SUCCEEDED,
        token.token_id, token.node_id, {"terminal_token_ids": [token.token_id]},
    )
    handlers = MissionGraphEffectHandlers(
        lifecycle_engine=lifecycle,
        graph_engine=OneStateGraph(graph_state),
        notification_handlers={},
    ).graph_handlers()

    first = handlers[WorkflowActionKind.RUN_SUCCEEDED]({
        "tenant_id": "tenant-a", "run_id": "mission-run",
    }, action)
    replay = handlers[WorkflowActionKind.RUN_SUCCEEDED]({
        "tenant_id": "tenant-a", "run_id": "mission-run",
    }, action)

    state = lifecycle.get_run("tenant-a", "lifecycle-run")
    assert state.status is LifecycleStatus.SUCCEEDED
    assert state.artifact_revision == "artifact-release"
    assert first["projected"] is True
    assert replay["duplicate"] is True
