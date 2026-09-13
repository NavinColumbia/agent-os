from __future__ import annotations

import pytest

from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import (
    TokenStatus,
    WorkflowActionKind,
    WorkflowRunStatus,
    WorkflowTransitionRejected,
    begin_node,
    cancel_workflow,
    complete_node,
    resume_wait,
    start_workflow,
    wait_node,
    wait_for_child,
)


def definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        "customer-jira", "tenant-1", "Adaptive Jira delivery", 1, "triage",
        (
            WorkflowNode("triage", NodeKind.AGENT, "Understand ambiguity", "engineer"),
            WorkflowNode("clarify", NodeKind.HUMAN, "Ask the correct product owner"),
            WorkflowNode("build", NodeKind.AGENT, "Implement and verify", "engineer"),
            WorkflowNode("publish", NodeKind.TOOL, "Update Jira"),
            WorkflowNode("done", NodeKind.TERMINAL, "Accepted outcome"),
        ),
        (
            WorkflowEdge("triage", "clarify", "needs_human", 10),
            WorkflowEdge("triage", "build", "requirements_clear", 5),
            WorkflowEdge("clarify", "triage", "answer_received"),
            WorkflowEdge("build", "triage", "repair_required", 10),
            WorkflowEdge("build", "publish", "verified", 5),
            WorkflowEdge("publish", "done", "vendor_confirmed"),
        ),
        "architect",
    )


def execute(state, definition, condition, evidence):
    token = state.ready()[0]
    state = begin_node(state, token.token_id, expected_version=state.version).state
    return complete_node(
        definition, state, token.token_id, expected_version=state.version,
        satisfied_conditions=frozenset({condition}), evidence_ids=(evidence,),
    )


def test_agent_designed_graph_loops_waits_for_a_correlated_human_and_finishes():
    graph = definition()
    started = start_workflow(graph, run_id="run-1", context={"jira_ticket": "ABC-123"})
    state = execute(started.state, graph, "needs_human", "triage-evidence").state
    clarify = state.ready()[0]
    state = begin_node(state, clarify.token_id, expected_version=state.version).state
    waiting = wait_node(
        state, clarify.token_id, expected_version=state.version,
        correlation_id="question-product-owner", reason="Acceptance criteria are ambiguous",
        recipient_ids=("human:product-owner",),
    )
    assert waiting.state.status is WorkflowRunStatus.WAITING
    assert waiting.actions[0].kind is WorkflowActionKind.NOTIFY_HUMAN

    resumed = resume_wait(
        waiting.state, expected_version=waiting.state.version,
        correlation_id="question-product-owner", response={"criteria": "export must be CSV"},
    )
    clarify = resumed.state.ready()[0]
    state = begin_node(resumed.state, clarify.token_id, expected_version=resumed.state.version).state
    state = complete_node(
        graph, state, clarify.token_id, expected_version=state.version,
        satisfied_conditions=frozenset({"answer_received"}), evidence_ids=("human-answer",),
    ).state
    assert state.ready()[0].node_id == "triage"
    assert state.ready()[0].iteration == 2

    state = execute(state, graph, "requirements_clear", "triage-2").state
    state = execute(state, graph, "repair_required", "failed-test").state
    assert state.ready()[0].node_id == "triage"  # intelligent repair loop, not a terminal timeout
    state = execute(state, graph, "requirements_clear", "triage-3").state
    state = execute(state, graph, "verified", "tests-pass").state
    state = execute(state, graph, "vendor_confirmed", "jira-update").state
    finished = execute(state, graph, "unused", "acceptance-proof")
    assert finished.state.status is WorkflowRunStatus.SUCCEEDED
    assert finished.actions[-1].kind is WorkflowActionKind.RUN_SUCCEEDED


def test_multiple_satisfied_edges_fan_out_into_parallel_work():
    graph = WorkflowDefinition(
        "parallel", "tenant-1", "Parallel", 1, "manager",
        (
            WorkflowNode("manager", NodeKind.AGENT, "Decompose", "manager"),
            WorkflowNode("research", NodeKind.AGENT, "Research", "researcher"),
            WorkflowNode("security", NodeKind.AGENT, "Threat model", "security"),
            WorkflowNode("done", NodeKind.TERMINAL, "Done"),
        ),
        (
            WorkflowEdge("manager", "research", "launch_research"),
            WorkflowEdge("manager", "security", "launch_security"),
            WorkflowEdge("research", "done", "always"),
            WorkflowEdge("security", "done", "always"),
        ),
        "architect",
    )
    state = start_workflow(graph, run_id="run-parallel").state
    manager = state.ready()[0]
    state = begin_node(state, manager.token_id, expected_version=0).state
    mutation = complete_node(
        graph, state, manager.token_id, expected_version=1,
        satisfied_conditions=frozenset({"launch_research", "launch_security"}),
        evidence_ids=("plan",),
    )
    assert {token.node_id for token in mutation.state.ready()} == {"research", "security"}
    assert len(mutation.actions) == 2


def test_completion_without_evidence_or_a_valid_path_fails_closed():
    graph = definition()
    state = start_workflow(graph, run_id="run-invalid").state
    token = state.ready()[0]
    state = begin_node(state, token.token_id, expected_version=0).state
    with pytest.raises(WorkflowTransitionRejected, match="evidence"):
        complete_node(
            graph, state, token.token_id, expected_version=1,
            satisfied_conditions=frozenset({"requirements_clear"}), evidence_ids=(),
        )
    failed = complete_node(
        graph, state, token.token_id, expected_version=1,
        satisfied_conditions=frozenset({"unknown"}), evidence_ids=("analysis",),
    )
    assert failed.state.status is WorkflowRunStatus.FAILED
    assert failed.state.tokens[0].status is TokenStatus.SUCCEEDED


def test_definition_state_tokens_and_actions_round_trip_for_durable_adapters():
    graph = definition()
    started = start_workflow(graph, run_id="roundtrip", context={"ticket": "A-1"})
    assert WorkflowDefinition.from_dict(graph.to_dict()) == graph
    assert type(started.state).from_dict(started.state.to_dict()) == started.state
    assert type(started.actions[0]).from_dict(started.actions[0].to_dict()) == started.actions[0]


def test_parent_cancellation_cascades_to_a_durably_waiting_child_without_human_noise():
    graph = WorkflowDefinition(
        "recursive", "tenant-1", "Recursive", 1, "team",
        (
            WorkflowNode("team", NodeKind.SUBWORKFLOW, "Delegate team", "manager"),
            WorkflowNode("done", NodeKind.TERMINAL, "Done"),
        ),
        (WorkflowEdge("team", "done", "child_succeeded"),),
        "architect",
    )
    state = start_workflow(graph, run_id="recursive-parent").state
    token = state.ready()[0]
    state = begin_node(state, token.token_id, expected_version=0).state
    waiting = wait_for_child(
        state, token.token_id, expected_version=1, child_run_id="recursive-child",
        child_program={"format": "agent-os.mission-program.v1", "revision": 1},
        program_artifact_id="program-artifact", actor_role="manager",
    )

    assert waiting.state.status is WorkflowRunStatus.WAITING
    assert all(action.kind is not WorkflowActionKind.NOTIFY_HUMAN for action in waiting.actions)
    cancelled = cancel_workflow(
        waiting.state, expected_version=waiting.state.version, reason="CEO cancelled parent",
    )
    assert [action.kind for action in cancelled.actions] == [
        WorkflowActionKind.CANCEL_CHILD,
        WorkflowActionKind.RUN_CANCELLED,
    ]
    assert cancelled.actions[0].payload["child_run_id"] == "recursive-child"
