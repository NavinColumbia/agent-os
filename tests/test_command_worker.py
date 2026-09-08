from __future__ import annotations

from agent_os.application.command_worker import (
    CommandRunStatus,
    DurableCommandWorker,
    FatalCommandError,
    RetryPolicy,
    RetryableCommandError,
)
from agent_os.application.ports import CommandLease, WorkflowReceipt


class FakeOutbox:
    def __init__(self) -> None:
        self.available = True
        self.attempt = 0
        self.owner = None
        self.state = "pending"
        self.result = None
        self.error = None

    def claim_command(self, organization_id, *, worker_id, lease_seconds=60):
        if not self.available:
            return None
        self.available = False
        self.attempt += 1
        self.owner = worker_id
        self.state = "executing"
        return CommandLease(
            {"command_id": "cmd-1", "organization_id": organization_id, "run_id": "run-1"},
            worker_id,
            self.attempt,
            "later",
        )

    def heartbeat_command(self, organization_id, command_id, *, worker_id, lease_seconds=60):
        return self.owner == worker_id and self.state == "executing"

    def complete_command(self, organization_id, command_id, *, worker_id, result):
        if self.owner != worker_id:
            return False
        self.state, self.result = "succeeded", result
        return True

    def retry_command(self, organization_id, command_id, *, worker_id, error, delay_seconds):
        if self.owner != worker_id:
            return False
        self.state, self.error, self.available = "pending", error, delay_seconds == 0
        return True

    def fail_command(self, organization_id, command_id, *, worker_id, error):
        if self.owner != worker_id:
            return False
        self.state, self.error = "failed", error
        return True


class SequenceExecutor:
    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)

    def execute(self, envelope):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_success_is_acknowledged_only_by_the_lease_owner():
    outbox = FakeOutbox()
    worker = DurableCommandWorker(
        outbox=outbox, executor=SequenceExecutor({"evidence": "artifact-1"}),
        worker_id="worker-1", lease_seconds=3,
    )
    report = worker.run_one("tenant-1")
    assert report.status is CommandRunStatus.SUCCEEDED
    assert outbox.result == {"evidence": "artifact-1"}


def test_provider_throttle_retries_from_durable_state_without_a_story_timeout():
    outbox = FakeOutbox()
    executor = SequenceExecutor(
        RetryableCommandError("provider throttled", retry_after_seconds=0),
        {"recovered": True},
    )
    worker = DurableCommandWorker(
        outbox=outbox, executor=executor, worker_id="worker-1", lease_seconds=3,
        retry_policy=RetryPolicy(base_delay_seconds=0, max_delay_seconds=10, max_attempts=None),
    )
    first = worker.run_one("tenant-1")
    second = worker.run_one("tenant-1")
    assert first.status is CommandRunStatus.RETRY_SCHEDULED
    assert second.status is CommandRunStatus.SUCCEEDED
    assert outbox.attempt == 2


class FakeWorkflowEngine:
    def __init__(self) -> None:
        self.events = []

    def submit_event(self, organization_id, run_id, event):
        self.events.append((organization_id, run_id, event))
        return WorkflowReceipt("workflow-followup")


def test_lifecycle_followup_is_durably_accepted_before_command_acknowledgement():
    outbox = FakeOutbox()
    workflow = FakeWorkflowEngine()
    waited = []
    executor = SequenceExecutor({
        "result": {"evidence": "report"},
        "lifecycle_event": {
            "event_id": "command-result-1",
            "kind": "research_completed",
            "expected_version": 1,
            "payload": {"report_id": "report-1"},
        },
    })
    worker = DurableCommandWorker(
        outbox=outbox,
        executor=executor,
        worker_id="worker-1",
        lease_seconds=3,
        workflow_engine=workflow,
        workflow_result_waiter=lambda workflow_id: waited.append(workflow_id) or {"ok": True},
    )

    report = worker.run_one("tenant-1")

    assert report.status is CommandRunStatus.SUCCEEDED
    assert workflow.events[0][0:2] == ("tenant-1", "run-1")
    assert workflow.events[0][2].kind.value == "research_completed"
    assert waited == ["workflow-followup"]
    assert outbox.state == "succeeded"


def test_permanent_invalid_action_fails_instead_of_looping_forever():
    outbox = FakeOutbox()
    worker = DurableCommandWorker(
        outbox=outbox, executor=SequenceExecutor(FatalCommandError("invalid recipient")),
        worker_id="worker-1", lease_seconds=3,
    )
    report = worker.run_one("tenant-1")
    assert report.status is CommandRunStatus.FAILED
    assert outbox.error["retryable"] is False
