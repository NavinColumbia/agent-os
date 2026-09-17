"""Durable, tenant-monotonic experience events shared by product stores."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
import hashlib
import json
from typing import Any

from sqlalchemy import (
    BigInteger,
    JSON,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    and_,
    insert,
    select,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from agent_os.application.ports import ExperienceEventPage


experience_event_metadata = MetaData()

experience_streams = Table(
    "aos_v2_experience_streams",
    experience_event_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("next_sequence", BigInteger, nullable=False),
    Column("retained_from_sequence", BigInteger, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

experience_events = Table(
    "aos_v2_experience_events",
    experience_event_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("tenant_sequence", BigInteger, primary_key=True),
    Column("event_id", String(96), nullable=False),
    Column("source_key", String(512), nullable=False),
    Column("resource_type", String(64), nullable=False),
    Column("resource_id", String(256), nullable=False),
    Column("projection_revision", BigInteger, nullable=False),
    Column("kind", String(128), nullable=False),
    Column("audience_ids", JSON, nullable=False),
    Column("safe_summary", String(500), nullable=False),
    Column("trace_id", String(256), nullable=True),
    Column("fingerprint", String(64), nullable=False),
    Column("record", JSON, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant_id", "event_id"),
    UniqueConstraint("tenant_id", "source_key"),
)

Index(
    "aos_v2_experience_events_tenant_time_idx",
    experience_events.c.tenant_id,
    experience_events.c.occurred_at.desc(),
    experience_events.c.tenant_sequence.desc(),
)

experience_event_audiences = Table(
    "aos_v2_experience_event_audiences",
    experience_event_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("tenant_sequence", BigInteger, primary_key=True),
    Column("audience_id", String(256), primary_key=True),
    ForeignKeyConstraint(
        ["tenant_id", "tenant_sequence"],
        ["aos_v2_experience_events.tenant_id", "aos_v2_experience_events.tenant_sequence"],
        ondelete="CASCADE",
    ),
)

Index(
    "aos_v2_experience_event_audiences_lookup_idx",
    experience_event_audiences.c.tenant_id,
    experience_event_audiences.c.audience_id,
    experience_event_audiences.c.tenant_sequence,
)


def experience_source_key(kind: str, *identities: str) -> str:
    """Bound arbitrary external identities before using them as a durable key."""

    digest = hashlib.sha256(json.dumps(
        [kind, *identities], ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()
    return f"{kind}:{digest}"


class SQLExperienceEventLog:
    """Append via a caller transaction; read via its tenant-fenced connection."""

    def __init__(
        self,
        tenant_connection: Callable[[str], AbstractContextManager[Connection]],
    ) -> None:
        self._tenant_connection = tenant_connection

    @staticmethod
    def create_schema(engine: Engine) -> None:
        experience_event_metadata.create_all(engine)

    def append(
        self,
        connection: Connection,
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
        """Append a safe event inside the caller's source-mutation transaction."""

        audiences = tuple(dict.fromkeys(value.strip() for value in audience_ids))
        if (
            not tenant_id.strip() or not 1 <= len(source_key) <= 512
            or not 1 <= len(resource_type) <= 64
            or not 1 <= len(resource_id) <= 256
            or projection_revision < 0
            or not 1 <= len(kind) <= 128
            or not audiences or len(audiences) > 128
            or any(not value or len(value) > 256 for value in audiences)
            or not 1 <= len(safe_summary) <= 500
            or (trace_id is not None and not 1 <= len(trace_id) <= 256)
            or occurred_at.tzinfo is None
        ):
            raise ValueError("experience event is invalid")
        semantic = {
            "tenant_id": tenant_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "projection_revision": projection_revision,
            "kind": kind,
            "audience_ids": list(audiences),
            "safe_summary": safe_summary,
            "trace_id": trace_id,
            "occurred_at": occurred_at.isoformat(),
        }
        fingerprint = hashlib.sha256(json.dumps(
            semantic, allow_nan=False, ensure_ascii=False,
            separators=(",", ":"), sort_keys=True,
        ).encode()).hexdigest()
        prior = connection.execute(select(
            experience_events.c.tenant_sequence,
            experience_events.c.fingerprint,
        ).where(and_(
            experience_events.c.tenant_id == tenant_id,
            experience_events.c.source_key == source_key,
        ))).mappings().one_or_none()
        if prior is not None:
            if prior["fingerprint"] != fingerprint:
                raise ValueError("experience event source was reused with different content")
            return int(prior["tenant_sequence"])

        stream_values = {
            "tenant_id": tenant_id,
            "next_sequence": 2,
            "retained_from_sequence": 1,
            "updated_at": occurred_at,
        }
        if connection.dialect.name == "postgresql":
            allocator = postgresql_insert(experience_streams).values(**stream_values)
        elif connection.dialect.name == "sqlite":
            allocator = sqlite_insert(experience_streams).values(**stream_values)
        else:  # pragma: no cover - composition only supports PostgreSQL/SQLite
            raise RuntimeError("experience events require PostgreSQL or SQLite")
        allocator = allocator.on_conflict_do_update(
            index_elements=[experience_streams.c.tenant_id],
            set_={
                "next_sequence": experience_streams.c.next_sequence + 1,
                "updated_at": occurred_at,
            },
        ).returning(experience_streams.c.next_sequence)
        tenant_sequence = int(connection.execute(allocator).scalar_one()) - 1
        event_id = "experience-" + hashlib.sha256(
            f"agent-os:experience-event:v1:{tenant_id}:{source_key}".encode()
        ).hexdigest()
        record = {
            **semantic,
            "tenant_sequence": tenant_sequence,
            "event_id": event_id,
        }
        connection.execute(insert(experience_events).values(
            tenant_id=tenant_id,
            tenant_sequence=tenant_sequence,
            event_id=event_id,
            source_key=source_key,
            resource_type=resource_type,
            resource_id=resource_id,
            projection_revision=projection_revision,
            kind=kind,
            audience_ids=list(audiences),
            safe_summary=safe_summary,
            trace_id=trace_id,
            fingerprint=fingerprint,
            record=record,
            occurred_at=occurred_at,
        ))
        connection.execute(insert(experience_event_audiences), [
            {
                "tenant_id": tenant_id,
                "tenant_sequence": tenant_sequence,
                "audience_id": audience_id,
            }
            for audience_id in audiences
        ])
        return tenant_sequence

    def list(
        self,
        tenant_id: str,
        *,
        after_sequence: int = 0,
        audience_ids: tuple[str, ...] | None = None,
        limit: int = 100,
    ) -> ExperienceEventPage:
        if after_sequence < 0 or not 1 <= limit <= 500:
            raise ValueError("experience event cursor or limit is invalid")
        audiences = None
        if audience_ids is not None:
            audiences = tuple(dict.fromkeys(value.strip() for value in audience_ids))
            if (
                not audiences or len(audiences) > 64
                or any(not value or len(value) > 256 for value in audiences)
            ):
                raise ValueError("experience event audience is invalid")
        with self._tenant_connection(tenant_id) as connection:
            stream = connection.execute(select(
                experience_streams.c.next_sequence,
                experience_streams.c.retained_from_sequence,
            ).where(
                experience_streams.c.tenant_id == tenant_id,
            )).mappings().one_or_none()
            if stream is None:
                if after_sequence:
                    raise ValueError("experience event cursor is ahead of this tenant")
                return ExperienceEventPage((), 0, 0, 0)
            latest = int(stream["next_sequence"]) - 1
            minimum = int(stream["retained_from_sequence"])
            if after_sequence > latest:
                raise ValueError("experience event cursor is ahead of this tenant")
            if after_sequence < minimum - 1:
                return ExperienceEventPage(
                    (), latest, minimum, latest, reset_required=True,
                )
            criteria = [
                experience_events.c.tenant_id == tenant_id,
                experience_events.c.tenant_sequence > after_sequence,
                experience_events.c.tenant_sequence <= latest,
            ]
            source = experience_events
            if audiences is not None:
                source = experience_events.join(
                    experience_event_audiences,
                    and_(
                        experience_event_audiences.c.tenant_id
                        == experience_events.c.tenant_id,
                        experience_event_audiences.c.tenant_sequence
                        == experience_events.c.tenant_sequence,
                    ),
                )
                criteria.extend((
                    experience_event_audiences.c.tenant_id == tenant_id,
                    experience_event_audiences.c.audience_id.in_(audiences),
                ))
            rows = connection.execute(select(
                experience_events.c.tenant_sequence,
                experience_events.c.record,
            ).select_from(source).where(and_(*criteria)).distinct().order_by(
                experience_events.c.tenant_sequence,
            ).limit(limit + 1)).mappings().all()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        cursor_sequence = (
            int(page_rows[-1]["tenant_sequence"])
            if has_more and page_rows else latest
        )
        return ExperienceEventPage(
            tuple(dict(row["record"]) for row in page_rows),
            cursor_sequence,
            minimum,
            latest,
            has_more=has_more,
        )
