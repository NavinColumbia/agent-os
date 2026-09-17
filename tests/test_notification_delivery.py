from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_os.application.command_worker import CommandRunStatus, RetryPolicy
from agent_os.domain.notifications import Notification, NotificationCategory
from agent_os.infrastructure.notification_delivery import (
    DurableNotificationDeliveryWorker,
    GovernedNotificationSender,
)
from agent_os.infrastructure.sql_connectors import SQLConnectorRegistry
from agent_os.infrastructure.sql_notifications import SQLNotificationStore


def connector_definition():
    return {
        "connector_id": "operator-hook",
        "display_name": "Operator webhook",
        "base_url": "https://notify.example.test",
        "allowed_path_prefixes": ["/v1/events/"],
        "allowed_methods": ["POST"],
        "auth_kind": "bearer",
        "credential_ref": "operator-hook-token",
        "idempotency_header": "Idempotency-Key",
        "timeout_seconds": 15,
        "max_response_bytes": 1024,
    }


def route_definition(**changes):
    value = {
        "route_id": "operator-alerts",
        "display_name": "Operator alerts",
        "connector_id": "operator-hook",
        "path": "/v1/events/agent-os",
        "categories": ["human_action_required", "management_attention"],
        "payload_format": "agent-os",
        "destination": None,
    }
    value.update(changes)
    return value


def notification(**changes):
    value = {
        "notification_id": "notification-one",
        "tenant_id": "tenant-a",
        "run_id": "run-one",
        "category": NotificationCategory.HUMAN_ACTION_REQUIRED,
        "recipient_ids": ("human:ceo",),
        "subject": "Decision required",
        "body": "Approve the production release.",
        "source_id": "wait-one",
        "created_at": "2026-09-13T12:00:00+00:00",
        "correlation_id": "approval-one",
        "payload": {"risk": "production"},
    }
    value.update(changes)
    return Notification(**value)


class Secrets:
    def resolve(self, tenant_id, credential_ref):
        assert (tenant_id, credential_ref) == ("tenant-a", "operator-hook-token")
        return "private-operator-token"


def stores(tmp_path: Path, clock):
    database_url = f"sqlite:///{tmp_path / 'delivery.sqlite3'}"
    registry = SQLConnectorRegistry(database_url, create_schema=True)
    notifications = SQLNotificationStore(database_url, create_schema=True, clock=clock)
    registry.register_connector(
        tenant_id="tenant-a",
        definition=connector_definition(),
        actor_id="human:ceo",
        idempotency_key="register-hook",
    )
    notifications.register_notification_route(
        tenant_id="tenant-a",
        definition=route_definition(),
        actor_id="human:ceo",
        idempotency_key="register-route",
    )
    return registry, notifications


def test_notification_publication_enqueues_only_matching_routes_and_delivers_secret_free(tmp_path):
    now = [datetime(2026, 9, 13, 12, tzinfo=timezone.utc)]
    registry, notifications = stores(tmp_path, lambda: now[0])
    calls = []

    def transport(method, url, headers, body, timeout, max_bytes):
        calls.append((method, url, dict(headers), body, timeout, max_bytes))
        return 202, {"Content-Type": "application/json"}, b'{"accepted":true}'

    try:
        assert notifications.publish_notification(notification()) is True
        assert notifications.publish_notification(notification()) is False
        notifications.publish_notification(notification(
            notification_id="notification-no-route",
            source_id="source-no-route",
            category=NotificationCategory.RUN_SUCCEEDED,
        ))
        notifications.publish_notification(notification(
            notification_id="notification-wrong-audience",
            source_id="source-wrong-audience",
            recipient_ids=("agent:engineer",),
        ))
        worker = DurableNotificationDeliveryWorker(
            store=notifications,
            sender=GovernedNotificationSender(registry, Secrets(), transport=transport),
            worker_id="worker-a",
            retry_policy=RetryPolicy(base_delay_seconds=0, max_delay_seconds=0),
        )

        report = worker.run_one("tenant-a")
        assert report.status is CommandRunStatus.SUCCEEDED
        assert worker.run_one("tenant-a").status is CommandRunStatus.IDLE
        assert len(calls) == 1
        method, url, headers, body, timeout, max_bytes = calls[0]
        assert (method, url) == ("POST", "https://notify.example.test/v1/events/agent-os")
        assert headers["Authorization"] == "Bearer private-operator-token"
        assert headers["Idempotency-Key"] == report.command_id
        assert b"private-operator-token" not in body
        assert b'"event":"agent_os.notification"' in body
        assert b"Approve the production release" not in body
        assert b'"risk"' not in body
        assert b"Open Agent OS to review this notification securely" in body
        assert (timeout, max_bytes) == (15, 1024)
        delivery = notifications.list_notification_deliveries("tenant-a")[0]
        assert delivery["status"] == "delivered"
        assert delivery["attempts"] == 1
        assert delivery["result"]["status_code"] == 202
        assert notifications.list_notification_deliveries("tenant-b") == ()
    finally:
        notifications.close()
        registry.close()


def test_batched_attention_is_kept_in_app_without_external_delivery(tmp_path):
    now = [datetime(2026, 9, 13, 12, tzinfo=timezone.utc)]
    registry, notifications = stores(tmp_path, lambda: now[0])
    try:
        value = notification(
            notification_id="notification-batched",
            source_id="source-batched",
            category=NotificationCategory.MANAGEMENT_ATTENTION,
            payload={
                "attention_disposition": "batch",
                "attention_reason": "retain for the decision digest",
            },
        )

        assert notifications.publish_notification(value) is True
        assert notifications.list_notifications("tenant-a")[0]["notification_id"] == value.notification_id
        assert notifications.list_notification_deliveries("tenant-a") == ()
    finally:
        notifications.close()
        registry.close()


def test_transient_delivery_retries_and_failed_delivery_can_be_redriven(tmp_path):
    now = [datetime(2026, 9, 13, 12, tzinfo=timezone.utc)]
    registry, notifications = stores(tmp_path, lambda: now[0])
    responses = [503, 401, 204]

    def transport(*_):
        status = responses.pop(0)
        return status, {"Retry-After": "2"}, b""

    try:
        notifications.publish_notification(notification())
        worker = DurableNotificationDeliveryWorker(
            store=notifications,
            sender=GovernedNotificationSender(registry, Secrets(), transport=transport),
            worker_id="worker-a",
            retry_policy=RetryPolicy(base_delay_seconds=1, max_delay_seconds=10),
        )
        retry = worker.run_one("tenant-a")
        assert retry.status is CommandRunStatus.RETRY_SCHEDULED
        assert retry.retry_after_seconds == 2
        assert worker.run_one("tenant-a").status is CommandRunStatus.IDLE
        now[0] += timedelta(seconds=2)
        failed = worker.run_one("tenant-a")
        assert failed.status is CommandRunStatus.FAILED
        redriven = notifications.redrive_notification_delivery(
            tenant_id="tenant-a",
            delivery_id=str(failed.command_id),
            actor_id="human:ceo",
            idempotency_key="redrive-after-fix",
        )
        assert redriven["status"] == "pending"
        assert notifications.redrive_notification_delivery(
            tenant_id="tenant-a",
            delivery_id=str(failed.command_id),
            actor_id="human:ceo",
            idempotency_key="redrive-after-fix",
        )["duplicate"] is True
        assert worker.run_one("tenant-a").status is CommandRunStatus.SUCCEEDED
    finally:
        notifications.close()
        registry.close()


def test_disabling_route_cancels_queued_delivery_and_prevents_future_enqueues(tmp_path):
    now = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
    registry, notifications = stores(tmp_path, lambda: now)
    try:
        notifications.publish_notification(notification())
        disabled = notifications.disable_notification_route(
            tenant_id="tenant-a",
            route_id="operator-alerts",
            actor_id="human:ceo",
            reason="Stop external alerts",
            idempotency_key="disable-route",
        )
        assert disabled["active"] is False
        assert notifications.list_notification_deliveries("tenant-a")[0]["status"] == "cancelled"
        notifications.publish_notification(notification(
            notification_id="notification-two", source_id="source-two",
        ))
        assert len(notifications.list_notification_deliveries("tenant-a")) == 1
        route_events = [
            event for event in notifications.list_experience_events(
                "tenant-a", audience_ids=("tenant:members",), limit=100,
            ).events
            if event["resource_type"] == "notification_route"
        ]
        assert [event["kind"] for event in route_events] == [
            "notification.route.registered", "notification.route.disabled",
        ]
        assert [event["projection_revision"] for event in route_events] == [1, 2]
        summaries = " ".join(event["safe_summary"] for event in route_events)
        assert "Stop external alerts" not in summaries
        assert "operator-alerts" not in summaries
    finally:
        notifications.close()
        registry.close()


def test_route_validation_rejects_unbounded_or_unknown_delivery_configuration(tmp_path):
    store = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'invalid-routes.sqlite3'}", create_schema=True,
    )
    try:
        for definition in (
            route_definition(path="/v1/../admin"),
            route_definition(categories=["not-a-category"]),
            route_definition(payload_format="slack", destination=None),
        ):
            try:
                store.register_notification_route(
                    tenant_id="tenant-a",
                    definition=definition,
                    actor_id="human:ceo",
                    idempotency_key="invalid-route",
                )
            except ValueError:
                pass
            else:
                raise AssertionError("unsafe notification route was admitted")
    finally:
        store.close()
