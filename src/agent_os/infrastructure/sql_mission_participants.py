"""Durable mission-scoped human participation and access projection."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from typing import Any, Mapping

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError

from agent_os.application.ports import MissionParticipantStore
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url
from agent_os.infrastructure.sql_experience_events import (
    SQLExperienceEventLog,
    experience_source_key,
)


mission_participant_metadata = MetaData()
PARTICIPATION_ROLES = frozenset({"builder", "reviewer", "client", "viewer"})

mission_participants = Table(
    "aos_v2_mission_participants",
    mission_participant_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("mission_id", String(256), primary_key=True),
    Column("subject_id", String(255), primary_key=True),
    Column("participation_role", String(32), nullable=False),
    Column("active", Boolean, nullable=False),
    Column("version", Integer, nullable=False),
    Column("granted_by", String(255), nullable=False),
    Column("granted_at", DateTime(timezone=True), nullable=False),
    Column("grant_key", String(200), nullable=False),
    Column("revoked_by", String(255), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column("revoked_reason", Text, nullable=True),
    Column("revocation_key", String(200), nullable=True),
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _resource_id(mission_id: str, subject_id: str) -> str:
    digest = hashlib.sha256(f"{mission_id}\0{subject_id}".encode()).hexdigest()
    return f"mission-participant-{digest}"


class SQLMissionParticipantStore(MissionParticipantStore):
    def __init__(self, database_url: str, *, create_schema: bool = False, clock=None) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._experience_events = SQLExperienceEventLog(
            lambda tenant_id: self._connection(tenant_id),
        )
        if create_schema:
            mission_participant_metadata.create_all(self._engine)
            self._experience_events.create_schema(self._engine)

    @contextmanager
    def _connection(self, tenant_id: str):
        if not tenant_id.strip() or len(tenant_id) > 128:
            raise ValueError("mission participant tenant is invalid")
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_app"))
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :value, true)"),
                    {"value": tenant_id},
                )
            yield connection

    @staticmethod
    def _identity(mission_id: str, subject_id: str) -> tuple[str, str]:
        mission_id = mission_id.strip()
        subject_id = subject_id.strip()
        if (
            not 1 <= len(mission_id) <= 256
            or not 1 <= len(subject_id) <= 255
            or "\0" in mission_id
            or "\0" in subject_id
        ):
            raise ValueError("mission participant identity is invalid")
        return mission_id, subject_id

    @staticmethod
    def _mutation(actor_id: str, idempotency_key: str) -> tuple[str, str]:
        actor_id = actor_id.strip()
        idempotency_key = idempotency_key.strip()
        if not 1 <= len(actor_id) <= 255 or not 8 <= len(idempotency_key) <= 200:
            raise ValueError("mission participant actor or idempotency key is invalid")
        return actor_id, idempotency_key

    @staticmethod
    def _record(row: Mapping[str, Any], *, duplicate: bool) -> Mapping[str, Any]:
        return {
            "mission_id": row["mission_id"],
            "subject_id": row["subject_id"],
            "participation_role": row["participation_role"],
            "active": bool(row["active"]),
            "version": int(row["version"]),
            "granted_by": row["granted_by"],
            "granted_at": _utc(row["granted_at"]).isoformat(),
            "revoked_by": row["revoked_by"],
            "revoked_at": (
                None if row["revoked_at"] is None else _utc(row["revoked_at"]).isoformat()
            ),
            "revoked_reason": row["revoked_reason"],
            "duplicate": duplicate,
        }

    def can_access(self, tenant_id: str, mission_id: str, subject_id: str) -> bool:
        mission_id, subject_id = self._identity(mission_id, subject_id)
        with self._connection(tenant_id) as connection:
            row = connection.execute(select(mission_participants.c.active).where(and_(
                mission_participants.c.tenant_id == tenant_id,
                mission_participants.c.mission_id == mission_id,
                mission_participants.c.subject_id == subject_id,
            ))).scalar_one_or_none()
        return bool(row)

    def mission_ids_for_subject(
        self, tenant_id: str, subject_id: str, *, limit: int = 1_000,
    ) -> tuple[str, ...]:
        _, subject_id = self._identity("mission-scope", subject_id)
        if not 1 <= limit <= 10_000:
            raise ValueError("mission participant query limit is invalid")
        with self._connection(tenant_id) as connection:
            rows = connection.execute(select(
                mission_participants.c.mission_id,
            ).where(and_(
                mission_participants.c.tenant_id == tenant_id,
                mission_participants.c.subject_id == subject_id,
                mission_participants.c.active.is_(True),
            )).order_by(
                mission_participants.c.granted_at.desc(),
                mission_participants.c.mission_id.desc(),
            ).limit(limit)).scalars().all()
        return tuple(str(value) for value in rows)

    def list_participants(
        self, tenant_id: str, mission_id: str,
    ) -> tuple[Mapping[str, Any], ...]:
        mission_id, _ = self._identity(mission_id, "participant-list")
        with self._connection(tenant_id) as connection:
            rows = connection.execute(select(mission_participants).where(and_(
                mission_participants.c.tenant_id == tenant_id,
                mission_participants.c.mission_id == mission_id,
            )).order_by(mission_participants.c.subject_id).limit(1_000)).mappings().all()
        return tuple(self._record(row, duplicate=False) for row in rows)

    def grant_participant(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        subject_id: str,
        participation_role: str,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        mission_id, subject_id = self._identity(mission_id, subject_id)
        actor_id, idempotency_key = self._mutation(actor_id, idempotency_key)
        participation_role = participation_role.strip()
        if participation_role not in PARTICIPATION_ROLES:
            raise ValueError("mission participation role must be builder, reviewer, client, or viewer")
        key = and_(
            mission_participants.c.tenant_id == tenant_id,
            mission_participants.c.mission_id == mission_id,
            mission_participants.c.subject_id == subject_id,
        )
        now = _utc(self._clock())
        try:
            with self._connection(tenant_id) as connection:
                prior = connection.execute(
                    select(mission_participants).where(key).with_for_update(),
                ).mappings().one_or_none()
                if prior is not None and prior["active"]:
                    if prior["participation_role"] != participation_role:
                        raise ValueError(
                            "active mission participant must be revoked before changing role"
                        )
                    return self._record(prior, duplicate=True)
                version = 1 if prior is None else int(prior["version"]) + 1
                values = {
                    "participation_role": participation_role,
                    "active": True,
                    "version": version,
                    "granted_by": actor_id,
                    "granted_at": now,
                    "grant_key": idempotency_key,
                    "revoked_by": None,
                    "revoked_at": None,
                    "revoked_reason": None,
                    "revocation_key": None,
                }
                if prior is None:
                    connection.execute(insert(mission_participants).values(
                        tenant_id=tenant_id,
                        mission_id=mission_id,
                        subject_id=subject_id,
                        **values,
                    ))
                else:
                    connection.execute(update(mission_participants).where(key).values(**values))
                self._experience_events.append(
                    connection,
                    tenant_id=tenant_id,
                    source_key=experience_source_key(
                        "mission.participant.granted", mission_id, subject_id, str(version),
                    ),
                    resource_type="mission_participant",
                    resource_id=_resource_id(mission_id, subject_id),
                    projection_revision=version,
                    kind="mission.participant.granted",
                    audience_ids=(subject_id, "role:manager"),
                    safe_summary="Mission access was granted to a participant.",
                    occurred_at=now,
                )
        except IntegrityError as exc:
            raise ValueError("mission participant grant conflicted with another writer") from exc
        return self._record({
            "mission_id": mission_id,
            "subject_id": subject_id,
            **values,
        }, duplicate=False)

    def revoke_participant(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        subject_id: str,
        actor_id: str,
        reason: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None:
        mission_id, subject_id = self._identity(mission_id, subject_id)
        actor_id, idempotency_key = self._mutation(actor_id, idempotency_key)
        reason = reason.strip()
        if not 1 <= len(reason) <= 2_000:
            raise ValueError("mission participant revocation reason is invalid")
        key = and_(
            mission_participants.c.tenant_id == tenant_id,
            mission_participants.c.mission_id == mission_id,
            mission_participants.c.subject_id == subject_id,
        )
        try:
            with self._connection(tenant_id) as connection:
                prior = connection.execute(
                    select(mission_participants).where(key).with_for_update(),
                ).mappings().one_or_none()
                if prior is None:
                    return None
                if not prior["active"]:
                    if prior["revocation_key"] != idempotency_key:
                        raise ValueError("mission participant was already revoked by another decision")
                    return self._record(prior, duplicate=True)
                now = _utc(self._clock())
                version = int(prior["version"]) + 1
                connection.execute(update(mission_participants).where(key).values(
                    active=False,
                    version=version,
                    revoked_by=actor_id,
                    revoked_at=now,
                    revoked_reason=reason,
                    revocation_key=idempotency_key,
                ))
                self._experience_events.append(
                    connection,
                    tenant_id=tenant_id,
                    source_key=experience_source_key(
                        "mission.participant.revoked", mission_id, subject_id, str(version),
                    ),
                    resource_type="mission_participant",
                    resource_id=_resource_id(mission_id, subject_id),
                    projection_revision=version,
                    kind="mission.participant.revoked",
                    audience_ids=(subject_id, "role:manager"),
                    safe_summary="Mission access was revoked from a participant.",
                    occurred_at=now,
                )
        except IntegrityError as exc:
            raise ValueError(
                "mission participant still owns active work or changed concurrently"
            ) from exc
        result = dict(prior)
        result.update({
            "active": False,
            "version": version,
            "revoked_by": actor_id,
            "revoked_at": now,
            "revoked_reason": reason,
            "revocation_key": idempotency_key,
        })
        return self._record(result, duplicate=False)

    def close(self) -> None:
        self._engine.dispose()
