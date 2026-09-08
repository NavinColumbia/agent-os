"""SQL-backed, idempotent in-app notification ledger."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Mapping, Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    insert,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError

from agent_os.application.ports import NotificationStore
from agent_os.domain.notifications import Notification, notification_fingerprint
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


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class SQLNotificationStore(NotificationStore):
    def __init__(self, database_url: str, *, create_schema: bool = False) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        if create_schema:
            notification_metadata.create_all(self._engine)

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

    def close(self) -> None:
        self._engine.dispose()
