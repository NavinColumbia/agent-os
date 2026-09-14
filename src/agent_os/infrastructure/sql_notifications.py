"""SQL-backed, idempotent in-app notification ledger."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Callable, Mapping, Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    MetaData,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    insert,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError

from agent_os.application.ports import NotificationDeliveryLease, NotificationStore
from agent_os.domain.notifications import (
    Notification,
    NotificationCategory,
    notification_fingerprint,
)
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url


notification_metadata = MetaData()

notifications = Table(
    "aos_v2_notifications",
    notification_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("notification_id", String(128), primary_key=True),
    Column("run_id", String(256), nullable=False),
    Column("category", String(64), nullable=False),
    Column("recipient_ids", JSON, nullable=False),
    Column("subject", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("source_id", String(256), nullable=False),
    Column("correlation_id", String(256)),
    Column("payload", JSON, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("record", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

notification_recipients = Table(
    "aos_v2_notification_recipients",
    notification_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("notification_id", String(128), primary_key=True),
    Column("recipient_id", String(256), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "notification_id"],
        ["aos_v2_notifications.tenant_id", "aos_v2_notifications.notification_id"],
        ondelete="CASCADE",
    ),
)

Index(
    "aos_v2_notification_recipients_inbox_idx",
    notification_recipients.c.tenant_id,
    notification_recipients.c.recipient_id,
    notification_recipients.c.created_at.desc(),
    notification_recipients.c.notification_id.desc(),
)

notification_routes = Table(
    "aos_v2_notification_routes",
    notification_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("route_id", String(64), primary_key=True),
    Column("connector_id", String(64), nullable=False),
    Column("categories", JSON, nullable=False),
    Column("definition", JSON, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("active", Boolean, nullable=False),
    Column("idempotency_key", String(200), nullable=False),
    Column("created_by", String(256), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("disabled_by", String(256), nullable=True),
    Column("disabled_at", DateTime(timezone=True), nullable=True),
    Column("disabled_reason", Text, nullable=True),
    Column("disable_idempotency_key", String(200), nullable=True),
    UniqueConstraint("tenant_id", "idempotency_key"),
)

notification_deliveries = Table(
    "aos_v2_notification_deliveries",
    notification_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("delivery_id", String(96), primary_key=True),
    Column("notification_id", String(128), nullable=False),
    Column("route_id", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("lease_owner", String(256), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("last_error", JSON, nullable=True),
    Column("result", JSON, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("delivered_at", DateTime(timezone=True), nullable=True),
    Column("redrive_idempotency_key", String(200), nullable=True),
    Column("redriven_by", String(256), nullable=True),
    ForeignKeyConstraint(
        ["tenant_id", "notification_id"],
        ["aos_v2_notifications.tenant_id", "aos_v2_notifications.notification_id"],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "route_id"],
        ["aos_v2_notification_routes.tenant_id", "aos_v2_notification_routes.route_id"],
    ),
)

Index(
    "aos_v2_notification_deliveries_ready_idx",
    notification_deliveries.c.tenant_id,
    notification_deliveries.c.status,
    notification_deliveries.c.available_at,
)

_ROUTE_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}[a-z0-9]$")
_DELIVERY_FORMATS = frozenset({"agent-os", "slack"})
_CATEGORIES = frozenset(item.value for item in NotificationCategory)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class SQLNotificationStore(NotificationStore):
    def __init__(
        self,
        database_url: str,
        *,
        create_schema: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if create_schema:
            notification_metadata.create_all(self._engine)

    @staticmethod
    def _route_definition(raw: Mapping[str, Any]) -> dict[str, Any]:
        route_id = str(raw.get("route_id") or "").strip()
        display_name = str(raw.get("display_name") or "").strip()
        connector_id = str(raw.get("connector_id") or "").strip()
        path = str(raw.get("path") or "").strip()
        payload_format = str(raw.get("payload_format") or "agent-os").strip().lower()
        destination_value = raw.get("destination")
        destination = None if destination_value is None else str(destination_value).strip()
        categories_raw = raw.get("categories", ())
        if not isinstance(categories_raw, (list, tuple)):
            raise ValueError("notification route categories must be a list")
        categories = tuple(dict.fromkeys(str(item).strip() for item in categories_raw))
        if not _ROUTE_ID.fullmatch(route_id) or not _ROUTE_ID.fullmatch(connector_id):
            raise ValueError("notification route and connector IDs must be lowercase slugs")
        if not 1 <= len(display_name) <= 200:
            raise ValueError("notification route display name is required")
        if (
            not path.startswith("/") or path.startswith("//") or "\\" in path
            or any(character in path for character in "\r\n?#")
            or any(segment in {".", ".."} for segment in path.split("/"))
            or len(path) > 2_000
        ):
            raise ValueError("notification route path must be a bounded absolute path")
        if not categories or len(categories) > len(_CATEGORIES) or set(categories) - _CATEGORIES:
            raise ValueError("notification route categories are missing or unsupported")
        if payload_format not in _DELIVERY_FORMATS:
            raise ValueError("notification route payload format is unsupported")
        if destination is not None and not 1 <= len(destination) <= 256:
            raise ValueError("notification route destination is invalid")
        if payload_format == "slack" and destination is None:
            raise ValueError("Slack notification routes require a destination channel")
        return {
            "route_id": route_id,
            "display_name": display_name,
            "connector_id": connector_id,
            "path": path,
            "categories": list(categories),
            "payload_format": payload_format,
            "destination": destination,
        }

    @staticmethod
    def _route_record(row: Mapping[str, Any], *, duplicate: bool = False) -> Mapping[str, Any]:
        record = dict(row["definition"])
        record.update({
            "active": bool(row["active"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"].isoformat(),
            "disabled_by": row["disabled_by"],
            "disabled_at": (
                None if row["disabled_at"] is None else row["disabled_at"].isoformat()
            ),
            "disabled_reason": row["disabled_reason"],
            "duplicate": duplicate,
        })
        return record

    @contextmanager
    def _tenant_connection(self, tenant_id: str):
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_app"))
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": tenant_id},
                )
            yield connection

    def publish_notification(self, notification: Notification) -> bool:
        raw = notification.to_dict()
        fingerprint = notification_fingerprint(notification)
        key = and_(
            notifications.c.tenant_id == notification.tenant_id,
            notifications.c.notification_id == notification.notification_id,
        )
        try:
            with self._tenant_connection(notification.tenant_id) as connection:
                prior = connection.execute(
                    select(notifications.c.fingerprint).where(key)
                ).scalar_one_or_none()
                if prior is not None:
                    if prior != fingerprint:
                        raise ValueError("notification_id was reused with different content")
                    return False
                created_at = _parse_time(notification.created_at)
                connection.execute(insert(notifications).values(
                    tenant_id=notification.tenant_id,
                    notification_id=notification.notification_id,
                    run_id=notification.run_id,
                    category=notification.category.value,
                    recipient_ids=list(notification.recipient_ids),
                    subject=notification.subject,
                    body=notification.body,
                    source_id=notification.source_id,
                    correlation_id=notification.correlation_id,
                    payload=dict(notification.payload),
                    fingerprint=fingerprint,
                    record=raw,
                    created_at=created_at,
                ))
                connection.execute(insert(notification_recipients), [
                    {
                        "tenant_id": notification.tenant_id,
                        "notification_id": notification.notification_id,
                        "recipient_id": recipient_id,
                        "created_at": created_at,
                    }
                    for recipient_id in notification.recipient_ids
                ])
                route_rows = connection.execute(select(
                    notification_routes.c.route_id,
                    notification_routes.c.categories,
                ).where(and_(
                    notification_routes.c.tenant_id == notification.tenant_id,
                    notification_routes.c.active.is_(True),
                )).order_by(notification_routes.c.route_id).limit(33)).mappings().all()
                if len(route_rows) > 32:
                    raise RuntimeError("tenant notification route limit was exceeded")
                eligible_routes = [
                    row for row in route_rows
                    if notification.category.value in row["categories"]
                ]
                if eligible_routes:
                    connection.execute(insert(notification_deliveries), [{
                        "tenant_id": notification.tenant_id,
                        "delivery_id": "delivery-" + hashlib.sha256(
                            f"agent-os:notification-delivery:v1:{notification.notification_id}:"
                            f"{row['route_id']}".encode()
                        ).hexdigest(),
                        "notification_id": notification.notification_id,
                        "route_id": row["route_id"],
                        "status": "pending",
                        "attempts": 0,
                        "available_at": created_at,
                        "created_at": created_at,
                    } for row in eligible_routes])
        except IntegrityError as exc:
            # Another replica can win the same deterministic insert after our
            # initial read. Re-read after rollback and accept only byte-for-byte
            # equivalent semantic content; an ID collision still fails closed.
            with self._tenant_connection(notification.tenant_id) as connection:
                prior = connection.execute(
                    select(notifications.c.fingerprint).where(key)
                ).scalar_one_or_none()
            if prior is None:
                raise
            if prior != fingerprint:
                raise ValueError("notification_id was reused with different content") from exc
            return False
        return True

    def list_notifications(
        self,
        tenant_id: str,
        *,
        run_id: str | None = None,
        recipient_id: str | None = None,
        limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]:
        if not 1 <= limit <= 500:
            raise ValueError("notification limit must be 1..500")
        with self._tenant_connection(tenant_id) as connection:
            criteria = [notifications.c.tenant_id == tenant_id]
            if run_id is not None:
                criteria.append(notifications.c.run_id == run_id)
            source = notifications
            if recipient_id is not None:
                source = notifications.join(notification_recipients, and_(
                    notification_recipients.c.tenant_id == notifications.c.tenant_id,
                    notification_recipients.c.notification_id == notifications.c.notification_id,
                ))
                criteria.extend((
                    notification_recipients.c.tenant_id == tenant_id,
                    notification_recipients.c.recipient_id == recipient_id,
                ))
            rows = connection.execute(select(notifications.c.record).select_from(source).where(and_(
                *criteria,
            )).order_by(
                notifications.c.created_at.desc(),
                notifications.c.notification_id.desc(),
            ).limit(limit)).scalars().all()
        return tuple(dict(raw) for raw in rows)

    def register_notification_route(
        self,
        *,
        tenant_id: str,
        definition: Mapping[str, Any],
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        actor_id = actor_id.strip()
        idempotency_key = idempotency_key.strip()
        if not actor_id or not 8 <= len(idempotency_key) <= 200:
            raise ValueError("notification route actor and bounded idempotency key are required")
        record = self._route_definition(definition)
        fingerprint = hashlib.sha256(json.dumps(
            record, allow_nan=False, separators=(",", ":"), sort_keys=True,
        ).encode()).hexdigest()
        now = self._clock()
        values = {
            "tenant_id": tenant_id,
            "route_id": record["route_id"],
            "connector_id": record["connector_id"],
            "categories": record["categories"],
            "definition": record,
            "fingerprint": fingerprint,
            "active": True,
            "idempotency_key": idempotency_key,
            "created_by": actor_id,
            "created_at": now,
        }
        try:
            with self._tenant_connection(tenant_id) as connection:
                prior = connection.execute(select(notification_routes).where(and_(
                    notification_routes.c.tenant_id == tenant_id,
                    notification_routes.c.idempotency_key == idempotency_key,
                ))).mappings().one_or_none()
                if prior is not None:
                    if prior["fingerprint"] != fingerprint:
                        raise ValueError(
                            "notification route idempotency key was reused with another definition"
                        )
                    return self._route_record(prior, duplicate=True)
                by_id = connection.execute(select(notification_routes.c.route_id).where(and_(
                    notification_routes.c.tenant_id == tenant_id,
                    notification_routes.c.route_id == record["route_id"],
                ))).scalar_one_or_none()
                if by_id is not None:
                    raise ValueError("notification route ID already exists and routes are immutable")
                count = len(connection.execute(select(
                    notification_routes.c.route_id,
                ).where(notification_routes.c.tenant_id == tenant_id).limit(33)).all())
                if count >= 32:
                    raise ValueError("tenant notification route limit reached")
                connection.execute(insert(notification_routes).values(**values))
        except IntegrityError as exc:
            with self._tenant_connection(tenant_id) as connection:
                prior = connection.execute(select(notification_routes).where(and_(
                    notification_routes.c.tenant_id == tenant_id,
                    notification_routes.c.idempotency_key == idempotency_key,
                ))).mappings().one_or_none()
            if prior is None or prior["fingerprint"] != fingerprint:
                raise ValueError("notification route registration conflicted") from exc
            return self._route_record(prior, duplicate=True)
        return {
            **record,
            "active": True,
            "created_by": actor_id,
            "created_at": now.isoformat(),
            "disabled_by": None,
            "disabled_at": None,
            "disabled_reason": None,
            "duplicate": False,
        }

    def list_notification_routes(
        self, tenant_id: str,
    ) -> tuple[Mapping[str, Any], ...]:
        with self._tenant_connection(tenant_id) as connection:
            rows = connection.execute(select(notification_routes).where(
                notification_routes.c.tenant_id == tenant_id,
            ).order_by(notification_routes.c.route_id)).mappings().all()
        return tuple(self._route_record(row) for row in rows)

    def disable_notification_route(
        self,
        *,
        tenant_id: str,
        route_id: str,
        actor_id: str,
        reason: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None:
        actor_id = actor_id.strip()
        reason = reason.strip()
        idempotency_key = idempotency_key.strip()
        if not actor_id or not reason or not 8 <= len(idempotency_key) <= 200:
            raise ValueError("route disable actor, reason, and idempotency key are required")
        key = and_(
            notification_routes.c.tenant_id == tenant_id,
            notification_routes.c.route_id == route_id,
        )
        with self._tenant_connection(tenant_id) as connection:
            row = connection.execute(
                select(notification_routes).where(key).with_for_update()
            ).mappings().one_or_none()
            if row is None:
                return None
            if not row["active"]:
                if row["disable_idempotency_key"] != idempotency_key:
                    raise ValueError("notification route is already disabled by another decision")
                return self._route_record(row, duplicate=True)
            now = self._clock()
            connection.execute(update(notification_routes).where(key).values(
                active=False,
                disabled_by=actor_id,
                disabled_at=now,
                disabled_reason=reason,
                disable_idempotency_key=idempotency_key,
            ))
            connection.execute(update(notification_deliveries).where(and_(
                notification_deliveries.c.tenant_id == tenant_id,
                notification_deliveries.c.route_id == route_id,
                notification_deliveries.c.status.in_(("pending", "executing")),
            )).values(
                status="cancelled",
                lease_owner=None,
                lease_expires_at=None,
                last_error={
                    "type": "RouteDisabled",
                    "message": reason[:2_000],
                    "retryable": False,
                },
            ))
            updated = dict(row)
            updated.update({
                "active": False,
                "disabled_by": actor_id,
                "disabled_at": now,
                "disabled_reason": reason,
                "disable_idempotency_key": idempotency_key,
            })
        return self._route_record(updated)

    @staticmethod
    def _eligible_delivery(now: datetime):
        return or_(
            and_(
                notification_deliveries.c.status == "pending",
                notification_deliveries.c.available_at <= now,
            ),
            and_(
                notification_deliveries.c.status == "executing",
                notification_deliveries.c.lease_expires_at < now,
            ),
        )

    def claim_notification_delivery(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> NotificationDeliveryLease | None:
        if not worker_id.strip() or lease_seconds < 3:
            raise ValueError("delivery worker and lease of at least three seconds are required")
        now = self._clock()
        expires = now + timedelta(seconds=lease_seconds)
        with self._tenant_connection(tenant_id) as connection:
            row = connection.execute(select(notification_deliveries).where(and_(
                notification_deliveries.c.tenant_id == tenant_id,
                self._eligible_delivery(now),
            )).order_by(
                notification_deliveries.c.available_at,
                notification_deliveries.c.delivery_id,
            ).limit(1).with_for_update(skip_locked=True)).mappings().one_or_none()
            if row is None:
                return None
            changed = connection.execute(update(notification_deliveries).where(and_(
                notification_deliveries.c.tenant_id == tenant_id,
                notification_deliveries.c.delivery_id == row["delivery_id"],
                self._eligible_delivery(now),
            )).values(
                status="executing",
                attempts=int(row["attempts"]) + 1,
                lease_owner=worker_id,
                lease_expires_at=expires,
            )).rowcount
            if changed != 1:
                return None
            notification = connection.execute(select(notifications.c.record).where(and_(
                notifications.c.tenant_id == tenant_id,
                notifications.c.notification_id == row["notification_id"],
            ))).scalar_one()
            route = connection.execute(select(notification_routes.c.definition).where(and_(
                notification_routes.c.tenant_id == tenant_id,
                notification_routes.c.route_id == row["route_id"],
            ))).scalar_one()
        return NotificationDeliveryLease(
            tenant_id=tenant_id,
            delivery_id=str(row["delivery_id"]),
            notification=dict(notification),
            route=dict(route),
            worker_id=worker_id,
            attempt=int(row["attempts"]) + 1,
            lease_expires_at=expires.isoformat(),
        )

    def heartbeat_notification_delivery(
        self,
        tenant_id: str,
        delivery_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> bool:
        if lease_seconds < 3:
            raise ValueError("delivery lease must be at least three seconds")
        with self._tenant_connection(tenant_id) as connection:
            changed = connection.execute(update(notification_deliveries).where(and_(
                notification_deliveries.c.tenant_id == tenant_id,
                notification_deliveries.c.delivery_id == delivery_id,
                notification_deliveries.c.status == "executing",
                notification_deliveries.c.lease_owner == worker_id,
            )).values(
                lease_expires_at=self._clock() + timedelta(seconds=lease_seconds),
            )).rowcount
        return changed == 1

    def complete_notification_delivery(
        self,
        tenant_id: str,
        delivery_id: str,
        *,
        worker_id: str,
        result: Mapping[str, Any],
    ) -> bool:
        json.dumps(result, allow_nan=False)
        now = self._clock()
        with self._tenant_connection(tenant_id) as connection:
            changed = connection.execute(update(notification_deliveries).where(and_(
                notification_deliveries.c.tenant_id == tenant_id,
                notification_deliveries.c.delivery_id == delivery_id,
                notification_deliveries.c.status == "executing",
                notification_deliveries.c.lease_owner == worker_id,
            )).values(
                status="delivered",
                lease_owner=None,
                lease_expires_at=None,
                last_error=None,
                result=dict(result),
                delivered_at=now,
            )).rowcount
        return changed == 1

    def retry_notification_delivery(
        self,
        tenant_id: str,
        delivery_id: str,
        *,
        worker_id: str,
        error: Mapping[str, Any],
        delay_seconds: int,
    ) -> bool:
        if not 0 <= delay_seconds <= 86_400:
            raise ValueError("notification retry delay must be between 0 and 86400")
        json.dumps(error, allow_nan=False)
        with self._tenant_connection(tenant_id) as connection:
            changed = connection.execute(update(notification_deliveries).where(and_(
                notification_deliveries.c.tenant_id == tenant_id,
                notification_deliveries.c.delivery_id == delivery_id,
                notification_deliveries.c.status == "executing",
                notification_deliveries.c.lease_owner == worker_id,
            )).values(
                status="pending",
                available_at=self._clock() + timedelta(seconds=delay_seconds),
                lease_owner=None,
                lease_expires_at=None,
                last_error=dict(error),
            )).rowcount
        return changed == 1

    def fail_notification_delivery(
        self,
        tenant_id: str,
        delivery_id: str,
        *,
        worker_id: str,
        error: Mapping[str, Any],
    ) -> bool:
        json.dumps(error, allow_nan=False)
        with self._tenant_connection(tenant_id) as connection:
            changed = connection.execute(update(notification_deliveries).where(and_(
                notification_deliveries.c.tenant_id == tenant_id,
                notification_deliveries.c.delivery_id == delivery_id,
                notification_deliveries.c.status == "executing",
                notification_deliveries.c.lease_owner == worker_id,
            )).values(
                status="failed",
                lease_owner=None,
                lease_expires_at=None,
                last_error=dict(error),
            )).rowcount
        return changed == 1

    @staticmethod
    def _delivery_record(row: Mapping[str, Any], *, duplicate: bool = False) -> Mapping[str, Any]:
        return {
            "delivery_id": row["delivery_id"],
            "notification_id": row["notification_id"],
            "route_id": row["route_id"],
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "available_at": row["available_at"].isoformat(),
            "last_error": row["last_error"],
            "result": row["result"],
            "created_at": row["created_at"].isoformat(),
            "delivered_at": (
                None if row["delivered_at"] is None else row["delivered_at"].isoformat()
            ),
            "redriven_by": row["redriven_by"],
            "duplicate": duplicate,
        }

    def list_notification_deliveries(
        self, tenant_id: str, *, limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]:
        if not 1 <= limit <= 500:
            raise ValueError("notification delivery limit must be 1..500")
        with self._tenant_connection(tenant_id) as connection:
            rows = connection.execute(select(notification_deliveries).where(
                notification_deliveries.c.tenant_id == tenant_id,
            ).order_by(
                notification_deliveries.c.created_at.desc(),
                notification_deliveries.c.delivery_id.desc(),
            ).limit(limit)).mappings().all()
        return tuple(self._delivery_record(row) for row in rows)

    def redrive_notification_delivery(
        self,
        *,
        tenant_id: str,
        delivery_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None:
        actor_id = actor_id.strip()
        idempotency_key = idempotency_key.strip()
        if not actor_id or not 8 <= len(idempotency_key) <= 200:
            raise ValueError("delivery redrive actor and bounded idempotency key are required")
        key = and_(
            notification_deliveries.c.tenant_id == tenant_id,
            notification_deliveries.c.delivery_id == delivery_id,
        )
        with self._tenant_connection(tenant_id) as connection:
            row = connection.execute(
                select(notification_deliveries).where(key).with_for_update()
            ).mappings().one_or_none()
            if row is None:
                return None
            if row["redrive_idempotency_key"] == idempotency_key:
                return self._delivery_record(row, duplicate=True)
            if row["status"] != "failed":
                raise ValueError("only failed notification deliveries can be redriven")
            now = self._clock()
            connection.execute(update(notification_deliveries).where(key).values(
                status="pending",
                available_at=now,
                lease_owner=None,
                lease_expires_at=None,
                last_error=None,
                result=None,
                delivered_at=None,
                redrive_idempotency_key=idempotency_key,
                redriven_by=actor_id,
            ))
            updated = dict(row)
            updated.update({
                "status": "pending",
                "available_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
                "last_error": None,
                "result": None,
                "delivered_at": None,
                "redrive_idempotency_key": idempotency_key,
                "redriven_by": actor_id,
            })
        return self._delivery_record(updated)

    def close(self) -> None:
        self._engine.dispose()
