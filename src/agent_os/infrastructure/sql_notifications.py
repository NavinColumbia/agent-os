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

from agent_os.application.ports import (
    DecisionResponseLease,
    ExperienceEventPage,
    NotificationDeliveryLease,
    NotificationStore,
)
from agent_os.domain.notifications import (
    Notification,
    NotificationCategory,
    NotificationPreferences,
    notification_fingerprint,
)
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url
from agent_os.infrastructure.sql_experience_events import (
    SQLExperienceEventLog,
    experience_source_key,
)


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

notification_states = Table(
    "aos_v2_notification_states",
    notification_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("subject_id", String(256), primary_key=True),
    Column("notification_id", String(128), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("snoozed_until", DateTime(timezone=True), nullable=True),
    Column("version", Integer, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("idempotency_key", String(200), nullable=False),
    Column("updated_by", String(256), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "notification_id"],
        ["aos_v2_notifications.tenant_id", "aos_v2_notifications.notification_id"],
        ondelete="CASCADE",
    ),
)

Index(
    "aos_v2_notification_states_inbox_idx",
    notification_states.c.tenant_id,
    notification_states.c.subject_id,
    notification_states.c.status,
    notification_states.c.snoozed_until,
)

notification_preferences = Table(
    "aos_v2_notification_preferences",
    notification_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("subject_id", String(256), primary_key=True),
    Column("record", JSON, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("idempotency_key", String(200), nullable=False),
    Column("updated_by", String(256), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

decision_responses = Table(
    "aos_v2_decision_responses",
    notification_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("response_id", String(96), primary_key=True),
    Column("notification_id", String(128), nullable=False),
    Column("run_id", String(256), nullable=False),
    Column("correlation_id", String(256), nullable=False),
    Column("event_id", String(256), nullable=False),
    Column("response", JSON, nullable=False),
    Column("expected_version", Integer, nullable=False),
    Column("actor_id", String(256), nullable=False),
    Column("idempotency_key", String(200), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("total_attempts", Integer, nullable=False, default=0),
    Column("redrive_count", Integer, nullable=False, default=0),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("lease_owner", String(256), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("last_error", JSON, nullable=True),
    Column("result", JSON, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("redrive_idempotency_key", String(200), nullable=True),
    Column("redriven_by", String(256), nullable=True),
    UniqueConstraint("tenant_id", "notification_id"),
    UniqueConstraint("tenant_id", "idempotency_key"),
    ForeignKeyConstraint(
        ["tenant_id", "notification_id"],
        ["aos_v2_notifications.tenant_id", "aos_v2_notifications.notification_id"],
        ondelete="CASCADE",
    ),
)

Index(
    "aos_v2_decision_responses_ready_idx",
    decision_responses.c.tenant_id,
    decision_responses.c.status,
    decision_responses.c.available_at,
)

decision_response_redrives = Table(
    "aos_v2_decision_response_redrives",
    notification_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("notification_id", String(128), primary_key=True),
    Column("idempotency_key", String(200), primary_key=True),
    Column("response_id", String(96), nullable=False),
    Column("redrive_number", Integer, nullable=False),
    Column("actor_id", String(256), nullable=False),
    Column("previous_attempts", Integer, nullable=False),
    Column("total_attempts_at_redrive", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant_id", "notification_id", "redrive_number"),
    ForeignKeyConstraint(
        ["tenant_id", "response_id"],
        ["aos_v2_decision_responses.tenant_id", "aos_v2_decision_responses.response_id"],
        ondelete="CASCADE",
    ),
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
        self._experience_events = SQLExperienceEventLog(self._tenant_connection)
        if create_schema:
            notification_metadata.create_all(self._engine)
            self._experience_events.create_schema(self._engine)

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
        recipients_raw = raw.get("recipient_ids", ("human:ceo",))
        if not isinstance(categories_raw, (list, tuple)):
            raise ValueError("notification route categories must be a list")
        if not isinstance(recipients_raw, (list, tuple)):
            raise ValueError("notification route recipients must be a list")
        categories = tuple(dict.fromkeys(str(item).strip() for item in categories_raw))
        recipients = tuple(dict.fromkeys(str(item).strip() for item in recipients_raw))
        redaction_policy = str(raw.get("redaction_policy") or "summary").strip()
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
        if (
            not recipients or len(recipients) > 128
            or any(not recipient or len(recipient) > 256 for recipient in recipients)
        ):
            raise ValueError("notification route recipients are missing or invalid")
        if redaction_policy not in {"summary", "full"}:
            raise ValueError("notification route redaction policy is unsupported")
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
            "recipient_ids": list(recipients),
            "redaction_policy": redaction_policy,
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

    def _append_experience_event(
        self,
        connection,
        *,
        tenant_id: str,
        source_key: str,
        resource_type: str,
        resource_id: str,
        projection_revision: int,
        kind: str,
        audience_ids: tuple[str, ...],
        safe_summary: str,
        occurred_at: datetime,
        trace_id: str | None = None,
    ) -> int:
        return self._experience_events.append(
            connection,
            tenant_id=tenant_id,
            source_key=source_key,
            resource_type=resource_type,
            resource_id=resource_id,
            projection_revision=projection_revision,
            kind=kind,
            audience_ids=audience_ids,
            safe_summary=safe_summary,
            occurred_at=occurred_at,
            trace_id=trace_id,
        )

    def list_experience_events(
        self,
        tenant_id: str,
        *,
        after_sequence: int = 0,
        audience_ids: tuple[str, ...] | None = None,
        limit: int = 100,
    ) -> ExperienceEventPage:
        return self._experience_events.list(
            tenant_id,
            after_sequence=after_sequence,
            audience_ids=audience_ids,
            limit=limit,
        )

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
                    notification_routes.c.definition,
                ).where(and_(
                    notification_routes.c.tenant_id == notification.tenant_id,
                    notification_routes.c.active.is_(True),
                )).order_by(notification_routes.c.route_id).limit(33)).mappings().all()
                if len(route_rows) > 32:
                    raise RuntimeError("tenant notification route limit was exceeded")
                # Batched/deferred attention remains durable and visible in the
                # in-app decision digest, but must not escape through an
                # interrupting external route. Critical decisions explicitly
                # marked ``interrupt`` continue through the outbox normally.
                externally_interrupting = notification.payload.get(
                    "attention_disposition"
                ) not in {"batch", "defer"}
                eligible_routes = [
                    row for row in route_rows
                    if externally_interrupting
                    and notification.category.value in row["categories"]
                    and (
                        "*" in row["definition"].get("recipient_ids", ())
                        or bool(
                            set(notification.recipient_ids)
                            & set(row["definition"].get("recipient_ids", ("human:ceo",)))
                        )
                    )
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
                self._append_experience_event(
                    connection,
                    tenant_id=notification.tenant_id,
                    source_key=experience_source_key(
                        "notification:published", notification.notification_id,
                    ),
                    resource_type="notification",
                    resource_id=notification.notification_id,
                    projection_revision=1,
                    kind="notification.published",
                    audience_ids=notification.recipient_ids,
                    safe_summary="A new attention item is available.",
                    occurred_at=created_at,
                )
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
        before: tuple[datetime, str] | None = None,
        limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]:
        if not 1 <= limit <= 501:
            raise ValueError("notification limit must be 1..501")
        if before is not None:
            before_created_at, before_notification_id = before
            if (
                before_created_at.tzinfo is None
                or not 1 <= len(before_notification_id) <= 128
                or "\0" in before_notification_id
            ):
                raise ValueError("notification cursor is invalid")
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
            if before is not None:
                criteria.append(or_(
                    notifications.c.created_at < before_created_at,
                    and_(
                        notifications.c.created_at == before_created_at,
                        notifications.c.notification_id < before_notification_id,
                    ),
                ))
            rows = connection.execute(select(notifications.c.record).select_from(source).where(and_(
                *criteria,
            )).order_by(
                notifications.c.created_at.desc(),
                notifications.c.notification_id.desc(),
            ).limit(limit)).scalars().all()
        return tuple(dict(raw) for raw in rows)

    def get_notification(
        self, tenant_id: str, notification_id: str,
    ) -> Mapping[str, Any] | None:
        with self._tenant_connection(tenant_id) as connection:
            raw = connection.execute(select(notifications.c.record).where(and_(
                notifications.c.tenant_id == tenant_id,
                notifications.c.notification_id == notification_id,
            ))).scalar_one_or_none()
        return None if raw is None else dict(raw)

    def list_notification_states(
        self,
        tenant_id: str,
        *,
        subject_id: str,
        notification_ids: tuple[str, ...],
    ) -> Mapping[str, Mapping[str, Any]]:
        """Return one person's presentation state without mutating ledger truth."""

        if not subject_id.strip() or len(subject_id) > 256:
            raise ValueError("notification state subject is required")
        bounded_ids = tuple(dict.fromkeys(notification_ids))
        if len(bounded_ids) > 500:
            raise ValueError("notification state lookup exceeds 500 items")
        if not bounded_ids:
            return {}
        with self._tenant_connection(tenant_id) as connection:
            rows = connection.execute(select(notification_states).where(and_(
                notification_states.c.tenant_id == tenant_id,
                notification_states.c.subject_id == subject_id,
                notification_states.c.notification_id.in_(bounded_ids),
            ))).mappings().all()
        return {
            str(row["notification_id"]): self._state_record(row)
            for row in rows
        }

    @staticmethod
    def _state_record(
        row: Mapping[str, Any], *, duplicate: bool = False,
    ) -> Mapping[str, Any]:
        return {
            "notification_id": row["notification_id"],
            "subject_id": row["subject_id"],
            "status": row["status"],
            "snoozed_until": (
                None if row["snoozed_until"] is None
                else row["snoozed_until"].isoformat()
            ),
            "version": int(row["version"]),
            "updated_at": row["updated_at"].isoformat(),
            "duplicate": duplicate,
        }

    def set_notification_state(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        notification_id: str,
        status: str,
        snoozed_until: str | None,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        subject_id = subject_id.strip()
        actor_id = actor_id.strip()
        idempotency_key = idempotency_key.strip()
        if not subject_id or not actor_id or not 8 <= len(idempotency_key) <= 200:
            raise ValueError("notification state subject, actor, and idempotency key are required")
        if status not in {"unread", "read", "dismissed", "snoozed", "resolved"}:
            raise ValueError("notification state is unsupported")
        snooze_time = None if snoozed_until is None else _parse_time(snoozed_until)
        if (status == "snoozed") != (snooze_time is not None):
            raise ValueError("snoozed state requires an expiry and other states forbid one")
        now = self._clock()
        if snooze_time is not None and snooze_time <= now:
            raise ValueError("notification snooze must end in the future")
        semantic = {"status": status, "snoozed_until": snoozed_until}
        fingerprint = hashlib.sha256(json.dumps(
            semantic, separators=(",", ":"), sort_keys=True,
        ).encode()).hexdigest()
        key = and_(
            notification_states.c.tenant_id == tenant_id,
            notification_states.c.subject_id == subject_id,
            notification_states.c.notification_id == notification_id,
        )
        with self._tenant_connection(tenant_id) as connection:
            exists = connection.execute(select(notifications.c.notification_id).where(and_(
                notifications.c.tenant_id == tenant_id,
                notifications.c.notification_id == notification_id,
            ))).scalar_one_or_none()
            if exists is None:
                raise LookupError("notification not found")
            prior = connection.execute(
                select(notification_states).where(key).with_for_update()
            ).mappings().one_or_none()
            if prior is not None and prior["idempotency_key"] == idempotency_key:
                if prior["fingerprint"] != fingerprint:
                    raise ValueError("notification state idempotency key was reused")
                return self._state_record(prior, duplicate=True)
            version = 1 if prior is None else int(prior["version"]) + 1
            values = {
                "tenant_id": tenant_id,
                "subject_id": subject_id,
                "notification_id": notification_id,
                "status": status,
                "snoozed_until": snooze_time,
                "version": version,
                "fingerprint": fingerprint,
                "idempotency_key": idempotency_key,
                "updated_by": actor_id,
                "updated_at": now,
            }
            if prior is None:
                connection.execute(insert(notification_states).values(**values))
            else:
                connection.execute(update(notification_states).where(key).values(**values))
            self._append_experience_event(
                connection,
                tenant_id=tenant_id,
                source_key=experience_source_key(
                    "notification:state", subject_id, notification_id, idempotency_key,
                ),
                resource_type="notification_state",
                resource_id=notification_id,
                projection_revision=version,
                kind="notification.state.changed",
                audience_ids=(subject_id,),
                safe_summary="Your attention-item state changed.",
                occurred_at=now,
            )
        return self._state_record(values)

    def get_notification_preferences(
        self, tenant_id: str, *, subject_id: str,
    ) -> Mapping[str, Any]:
        if not subject_id.strip() or len(subject_id) > 256:
            raise ValueError("notification preference subject is required")
        with self._tenant_connection(tenant_id) as connection:
            row = connection.execute(select(notification_preferences).where(and_(
                notification_preferences.c.tenant_id == tenant_id,
                notification_preferences.c.subject_id == subject_id,
            ))).mappings().one_or_none()
        if row is None:
            return {
                **NotificationPreferences(tenant_id, subject_id).to_dict(),
                "version": 0,
                "updated_at": None,
                "duplicate": False,
            }
        return {
            **dict(row["record"]),
            "version": int(row["version"]),
            "updated_at": row["updated_at"].isoformat(),
            "duplicate": False,
        }

    def set_notification_preferences(
        self,
        preferences: NotificationPreferences,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        actor_id = actor_id.strip()
        idempotency_key = idempotency_key.strip()
        if not actor_id or not 8 <= len(idempotency_key) <= 200:
            raise ValueError("notification preference actor and idempotency key are required")
        record = preferences.to_dict()
        fingerprint = hashlib.sha256(json.dumps(
            record, separators=(",", ":"), sort_keys=True,
        ).encode()).hexdigest()
        key = and_(
            notification_preferences.c.tenant_id == preferences.tenant_id,
            notification_preferences.c.subject_id == preferences.subject_id,
        )
        now = self._clock()
        with self._tenant_connection(preferences.tenant_id) as connection:
            prior = connection.execute(
                select(notification_preferences).where(key).with_for_update()
            ).mappings().one_or_none()
            if prior is not None and prior["idempotency_key"] == idempotency_key:
                if prior["fingerprint"] != fingerprint:
                    raise ValueError("notification preference idempotency key was reused")
                return {
                    **dict(prior["record"]),
                    "version": int(prior["version"]),
                    "updated_at": prior["updated_at"].isoformat(),
                    "duplicate": True,
                }
            version = 1 if prior is None else int(prior["version"]) + 1
            values = {
                "tenant_id": preferences.tenant_id,
                "subject_id": preferences.subject_id,
                "record": record,
                "fingerprint": fingerprint,
                "version": version,
                "idempotency_key": idempotency_key,
                "updated_by": actor_id,
                "updated_at": now,
            }
            if prior is None:
                connection.execute(insert(notification_preferences).values(**values))
            else:
                connection.execute(update(notification_preferences).where(key).values(**values))
            self._append_experience_event(
                connection,
                tenant_id=preferences.tenant_id,
                source_key=experience_source_key(
                    "notification:preferences", preferences.subject_id, idempotency_key,
                ),
                resource_type="notification_preferences",
                resource_id=preferences.subject_id,
                projection_revision=version,
                kind="notification.preferences.changed",
                audience_ids=(preferences.subject_id,),
                safe_summary="Your attention preferences changed.",
                occurred_at=now,
            )
        return {
            **record,
            "version": version,
            "updated_at": now.isoformat(),
            "duplicate": False,
        }

    @staticmethod
    def _decision_record(
        row: Mapping[str, Any], *, duplicate: bool = False,
    ) -> Mapping[str, Any]:
        return {
            "response_id": row["response_id"],
            "notification_id": row["notification_id"],
            "run_id": row["run_id"],
            "correlation_id": row["correlation_id"],
            "response": dict(row["response"]),
            "expected_version": int(row["expected_version"]),
            "actor_id": row["actor_id"],
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "total_attempts": int(row["total_attempts"]),
            "redrive_count": int(row["redrive_count"]),
            "last_error": row["last_error"],
            "result": row["result"],
            "created_at": row["created_at"].isoformat(),
            "completed_at": (
                None if row["completed_at"] is None else row["completed_at"].isoformat()
            ),
            "redriven_by": row["redriven_by"],
            "duplicate": duplicate,
        }

    @staticmethod
    def _decision_ids(tenant_id: str, notification_id: str) -> tuple[str, str]:
        material = f"agent-os:decision-response:v1:{tenant_id}:{notification_id}"
        digest = hashlib.sha256(material.encode()).hexdigest()
        return f"decision-{digest}", f"decision-event-{digest}"

    def admit_decision_response(
        self,
        *,
        tenant_id: str,
        notification_id: str,
        run_id: str,
        correlation_id: str,
        response: Mapping[str, Any],
        expected_version: int,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        actor_id = actor_id.strip()
        idempotency_key = idempotency_key.strip()
        if (
            not tenant_id.strip() or not notification_id.strip() or not run_id.strip()
            or not correlation_id.strip() or not actor_id
            or not 8 <= len(idempotency_key) <= 200 or expected_version < 0
        ):
            raise ValueError("decision response identity and version are invalid")
        if not 1 <= len(response) <= 32 or any(
            not isinstance(key, str) or not 1 <= len(key) <= 128 for key in response
        ):
            raise ValueError("decision response must contain 1..32 bounded fields")
        try:
            encoded_response = json.dumps(
                response, allow_nan=False, ensure_ascii=False,
                separators=(",", ":"), sort_keys=True,
            ).encode()
        except (TypeError, ValueError) as exc:
            raise ValueError("decision response must contain JSON-compatible primitives") from exc
        if len(encoded_response) > 64 * 1024:
            raise ValueError("decision response exceeds 64 KiB")
        semantic = {
            "tenant_id": tenant_id,
            "notification_id": notification_id,
            "run_id": run_id,
            "correlation_id": correlation_id,
            "response": dict(response),
            "actor_id": actor_id,
        }
        fingerprint = hashlib.sha256(json.dumps(
            semantic, allow_nan=False, ensure_ascii=False,
            separators=(",", ":"), sort_keys=True,
        ).encode()).hexdigest()
        response_id, event_id = self._decision_ids(tenant_id, notification_id)
        now = self._clock()
        values = {
            "tenant_id": tenant_id,
            "response_id": response_id,
            "notification_id": notification_id,
            "run_id": run_id,
            "correlation_id": correlation_id,
            "event_id": event_id,
            "response": dict(response),
            "expected_version": expected_version,
            "actor_id": actor_id,
            "idempotency_key": idempotency_key,
            "fingerprint": fingerprint,
            "status": "pending",
            "attempts": 0,
            "total_attempts": 0,
            "redrive_count": 0,
            "available_at": now,
            "lease_owner": None,
            "lease_expires_at": None,
            "last_error": None,
            "result": None,
            "created_at": now,
            "completed_at": None,
            "redrive_idempotency_key": None,
            "redriven_by": None,
        }
        prior_filter = and_(
            decision_responses.c.tenant_id == tenant_id,
            or_(
                decision_responses.c.notification_id == notification_id,
                decision_responses.c.idempotency_key == idempotency_key,
            ),
        )
        try:
            with self._tenant_connection(tenant_id) as connection:
                notification = connection.execute(select(
                    notifications.c.run_id,
                    notifications.c.category,
                    notifications.c.correlation_id,
                ).where(and_(
                    notifications.c.tenant_id == tenant_id,
                    notifications.c.notification_id == notification_id,
                ))).mappings().one_or_none()
                if notification is None:
                    raise LookupError("notification not found")
                if (
                    notification["category"] != NotificationCategory.HUMAN_ACTION_REQUIRED.value
                    or notification["run_id"] != run_id
                    or notification["correlation_id"] != correlation_id
                ):
                    raise ValueError("notification is not the requested human decision")
                priors = connection.execute(
                    select(decision_responses).where(prior_filter).limit(2).with_for_update()
                ).mappings().all()
                if len(priors) > 1:
                    raise ValueError("decision notification and idempotency key conflict")
                prior = None if not priors else priors[0]
                if prior is not None:
                    if prior["fingerprint"] != fingerprint:
                        raise ValueError("human decision was already answered differently")
                    return self._decision_record(prior, duplicate=True)
                connection.execute(insert(decision_responses).values(**values))
                self._append_experience_event(
                    connection,
                    tenant_id=tenant_id,
                    source_key=experience_source_key("decision:admitted", response_id),
                    resource_type="decision_response",
                    resource_id=notification_id,
                    projection_revision=1,
                    kind="decision.response.admitted",
                    audience_ids=(actor_id,),
                    safe_summary="Your decision response was safely recorded.",
                    occurred_at=now,
                )
        except IntegrityError as exc:
            with self._tenant_connection(tenant_id) as connection:
                priors = connection.execute(
                    select(decision_responses).where(prior_filter).limit(2)
                ).mappings().all()
            prior = priors[0] if len(priors) == 1 else None
            if prior is None or prior["fingerprint"] != fingerprint:
                raise ValueError("human decision response conflicted") from exc
            return self._decision_record(prior, duplicate=True)
        return self._decision_record(values)

    def get_decision_response(
        self, tenant_id: str, *, notification_id: str,
    ) -> Mapping[str, Any] | None:
        with self._tenant_connection(tenant_id) as connection:
            row = connection.execute(select(decision_responses).where(and_(
                decision_responses.c.tenant_id == tenant_id,
                decision_responses.c.notification_id == notification_id,
            ))).mappings().one_or_none()
        return None if row is None else self._decision_record(row)

    def list_decision_responses(
        self, tenant_id: str, *, notification_ids: tuple[str, ...],
    ) -> Mapping[str, Mapping[str, Any]]:
        bounded = tuple(dict.fromkeys(notification_ids))
        if len(bounded) > 500:
            raise ValueError("decision response lookup exceeds 500 items")
        if not bounded:
            return {}
        with self._tenant_connection(tenant_id) as connection:
            rows = connection.execute(select(decision_responses).where(and_(
                decision_responses.c.tenant_id == tenant_id,
                decision_responses.c.notification_id.in_(bounded),
            ))).mappings().all()
        return {
            str(row["notification_id"]): self._decision_record(row)
            for row in rows
        }

    @staticmethod
    def _eligible_decision_response(now: datetime):
        return or_(
            and_(
                decision_responses.c.status == "pending",
                decision_responses.c.available_at <= now,
            ),
            and_(
                decision_responses.c.status == "executing",
                decision_responses.c.lease_expires_at < now,
            ),
        )

    def claim_decision_response(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> DecisionResponseLease | None:
        if not worker_id.strip() or lease_seconds < 3:
            raise ValueError("decision worker and lease of at least three seconds are required")
        now = self._clock()
        expires = now + timedelta(seconds=lease_seconds)
        with self._tenant_connection(tenant_id) as connection:
            row = connection.execute(select(decision_responses).where(and_(
                decision_responses.c.tenant_id == tenant_id,
                self._eligible_decision_response(now),
            )).order_by(
                decision_responses.c.available_at,
                decision_responses.c.response_id,
            ).limit(1).with_for_update(skip_locked=True)).mappings().one_or_none()
            if row is None:
                return None
            changed = connection.execute(update(decision_responses).where(and_(
                decision_responses.c.tenant_id == tenant_id,
                decision_responses.c.response_id == row["response_id"],
                self._eligible_decision_response(now),
            )).values(
                status="executing",
                attempts=int(row["attempts"]) + 1,
                total_attempts=int(row["total_attempts"]) + 1,
                lease_owner=worker_id,
                lease_expires_at=expires,
            )).rowcount
            if changed != 1:
                return None
        return DecisionResponseLease(
            tenant_id=tenant_id,
            response_id=str(row["response_id"]),
            notification_id=str(row["notification_id"]),
            run_id=str(row["run_id"]),
            correlation_id=str(row["correlation_id"]),
            event_id=str(row["event_id"]),
            response=dict(row["response"]),
            expected_version=int(row["expected_version"]),
            actor_id=str(row["actor_id"]),
            worker_id=worker_id,
            attempt=int(row["attempts"]) + 1,
            lease_expires_at=expires.isoformat(),
        )

    def rebase_decision_response(
        self,
        tenant_id: str,
        response_id: str,
        *,
        worker_id: str,
        expected_version: int,
        error: Mapping[str, Any],
    ) -> bool:
        if expected_version < 0:
            raise ValueError("decision response version is invalid")
        json.dumps(error, allow_nan=False)
        with self._tenant_connection(tenant_id) as connection:
            changed = connection.execute(update(decision_responses).where(and_(
                decision_responses.c.tenant_id == tenant_id,
                decision_responses.c.response_id == response_id,
                decision_responses.c.status == "executing",
                decision_responses.c.lease_owner == worker_id,
            )).values(
                status="pending",
                expected_version=expected_version,
                available_at=self._clock(),
                lease_owner=None,
                lease_expires_at=None,
                last_error=dict(error),
            )).rowcount
        return changed == 1

    def complete_decision_response(
        self,
        tenant_id: str,
        response_id: str,
        *,
        worker_id: str,
        outcome: str,
        result: Mapping[str, Any],
    ) -> bool:
        if outcome not in {"applied", "superseded"}:
            raise ValueError("decision response outcome is invalid")
        json.dumps(result, allow_nan=False)
        now = self._clock()
        with self._tenant_connection(tenant_id) as connection:
            row = connection.execute(select(decision_responses).where(and_(
                decision_responses.c.tenant_id == tenant_id,
                decision_responses.c.response_id == response_id,
                decision_responses.c.status == "executing",
                decision_responses.c.lease_owner == worker_id,
            )).with_for_update()).mappings().one_or_none()
            if row is None:
                return False
            connection.execute(update(decision_responses).where(and_(
                decision_responses.c.tenant_id == tenant_id,
                decision_responses.c.response_id == response_id,
            )).values(
                status=outcome,
                lease_owner=None,
                lease_expires_at=None,
                last_error=None,
                result=dict(result),
                completed_at=now,
            ))
            state_key = and_(
                notification_states.c.tenant_id == tenant_id,
                notification_states.c.subject_id == row["actor_id"],
                notification_states.c.notification_id == row["notification_id"],
            )
            prior_state = connection.execute(
                select(notification_states).where(state_key).with_for_update()
            ).mappings().one_or_none()
            state_fingerprint = hashlib.sha256(
                b'{"snoozed_until":null,"status":"resolved"}'
            ).hexdigest()
            state_values = {
                "tenant_id": tenant_id,
                "subject_id": row["actor_id"],
                "notification_id": row["notification_id"],
                "status": "resolved",
                "snoozed_until": None,
                "version": 1 if prior_state is None else int(prior_state["version"]) + 1,
                "fingerprint": state_fingerprint,
                "idempotency_key": f"resolve-{response_id}",
                "updated_by": row["actor_id"],
                "updated_at": now,
            }
            if prior_state is None:
                connection.execute(insert(notification_states).values(**state_values))
            else:
                connection.execute(update(notification_states).where(state_key).values(**state_values))
            self._append_experience_event(
                connection,
                tenant_id=tenant_id,
                source_key=experience_source_key(
                    "decision:completed", response_id, outcome,
                ),
                resource_type="decision_response",
                resource_id=str(row["notification_id"]),
                projection_revision=int(row["redrive_count"]) + 2,
                kind=f"decision.response.{outcome}",
                audience_ids=(str(row["actor_id"]),),
                safe_summary="Your decision response finished processing.",
                occurred_at=now,
            )
        return True

    def retry_decision_response(
        self,
        tenant_id: str,
        response_id: str,
        *,
        worker_id: str,
        error: Mapping[str, Any],
        delay_seconds: int,
    ) -> bool:
        if not 0 <= delay_seconds <= 86_400:
            raise ValueError("decision retry delay must be between 0 and 86400")
        json.dumps(error, allow_nan=False)
        with self._tenant_connection(tenant_id) as connection:
            changed = connection.execute(update(decision_responses).where(and_(
                decision_responses.c.tenant_id == tenant_id,
                decision_responses.c.response_id == response_id,
                decision_responses.c.status == "executing",
                decision_responses.c.lease_owner == worker_id,
            )).values(
                status="pending",
                available_at=self._clock() + timedelta(seconds=delay_seconds),
                lease_owner=None,
                lease_expires_at=None,
                last_error=dict(error),
            )).rowcount
        return changed == 1

    def fail_decision_response(
        self,
        tenant_id: str,
        response_id: str,
        *,
        worker_id: str,
        error: Mapping[str, Any],
    ) -> bool:
        json.dumps(error, allow_nan=False)
        with self._tenant_connection(tenant_id) as connection:
            changed = connection.execute(update(decision_responses).where(and_(
                decision_responses.c.tenant_id == tenant_id,
                decision_responses.c.response_id == response_id,
                decision_responses.c.status == "executing",
                decision_responses.c.lease_owner == worker_id,
            )).values(
                status="failed",
                lease_owner=None,
                lease_expires_at=None,
                last_error=dict(error),
            )).rowcount
        return changed == 1

    def redrive_decision_response(
        self,
        *,
        tenant_id: str,
        notification_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None:
        """Start a fresh bounded attempt cycle for durable intent that exhausted retries."""

        actor_id = actor_id.strip()
        idempotency_key = idempotency_key.strip()
        if not actor_id or not 8 <= len(idempotency_key) <= 200:
            raise ValueError("decision redrive actor and bounded idempotency key are required")
        key = and_(
            decision_responses.c.tenant_id == tenant_id,
            decision_responses.c.notification_id == notification_id,
        )
        with self._tenant_connection(tenant_id) as connection:
            row = connection.execute(
                select(decision_responses).where(key).with_for_update()
            ).mappings().one_or_none()
            if row is None:
                return None
            prior_redrive = connection.execute(select(
                decision_response_redrives.c.notification_id,
            ).where(and_(
                decision_response_redrives.c.tenant_id == tenant_id,
                decision_response_redrives.c.notification_id == notification_id,
                decision_response_redrives.c.idempotency_key == idempotency_key,
            ))).scalar_one_or_none()
            if prior_redrive is not None:
                return self._decision_record(row, duplicate=True)
            if row["status"] != "failed":
                raise ValueError("only failed decision responses can be redriven")
            now = self._clock()
            redrive_number = int(row["redrive_count"]) + 1
            connection.execute(insert(decision_response_redrives).values(
                tenant_id=tenant_id,
                notification_id=notification_id,
                idempotency_key=idempotency_key,
                response_id=row["response_id"],
                redrive_number=redrive_number,
                actor_id=actor_id,
                previous_attempts=int(row["attempts"]),
                total_attempts_at_redrive=int(row["total_attempts"]),
                created_at=now,
            ))
            values = {
                "status": "pending",
                "attempts": 0,
                "available_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
                "last_error": None,
                "result": None,
                "completed_at": None,
                "redrive_count": redrive_number,
                "redrive_idempotency_key": idempotency_key,
                "redriven_by": actor_id,
            }
            connection.execute(update(decision_responses).where(key).values(**values))
            updated = dict(row)
            updated.update(values)
        return self._decision_record(updated)

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
                self._append_experience_event(
                    connection,
                    tenant_id=tenant_id,
                    source_key=experience_source_key(
                        "notification:route:registered", record["route_id"],
                    ),
                    resource_type="notification_route",
                    resource_id=record["route_id"],
                    projection_revision=1,
                    kind="notification.route.registered",
                    audience_ids=("tenant:members",),
                    safe_summary="An external notification route was registered.",
                    occurred_at=now,
                )
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
            self._append_experience_event(
                connection,
                tenant_id=tenant_id,
                source_key=experience_source_key(
                    "notification:route:disabled", route_id, idempotency_key,
                ),
                resource_type="notification_route",
                resource_id=route_id,
                projection_revision=2,
                kind="notification.route.disabled",
                audience_ids=("tenant:members",),
                safe_summary="An external notification route was disabled.",
                occurred_at=now,
            )
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
