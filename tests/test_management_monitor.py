from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

from agent_os.application.management_monitor import DurableManagementMonitor
from agent_os.application.ports import ManagementWatchLease
from agent_os.domain.notifications import NotificationCategory
from agent_os.domain.mission_model import MissionSpec
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import NodeToken, TokenStatus, WorkflowRunState, WorkflowRunStatus


NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


def definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        "mission", "tenant-a", "Mission", 1, "work",
        (
            WorkflowNode("work", NodeKind.AGENT, "Build carefully", "engineer"),
            WorkflowNode("done", NodeKind.TERMINAL, "Done"),
        ),
        (WorkflowEdge("work", "done"),),
        "agent:mission-architect",
    )


class Watches:
    def __init__(self) -> None:
        self.available = True
        self.last_fingerprint = None
        self.consecutive = 0
        self.notified_level = 0
        self.last_result = None
        self.retired = False
        self.heartbeats = 0

    def claim_management_watch(self, tenant_id, *, worker_id, lease_seconds=60):
        if tenant_id != "tenant-a" or not self.available or self.retired:
            return None
        self.available = False
        return ManagementWatchLease(
            tenant_id, "graph-run", worker_id, 1, (NOW + timedelta(seconds=lease_seconds)).isoformat(),
            self.last_fingerprint, self.consecutive, self.notified_level, self.last_result,
        )

    def complete_management_watch(
        self, tenant_id, run_id, *, worker_id, next_check_seconds, signal_fingerprint,
        consecutive_signal_checks, notified_level, result, retire=False,
    ):
        assert tenant_id == "tenant-a" and run_id == "graph-run" and worker_id == "manager"
        self.last_fingerprint = signal_fingerprint
        self.consecutive = consecutive_signal_checks
        self.notified_level = notified_level
        self.last_result = dict(result)
        self.retired = retire
        return True

    def retry_management_watch(self, *args, **kwargs):
        raise AssertionError("healthy monitor test must not retry")

    def heartbeat_management_watch(
        self, tenant_id, run_id, *, worker_id, lease_seconds=60,
    ):
        assert tenant_id == "tenant-a" and run_id == "graph-run"
        assert worker_id == "manager" and lease_seconds >= 3
        self.heartbeats += 1
        return True

    def make_due(self):
        self.available = True


class Graph:
    def __init__(self) -> None:
        self.state = WorkflowRunState(
            "graph-run", "tenant-a", "mission", 1, 1, WorkflowRunStatus.ACTIVE,
            (NodeToken("work-token", "work", TokenStatus.RUNNING, 1, attempt=1),),
            context={"lifecycle_run_id": "lifecycle-run"},
        )
        self.action_created_at = NOW - timedelta(minutes=20)

    def get_graph_run(self, tenant_id, run_id):
        assert tenant_id == "tenant-a" and run_id == "graph-run"
        return self.state

    def get_workflow_definition(self, tenant_id, workflow_id, version):
        assert tenant_id == "tenant-a" and workflow_id == "mission" and version == 1
        return definition()

    def inspect_graph_run(self, tenant_id, run_id, *, action_limit=1000):
        assert action_limit == 1000
        return {
            "updated_at": NOW.isoformat(),
            "actions": [{
                "state_version": 1,
                "action": {"token_id": "work-token", "node_id": "work"},
                "status": "executing",
                "attempts": 1,
                "created_at": self.action_created_at.isoformat(),
                "available_at": self.action_created_at.isoformat(),
                "lease_expires_at": (NOW + timedelta(minutes=1)).isoformat(),
            }],
        }


class Notifications:
    def __init__(self) -> None:
        self.items = []

    def publish_notification(self, notification):
        if any(item.notification_id == notification.notification_id for item in self.items):
            return False
        self.items.append(notification)
        return True

    def list_notifications(self, tenant_id, *, run_id=None, recipient_id=None, limit=100):
        values = [item.to_dict() for item in reversed(self.items) if item.tenant_id == tenant_id]
        if run_id is not None:
            values = [item for item in values if item["run_id"] == run_id]
        if recipient_id is not None:
            values = [item for item in values if recipient_id in item["recipient_ids"]]
        return tuple(values[:limit])


class Missions:
    def __init__(self, *, mode="balanced", daily_interrupt_limit=8):
        self.spec = MissionSpec(
            mission_id="lifecycle-run",
            tenant_id="tenant-a",
            objective="Complete the mission",
            principal_id="human:ceo",
            accountable_owner_id="human:ceo",
            budget_limit_cents=1_000,
            success_measures=("verified outcome",),
            human_involvement_mode=mode,
            daily_interrupt_limit=daily_interrupt_limit,
        )

    def get_mission(self, tenant_id, mission_id):
        if (tenant_id, mission_id) == ("tenant-a", "lifecycle-run"):
            return self.spec
        return None


def test_monitor_alerts_manager_escalates_persistent_delay_and_reports_recovery():
    watches = Watches()
    graph = Graph()
    notifications = Notifications()
    monitor = DurableManagementMonitor(
        watches=watches, graph=graph, inspector=graph, notifications=notifications,
        worker_id="manager", check_interval_seconds=30, slow_after_seconds=300,
        escalation_checks=2, clock=lambda: NOW,
    )

    first = monitor.run_one("tenant-a")
    assert first.status.value == "succeeded"
    assert notifications.items[0].category is NotificationCategory.MANAGEMENT_ATTENTION
    assert notifications.items[0].recipient_ids == ("agent:mission-manager",)
    assert notifications.items[0].run_id == "lifecycle-run"
    assert watches.consecutive == 1 and watches.notified_level == 1

    watches.make_due()
    second = monitor.run_one("tenant-a")
    assert second.status.value == "succeeded"
    assert notifications.items[1].recipient_ids == ("agent:mission-manager", "human:ceo")
    assert notifications.items[1].payload["consecutive_checks"] == 2
    assert watches.notified_level == 2

    graph.action_created_at = NOW
    watches.make_due()
    recovered = monitor.run_one("tenant-a")
    assert recovered.status.value == "succeeded"
    assert notifications.items[2].category is NotificationCategory.WORK_RECOVERED
    assert notifications.items[2].recipient_ids == ("agent:mission-manager", "human:ceo")
    assert watches.last_fingerprint is None
    assert watches.consecutive == 0


def test_monitor_batches_human_alert_after_mission_attention_budget_is_exhausted():
    watches = Watches()
    graph = Graph()
    notifications = Notifications()
    monitor = DurableManagementMonitor(
        watches=watches, graph=graph, inspector=graph, notifications=notifications,
        mission_control=Missions(daily_interrupt_limit=0), worker_id="manager",
        check_interval_seconds=30, slow_after_seconds=300,
        escalation_checks=2, clock=lambda: NOW,
    )

    monitor.run_one("tenant-a")
    watches.make_due()
    monitor.run_one("tenant-a")

    executive = notifications.items[-1]
    assert "human:ceo" in executive.recipient_ids
    assert executive.payload["attention_disposition"] == "batch"
    assert "decision digest" in executive.payload["attention_reason"]


def test_monitor_retires_terminal_run_without_manufacturing_recovery_notice():
    watches = Watches()
    graph = Graph()
    graph.state = WorkflowRunState(
        "graph-run", "tenant-a", "mission", 1, 2, WorkflowRunStatus.SUCCEEDED,
        (NodeToken(
            "work-token", "work", TokenStatus.SUCCEEDED, 1, attempt=1,
            evidence_ids=("proof",),
        ),),
        context={"lifecycle_run_id": "lifecycle-run"},
    )
    watches.last_fingerprint = "a" * 64
    watches.consecutive = 2
    watches.notified_level = 1
    notifications = Notifications()
    monitor = DurableManagementMonitor(
        watches=watches, graph=graph, inspector=graph, notifications=notifications,
        worker_id="manager", clock=lambda: NOW,
    )

    result = monitor.run_one("tenant-a")

    assert result.status.value == "succeeded"
    assert watches.retired is True
    assert notifications.items == []


class ManagerRuntime:
    def __init__(self) -> None:
        self.calls = []

    def run_agent(self, **values):
        self.calls.append(values)
        time.sleep(1.1)
        return {"output": {
            "summary": "The lease is healthy; inspect the slow provider call before intervening.",
            "disposition": "continue",
            "progress_percent": 45,
            "observations": ["The action still has a renewable owner."],
            "risks": ["A provider response may be delayed."],
            "next_actions": ["Inspect provider-call timing and retain the current worker."],
            "messages": [],
            "proposed_work": [],
            "hiring_requests": [],
            "decisions": [{
                "intent": "Choose whether to restart",
                "considered_options": ["wait", "restart"],
                "chosen_option": "wait",
                "rationale": "The current renewable lease proves ownership.",
                "evidence_ids": [],
                "confidence": 0.9,
                "reversible": True,
                "needs_human_approval": False,
            }],
        }}


def test_monitor_runs_one_metered_lease_renewed_manager_diagnosis_per_signal():
    watches = Watches()
    graph = Graph()
    notifications = Notifications()
    runtime = ManagerRuntime()
    monitor = DurableManagementMonitor(
        watches=watches, graph=graph, inspector=graph, notifications=notifications,
        worker_id="manager", lease_seconds=3, check_interval_seconds=30,
        slow_after_seconds=300, escalation_checks=2, manager_runtime=runtime,
        manager_turn_budget_cents=7, clock=lambda: NOW,
    )

    first = monitor.run_one("tenant-a")

    assert first.status.value == "succeeded"
    assert watches.heartbeats >= 1
    assert len(runtime.calls) == 1
    assert runtime.calls[0]["role"] == "mission-manager"
    assert runtime.calls[0]["usage_category"] == "management_review"
    assert runtime.calls[0]["budget_cents"] == 7
    assert runtime.calls[0]["context"]["management_signals"][0]["signal"] == "slow_but_owned"
    assert len(notifications.items) == 2
    diagnosis = notifications.items[1]
    assert diagnosis.subject == "Mission manager diagnosis"
    assert diagnosis.payload["manager_review"]["decisions"][0]["chosen_option"] == "wait"
    assert watches.last_result["manager_review"]["summary"].startswith("The lease is healthy")

    watches.make_due()
    second = monitor.run_one("tenant-a")

    assert second.status.value == "succeeded"
    assert len(runtime.calls) == 1
    assert len(notifications.items) == 3
    assert notifications.items[-1].recipient_ids == ("agent:mission-manager", "human:ceo")
    assert notifications.items[-1].payload["manager_review"]["decisions"][0]["chosen_option"] == "wait"


def test_manager_provider_failure_never_blocks_health_escalation():
    watches = Watches()
    graph = Graph()
    notifications = Notifications()

    class UnavailableRuntime:
        def __init__(self):
            self.calls = 0

        def run_agent(self, **_values):
            self.calls += 1
            raise ConnectionError("provider unavailable")

    runtime = UnavailableRuntime()
    monitor = DurableManagementMonitor(
        watches=watches, graph=graph, inspector=graph, notifications=notifications,
        worker_id="manager", check_interval_seconds=30, slow_after_seconds=300,
        escalation_checks=2, manager_runtime=runtime, clock=lambda: NOW,
    )

    first = monitor.run_one("tenant-a")
    assert first.status.value == "succeeded"
    assert watches.last_result["manager_review_error"]["type"] == "ConnectionError"
    assert notifications.items[0].recipient_ids == ("agent:mission-manager",)

    watches.make_due()
    second = monitor.run_one("tenant-a")
    assert second.status.value == "succeeded"
    assert runtime.calls == 2
    assert notifications.items[-1].recipient_ids == ("agent:mission-manager", "human:ceo")
    assert watches.consecutive == 2
