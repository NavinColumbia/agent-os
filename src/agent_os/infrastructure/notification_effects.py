"""Map durable lifecycle/graph effects into the real in-product inbox."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Callable, Mapping, Any

from agent_os.application.lifecycle import CommandEnvelope
from agent_os.application.ports import NotificationStore
from agent_os.domain.lifecycle import CommandKind
from agent_os.domain.notifications import Notification, NotificationCategory
from agent_os.domain.workflow_runtime import WorkflowAction, WorkflowActionKind


def _id(source_id: str, category: NotificationCategory) -> str:
    material = f"agent-os:notification:v1:{source_id}:{category.value}"
    return "notification-" + hashlib.sha256(material.encode()).hexdigest()


class NotificationEffectHandlers:
    """Idempotently publish command/action outcomes to the tenant inbox."""

    def __init__(
        self,
        store: NotificationStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _publish(
        self,
        *,
        tenant_id: str,
        run_id: str,
        source_id: str,
        category: NotificationCategory,
        recipients: tuple[str, ...],
        subject: str,
        body: str,
        correlation_id: str | None,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        notification = Notification(
            notification_id=_id(source_id, category),
            tenant_id=tenant_id,
            run_id=run_id,
            category=category,
            recipient_ids=recipients,
            subject=subject,
            body=body,
            source_id=source_id,
            created_at=self._clock().isoformat(),
            correlation_id=correlation_id,
            payload=dict(payload),
        )
        created = self._store.publish_notification(notification)
        return {
            "notification_id": notification.notification_id,
            "channel": "in_app",
            "created": created,
        }

    def lifecycle_handlers(self):
        return {
            CommandKind.NOTIFY_HUMAN: self._lifecycle_human,
            CommandKind.NOTIFY_OPERATOR: self._lifecycle_operator,
            CommandKind.PUBLISH_COMPLETION: self._lifecycle_completion,
        }

    def graph_handlers(self):
        return {
            WorkflowActionKind.NOTIFY_HUMAN: self._graph_human,
            WorkflowActionKind.RUN_SUCCEEDED: self._graph_succeeded,
            WorkflowActionKind.RUN_FAILED: self._graph_failed,
            WorkflowActionKind.RUN_CANCELLED: self._graph_cancelled,
        }

    def _lifecycle_human(self, item: CommandEnvelope) -> Mapping[str, Any]:
        payload = dict(item.command.payload)
        recipients = tuple(str(value) for value in payload.get("recipient_ids", ())) or ("human:ceo",)
        reason = str(payload.get("reason") or "Your input is required to continue this mission.")
        return self._publish(
            tenant_id=item.organization_id,
            run_id=item.run_id,
            source_id=item.command_id,
            category=NotificationCategory.HUMAN_ACTION_REQUIRED,
            recipients=recipients,
            subject="Agent OS needs your input",
            body=reason,
            correlation_id=str(payload.get("correlation_id") or "") or None,
            payload=payload,
        )

    def _lifecycle_operator(self, item: CommandEnvelope) -> Mapping[str, Any]:
        payload = dict(item.command.payload)
        reason = str(payload.get("reason") or "A mission operation needs attention.")
        return self._publish(
            tenant_id=item.organization_id,
            run_id=item.run_id,
            source_id=item.command_id,
            category=NotificationCategory.OPERATOR_ATTENTION,
            recipients=("operator:on-call",),
            subject=f"Operation needs attention: {payload.get('operation') or 'unknown'}",
            body=reason,
            correlation_id=None,
            payload=payload,
        )

    def _lifecycle_completion(self, item: CommandEnvelope) -> Mapping[str, Any]:
        payload = dict(item.command.payload)
        return self._publish(
            tenant_id=item.organization_id,
            run_id=item.run_id,
            source_id=item.command_id,
            category=NotificationCategory.RUN_SUCCEEDED,
            recipients=("human:ceo",),
            subject="Mission completed",
            body=str(payload.get("summary") or "The mission reached its accepted release outcome."),
            correlation_id=None,
            payload=payload,
        )

    @staticmethod
    def _graph_identity(envelope: Mapping[str, Any], action: WorkflowAction) -> tuple[str, str, dict[str, Any]]:
        tenant_id = str(envelope.get("tenant_id") or "")
        run_id = str(envelope.get("run_id") or "")
        if not tenant_id or not run_id:
            raise ValueError("graph notification requires tenant and run identity")
        return tenant_id, run_id, dict(action.payload)

    def _graph_human(self, envelope: Mapping[str, Any], action: WorkflowAction) -> Mapping[str, Any]:
        tenant_id, run_id, payload = self._graph_identity(envelope, action)
        recipients = tuple(str(value) for value in payload.get("recipient_ids", ()))
        return self._publish(
            tenant_id=tenant_id,
            run_id=run_id,
            source_id=action.action_id,
            category=NotificationCategory.HUMAN_ACTION_REQUIRED,
            recipients=recipients,
            subject="Workflow needs your input",
            body=str(payload.get("reason") or "Your input is required."),
            correlation_id=str(payload.get("correlation_id") or "") or None,
            payload=payload,
        )

    def _graph_status(
        self,
        envelope: Mapping[str, Any],
        action: WorkflowAction,
        category: NotificationCategory,
        subject: str,
    ) -> Mapping[str, Any]:
        tenant_id, run_id, payload = self._graph_identity(envelope, action)
        return self._publish(
            tenant_id=tenant_id,
            run_id=run_id,
            source_id=action.action_id,
            category=category,
            recipients=("human:ceo",),
            subject=subject,
            body=str(payload.get("reason") or subject),
            correlation_id=None,
            payload=payload,
        )

    def _graph_succeeded(self, envelope: Mapping[str, Any], action: WorkflowAction) -> Mapping[str, Any]:
        return self._graph_status(
            envelope, action, NotificationCategory.RUN_SUCCEEDED, "Workflow completed",
        )

    def _graph_failed(self, envelope: Mapping[str, Any], action: WorkflowAction) -> Mapping[str, Any]:
        return self._graph_status(
            envelope, action, NotificationCategory.RUN_FAILED, "Workflow failed",
        )

    def _graph_cancelled(self, envelope: Mapping[str, Any], action: WorkflowAction) -> Mapping[str, Any]:
        return self._graph_status(
            envelope, action, NotificationCategory.RUN_CANCELLED, "Workflow cancelled",
        )
