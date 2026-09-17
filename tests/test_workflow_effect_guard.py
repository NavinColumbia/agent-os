from __future__ import annotations

from pathlib import Path

import pytest

from agent_os.application.assurance import AssuranceKernel
from agent_os.application.command_worker import FatalCommandError, RetryableCommandError
from agent_os.application.runtime_effects import WorkflowEffectGuard
from agent_os.domain.mission_model import MissionSpec
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import (
    NodeToken,
    TokenStatus,
    WorkflowAction,
    WorkflowActionKind,
    WorkflowRunState,
    WorkflowRunStatus,
)
from agent_os.infrastructure.authzen_policy import baseline_effect_policy
from agent_os.infrastructure.sql_mission_control import SQLMissionControl
from agent_os.infrastructure.tool_node_router import GraphToolNodeRouter


def _store(path: Path) -> SQLMissionControl:
    store = SQLMissionControl(
        f"sqlite:///{path}",
        assurance_kernel=AssuranceKernel(baseline_effect_policy()),
        create_schema=True,
    )
    store.create_mission(MissionSpec(
        mission_id="mission-1",
        tenant_id="tenant-a",
        objective="Ship a governed app",
        principal_id="human:ceo",
        accountable_owner_id="human:ceo",
        budget_limit_cents=0,
        success_measures=("the deployed app is verifiably reachable",),
    ))
    return store


def _action() -> WorkflowAction:
    return WorkflowAction(
        action_id="action-publish-1",
        kind=WorkflowActionKind.EXECUTE_NODE,
        token_id="token-publish",
        node_id="publish",
    )


def _state(*tokens: NodeToken) -> WorkflowRunState:
    return WorkflowRunState(
        run_id="graph-run-1",
        tenant_id="tenant-a",
        workflow_id="workflow-1",
        workflow_version=1,
        version=3,
        status=WorkflowRunStatus.ACTIVE,
        tokens=tokens or (
            NodeToken("token-publish", "publish", TokenStatus.RUNNING, 1),
        ),
        context={"lifecycle_run_id": "mission-1"},
    )


def _definition(node: WorkflowNode, *, with_approval: bool = False) -> WorkflowDefinition:
    terminal = WorkflowNode("done", NodeKind.TERMINAL, "Done")
    if with_approval:
        approval = WorkflowNode("approve", NodeKind.HUMAN, "Approve exact release")
        nodes = (approval, node, terminal)
        edges = (
            WorkflowEdge("approve", "publish", "approved"),
            WorkflowEdge("publish", "done", "published"),
        )
        entry = "approve"
    else:
        nodes = (node, terminal)
        edges = (WorkflowEdge("publish", "done", "published"),)
        entry = "publish"
    return WorkflowDefinition(
        workflow_id="workflow-1", tenant_id="tenant-a", name="Release", version=1,
        entry_node_id=entry, nodes=nodes, edges=edges, created_by="agent:architect",
    )


def test_external_tool_is_admitted_settled_and_audited(tmp_path: Path):
    store = _store(tmp_path / "effects.sqlite3")
    called: list[str] = []
    node = WorkflowNode(
        "publish", NodeKind.TOOL, "Publish preview",
        configuration={"tool": "deploy.preview"},
    )

    def handler(*_arguments):
        called.append("yes")
        return {"disposition": "complete"}

    router = GraphToolNodeRouter(
        {"deploy.preview": handler}, effect_guard=WorkflowEffectGuard(store),
    )
    try:
        assert router.execute(
            "tenant-a", "graph-run-1", _definition(node), _state(), _action(), node,
        )["disposition"] == "complete"
        assert called == ["yes"]
        view = store.control_view("tenant-a", "mission-1")
        assert view is not None
        assert view["effects"][0]["status"] == "succeeded"
        assert view["effects"][0]["decision"]["disposition"] == "allowed"
        assert any(item["parent_grant_id"] for item in view["authorities"])
    finally:
        store.close()


def test_transient_external_failure_keeps_admission_for_idempotent_retry(tmp_path: Path):
    store = _store(tmp_path / "transient-effect.sqlite3")
    calls = 0
    node = WorkflowNode(
        "publish", NodeKind.TOOL, "Publish preview",
        configuration={"tool": "deploy.preview"},
    )

    def handler(*_arguments):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RetryableCommandError("provider temporarily unavailable")
        return {"disposition": "complete"}

    router = GraphToolNodeRouter(
        {"deploy.preview": handler}, effect_guard=WorkflowEffectGuard(store),
    )
    try:
        with pytest.raises(RetryableCommandError):
            router.execute(
                "tenant-a", "graph-run-1", _definition(node), _state(), _action(), node,
            )
        first = store.control_view("tenant-a", "mission-1")
        assert first is not None and first["effects"][0]["status"] == "admitted"

        assert router.execute(
            "tenant-a", "graph-run-1", _definition(node), _state(), _action(), node,
        )["disposition"] == "complete"
        second = store.control_view("tenant-a", "mission-1")
        assert second is not None and second["effects"][0]["status"] == "succeeded"
        assert calls == 2
    finally:
        store.close()


@pytest.mark.parametrize("approved", [False, True])
def test_production_effect_requires_exact_human_receipt(tmp_path: Path, approved: bool):
    store = _store(tmp_path / f"production-{approved}.sqlite3")
    called: list[str] = []
    node = WorkflowNode(
        "publish", NodeKind.TOOL, "Publish production",
        configuration={
            "tool": "deploy.static", "app_slug": "customer-app",
            "approval": {"node_id": "approve", "output_path": ["human_response", "approved"]},
        },
    )
    definition = _definition(node, with_approval=True)
    state = _state(
        NodeToken(
            "token-approve", "approve", TokenStatus.SUCCEEDED, 1,
            evidence_ids=("artifact:approval-receipt",),
            output={"human_response": {"approved": approved}},
        ),
        NodeToken("token-publish", "publish", TokenStatus.RUNNING, 1),
    )

    def handler(*_arguments):
        called.append("yes")
        return {"disposition": "complete"}

    router = GraphToolNodeRouter(
        {"deploy.static": handler}, effect_guard=WorkflowEffectGuard(store),
    )
    try:
        if approved:
            router.execute(
                "tenant-a", "graph-run-1", definition, state, _action(), node,
            )
            assert called == ["yes"]
            view = store.control_view("tenant-a", "mission-1")
            exact = next(
                item for item in view["authorities"] if item["parent_grant_id"] is not None
            )
            assert exact["human_approved"] is True
            assert exact["approval_evidence_ids"] == [
                "artifact:approval-receipt", "token:token-approve",
            ]
        else:
            with pytest.raises(FatalCommandError, match="requires human release"):
                router.execute(
                    "tenant-a", "graph-run-1", definition, state, _action(), node,
                )
            assert called == []
    finally:
        store.close()
