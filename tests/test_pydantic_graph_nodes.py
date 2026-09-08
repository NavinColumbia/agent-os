from __future__ import annotations

import pytest
from pydantic_ai.models.test import TestModel

from agent_os.application.command_worker import FatalCommandError
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import begin_node, complete_node, resume_wait, start_workflow, wait_node
from agent_os.infrastructure.pydantic_graph_nodes import PydanticGraphNodeRuntime


def agent_graph() -> WorkflowDefinition:
    return WorkflowDefinition(
        "graph", "tenant-a", "Graph", 1, "agent",
        (
            WorkflowNode("agent", NodeKind.AGENT, "Choose the verified path", "engineer"),
            WorkflowNode("done", NodeKind.TERMINAL, "Accept upstream evidence"),
        ),
        (WorkflowEdge("agent", "done", "verified"),),
        "architect",
    )


def test_agent_node_uses_structured_output_and_only_declared_conditions():
    definition = agent_graph()
    started = start_workflow(definition, run_id="run-agent")
    action = started.actions[0]
    running = begin_node(started.state, action.token_id, expected_version=0).state
    output = {
        "summary": "Verified the path.",
        "disposition": "complete",
        "satisfied_conditions": ["verified"],
        "evidence_ids": ["evidence-1"],
        "output": {"decision": "ship"},
        "recipient_ids": [],
        "correlation_id": None,
        "reason": None,
        "retryable": False,
    }
    runtime = PydanticGraphNodeRuntime(TestModel(custom_output_args=output), max_turn_budget_cents=1)

    result = runtime.execute_node(
        tenant_id="tenant-a", run_id="run-agent", definition=definition,
        state=running, action=action, idempotency_key=action.action_id,
    )

    assert result["disposition"] == "complete"
    assert result["satisfied_conditions"] == ["verified"]
    assert result["output"]["summary"] == "Verified the path."


def test_agent_node_rejects_a_hallucinated_branch():
    definition = agent_graph()
    started = start_workflow(definition, run_id="run-branch")
    action = started.actions[0]
    running = begin_node(started.state, action.token_id, expected_version=0).state
    output = {
        "summary": "Invented a path.",
        "disposition": "complete",
        "satisfied_conditions": ["secret-shortcut"],
        "evidence_ids": ["evidence-1"],
        "output": {},
        "recipient_ids": [],
        "correlation_id": None,
        "reason": None,
        "retryable": False,
    }
    runtime = PydanticGraphNodeRuntime(TestModel(custom_output_args=output))

    with pytest.raises(FatalCommandError, match="unknown conditions"):
        runtime.execute_node(
            tenant_id="tenant-a", run_id="run-branch", definition=definition,
            state=running, action=action, idempotency_key=action.action_id,
        )


def test_human_node_creates_a_deterministic_correlated_wait_without_model_spend():
    definition = WorkflowDefinition(
        "human", "tenant-a", "Human", 1, "approval",
        (
            WorkflowNode(
                "approval", NodeKind.HUMAN, "Approve the irreversible deployment",
                configuration={"recipient_ids": ["human:ceo"]},
            ),
            WorkflowNode("done", NodeKind.TERMINAL, "Done"),
        ),
        (WorkflowEdge("approval", "done", "always"),),
        "architect",
    )
    started = start_workflow(definition, run_id="run-human")
    action = started.actions[0]
    running = begin_node(started.state, action.token_id, expected_version=0).state
    runtime = PydanticGraphNodeRuntime(TestModel())

    first = runtime.execute_node(
        tenant_id="tenant-a", run_id="run-human", definition=definition,
        state=running, action=action, idempotency_key=action.action_id,
    )
    repeated = runtime.execute_node(
        tenant_id="tenant-a", run_id="run-human", definition=definition,
        state=running, action=action, idempotency_key=action.action_id,
    )

    assert first == repeated
    assert first["disposition"] == "wait"
    assert first["recipient_ids"] == ["human:ceo"]
    assert first["correlation_id"].startswith("graph-question-")

    waiting = wait_node(
        running,
        action.token_id,
        expected_version=1,
        correlation_id=first["correlation_id"],
        reason=first["reason"],
        recipient_ids=("human:ceo",),
    )
    resumed = resume_wait(
        waiting.state,
        expected_version=2,
        correlation_id=first["correlation_id"],
        response={"approved": True},
    )
    resumed_action = resumed.actions[0]
    resumed_running = begin_node(
        resumed.state, resumed_action.token_id, expected_version=3,
    ).state
    completed = runtime.execute_node(
        tenant_id="tenant-a", run_id="run-human", definition=definition,
        state=resumed_running, action=resumed_action, idempotency_key=resumed_action.action_id,
    )
    assert completed["disposition"] == "complete"
    assert completed["output"]["human_response"] == {"approved": True}
    assert completed["evidence_ids"][0].startswith("human-response-")


def test_terminal_node_aggregates_real_upstream_evidence():
    definition = agent_graph()
    started = start_workflow(definition, run_id="run-terminal")
    agent_action = started.actions[0]
    running = begin_node(started.state, agent_action.token_id, expected_version=0).state
    advanced = complete_node(
        definition,
        running,
        agent_action.token_id,
        expected_version=1,
        satisfied_conditions=frozenset({"verified"}),
        evidence_ids=("verified-build",),
    )
    terminal_action = advanced.actions[0]
    terminal_running = begin_node(
        advanced.state, terminal_action.token_id, expected_version=2,
    ).state
    runtime = PydanticGraphNodeRuntime(TestModel())

    result = runtime.execute_node(
        tenant_id="tenant-a", run_id="run-terminal", definition=definition,
        state=terminal_running, action=terminal_action,
        idempotency_key=terminal_action.action_id,
    )

    assert result["evidence_ids"] == ["verified-build"]
    assert result["output"]["accepted_upstream_evidence"] == ["verified-build"]
