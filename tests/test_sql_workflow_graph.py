from __future__ import annotations

from pathlib import Path
from dataclasses import replace

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


def test_workflow_revision_updates_indexed_version_and_executes_replacement(engine):
    original = graph()
    replacement = replace(
        original,
        version=2,
        supersedes_version=1,
        name="Adaptive delivery revision two",
    )
    engine.register_workflow(original)
    engine.register_workflow(replacement)
    started = engine.start_graph_run(
        "tenant-a", "delivery", 1, run_id="revised-run", request_id="revised-start",
    )

    revised = engine.submit_graph_event("tenant-a", "revised-run", WorkflowEvent(
        "revise",
        WorkflowEventKind.RUN_REVISED,
        started.state.version,
        {
            "replacement_workflow": replacement.to_dict(),
            "mission_program": {"revision": 2},
            "evidence_ids": ["revision-evidence"],
            "reason": "Repair the live delivery graph.",
        },
    ))
    assert revised.state.workflow_version == 2
    replacement_token = revised.state.ready()[0]

    running = engine.submit_graph_event("tenant-a", "revised-run", WorkflowEvent(
        "revised-begin",
        WorkflowEventKind.NODE_BEGAN,
        revised.state.version,
        {"token_id": replacement_token.token_id},
    ))
    completed = engine.submit_graph_event("tenant-a", "revised-run", WorkflowEvent(
        "revised-complete",
        WorkflowEventKind.NODE_COMPLETED,
        running.state.version,
        {
            "token_id": replacement_token.token_id,
            "satisfied_conditions": ["clear"],
            "evidence_ids": ["revised-node-evidence"],
        },
    ))

    assert completed.state.workflow_version == 2
    assert completed.state.ready()[0].node_id == "done"


def test_operator_can_retry_exact_failed_node_after_repair(engine):
    engine.register_workflow(graph())
    started = engine.start_graph_run(
        "tenant-a", "delivery", 1, run_id="recover-run", request_id="recover-start",
    )
    token_id = started.state.ready()[0].token_id
    running = engine.submit_graph_event("tenant-a", "recover-run", WorkflowEvent(
        "recover-begin", WorkflowEventKind.NODE_BEGAN, 0, {"token_id": token_id},
    ))
    failed = engine.submit_graph_event("tenant-a", "recover-run", WorkflowEvent(
        "recover-fail", WorkflowEventKind.NODE_FAILED, running.state.version,
        {"token_id": token_id, "reason": "context boundary", "retryable": False},
    ))
    assert failed.state.status.value == "failed"

    recovered = engine.submit_graph_event("tenant-a", "recover-run", WorkflowEvent(
        "recover-request", WorkflowEventKind.NODE_RETRY_REQUESTED, failed.state.version,
        {"token_id": token_id, "reason": "Context compaction was repaired."},
    ))

    assert recovered.state.status.value == "active"
    assert recovered.state.failure is None
    assert recovered.state.ready()[0].token_id == token_id
    assert recovered.actions[0].payload["operator_recovery"] is True


def test_graph_action_outbox_is_tenant_fenced_recoverable_and_lease_owned(engine):
    engine.register_workflow(graph())
    started = engine.start_graph_run(
        "tenant-a", "delivery", 1, run_id="leased-run", request_id="leased-start",
    )
    action_id = started.actions[0].action_id

    first = engine.claim_graph_action("tenant-a", worker_id="worker-a", lease_seconds=30)

    assert first is not None and first.attempt == 1
    assert first.envelope["action"]["action_id"] == action_id
    assert engine.claim_graph_action("tenant-a", worker_id="worker-b") is None
    assert engine.claim_graph_action("tenant-b", worker_id="worker-b") is None
    assert engine.complete_graph_action(
        "tenant-a", action_id, worker_id="worker-b", result={"forged": True},
    ) is False
    assert engine.heartbeat_graph_action(
        "tenant-a", action_id, worker_id="worker-a", lease_seconds=45,
    ) is True
    assert engine.retry_graph_action(
        "tenant-a", action_id, worker_id="worker-a",
        error={"type": "ConnectionError", "retryable": True}, delay_seconds=0,
    ) is True

    retry = engine.claim_graph_action("tenant-a", worker_id="worker-b")
    assert retry is not None and retry.attempt == 2
    assert engine.complete_graph_action(
        "tenant-a", action_id, worker_id="worker-b", result={"evidence_ids": ["proof"]},
    ) is True
    record = engine.get_graph_action_record("tenant-a", action_id)
    assert record is not None
    assert record["status"] == "succeeded"
    assert record["attempts"] == 2
    assert record["last_error"] is None

    inspection = engine.inspect_graph_run("tenant-a", "leased-run")
    assert inspection is not None
    assert inspection["run_id"] == "leased-run"
    assert inspection["actions"][0]["action_id"] == action_id
    assert inspection["actions"][0]["status"] == "succeeded"
    assert isinstance(inspection["created_at"], str)
    assert engine.inspect_graph_run("tenant-b", "leased-run") is None
    with pytest.raises(ValueError, match="between 1 and 5000"):
        engine.inspect_graph_run("tenant-a", "leased-run", action_limit=0)


def test_identical_customer_run_ids_do_not_collide_across_tenants(engine):
    tenant_a = graph()
    tenant_b = replace(tenant_a, tenant_id="tenant-b")
    engine.register_workflow(tenant_a)
    engine.register_workflow(tenant_b)

    run_a = engine.start_graph_run(
        "tenant-a", "delivery", 1, run_id="customer-chosen", request_id="start-a",
    )
    run_b = engine.start_graph_run(
        "tenant-b", "delivery", 1, run_id="customer-chosen", request_id="start-b",
    )

    # Deterministic action IDs may match; the durable identity is tenant + ID.
    assert run_a.actions[0].action_id == run_b.actions[0].action_id
    assert engine.claim_graph_action("tenant-a", worker_id="worker-a") is not None
    assert engine.claim_graph_action("tenant-b", worker_id="worker-b") is not None


def test_management_watch_is_scheduled_leased_and_owner_fenced(engine):
    engine.register_workflow(graph())
    engine.start_graph_run(
        "tenant-a", "delivery", 1, run_id="managed-run", request_id="managed-start",
    )

    watch = engine.claim_management_watch(
        "tenant-a", worker_id="manager-a", lease_seconds=30,
    )

    assert watch is not None
    assert watch.run_id == "managed-run"
    assert watch.attempt == 1
    assert engine.complete_management_watch(
        "tenant-a",
        "managed-run",
        worker_id="manager-b",
        next_check_seconds=30,
        signal_fingerprint=None,
        consecutive_signal_checks=0,
        notified_level=0,
        result={"health": "forged"},
    ) is False
    assert engine.complete_management_watch(
        "tenant-a",
        "managed-run",
        worker_id="manager-a",
        next_check_seconds=30,
        signal_fingerprint=None,
        consecutive_signal_checks=0,
        notified_level=0,
        result={"health": "healthy"},
    ) is True
    assert engine.claim_management_watch("tenant-a", worker_id="manager-b") is None
    assert engine.claim_management_watch("tenant-b", worker_id="manager-b") is None
