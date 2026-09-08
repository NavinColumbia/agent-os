"""Immutable, tenant-scoped product notifications."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from typing import Any, Mapping


class NotificationCategory(str, Enum):
    HUMAN_ACTION_REQUIRED = "human_action_required"
    OPERATOR_ATTENTION = "operator_attention"
    RUN_SUCCEEDED = "run_succeeded"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    MANAGEMENT_ATTENTION = "management_attention"
    WORK_RECOVERED = "work_recovered"


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
