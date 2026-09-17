"""Crash-tolerant delivery of durable notifications through governed connectors."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event as ThreadEvent, Thread
import json
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from agent_os.application.command_worker import (
    CommandRunReport,
    CommandRunStatus,
    FatalCommandError,
    RetryPolicy,
    RetryableCommandError,
    is_retryable_execution_error,
)
from agent_os.application.ports import ConnectorRegistry, NotificationDeliveryLease, NotificationStore
from agent_os.infrastructure.http_connector_tools import (
    ConnectorSecretResolver,
    ConnectorTransport,
    pinned_https_request,
)
from agent_os.infrastructure.sql_connectors import HTTPConnectorDefinition


def _connector_path_allowed(connector: HTTPConnectorDefinition, path: str) -> bool:
    decoded_segments = unquote(path).split("/")
    return bool(
        path.startswith("/") and not path.startswith("//") and "\\" not in path
        and not any(character in path for character in "\r\n?#")
        and not any(segment in {".", ".."} for segment in decoded_segments)
        and any(
            path == prefix
            or path.startswith(prefix if prefix.endswith("/") else prefix + "/")
            for prefix in connector.allowed_path_prefixes
        )
    )


def _retry_after(headers: Mapping[str, str]) -> int | None:
    raw = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
    try:
        value = int(raw) if raw is not None else None
    except ValueError:
        return None
    return None if value is None else min(max(value, 0), 300)


class GovernedNotificationSender:
    """Render a secret-free notification and send it within an approved HTTP envelope."""

    def __init__(
        self,
        registry: ConnectorRegistry,
        secrets: ConnectorSecretResolver,
        *,
        transport: ConnectorTransport = pinned_https_request,
    ) -> None:
        self._registry = registry
        self._secrets = secrets
        self._transport = transport

    @staticmethod
    def _body(lease: NotificationDeliveryLease) -> bytes:
        notification = dict(lease.notification)
        route = lease.route
        if route.get("redaction_policy", "summary") == "summary":
            notification = {
                key: notification.get(key)
                for key in (
                    "notification_id", "run_id", "category", "subject", "created_at",
                )
            }
            notification["body"] = "Open Agent OS to review this notification securely."
        payload_format = route.get("payload_format")
        if payload_format == "slack":
            rendered: Mapping[str, Any] = {
                "channel": route.get("destination"),
                "text": f"*{notification.get('subject', 'Agent OS update')}*\n"
                f"{notification.get('body', '')}",
                "unfurl_links": False,
                "unfurl_media": False,
            }
        elif payload_format == "agent-os":
            rendered = {
                "event": "agent_os.notification",
                "version": 1,
                "delivery_id": lease.delivery_id,
                "notification": notification,
            }
        else:
            raise FatalCommandError("notification route has an unsupported payload format")
        try:
            body = json.dumps(
                rendered, allow_nan=False, ensure_ascii=False,
                separators=(",", ":"), sort_keys=True,
            ).encode()
        except (TypeError, ValueError) as exc:
            raise FatalCommandError("notification payload is not JSON serializable") from exc
        if len(body) > 512 * 1024:
            raise FatalCommandError("notification delivery exceeds 512 KiB")
        return body

    def deliver(self, lease: NotificationDeliveryLease) -> Mapping[str, Any]:
        connector_id = str(lease.route.get("connector_id") or "")
        stored = self._registry.get_connector(lease.tenant_id, connector_id)
        if stored is None or not stored.get("active"):
            raise FatalCommandError("notification connector is missing or disabled")
        try:
            connector = HTTPConnectorDefinition.model_validate({
                key: stored[key] for key in HTTPConnectorDefinition.model_fields
            })
        except (KeyError, ValueError) as exc:
            raise FatalCommandError("notification connector definition is invalid") from exc
        path = str(lease.route.get("path") or "")
        if "POST" not in connector.allowed_methods or not _connector_path_allowed(connector, path):
            raise FatalCommandError(
                "notification route is outside its owner-approved connector capability"
            )
        headers = {
            "Accept": "application/json, text/plain;q=0.8",
            "Content-Type": "application/json",
            "Host": urlparse(connector.base_url).hostname or "",
            "User-Agent": "AgentOS-Notification/1.0",
            str(connector.idempotency_header): lease.delivery_id,
        }
        if connector.auth_kind != "none":
            secret = self._secrets.resolve(lease.tenant_id, str(connector.credential_ref))
            if connector.auth_kind == "bearer":
                headers["Authorization"] = f"Bearer {secret}"
            else:
                headers[str(connector.auth_header)] = secret
        body = self._body(lease)
        status, response_headers, response_body = self._transport(
            "POST",
            connector.base_url + path,
            headers,
            body,
            connector.timeout_seconds,
            min(connector.max_response_bytes, 256 * 1024),
        )
        if status in {408, 425, 429} or 500 <= status <= 599:
            raise RetryableCommandError(
                f"notification connector returned retryable HTTP status {status}",
                retry_after_seconds=_retry_after(response_headers),
            )
        if not 200 <= status <= 299:
            raise FatalCommandError(
                f"notification connector returned terminal HTTP status {status}"
            )
        if lease.route.get("payload_format") == "slack":
            try:
                response = json.loads(response_body or b"{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                response = None
            if isinstance(response, Mapping) and response.get("ok") is False:
                raise FatalCommandError("Slack rejected the notification delivery")
        return {
            "connector_id": connector.connector_id,
            "route_id": lease.route.get("route_id"),
            "status_code": status,
        }


class DurableNotificationDeliveryWorker:
    """Lease, deliver, retry, and redrive external notifications without duplication."""

    def __init__(
        self,
        *,
        store: NotificationStore,
        sender: GovernedNotificationSender,
        worker_id: str,
        lease_seconds: int = 60,
        retry_policy: RetryPolicy | None = None,
        retry_classifier: Callable[[Exception], bool] = is_retryable_execution_error,
    ) -> None:
        if not worker_id.strip() or lease_seconds < 3:
            raise ValueError("worker_id and a lease of at least three seconds are required")
        self._store = store
        self._sender = sender
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._retry = retry_policy or RetryPolicy()
        self._retry_classifier = retry_classifier

    def run_one(self, tenant_id: str) -> CommandRunReport:
        lease = self._store.claim_notification_delivery(
            tenant_id, worker_id=self._worker_id, lease_seconds=self._lease_seconds,
        )
        if lease is None:
            return CommandRunReport(CommandRunStatus.IDLE)
        stopped = ThreadEvent()
        lease_lost = ThreadEvent()

        def renew() -> None:
            interval = max(1.0, self._lease_seconds / 3)
            while not stopped.wait(interval):
                try:
                    owned = self._store.heartbeat_notification_delivery(
                        tenant_id,
                        lease.delivery_id,
                        worker_id=self._worker_id,
                        lease_seconds=self._lease_seconds,
                    )
                except Exception:
                    lease_lost.set()
                    return
                if not owned:
                    lease_lost.set()
                    return

        heartbeat = Thread(
            target=renew,
            name=f"aos-notification-lease-{lease.delivery_id[-12:]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            result = self._sender.deliver(lease)
        except Exception as exc:
            stopped.set()
            heartbeat.join(timeout=1)
            if lease_lost.is_set():
                return CommandRunReport(
                    CommandRunStatus.LEASE_LOST,
                    lease.delivery_id,
                    lease.attempt,
                    error_type=type(exc).__name__,
                )
            retryable = self._retry_classifier(exc)
            exhausted = (
                self._retry.max_attempts is not None
                and lease.attempt >= self._retry.max_attempts
            )
            error = {
                "type": type(exc).__name__,
                "message": str(exc)[:2_000],
                "retryable": retryable and not exhausted,
                "attempt": lease.attempt,
            }
            if retryable and not exhausted:
                hint = exc.retry_after_seconds if isinstance(exc, RetryableCommandError) else None
                delay = self._retry.delay(lease.attempt, hint)
                changed = self._store.retry_notification_delivery(
                    tenant_id,
                    lease.delivery_id,
                    worker_id=self._worker_id,
                    error=error,
                    delay_seconds=delay,
                )
                return CommandRunReport(
                    CommandRunStatus.RETRY_SCHEDULED if changed else CommandRunStatus.LEASE_LOST,
                    lease.delivery_id,
                    lease.attempt,
                    retry_after_seconds=delay if changed else None,
                    error_type=type(exc).__name__,
                )
            changed = self._store.fail_notification_delivery(
                tenant_id,
                lease.delivery_id,
                worker_id=self._worker_id,
                error=error,
            )
            return CommandRunReport(
                CommandRunStatus.FAILED if changed else CommandRunStatus.LEASE_LOST,
                lease.delivery_id,
                lease.attempt,
                error_type=type(exc).__name__,
            )
        else:
            stopped.set()
            heartbeat.join(timeout=1)
            if lease_lost.is_set():
                return CommandRunReport(
                    CommandRunStatus.LEASE_LOST, lease.delivery_id, lease.attempt,
                )
            changed = self._store.complete_notification_delivery(
                tenant_id,
                lease.delivery_id,
                worker_id=self._worker_id,
                result=result,
            )
            return CommandRunReport(
                CommandRunStatus.SUCCEEDED if changed else CommandRunStatus.LEASE_LOST,
                lease.delivery_id,
                lease.attempt,
            )
