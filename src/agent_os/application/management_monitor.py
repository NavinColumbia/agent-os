"""Durable proactive management checks for adaptive graph work."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable, Mapping

from agent_os.application.command_worker import CommandRunReport, CommandRunStatus
from agent_os.application.mission_control import project_mission_control
from agent_os.application.ports import (
    GraphRunInspector,
    GraphWorkflowEngine,
    ManagementWatchLease,
    ManagementWatchStore,
    NotificationStore,
)
from agent_os.domain.notifications import Notification, NotificationCategory
from agent_os.domain.workflow_runtime import WorkflowRunStatus


_ACTIONABLE = {
    "slow_but_owned",
    "recovering",
    "dispatch_delayed",
    "attention_required",
}
_TERMINAL = {
    WorkflowRunStatus.SUCCEEDED,
    WorkflowRunStatus.FAILED,
    WorkflowRunStatus.CANCELLED,
}


def _fingerprint(signals: list[Mapping[str, Any]]) -> str | None:
    if not signals:
        return None
    material = [{
        "signal": item.get("signal"),
        "severity": item.get("severity"),
        "work_id": item.get("work_id"),
        "owner_id": item.get("owner_id"),
        "recipient_id": item.get("recipient_id"),
        "reason": item.get("reason"),
    } for item in signals]
    encoded = json.dumps(material, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _notification_id(source_id: str, category: NotificationCategory) -> str:
    material = f"agent-os:management-notification:v1:{source_id}:{category.value}"
    return "notification-" + hashlib.sha256(material.encode()).hexdigest()


class DurableManagementMonitor:
    """Inspect one due run, persist its next check, and notify without spam."""

    def __init__(
        self,
        *,
        watches: ManagementWatchStore,
        graph: GraphWorkflowEngine,
        inspector: GraphRunInspector,
        notifications: NotificationStore,
        worker_id: str,
        lease_seconds: int = 60,
        check_interval_seconds: int = 30,
        slow_after_seconds: int = 300,
        escalation_checks: int = 3,
        retry_delay_seconds: int = 10,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not worker_id.strip() or lease_seconds < 3:
            raise ValueError("worker_id and a lease of at least three seconds are required")
        if min(check_interval_seconds, slow_after_seconds, escalation_checks, retry_delay_seconds) < 1:
            raise ValueError("management monitor intervals and escalation checks must be positive")
        self._watches = watches
        self._graph = graph
        self._inspector = inspector
        self._notifications = notifications
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._check_interval_seconds = check_interval_seconds
        self._slow_after_seconds = slow_after_seconds
        self._escalation_checks = escalation_checks
        self._retry_delay_seconds = retry_delay_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _publish(
        self,
        lease: ManagementWatchLease,
        *,
        category: NotificationCategory,
        source_id: str,
        notification_run_id: str,
        recipients: tuple[str, ...],
        subject: str,
        body: str,
        payload: Mapping[str, Any],
    ) -> bool:
        notification = Notification(
            notification_id=_notification_id(source_id, category),
            tenant_id=lease.tenant_id,
            run_id=notification_run_id,
            category=category,
            recipient_ids=tuple(dict.fromkeys(recipients)),
            subject=subject,
            body=body,
            source_id=source_id,
            created_at=self._clock().isoformat(),
            payload=dict(payload),
        )
        return self._notifications.publish_notification(notification)

    @staticmethod
    def _recipients(signals: list[Mapping[str, Any]], level: int) -> tuple[str, ...]:
        recipients = [
            str(item["recipient_id"])
            for item in signals
            if isinstance(item.get("recipient_id"), str) and item["recipient_id"]
        ]
        if any(item.get("severity") == "critical" or item.get("signal") == "recovering"
               for item in signals):
            recipients.append("operator:on-call")
        if level >= 2:
            recipients.append("human:ceo")
        return tuple(dict.fromkeys(recipients or ["agent:mission-manager"]))

    def _complete(
        self,
        lease: ManagementWatchLease,
        *,
        fingerprint: str | None,
        consecutive: int,
        notified_level: int,
        result: Mapping[str, Any],
        retire: bool,
    ) -> CommandRunReport:
        changed = self._watches.complete_management_watch(
            lease.tenant_id,
            lease.run_id,
            worker_id=self._worker_id,
            next_check_seconds=self._check_interval_seconds,
            signal_fingerprint=fingerprint,
            consecutive_signal_checks=consecutive,
            notified_level=notified_level,
            result=result,
            retire=retire,
        )
        return CommandRunReport(
            CommandRunStatus.SUCCEEDED if changed else CommandRunStatus.LEASE_LOST,
            f"management:{lease.run_id}",
            lease.attempt,
        )

    def run_one(self, tenant_id: str) -> CommandRunReport:
        lease = self._watches.claim_management_watch(
            tenant_id,
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if lease is None:
            return CommandRunReport(CommandRunStatus.IDLE)
        try:
            state = self._graph.get_graph_run(tenant_id, lease.run_id)
            if state is None:
                # A cascading delete can race a claimed watch. There is no work
                # left to diagnose and no row left whose lease can be released.
                return CommandRunReport(
                    CommandRunStatus.LEASE_LOST,
                    f"management:{lease.run_id}",
                    lease.attempt,
                )
            definition = self._graph.get_workflow_definition(
                tenant_id, state.workflow_id, state.workflow_version,
            )
            observation = self._inspector.inspect_graph_run(tenant_id, lease.run_id)
            if definition is None or observation is None:
                raise LookupError("management watch cannot read its workflow definition/observation")
            lifecycle_run_id = str(state.context.get("lifecycle_run_id") or lease.run_id)
            projection = project_mission_control(
                lifecycle_run_id=lifecycle_run_id,
                planning_state=state,
                execution_state=None,
                definition=definition,
                observation=observation,
                now=self._clock(),
                slow_after_seconds=self._slow_after_seconds,
            )
            signals = [
                dict(item) for item in projection["management_signals"]
                if item.get("signal") in _ACTIONABLE
            ]
            fingerprint = _fingerprint(signals)
            same = fingerprint is not None and fingerprint == lease.last_signal_fingerprint
            consecutive = lease.consecutive_signal_checks + 1 if same else (1 if fingerprint else 0)
            notified_level = lease.notified_level if same else 0
            level = 2 if consecutive >= self._escalation_checks else 1
            recipients: tuple[str, ...] = ()
            created = False
            if fingerprint is not None and level > notified_level:
                recipients = self._recipients(signals, level)
                source_id = f"management:{lease.run_id}:{fingerprint}:level:{level}"
                created = self._publish(
                    lease,
                    category=NotificationCategory.MANAGEMENT_ATTENTION,
                    source_id=source_id,
                    notification_run_id=lifecycle_run_id,
                    recipients=recipients,
                    subject=(
                        "Mission health needs executive attention"
                        if level >= 2 else "Mission manager review required"
                    ),
                    body=(
                        f"{len(signals)} work item(s) need diagnosis. "
                        "Healthy long-running work remains active; no wall-clock timeout was applied."
                    ),
                    payload={
                        "graph_run_id": lease.run_id,
                        "lifecycle_run_id": lifecycle_run_id,
                        "state_version": state.version,
                        "level": level,
                        "consecutive_checks": consecutive,
                        "signals": signals,
                    },
                )
                notified_level = level
            elif fingerprint is None and lease.last_signal_fingerprint and state.status not in _TERMINAL:
                prior_recipients = ()
                if lease.last_result is not None:
                    raw = lease.last_result.get("notified_recipient_ids", ())
                    if isinstance(raw, list):
                        prior_recipients = tuple(str(item) for item in raw if str(item))
                recipients = prior_recipients or ("agent:mission-manager",)
                created = self._publish(
                    lease,
                    category=NotificationCategory.WORK_RECOVERED,
                    source_id=f"management:{lease.run_id}:{lease.last_signal_fingerprint}:recovered",
                    notification_run_id=lifecycle_run_id,
                    recipients=recipients,
                    subject="Mission work recovered",
                    body="The previously reported execution-health condition is no longer present.",
                    payload={
                        "graph_run_id": lease.run_id,
                        "lifecycle_run_id": lifecycle_run_id,
                        "state_version": state.version,
                        "prior_signal_fingerprint": lease.last_signal_fingerprint,
                    },
                )
                notified_level = 0

            result = {
                "graph_run_id": lease.run_id,
                "lifecycle_run_id": lifecycle_run_id,
                "state_version": state.version,
                "status": state.status.value,
                "health": projection["health"],
                "signal_fingerprint": fingerprint,
                "signal_count": len(signals),
                "consecutive_signal_checks": consecutive,
                "notified_level": notified_level,
                "notified_recipient_ids": list(recipients),
                "notification_created": created,
            }
            return self._complete(
                lease,
                fingerprint=fingerprint,
                consecutive=consecutive,
                notified_level=notified_level,
                result=result,
                retire=state.status in _TERMINAL,
            )
        except Exception as exc:
            updated = self._watches.retry_management_watch(
                tenant_id,
                lease.run_id,
                worker_id=self._worker_id,
                delay_seconds=self._retry_delay_seconds,
                error={"type": type(exc).__name__, "message": str(exc)[:2_000]},
            )
            return CommandRunReport(
                CommandRunStatus.RETRY_SCHEDULED if updated else CommandRunStatus.LEASE_LOST,
                f"management:{lease.run_id}",
                lease.attempt,
                retry_after_seconds=self._retry_delay_seconds if updated else None,
                error_type=type(exc).__name__,
            )
