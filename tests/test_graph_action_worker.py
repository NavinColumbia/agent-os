from __future__ import annotations

from pathlib import Path

import pytest

from agent_os.application.command_worker import CommandRunStatus, RetryPolicy
from agent_os.application.graph_action_worker import DurableGraphActionWorker
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.infrastructure.graph_action_executor import DurableGraphActionExecutor
from agent_os.infrastructure.sql_workflow_graph import SQLGraphWorkflowEngine


def graph() -> WorkflowDefinition:
    return WorkflowDefinition(
        "delivery",
        "tenant-a",
        "Adaptive delivery",
        1,
        "triage",
        (
            WorkflowNode("triage", NodeKind.AGENT, "Triage the work", "engineer"),
            WorkflowNode("done", NodeKind.TERMINAL, "Accept the evidence"),
        ),
        (WorkflowEdge("triage", "done", "ready"),),
        "agent:architect",
    )


@pytest.fixture
def engine(tmp_path: Path):
    value = SQLGraphWorkflowEngine(f"sqlite:///{tmp_path / 'graph-actions.sqlite3'}", create_schema=True)
    value.register_workflow(graph())
    try:
        yield value
    finally:
        value.close()


class SequenceNodeRuntime:
    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = []

    def execute_node(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def completion():
    return {
        "disposition": "complete",
        "satisfied_conditions": ["ready"],
        "evidence_ids": ["triage-evidence"],
        "output": {"summary": "ready to proceed"},
    }


def start(engine, run_id="run-1"):
    return engine.start_graph_run(
        "tenant-a", "delivery", 1, run_id=run_id, request_id=f"start-{run_id}",
    )


def test_graph_action_executor_advances_token_and_replay_does_not_repeat_node_runtime(engine):
    started = start(engine)
    lease = engine.claim_graph_action("tenant-a", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    runtime = SequenceNodeRuntime(completion())
    executor = DurableGraphActionExecutor(engine=engine, node_runtime=runtime)

    first = executor.execute(lease.envelope)
    replay = executor.execute(lease.envelope)

    assert first["token_status"] == "succeeded"
    assert replay["replayed"] is True
    assert len(runtime.calls) == 1
    assert runtime.calls[0]["idempotency_key"] == started.actions[0].action_id
    state = engine.get_graph_run("tenant-a", "run-1")
    assert state is not None
    assert state.ready()[0].node_id == "done"


def test_graph_action_worker_retries_transient_runtime_failure_from_running_token(engine):
    started = start(engine, "run-retry")
    runtime = SequenceNodeRuntime(ConnectionError("provider unavailable"), completion())
    executor = DurableGraphActionExecutor(engine=engine, node_runtime=runtime)
    worker = DurableGraphActionWorker(
        outbox=engine,
        executor=executor,
        worker_id="worker-a",
        lease_seconds=3,
        retry_policy=RetryPolicy(base_delay_seconds=0, max_delay_seconds=1),
    )

    first = worker.run_one("tenant-a")
    second = worker.run_one("tenant-a")

    assert first.status is CommandRunStatus.RETRY_SCHEDULED
    assert second.status is CommandRunStatus.SUCCEEDED
    assert first.action_id == started.actions[0].action_id
    assert second.action_id == started.actions[0].action_id
    assert len(runtime.calls) == 2
    assert {call["idempotency_key"] for call in runtime.calls} == {started.actions[0].action_id}
    record = engine.get_graph_action_record("tenant-a", started.actions[0].action_id)
    assert record is not None and record["status"] == "succeeded" and record["attempts"] == 2


def test_graph_effect_without_explicit_handler_fails_closed_and_is_not_acknowledged(engine):
    start(engine, "run-effect")
    runtime = SequenceNodeRuntime(completion(), completion())
    executor = DurableGraphActionExecutor(engine=engine, node_runtime=runtime)
    worker = DurableGraphActionWorker(
        outbox=engine,
        executor=executor,
        worker_id="worker-a",
        lease_seconds=3,
    )
    assert worker.run_one("tenant-a").status is CommandRunStatus.SUCCEEDED
    assert worker.run_one("tenant-a").status is CommandRunStatus.SUCCEEDED

    publication = worker.run_one("tenant-a")

    assert publication.status is CommandRunStatus.FAILED
    assert publication.error_type == "FatalCommandError"
    record = engine.get_graph_action_record("tenant-a", publication.action_id)
    assert record is not None
    assert record["status"] == "failed"
    assert "not acknowledged as delivered" in record["last_error"]["message"]
