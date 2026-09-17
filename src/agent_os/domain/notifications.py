"""Immutable, tenant-scoped product notifications."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Mapping


class NotificationCategory(str, Enum):
    HUMAN_ACTION_REQUIRED = "human_action_required"
    OPERATOR_ATTENTION = "operator_attention"
    RUN_SUCCEEDED = "run_succeeded"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    MANAGEMENT_ATTENTION = "management_attention"
    WORK_RECOVERED = "work_recovered"


class NotificationPreferenceMode(str, Enum):
    """How much activity a person wants promoted in their personal inbox."""

    FOCUSED = "focused"
    BALANCED = "balanced"
    ALL = "all"


@dataclass(frozen=True)
class NotificationPreferences:
    """Durable, per-person attention preferences.

    The immutable notification ledger remains complete regardless of these
    preferences.  They only control presentation and interruption, never
    delete audit history or weaken a mandatory approval gate.
    """

    tenant_id: str
    subject_id: str
    mode: NotificationPreferenceMode = NotificationPreferenceMode.BALANCED
    browser_notifications: bool = False
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    timezone_name: str = "UTC"
    digest_interval_minutes: int = 60

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not self.subject_id.strip():
            raise ValueError("notification preference tenant and subject are required")
        if len(self.tenant_id) > 128 or len(self.subject_id) > 256:
            raise ValueError("notification preference identity is too long")
        if (self.quiet_hours_start is None) != (self.quiet_hours_end is None):
            raise ValueError("quiet hours require both a start and end")
        for value in (self.quiet_hours_start, self.quiet_hours_end):
            if value is None:
                continue
            try:
                hour, minute = (int(part) for part in value.split(":"))
            except (TypeError, ValueError) as exc:
                raise ValueError("quiet hours must use HH:MM") from exc
            if not 0 <= hour <= 23 or not 0 <= minute <= 59 or len(value) != 5:
                raise ValueError("quiet hours must use HH:MM")
        if (
            self.quiet_hours_start is not None
            and self.quiet_hours_start == self.quiet_hours_end
        ):
            raise ValueError("quiet hours start and end must differ")
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("notification timezone is unknown") from exc
        if self.digest_interval_minutes not in {15, 30, 60, 240, 1_440}:
            raise ValueError("digest interval must be 15, 30, 60, 240, or 1440 minutes")

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "subject_id": self.subject_id,
            "mode": self.mode.value,
            "browser_notifications": self.browser_notifications,
            "quiet_hours_start": self.quiet_hours_start,
            "quiet_hours_end": self.quiet_hours_end,
            "timezone": self.timezone_name,
            "digest_interval_minutes": self.digest_interval_minutes,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NotificationPreferences":
        return cls(
            tenant_id=str(raw["tenant_id"]),
            subject_id=str(raw["subject_id"]),
            mode=NotificationPreferenceMode(str(raw.get("mode", "balanced"))),
            browser_notifications=bool(raw.get("browser_notifications", False)),
            quiet_hours_start=(
                None if raw.get("quiet_hours_start") is None
                else str(raw["quiet_hours_start"])
            ),
            quiet_hours_end=(
                None if raw.get("quiet_hours_end") is None
                else str(raw["quiet_hours_end"])
            ),
            timezone_name=str(raw.get("timezone", "UTC")),
            digest_interval_minutes=int(raw.get("digest_interval_minutes", 60)),
        )


@dataclass(frozen=True)
class Notification:
    notification_id: str
    tenant_id: str
    run_id: str
    category: NotificationCategory
    recipient_ids: tuple[str, ...]
    subject: str
    body: str
    source_id: str
    created_at: str
    correlation_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        required = (
            self.notification_id,
            self.tenant_id,
            self.run_id,
            self.subject,
            self.body,
            self.source_id,
            self.created_at,
        )
        if not all(value.strip() for value in required) or not self.recipient_ids:
            raise ValueError("notification identity, recipients, content, source, and time are required")
        if any(not recipient.strip() for recipient in self.recipient_ids):
            raise ValueError("notification recipients must be nonempty")
        if len(set(self.recipient_ids)) != len(self.recipient_ids):
            raise ValueError("notification recipients must be unique")
        if len(self.recipient_ids) > 128:
            raise ValueError("notification recipient count exceeds 128")
        if any(len(recipient) > 256 for recipient in self.recipient_ids):
            raise ValueError("notification recipient identity is too long")
        if len(self.subject) > 500 or len(self.body) > 16_384:
            raise ValueError("notification subject or body is too long")
        if len(self.notification_id) > 128 or len(self.tenant_id) > 128:
            raise ValueError("notification identity is too long")
        if len(self.run_id) > 256 or len(self.source_id) > 256:
            raise ValueError("notification run or source identity is too long")
        if self.correlation_id is not None and len(str(self.correlation_id)) > 256:
            raise ValueError("notification correlation identity is too long")
        try:
            payload_size = len(json.dumps(
                self.payload, allow_nan=False, ensure_ascii=False,
                separators=(",", ":"), sort_keys=True,
            ).encode())
        except (TypeError, ValueError) as exc:
            raise ValueError("notification payload must contain JSON-compatible primitives") from exc
        if payload_size > 256 * 1024:
            raise ValueError("notification payload exceeds 256 KiB")
        notification_fingerprint(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "category": self.category.value,
            "recipient_ids": list(self.recipient_ids),
            "subject": self.subject,
            "body": self.body,
            "source_id": self.source_id,
            "created_at": self.created_at,
            "correlation_id": self.correlation_id,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Notification":
        payload = raw.get("payload", {})
        if not isinstance(payload, Mapping):
            raise ValueError("notification payload must be an object")
        return cls(
            notification_id=str(raw["notification_id"]),
            tenant_id=str(raw["tenant_id"]),
            run_id=str(raw["run_id"]),
            category=NotificationCategory(str(raw["category"])),
            recipient_ids=tuple(str(item) for item in raw.get("recipient_ids", ())),
            subject=str(raw["subject"]),
            body=str(raw["body"]),
            source_id=str(raw["source_id"]),
            created_at=str(raw["created_at"]),
            correlation_id=raw.get("correlation_id"),
            payload=dict(payload),
        )


def notification_fingerprint(notification: Notification) -> str:
    try:
        raw = notification.to_dict()
        # Publication time is store metadata, not caller-controlled semantic
        # content. Excluding it lets a process safely retry the same source ID
        # after committing the notification but before acknowledging its action.
        raw.pop("created_at", None)
        encoded = json.dumps(
            raw,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("notification payload must contain JSON-compatible primitives") from exc
    return hashlib.sha256(encoded.encode()).hexdigest()
