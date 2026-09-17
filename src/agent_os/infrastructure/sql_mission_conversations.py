"""Durable mission-scoped conversation with immutable, idempotent messages."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    insert,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError

from agent_os.application.ports import MissionConversationStore
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url
from agent_os.infrastructure.sql_experience_events import (
    SQLExperienceEventLog,
    experience_source_key,
)


mission_conversation_metadata = MetaData()
MISSION_MESSAGE_CHANNELS = frozenset({"shared", "internal"})
MISSION_MESSAGE_KINDS = frozenset({"comment", "question", "update"})

mission_messages = Table(
    "aos_v2_mission_messages",
    mission_conversation_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("mission_id", String(256), primary_key=True),
    Column("message_id", String(96), primary_key=True),
    Column("sender_id", String(255), nullable=False),
    Column("sender_persona", String(32), nullable=False),
    Column("channel", String(16), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("body", Text, nullable=False),
    Column("reply_to_message_id", String(96), nullable=True),
    Column("idempotency_key", String(200), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "tenant_id", "mission_id", "sender_id", "idempotency_key",
        name="uq_aos_v2_mission_messages_idempotency",
    ),
)

Index(
    "aos_v2_mission_messages_timeline_idx",
    mission_messages.c.tenant_id,
    mission_messages.c.mission_id,
    mission_messages.c.created_at.desc(),
    mission_messages.c.message_id.desc(),
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _message_id(tenant_id: str, mission_id: str, sender_id: str, key: str) -> str:
    material = f"agent-os:mission-message:v1:{tenant_id}:{mission_id}:{sender_id}:{key}"
    return "message-" + hashlib.sha256(material.encode()).hexdigest()


def _fingerprint(
    *, channel: str, kind: str, body: str, reply_to_message_id: str | None,
) -> str:
    encoded = json.dumps({
        "channel": channel,
        "kind": kind,
        "body": body,
        "reply_to_message_id": reply_to_message_id,
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


class SQLMissionConversationStore(MissionConversationStore):
    def __init__(self, database_url: str, *, create_schema: bool = False, clock=None) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._experience_events = SQLExperienceEventLog(
            lambda tenant_id: self._connection(tenant_id),
        )
        if create_schema:
            mission_conversation_metadata.create_all(self._engine)
            self._experience_events.create_schema(self._engine)

    @contextmanager
    def _connection(self, tenant_id: str):
        if not 1 <= len(tenant_id.strip()) <= 128:
            raise ValueError("mission conversation tenant is invalid")
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_app"))
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :value, true)"),
                    {"value": tenant_id},
                )
            yield connection

    @staticmethod
    def _identity(mission_id: str, sender_id: str) -> tuple[str, str]:
        mission_id = mission_id.strip()
        sender_id = sender_id.strip()
        if (
            not 1 <= len(mission_id) <= 256
            or not 1 <= len(sender_id) <= 255
            or "\0" in mission_id
            or "\0" in sender_id
        ):
            raise ValueError("mission message identity is invalid")
        return mission_id, sender_id

    @staticmethod
    def _record(row: Mapping[str, Any], *, duplicate: bool) -> Mapping[str, Any]:
        return {
            "message_id": row["message_id"],
            "mission_id": row["mission_id"],
            "sender_id": row["sender_id"],
            "sender_persona": row["sender_persona"],
            "channel": row["channel"],
            "kind": row["kind"],
            "body": row["body"],
            "reply_to_message_id": row["reply_to_message_id"],
            "created_at": _utc(row["created_at"]).isoformat(),
            "duplicate": duplicate,
        }

    def list_messages(
        self,
        tenant_id: str,
        mission_id: str,
        *,
        include_internal: bool,
        limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]:
        mission_id, _ = self._identity(mission_id, "conversation-reader")
        if not 1 <= limit <= 500:
            raise ValueError("mission conversation limit is invalid")
        predicate = and_(
            mission_messages.c.tenant_id == tenant_id,
            mission_messages.c.mission_id == mission_id,
        )
        if not include_internal:
            predicate = and_(predicate, mission_messages.c.channel == "shared")
        with self._connection(tenant_id) as connection:
            rows = connection.execute(
                select(mission_messages).where(predicate).order_by(
                    mission_messages.c.created_at.desc(),
                    mission_messages.c.message_id.desc(),
                ).limit(limit)
            ).mappings().all()
        return tuple(
            self._record(row, duplicate=False) for row in reversed(rows)
        )

    def append_message(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        sender_id: str,
        sender_persona: str,
        channel: str,
        kind: str,
        body: str,
        reply_to_message_id: str | None,
        audience_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        mission_id, sender_id = self._identity(mission_id, sender_id)
        sender_persona = sender_persona.strip()
        channel = channel.strip()
        kind = kind.strip()
        body = body.strip()
        idempotency_key = idempotency_key.strip()
        reply_to_message_id = (
            None if reply_to_message_id is None else reply_to_message_id.strip()
        )
        audience_ids = tuple(dict.fromkeys(
            value.strip() for value in audience_ids if value.strip()
        ))
        if not 1 <= len(sender_persona) <= 32:
            raise ValueError("mission message sender persona is invalid")
        if channel not in MISSION_MESSAGE_CHANNELS:
            raise ValueError("mission message channel must be shared or internal")
        if kind not in MISSION_MESSAGE_KINDS:
            raise ValueError("mission message kind must be comment, question, or update")
        if not 1 <= len(body) <= 8_000 or "\0" in body:
            raise ValueError("mission message body must contain 1 to 8000 characters")
        if not 8 <= len(idempotency_key) <= 200:
            raise ValueError("mission message idempotency key is invalid")
        if reply_to_message_id is not None and not 1 <= len(reply_to_message_id) <= 96:
            raise ValueError("mission message reply target is invalid")
        if not audience_ids or len(audience_ids) > 128 or any(
            len(value) > 256 for value in audience_ids
        ):
            raise ValueError("mission message experience audience is invalid")

        message_id = _message_id(tenant_id, mission_id, sender_id, idempotency_key)
        fingerprint = _fingerprint(
            channel=channel,
            kind=kind,
            body=body,
            reply_to_message_id=reply_to_message_id,
        )
        now = _utc(self._clock())
        values = {
            "tenant_id": tenant_id,
            "mission_id": mission_id,
            "message_id": message_id,
            "sender_id": sender_id,
            "sender_persona": sender_persona,
            "channel": channel,
            "kind": kind,
            "body": body,
            "reply_to_message_id": reply_to_message_id,
            "idempotency_key": idempotency_key,
            "fingerprint": fingerprint,
            "created_at": now,
        }
        try:
            with self._connection(tenant_id) as connection:
                prior = connection.execute(select(mission_messages).where(and_(
                    mission_messages.c.tenant_id == tenant_id,
                    mission_messages.c.mission_id == mission_id,
                    mission_messages.c.sender_id == sender_id,
                    mission_messages.c.idempotency_key == idempotency_key,
                ))).mappings().one_or_none()
                if prior is not None:
                    if prior["fingerprint"] != fingerprint:
                        raise ValueError(
                            "mission message idempotency key was reused with different content"
                        )
                    return self._record(prior, duplicate=True)
                if reply_to_message_id is not None:
                    parent = connection.execute(select(
                        mission_messages.c.message_id,
                    ).where(and_(
                        mission_messages.c.tenant_id == tenant_id,
                        mission_messages.c.mission_id == mission_id,
                        mission_messages.c.message_id == reply_to_message_id,
                    ))).scalar_one_or_none()
                    if parent is None:
                        raise ValueError("mission message reply target does not exist")
                connection.execute(insert(mission_messages).values(**values))
                self._experience_events.append(
                    connection,
                    tenant_id=tenant_id,
                    source_key=experience_source_key("mission.message.appended", message_id),
                    resource_type="mission_message",
                    resource_id=message_id,
                    projection_revision=1,
                    kind="mission.message.appended",
                    audience_ids=audience_ids,
                    safe_summary=(
                        "A mission participant asked a question."
                        if kind == "question" else "A mission participant posted an update."
                    ),
                    occurred_at=now,
                )
        except IntegrityError as exc:
            with self._connection(tenant_id) as connection:
                prior = connection.execute(select(mission_messages).where(and_(
                    mission_messages.c.tenant_id == tenant_id,
                    mission_messages.c.mission_id == mission_id,
                    mission_messages.c.sender_id == sender_id,
                    mission_messages.c.idempotency_key == idempotency_key,
                ))).mappings().one_or_none()
            if prior is not None and prior["fingerprint"] == fingerprint:
                return self._record(prior, duplicate=True)
            raise ValueError("mission message conflicted with another writer") from exc
        return self._record(values, duplicate=False)

    def close(self) -> None:
        self._engine.dispose()
