"""Crash-recoverable, privacy-reduced background Web Push delivery."""

from __future__ import annotations

import base64
import hmac
import json
from threading import Event as ThreadEvent, Thread
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from py_vapid import Vapid02
from pywebpush import WebPushException, webpush
from requests.exceptions import RequestException

from agent_os.application.command_worker import (
    CommandRunReport,
    CommandRunStatus,
    FatalCommandError,
    RetryPolicy,
    RetryableCommandError,
)
from agent_os.application.ports import NotificationStore, WebPushDeliveryLease


class ExpiredWebPushSubscription(FatalCommandError):
    """The browser push service says this device capability no longer exists."""


class WebPushSender:
    """Encrypt and send only a bounded generic wake-up notification."""

    def __init__(
        self,
        *,
        vapid_private_key: str,
        vapid_public_key: str,
        vapid_subject: str,
        timeout_seconds: float = 10.0,
        send: Callable[..., Any] = webpush,
    ) -> None:
        parsed_subject = urlparse(vapid_subject)
        if not (
            (parsed_subject.scheme == "mailto" and bool(parsed_subject.path))
            or (
                parsed_subject.scheme == "https"
                and bool(parsed_subject.netloc)
                and parsed_subject.username is None
                and parsed_subject.password is None
            )
        ):
            raise ValueError("Web Push VAPID subject must be a mailto: or HTTPS contact URI")
        if not 1 <= timeout_seconds <= 30:
            raise ValueError("Web Push timeout must be between 1 and 30 seconds")
        try:
            vapid = Vapid02.from_string(vapid_private_key)
            numbers = vapid.public_key.public_numbers()
            derived = base64.urlsafe_b64encode(
                b"\x04" + numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")
            ).decode().rstrip("=")
        except Exception as exc:
            raise ValueError("Web Push VAPID private key is invalid") from exc
        if not hmac.compare_digest(derived, vapid_public_key):
            raise ValueError("Web Push public and private VAPID keys do not match")
        self._private_key = vapid_private_key
        self._subject = vapid_subject
        self._timeout = timeout_seconds
        self._send = send

    def deliver(self, lease: WebPushDeliveryLease) -> Mapping[str, Any]:
        encoded = json.dumps(
            dict(lease.payload), allow_nan=False, ensure_ascii=False,
            separators=(",", ":"), sort_keys=True,
        )
        if len(encoded.encode()) > 3_072:
            raise FatalCommandError("Web Push payload exceeds the privacy-reduced size limit")
        subscription = lease.subscription.to_secret_dict()
        subscription.pop("expirationTime", None)
        try:
            response = self._send(
                subscription_info=subscription,
                data=encoded,
                vapid_private_key=self._private_key,
                vapid_claims={"sub": self._subject},
                content_encoding="aes128gcm",
                timeout=self._timeout,
                ttl=300,
                headers={"Urgency": "high"},
            )
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in {404, 410}:
                raise ExpiredWebPushSubscription(
                    "push service reported an expired device subscription"
                ) from exc
            if status in {408, 425, 429} or (status is not None and status >= 500):
                raise RetryableCommandError(
                    f"push service returned retryable HTTP status {status}"
                ) from exc
            raise FatalCommandError(
                f"push service rejected the delivery with HTTP status {status or 'unknown'}"
            ) from exc
        except RequestException as exc:
            raise RetryableCommandError("push service was temporarily unreachable") from exc
        status_code = int(getattr(response, "status_code", 201))
        return {
            "subscription_id": lease.subscription_id,
            "status_code": status_code,
            "payload_policy": "generic-wakeup-v1",
        }


class DurableWebPushDeliveryWorker:
    def __init__(
        self,
        *,
        store: NotificationStore,
        sender: WebPushSender,
        worker_id: str,
        lease_seconds: int = 60,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        if not worker_id.strip() or lease_seconds < 3:
            raise ValueError("worker_id and a lease of at least three seconds are required")
        self._store = store
        self._sender = sender
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._retry = retry_policy or RetryPolicy(max_attempts=5)

    def run_one(self, tenant_id: str) -> CommandRunReport:
        lease = self._store.claim_web_push_delivery(
            tenant_id, worker_id=self._worker_id, lease_seconds=self._lease_seconds,
        )
        if lease is None:
            return CommandRunReport(CommandRunStatus.IDLE)
        stopped = ThreadEvent()
        lease_lost = ThreadEvent()

        def renew() -> None:
            while not stopped.wait(max(1.0, self._lease_seconds / 3)):
                try:
                    owned = self._store.heartbeat_web_push_delivery(
                        tenant_id, lease.delivery_id,
                        worker_id=self._worker_id, lease_seconds=self._lease_seconds,
                    )
                except Exception:
                    lease_lost.set()
                    return
                if not owned:
                    lease_lost.set()
                    return

        heartbeat = Thread(
            target=renew, name=f"aos-web-push-{lease.delivery_id[-12:]}", daemon=True,
        )
        heartbeat.start()
        try:
            result = self._sender.deliver(lease)
        except Exception as exc:
            stopped.set()
            heartbeat.join(timeout=1)
            if lease_lost.is_set():
                return CommandRunReport(
                    CommandRunStatus.LEASE_LOST, lease.delivery_id, lease.attempt,
                    error_type=type(exc).__name__,
                )
            retryable = isinstance(exc, RetryableCommandError)
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
                delay = self._retry.delay(lease.attempt)
                changed = self._store.retry_web_push_delivery(
                    tenant_id, lease.delivery_id, worker_id=self._worker_id,
                    error=error, delay_seconds=delay,
                )
                return CommandRunReport(
                    CommandRunStatus.RETRY_SCHEDULED if changed else CommandRunStatus.LEASE_LOST,
                    lease.delivery_id, lease.attempt,
                    retry_after_seconds=delay if changed else None,
                    error_type=type(exc).__name__,
                )
            changed = self._store.fail_web_push_delivery(
                tenant_id, lease.delivery_id, worker_id=self._worker_id,
                error=error,
                revoke_subscription=isinstance(exc, ExpiredWebPushSubscription),
            )
            return CommandRunReport(
                CommandRunStatus.FAILED if changed else CommandRunStatus.LEASE_LOST,
                lease.delivery_id, lease.attempt, error_type=type(exc).__name__,
            )
        else:
            stopped.set()
            heartbeat.join(timeout=1)
            if lease_lost.is_set():
                return CommandRunReport(
                    CommandRunStatus.LEASE_LOST, lease.delivery_id, lease.attempt,
                )
            changed = self._store.complete_web_push_delivery(
                tenant_id, lease.delivery_id, worker_id=self._worker_id, result=result,
            )
            return CommandRunReport(
                CommandRunStatus.SUCCEEDED if changed else CommandRunStatus.LEASE_LOST,
                lease.delivery_id, lease.attempt,
            )
