from __future__ import annotations

from pathlib import Path

import pytest

from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import WorkflowEvent, WorkflowEventKind
from agent_os.infrastructure.sql_workflow_graph import SQLGraphWorkflowEngine


def graph(name: str = "Adaptive delivery") -> WorkflowDefinition:
    return WorkflowDefinition(
        "delivery", "tenant-a", name, 1, "triage",
        (
            WorkflowNode("triage", NodeKind.AGENT, "Triage", "engineer"),
            WorkflowNode("human", NodeKind.HUMAN, "Clarify"),
            WorkflowNode("done", NodeKind.TERMINAL, "Done"),
        ),
        (
            WorkflowEdge("triage", "human", "needs_human"),
            WorkflowEdge("triage", "done", "clear"),
            WorkflowEdge("human", "triage", "answered"),
        ),
        "agent:architect",
    )


@pytest.fixture
def engine(tmp_path: Path):
    runtime = SQLGraphWorkflowEngine(
        f"sqlite:///{tmp_path / 'graphs.sqlite3'}", create_schema=True,
    )
    try:
        yield runtime
    finally:
        runtime.close()


def test_definition_and_graph_run_are_versioned_idempotent_and_tenant_bound(engine):
    definition = graph()
    assert engine.register_workflow(definition) is True
    assert engine.register_workflow(definition) is False
    with pytest.raises(ValueError, match="different content"):
        engine.register_workflow(graph("Conflicting content"))

    started = engine.start_graph_run(
        "tenant-a", "delivery", 1,
        run_id="graph-run-1", request_id="start-1", context={"ticket": "ABC-1"},
    )
    repeated = engine.start_graph_run(
        "tenant-a", "delivery", 1,
        run_id="graph-run-1", request_id="start-1", context={"ticket": "ABC-1"},
    )
    assert started.state.version == 0
    assert started.actions[0].node_id == "triage"
    assert repeated.duplicate is True
    assert repeated.actions == started.actions
    assert engine.get_graph_run("tenant-b", "graph-run-1") is None


def test_graph_events_commit_state_and_actions_together_with_full_history_dedup(engine):
    engine.register_workflow(graph())
    started = engine.start_graph_run(
        "tenant-a", "delivery", 1, run_id="run", request_id="start",
    )
    token_id = started.state.ready()[0].token_id
    began = WorkflowEvent("begin", WorkflowEventKind.NODE_BEGAN, 0, {"token_id": token_id})
    running = engine.submit_graph_event("tenant-a", "run", began)
    repeated = engine.submit_graph_event("tenant-a", "run", began)
    assert running.state.version == 1
    assert repeated.duplicate is True

    completed = engine.submit_graph_event("tenant-a", "run", WorkflowEvent(
        "triaged",
        WorkflowEventKind.NODE_COMPLETED,
        1,
        {
            "token_id": token_id,
            "satisfied_conditions": ["needs_human"],
            "evidence_ids": ["triage-report"],
            "output": {"question": "What is the acceptance criterion?"},
        },
    ))
    assert completed.state.version == 2
    assert completed.state.ready()[0].node_id == "human"
    assert completed.actions[0].kind.value == "execute_node"

    with pytest.raises(ValueError, match="different content"):
        engine.submit_graph_event("tenant-a", "run", WorkflowEvent(
            "triaged", WorkflowEventKind.NODE_COMPLETED, 1,
            {"token_id": token_id, "satisfied_conditions": ["clear"], "evidence_ids": ["x"]},
        ))
    with pytest.raises(Exception, match="stale workflow version"):
        engine.submit_graph_event("tenant-a", "run", WorkflowEvent(
            "stale", WorkflowEventKind.NODE_BEGAN, 0,
            {"token_id": completed.state.ready()[0].token_id},
        ))
