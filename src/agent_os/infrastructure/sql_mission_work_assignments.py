"""Durable, version-fenced human responsibility over mission work."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    JSON,
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
    update,
)
from sqlalchemy.exc import IntegrityError

from agent_os.application.ports import MissionWorkAssignmentStore
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url
from agent_os.infrastructure.sql_experience_events import (
    SQLExperienceEventLog,
    experience_source_key,
)


mission_work_assignment_metadata = MetaData()
WORK_DUTIES = frozenset({"responsible", "reviewer"})
WORK_DUTY_ROLE = {"responsible": "builder", "reviewer": "reviewer"}

mission_work_assignments = Table(
    "aos_v2_mission_work_assignments",
    mission_work_assignment_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("mission_id", String(256), primary_key=True),
    Column("work_id", String(256), primary_key=True),
    Column("duty", String(32), primary_key=True),
    Column("work_fingerprint", String(64), nullable=False),
    Column("subject_id", String(255), nullable=False),
    Column("participation_role", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("active", Boolean, nullable=False),
    Column("version", Integer, nullable=False),
    Column("assigned_by", String(255), nullable=False),
    Column("assigned_at", DateTime(timezone=True), nullable=False),
    Column("assignment_reason", Text, nullable=False),
    Column("responded_by", String(255), nullable=True),
    Column("responded_at", DateTime(timezone=True), nullable=True),
    Column("response_reason", Text, nullable=True),
    Column("revoked_by", String(255), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column("revoked_reason", Text, nullable=True),
)

mission_work_assignment_events = Table(
    "aos_v2_mission_work_assignment_events",
    mission_work_assignment_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("mutation_key", String(200), primary_key=True),
    Column("request_fingerprint", String(64), nullable=False),
    Column("mission_id", String(256), nullable=False),
    Column("work_id", String(256), nullable=False),
    Column("duty", String(32), nullable=False),
    Column("assignment_version", Integer, nullable=False),
    Column("event_kind", String(32), nullable=False),
    Column("subject_id", String(255), nullable=False),
    Column("actor_id", String(255), nullable=False),
    Column("reason", Text, nullable=False),
    Column("snapshot", JSON, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "tenant_id", "mission_id", "work_id", "duty", "assignment_version",
        name="aos_v2_mission_work_assignment_event_version_uq",
    ),
)

Index(
    "aos_v2_mission_work_assignments_subject_idx",
    mission_work_assignments.c.tenant_id,
    mission_work_assignments.c.subject_id,
    mission_work_assignments.c.active,
    mission_work_assignments.c.assigned_at.desc(),
)
Index(
    "aos_v2_mission_work_assignment_events_history_idx",
    mission_work_assignment_events.c.tenant_id,
    mission_work_assignment_events.c.mission_id,
    mission_work_assignment_events.c.occurred_at.desc(),
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _resource_id(mission_id: str, work_id: str, duty: str) -> str:
    digest = hashlib.sha256(f"{mission_id}\0{work_id}\0{duty}".encode()).hexdigest()
    return f"mission-work-assignment-{digest}"


def _fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode()).hexdigest()


class SQLMissionWorkAssignmentStore(MissionWorkAssignmentStore):
    def __init__(self, database_url: str, *, create_schema: bool = False, clock=None) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._experience_events = SQLExperienceEventLog(
            lambda tenant_id: self._connection(tenant_id),
        )
        if create_schema:
            mission_work_assignment_metadata.create_all(self._engine)
            self._experience_events.create_schema(self._engine)

    @contextmanager
    def _connection(self, tenant_id: str):
        if not 1 <= len(tenant_id.strip()) <= 128:
            raise ValueError("mission work tenant is invalid")
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_app"))
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :value, true)"),
                    {"value": tenant_id},
                )
            yield connection

    @staticmethod
    def _identity(
        mission_id: str, work_id: str, duty: str, subject_id: str,
    ) -> tuple[str, str, str, str]:
        values = (mission_id.strip(), work_id.strip(), duty.strip(), subject_id.strip())
        if (
            not 1 <= len(values[0]) <= 256
            or not 1 <= len(values[1]) <= 256
            or values[2] not in WORK_DUTIES
            or not 1 <= len(values[3]) <= 255
            or any("\0" in value for value in values)
        ):
            raise ValueError("mission work assignment identity is invalid")
        return values

    @staticmethod
    def _mutation(
        actor_id: str, reason: str, expected_version: int, idempotency_key: str,
    ) -> tuple[str, str, int, str]:
        actor_id = actor_id.strip()
        reason = reason.strip()
        idempotency_key = idempotency_key.strip()
        if (
            not 1 <= len(actor_id) <= 255
            or len(reason) > 2_000
            or expected_version < 0
            or not 8 <= len(idempotency_key) <= 200
        ):
            raise ValueError("mission work mutation is invalid")
        return actor_id, reason, expected_version, idempotency_key

    @staticmethod
    def _work_fingerprint(value: str) -> str:
        value = value.strip()
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("mission work fingerprint is invalid")
        return value

    @staticmethod
    def _record(row: Mapping[str, Any], *, duplicate: bool) -> dict[str, Any]:
        return {
            "mission_id": row["mission_id"],
            "work_id": row["work_id"],
            "duty": row["duty"],
            "work_fingerprint": row["work_fingerprint"],
            "subject_id": row["subject_id"],
            "participation_role": row["participation_role"],
            "status": row["status"],
            "active": bool(row["active"]),
            "version": int(row["version"]),
            "assigned_by": row["assigned_by"],
            "assigned_at": _utc(row["assigned_at"]).isoformat(),
            "assignment_reason": row["assignment_reason"],
            "responded_by": row["responded_by"],
            "responded_at": (
                None if row["responded_at"] is None else _utc(row["responded_at"]).isoformat()
            ),
            "response_reason": row["response_reason"],
            "revoked_by": row["revoked_by"],
            "revoked_at": (
                None if row["revoked_at"] is None else _utc(row["revoked_at"]).isoformat()
            ),
            "revoked_reason": row["revoked_reason"],
            "duplicate": duplicate,
        }

    @staticmethod
    def _replay(connection, tenant_id: str, key: str, request_fingerprint: str):
        prior = connection.execute(select(
            mission_work_assignment_events.c.request_fingerprint,
            mission_work_assignment_events.c.snapshot,
        ).where(and_(
            mission_work_assignment_events.c.tenant_id == tenant_id,
            mission_work_assignment_events.c.mutation_key == key,
        ))).mappings().one_or_none()
        if prior is None:
            return None
        if prior["request_fingerprint"] != request_fingerprint:
            raise ValueError("mission work idempotency key was reused with different parameters")
        return {**dict(prior["snapshot"]), "duplicate": True}

    def _replay_after_integrity_conflict(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        request_fingerprint: str,
        conflict: IntegrityError,
    ) -> Mapping[str, Any]:
        """Resolve a concurrent same-command commit after our transaction rolled back."""
        with self._connection(tenant_id) as connection:
            replay = self._replay(
                connection, tenant_id, idempotency_key, request_fingerprint,
            )
        if replay is not None:
            return replay
        raise ValueError("mission work assignment conflicted with another writer") from conflict

    def _append_history(
        self,
        connection,
        *,
        tenant_id: str,
        mutation_key: str,
        request_fingerprint: str,
        event_kind: str,
        actor_id: str,
        reason: str,
        row: Mapping[str, Any],
        occurred_at: datetime,
        additional_audience_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        snapshot = self._record(row, duplicate=False)
        connection.execute(insert(mission_work_assignment_events).values(
            tenant_id=tenant_id,
            mutation_key=mutation_key,
            request_fingerprint=request_fingerprint,
            mission_id=row["mission_id"],
            work_id=row["work_id"],
            duty=row["duty"],
            assignment_version=row["version"],
            event_kind=event_kind,
            subject_id=row["subject_id"],
            actor_id=actor_id,
            reason=reason,
            snapshot=snapshot,
            occurred_at=occurred_at,
        ))
        self._experience_events.append(
            connection,
            tenant_id=tenant_id,
            source_key=experience_source_key(
                f"mission.work.{event_kind}", row["mission_id"], row["work_id"],
                row["duty"], str(row["version"]),
            ),
            resource_type="mission_work_assignment",
            resource_id=_resource_id(row["mission_id"], row["work_id"], row["duty"]),
            projection_revision=int(row["version"]),
            kind=f"mission.work.{event_kind}",
            audience_ids=tuple(dict.fromkeys((
                str(row["subject_id"]), "role:manager", *additional_audience_ids,
            ))),
            safe_summary=f"A mission work {row['duty']} assignment was {event_kind}.",
            occurred_at=occurred_at,
        )
        return snapshot

    def list_for_mission(
        self, tenant_id: str, mission_id: str,
    ) -> tuple[Mapping[str, Any], ...]:
        mission_id, _, _, _ = self._identity(
            mission_id, "assignment-list", "responsible", "assignment-reader",
        )
        with self._connection(tenant_id) as connection:
            rows = connection.execute(select(mission_work_assignments).where(and_(
                mission_work_assignments.c.tenant_id == tenant_id,
                mission_work_assignments.c.mission_id == mission_id,
            )).order_by(
                mission_work_assignments.c.work_id,
                mission_work_assignments.c.duty,
            ).limit(4_000)).mappings().all()
        return tuple(self._record(row, duplicate=False) for row in rows)

    def list_for_subject(
        self,
        tenant_id: str,
        subject_id: str,
        *,
        mission_ids: tuple[str, ...],
        limit: int = 200,
    ) -> tuple[Mapping[str, Any], ...]:
        _, _, _, subject_id = self._identity(
            "subject-list", "subject-work", "responsible", subject_id,
        )
        mission_ids = tuple(dict.fromkeys(value.strip() for value in mission_ids))
        if (
            not 1 <= limit <= 500 or len(mission_ids) > 1_000
            or any(not 1 <= len(value) <= 256 or "\0" in value for value in mission_ids)
        ):
            raise ValueError("mission work assignment query is invalid")
        if not mission_ids:
            return ()
        with self._connection(tenant_id) as connection:
            rows = connection.execute(select(mission_work_assignments).where(and_(
                mission_work_assignments.c.tenant_id == tenant_id,
                mission_work_assignments.c.subject_id == subject_id,
                mission_work_assignments.c.mission_id.in_(mission_ids),
                mission_work_assignments.c.active.is_(True),
            )).order_by(
                mission_work_assignments.c.assigned_at.desc(),
                mission_work_assignments.c.mission_id.desc(),
                mission_work_assignments.c.work_id.desc(),
                mission_work_assignments.c.duty,
            ).limit(limit)).mappings().all()
        return tuple(self._record(row, duplicate=False) for row in rows)

    def list_history(
        self, tenant_id: str, mission_id: str, *, limit: int = 500,
    ) -> tuple[Mapping[str, Any], ...]:
        mission_id, _, _, _ = self._identity(
            mission_id, "assignment-history", "responsible", "history-reader",
        )
        if not 1 <= limit <= 2_000:
            raise ValueError("mission work assignment history limit is invalid")
        with self._connection(tenant_id) as connection:
            rows = connection.execute(select(mission_work_assignment_events).where(and_(
                mission_work_assignment_events.c.tenant_id == tenant_id,
                mission_work_assignment_events.c.mission_id == mission_id,
            )).order_by(
                mission_work_assignment_events.c.occurred_at.desc(),
                mission_work_assignment_events.c.assignment_version.desc(),
            ).limit(limit)).mappings().all()
        return tuple({
            "event_kind": row["event_kind"],
            "actor_id": row["actor_id"],
            "reason": row["reason"],
            "occurred_at": _utc(row["occurred_at"]).isoformat(),
            **dict(row["snapshot"]),
        } for row in rows)

    def assign_work(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        work_id: str,
        duty: str,
        work_fingerprint: str,
        subject_id: str,
        participation_role: str,
        assigned_by: str,
        reason: str,
        expected_version: int,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        mission_id, work_id, duty, subject_id = self._identity(
            mission_id, work_id, duty, subject_id,
        )
        assigned_by, reason, expected_version, idempotency_key = self._mutation(
            assigned_by, reason, expected_version, idempotency_key,
        )
        work_fingerprint = self._work_fingerprint(work_fingerprint)
        participation_role = participation_role.strip()
        if participation_role != WORK_DUTY_ROLE[duty]:
            raise ValueError("mission work duty does not match the participant role")
        if not reason:
            raise ValueError("mission work assignment reason is required")
        request_fingerprint = _fingerprint({
            "operation": "assign", "mission_id": mission_id, "work_id": work_id,
            "duty": duty, "work_fingerprint": work_fingerprint, "subject_id": subject_id,
            "participation_role": participation_role, "assigned_by": assigned_by,
            "reason": reason, "expected_version": expected_version,
        })
        key = and_(
            mission_work_assignments.c.tenant_id == tenant_id,
            mission_work_assignments.c.mission_id == mission_id,
            mission_work_assignments.c.work_id == work_id,
            mission_work_assignments.c.duty == duty,
        )
        now = _utc(self._clock())
        try:
            with self._connection(tenant_id) as connection:
                replay = self._replay(
                    connection, tenant_id, idempotency_key, request_fingerprint,
                )
                if replay is not None:
                    return replay
                prior = connection.execute(
                    select(mission_work_assignments).where(key).with_for_update(),
                ).mappings().one_or_none()
                # The command may have committed while this transaction waited on the row lock.
                replay = self._replay(
                    connection, tenant_id, idempotency_key, request_fingerprint,
                )
                if replay is not None:
                    return replay
                current_version = 0 if prior is None else int(prior["version"])
                if current_version != expected_version:
                    raise ValueError(
                        f"mission work assignment version changed; expected {expected_version}, "
                        f"current {current_version}"
                    )
                if prior is not None and prior["active"] and (
                    prior["subject_id"] == subject_id
                    and prior["work_fingerprint"] == work_fingerprint
                ):
                    raise ValueError("mission work duty is already assigned to this participant")
                version = current_version + 1
                values = {
                    "tenant_id": tenant_id,
                    "mission_id": mission_id,
                    "work_id": work_id,
                    "duty": duty,
                    "work_fingerprint": work_fingerprint,
                    "subject_id": subject_id,
                    "participation_role": participation_role,
                    "status": "pending",
                    "active": True,
                    "version": version,
                    "assigned_by": assigned_by,
                    "assigned_at": now,
                    "assignment_reason": reason,
                    "responded_by": None,
                    "responded_at": None,
                    "response_reason": None,
                    "revoked_by": None,
                    "revoked_at": None,
                    "revoked_reason": None,
                }
                if prior is None:
                    connection.execute(insert(mission_work_assignments).values(**values))
                else:
                    connection.execute(update(mission_work_assignments).where(key).values(**values))
                return self._append_history(
                    connection,
                    tenant_id=tenant_id,
                    mutation_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    event_kind="reassigned" if prior is not None else "assigned",
                    actor_id=assigned_by,
                    reason=reason,
                    row=values,
                    occurred_at=now,
                    additional_audience_ids=(
                        () if prior is None or prior["subject_id"] == subject_id
                        else (str(prior["subject_id"]),)
                    ),
                )
        except IntegrityError as exc:
            return self._replay_after_integrity_conflict(
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                conflict=exc,
            )

    def respond_to_assignment(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        work_id: str,
        duty: str,
        subject_id: str,
        response: str,
        reason: str,
        expected_version: int,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        mission_id, work_id, duty, subject_id = self._identity(
            mission_id, work_id, duty, subject_id,
        )
        subject_id, reason, expected_version, idempotency_key = self._mutation(
            subject_id, reason, expected_version, idempotency_key,
        )
        response = response.strip()
        if response not in {"accept", "decline"} or (response == "decline" and not reason):
            raise ValueError("mission work response must be accept or a reasoned decline")
        request_fingerprint = _fingerprint({
            "operation": "respond", "mission_id": mission_id, "work_id": work_id,
            "duty": duty, "subject_id": subject_id, "response": response,
            "reason": reason, "expected_version": expected_version,
        })
        key = and_(
            mission_work_assignments.c.tenant_id == tenant_id,
            mission_work_assignments.c.mission_id == mission_id,
            mission_work_assignments.c.work_id == work_id,
            mission_work_assignments.c.duty == duty,
        )
        now = _utc(self._clock())
        try:
            with self._connection(tenant_id) as connection:
                replay = self._replay(
                    connection, tenant_id, idempotency_key, request_fingerprint,
                )
                if replay is not None:
                    return replay
                prior = connection.execute(
                    select(mission_work_assignments).where(key).with_for_update(),
                ).mappings().one_or_none()
                # Recheck after the lock wait so a concurrent identical command replays.
                replay = self._replay(
                    connection, tenant_id, idempotency_key, request_fingerprint,
                )
                if replay is not None:
                    return replay
                if prior is None:
                    raise ValueError("mission work assignment does not exist")
                if int(prior["version"]) != expected_version:
                    raise ValueError("mission work assignment version changed")
                if prior["subject_id"] != subject_id:
                    raise ValueError("only the assigned participant can respond")
                if not prior["active"] or prior["status"] != "pending":
                    raise ValueError("mission work assignment is not awaiting a response")
                version = int(prior["version"]) + 1
                values = dict(prior)
                values.update({
                    "status": "accepted" if response == "accept" else "declined",
                    "active": response == "accept",
                    "version": version,
                    "responded_by": subject_id,
                    "responded_at": now,
                    "response_reason": reason or None,
                })
                connection.execute(update(mission_work_assignments).where(key).values(
                    status=values["status"],
                    active=values["active"],
                    version=version,
                    responded_by=subject_id,
                    responded_at=now,
                    response_reason=reason or None,
                ))
                return self._append_history(
                    connection,
                    tenant_id=tenant_id,
                    mutation_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    event_kind="accepted" if response == "accept" else "declined",
                    actor_id=subject_id,
                    reason=reason,
                    row=values,
                    occurred_at=now,
                )
        except IntegrityError as exc:
            return self._replay_after_integrity_conflict(
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                conflict=exc,
            )

    def revoke_assignment(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        work_id: str,
        duty: str,
        revoked_by: str,
        reason: str,
        expected_version: int,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None:
        mission_id, work_id, duty, _ = self._identity(
            mission_id, work_id, duty, "assignment-subject",
        )
        revoked_by, reason, expected_version, idempotency_key = self._mutation(
            revoked_by, reason, expected_version, idempotency_key,
        )
        if not reason:
            raise ValueError("mission work revocation reason is required")
        request_fingerprint = _fingerprint({
            "operation": "revoke", "mission_id": mission_id, "work_id": work_id,
            "duty": duty, "revoked_by": revoked_by, "reason": reason,
            "expected_version": expected_version,
        })
        key = and_(
            mission_work_assignments.c.tenant_id == tenant_id,
            mission_work_assignments.c.mission_id == mission_id,
            mission_work_assignments.c.work_id == work_id,
            mission_work_assignments.c.duty == duty,
        )
        now = _utc(self._clock())
        try:
            with self._connection(tenant_id) as connection:
                replay = self._replay(
                    connection, tenant_id, idempotency_key, request_fingerprint,
                )
                if replay is not None:
                    return replay
                prior = connection.execute(
                    select(mission_work_assignments).where(key).with_for_update(),
                ).mappings().one_or_none()
                # Recheck after the lock wait so a concurrent identical command replays.
                replay = self._replay(
                    connection, tenant_id, idempotency_key, request_fingerprint,
                )
                if replay is not None:
                    return replay
                if prior is None:
                    return None
                if int(prior["version"]) != expected_version:
                    raise ValueError("mission work assignment version changed")
                if not prior["active"]:
                    raise ValueError("mission work assignment is not active")
                version = int(prior["version"]) + 1
                values = dict(prior)
                values.update({
                    "status": "revoked",
                    "active": False,
                    "version": version,
                    "revoked_by": revoked_by,
                    "revoked_at": now,
                    "revoked_reason": reason,
                })
                connection.execute(update(mission_work_assignments).where(key).values(
                    status="revoked",
                    active=False,
                    version=version,
                    revoked_by=revoked_by,
                    revoked_at=now,
                    revoked_reason=reason,
                ))
                return self._append_history(
                    connection,
                    tenant_id=tenant_id,
                    mutation_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    event_kind="revoked",
                    actor_id=revoked_by,
                    reason=reason,
                    row=values,
                    occurred_at=now,
                )
        except IntegrityError as exc:
            return self._replay_after_integrity_conflict(
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                conflict=exc,
            )

    def close(self) -> None:
        self._engine.dispose()
