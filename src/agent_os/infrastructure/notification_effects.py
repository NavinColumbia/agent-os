"""Map durable lifecycle/graph effects into the real in-product inbox."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Callable, Mapping, Any

from agent_os.application.lifecycle import CommandEnvelope
from agent_os.application.ports import MissionParticipantStore, NotificationStore
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
        mission_participants: MissionParticipantStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._mission_participants = mission_participants
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _mission_recipients(
        self,
        tenant_id: str,
        run_id: str,
        recipients: tuple[str, ...],
        payload: Mapping[str, Any],
    ) -> tuple[tuple[str, ...], Mapping[str, Any]]:
        scoped_roles = {"builder", "reviewer", "client", "viewer"}
        requested_roles = {
            recipient.removeprefix("role:")
            for recipient in recipients
            if recipient.startswith("role:")
            and recipient.removeprefix("role:") in scoped_roles
        }
        if self._mission_participants is None or not requested_roles:
            return recipients, payload
        mission_id = str(payload.get("lifecycle_run_id") or run_id).strip()
        participants = self._mission_participants.list_participants(tenant_id, mission_id)
        expanded = [
            recipient for recipient in recipients
            if recipient.removeprefix("role:") not in requested_roles
        ]
        matched = [
            str(participant["subject_id"])
            for participant in participants
            if participant.get("active")
            and participant.get("participation_role") in requested_roles
        ]
        governed_payload = dict(payload)
        if not matched:
            # Retain the authoritative role address so a later valid mission
            # assignment can discover and answer the still-open request. The
            # manager/CEO aliases are monitoring fallbacks, not substitute
            # decision owners.
            expanded.extend(
                recipient for recipient in recipients
                if recipient.removeprefix("role:") in requested_roles
            )
            expanded.extend(("role:manager", "human:ceo"))
            governed_payload["participant_routing_fallback"] = sorted(requested_roles)
        else:
            expanded.extend(matched[:120])
            if len(matched) > 120:
                expanded.extend(("role:manager", "human:ceo"))
                governed_payload["participant_routing_truncated"] = len(matched) - 120
        return tuple(dict.fromkeys(expanded)), governed_payload

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
        payload = dict(payload)
        if category is NotificationCategory.HUMAN_ACTION_REQUIRED:
            if len(recipients) != 1:
                raise ValueError(
                    "a human request requires exactly one authoritative recipient"
                )
            payload["request_recipient_id"] = recipients[0]
            payload["request_kind"] = (
                "workflow_blocking" if correlation_id else "advisory"
            )
        recipients, payload = self._mission_recipients(
            tenant_id, run_id, recipients, payload,
        )
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
        close_requests = getattr(self._store, "close_active_human_requests", None)
        closed_requests = 0
        if payload.get("recoverable") is False and close_requests is not None:
            closed_requests = close_requests(
                tenant_id=item.organization_id,
                run_id=item.run_id,
                actor_id="system:lifecycle-runtime",
                reason="run_failed_terminally",
            )
        result = self._publish(
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
        return {**result, "closed_human_requests": closed_requests}

    def _lifecycle_completion(self, item: CommandEnvelope) -> Mapping[str, Any]:
        payload = dict(item.command.payload)
        close_requests = getattr(self._store, "close_active_human_requests", None)
        closed_requests = 0
        if close_requests is not None:
            closed_requests = close_requests(
                tenant_id=item.organization_id,
                run_id=item.run_id,
                actor_id="system:lifecycle-runtime",
                reason="run_succeeded",
            )
        result = self._publish(
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
        return {**result, "closed_human_requests": closed_requests}

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
        if len(recipients) != 1:
            raise ValueError("graph human request requires exactly one authoritative recipient")
        payload.update({
            "request_recipient_id": recipients[0],
            "request_kind": "workflow_blocking",
        })
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
        close_requests = getattr(self._store, "close_active_human_requests", None)
        closed_requests = 0
        if close_requests is not None:
            closed_requests = close_requests(
                tenant_id=tenant_id,
                run_id=run_id,
                actor_id="system:workflow-runtime",
                reason=category.value,
            )
        result = self._publish(
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
        return {**result, "closed_human_requests": closed_requests}

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
