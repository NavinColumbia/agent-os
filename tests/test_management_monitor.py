from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent_os.application.management_monitor import DurableManagementMonitor
from agent_os.application.ports import ManagementWatchLease
from agent_os.domain.notifications import NotificationCategory
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
