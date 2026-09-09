from __future__ import annotations

import pytest
from pydantic_ai.models.test import TestModel

from agent_os.application.command_worker import FatalCommandError
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import begin_node, complete_node, resume_wait, start_workflow, wait_node
from agent_os.infrastructure.pydantic_graph_nodes import PydanticGraphNodeRuntime
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore


class FakeUsageMeter:
    def __init__(self):
        self.reservations = []
        self.settlements = []

    def reserve_model_turn(self, **values):
        self.reservations.append(values)
        return values

    def settle_model_turn(self, **values):
        self.settlements.append(values)
        return values


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
    meter = FakeUsageMeter()
    runtime = PydanticGraphNodeRuntime(
        TestModel(custom_output_args=output), max_turn_budget_cents=1,
        usage_meter=meter, model_name="test:model",
    )

    result = runtime.execute_node(
        tenant_id="tenant-a", run_id="run-agent", definition=definition,
        state=running, action=action, idempotency_key=action.action_id,
    )

    assert result["disposition"] == "complete"
    assert result["satisfied_conditions"] == ["verified"]
    assert result["output"]["summary"] == "Verified the path."
    assert meter.reservations[0]["category"] == "graph_agent"
    assert meter.reservations[0]["source_id"] == action.action_id
    assert meter.settlements[0]["usage"] == result["output"]["usage"]


def test_agent_node_persists_bounded_management_proposals_in_durable_output():
    definition = agent_graph()
    started = start_workflow(definition, run_id="run-management")
    action = started.actions[0]
    running = begin_node(started.state, action.token_id, expected_version=0).state
    output = {
        "summary": "Found a missing reviewer and delegated follow-up.",
        "disposition": "complete",
        "satisfied_conditions": ["verified"],
        "evidence_ids": ["evidence-1"],
        "output": {},
        "risks": ["No independent security review"],
        "messages": [{
            "audience": "human", "kind": "update", "recipient_ids": ["human:ceo"],
            "subject": "Review gap", "body": "Security review is still missing.",
        }],
        "proposed_work": [{
            "objective": "Review authentication", "owner_role": "security-specialist",
            "acceptance_criteria": ["Threats documented"],
        }],
        "hiring_requests": [{
            "role": "security-specialist", "reason": "No reviewer is assigned",
            "capabilities": ["security"],
        }],
        "decisions": [{
            "intent": "Gate launch", "considered_options": ["launch", "review"],
            "chosen_option": "review", "rationale": "Reduce risk", "confidence": 0.9,
            "reversible": True,
        }],
        "next_actions": ["Assign the review"],
    }
    runtime = PydanticGraphNodeRuntime(TestModel(custom_output_args=output))

    result = runtime.execute_node(
        tenant_id="tenant-a", run_id="run-management", definition=definition,
        state=running, action=action, idempotency_key=action.action_id,
    )

    actions = result["output"]["organization_actions"]
    assert actions["risks"] == ["No independent security review"]
    assert actions["messages"][0]["recipient_ids"] == ["human:ceo"]
    assert actions["proposed_work"][0]["owner_role"] == "security-specialist"
    assert actions["hiring_requests"][0]["requested_count"] == 1
    assert actions["decisions"][0]["chosen_option"] == "review"
    assert actions["next_actions"] == ["Assign the review"]


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


def test_human_rejection_selects_only_an_explicit_rejection_path():
    definition = WorkflowDefinition(
        "human-decision", "tenant-a", "Human decision", 1, "approval",
        (
            WorkflowNode(
                "approval", NodeKind.HUMAN, "Approve release",
                configuration={
                    "response_condition": "approved",
                    "rejection_condition": "rejected",
                },
            ),
            WorkflowNode("ship", NodeKind.TERMINAL, "Ship"),
            WorkflowNode("repair", NodeKind.TERMINAL, "Repair"),
        ),
        (
            WorkflowEdge("approval", "ship", "approved"),
            WorkflowEdge("approval", "repair", "rejected"),
        ),
        "architect",
    )
    started = start_workflow(definition, run_id="run-reject")
    action = started.actions[0]
    running = begin_node(started.state, action.token_id, expected_version=0).state
    waiting = wait_node(
        running, action.token_id, expected_version=1,
        correlation_id="decision", reason="Approve release", recipient_ids=("human:ceo",),
    )
    resumed = resume_wait(
        waiting.state, expected_version=2,
        correlation_id="decision", response={"approved": False},
    )
    resumed_action = resumed.actions[0]
    resumed_running = begin_node(
        resumed.state, resumed_action.token_id, expected_version=3,
    ).state

    result = PydanticGraphNodeRuntime(TestModel()).execute_node(
        tenant_id="tenant-a", run_id="run-reject", definition=definition,
        state=resumed_running, action=resumed_action,
        idempotency_key=resumed_action.action_id,
    )

    assert result["satisfied_conditions"] == ["rejected"]


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


def test_graph_agent_persists_new_evidence_and_rejects_invented_ids(tmp_path):
    definition = agent_graph()
    started = start_workflow(definition, run_id="run-evidence")
    action = started.actions[0]
    running = begin_node(started.state, action.token_id, expected_version=0).state
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'graph-artifacts.sqlite3'}", create_schema=True,
    )
    proposed = {
        "summary": "Built a small application.",
        "disposition": "complete",
        "satisfied_conditions": ["verified"],
        "evidence_ids": [],
        "artifacts": [{
            "label": "application-source",
            "media_type": "application/vnd.agent-os.source-bundle+json",
            "files": {"index.html": "<h1>Built</h1>"},
        }],
        "output": {"decision": "test"},
        "recipient_ids": [],
        "correlation_id": None,
        "reason": None,
        "retryable": False,
    }
    try:
        runtime = PydanticGraphNodeRuntime(
            TestModel(custom_output_args=proposed),
            artifact_store=artifacts,
        )
        result = runtime.execute_node(
            tenant_id="tenant-a", run_id="run-evidence", definition=definition,
            state=running, action=action, idempotency_key=action.action_id,
        )
        artifact_id = result["evidence_ids"][0]
        assert artifacts.describe("tenant-a", artifact_id)["media_type"].endswith(
            "source-bundle+json"
        )
        assert result["artifacts"][0]["artifact_id"] == artifact_id
        assert result["output"]["artifact_ids"] == {"application-source": artifact_id}

        hallucinated = {**proposed, "evidence_ids": ["invented"], "artifacts": []}
        runtime = PydanticGraphNodeRuntime(
            TestModel(custom_output_args=hallucinated),
            artifact_store=artifacts,
        )
        with pytest.raises(FatalCommandError, match="unknown or cross-tenant"):
            runtime.execute_node(
                tenant_id="tenant-a", run_id="run-evidence", definition=definition,
                state=running, action=action, idempotency_key="different-action",
            )
    finally:
        artifacts.close()
