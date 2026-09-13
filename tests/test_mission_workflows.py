from __future__ import annotations

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
                "node_id": "done",
                "kind": "terminal",
                "purpose": "Accept the implementation evidence.",
                "configuration": {"max_iterations": 1},
            },
        ],
        "edges": [{"source": "build", "target": "done", "condition": "ready"}],
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
    assert context["available_tools"] == ["deploy.preview", "sandbox.run"]
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
    bad["nodes"].append({
        "node_id": "loop",
        "kind": "decision",
        "purpose": "Loop forever.",
        "configuration": {"max_iterations": 2},
    })
    bad["edges"] = [
        {"source": "build", "target": "loop", "condition": "ready"},
        {"source": "build", "target": "done", "condition": "skip"},
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
                "json_value": proposed_workflow(),
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
