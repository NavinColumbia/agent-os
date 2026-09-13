"""DBOS durable adapter for the authoritative V2 lifecycle.

Each accepted API mutation is a small DBOS workflow.  The workflow commits the
event, projected state, and command outbox in one serializable application
transaction.  It deliberately does not implement a second scheduler or infer
progress from worker output.

The module has no import-time singleton or network side effect.  A dedicated V2
service process creates exactly one :class:`DBOSLifecycleEngine` and owns the
DBOS singleton for that process.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
from contextlib import contextmanager
from typing import Any, Mapping

from dbos import DBOS, DBOSConfig, SetWorkflowID
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    ForeignKeyConstraint,
    and_,
    create_engine,
    insert,
    or_,
    select,
    text,
    update,
)

from agent_os.application.lifecycle import event_fingerprint, plan_transition
from agent_os.application.ports import (
    CommandLease,
    CommandOutbox,
    OrganizationEventReceipt,
    OrganizationLedger,
    WorkflowEngine,
    WorkflowReceipt,
)
from agent_os.domain.lifecycle import CommandKind, Event, EventKind, LifecycleState
from agent_os.domain.organization_events import (
    OrganizationEvent,
    OrganizationEventKind,
    organization_event_fingerprint,
)


metadata = MetaData()

runs = Table(
    "aos_v2_lifecycle_runs",
    metadata,
    # During the bootstrap vertical one organization is one tenant.  The
    # physical name is tenant_id so PostgreSQL RLS tooling cannot miss it.
    Column("tenant_id", String(128), key="organization_id", primary_key=True),
    Column("run_id", String(128), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("state", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

events = Table(
    "aos_v2_lifecycle_events",
    metadata,
    Column("tenant_id", String(128), key="organization_id", primary_key=True),
    Column("run_id", String(128), primary_key=True),
    Column("event_id", String(256), primary_key=True),
    Column("fingerprint", String(64), nullable=False),
    Column("aggregate_version", Integer, nullable=False),
    Column("event", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

commands = Table(
    "aos_v2_lifecycle_commands",
    metadata,
    Column("command_id", String(64), primary_key=True),
    Column("tenant_id", String(128), key="organization_id", nullable=False, index=True),
    Column("run_id", String(128), nullable=False, index=True),
    Column("event_id", String(256), nullable=False),
    Column("aggregate_version", Integer, nullable=False),
    Column("position", Integer, nullable=False),
    Column("envelope", JSON, nullable=False),
    Column("status", String(32), nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("lease_owner", String(256)),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
    Column("result", JSON),
    Column("last_error", JSON),
)

organization_streams = Table(
    "aos_v2_organization_streams",
    metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("run_id", String(128), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "run_id"],
        ["aos_v2_lifecycle_runs.organization_id", "aos_v2_lifecycle_runs.run_id"],
        ondelete="CASCADE",
    ),
)

organization_events = Table(
    "aos_v2_organization_events",
    metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("run_id", String(128), primary_key=True),
    Column("event_id", String(256), primary_key=True),
    Column("stream_version", Integer, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("kind", String(80), nullable=False),
    Column("actor_id", String(256), nullable=False),
    Column("occurred_at", String(64), nullable=False),
    Column("causation_id", String(256)),
    Column("correlation_id", String(256)),
    Column("payload", JSON, nullable=False),
    Column("event", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "run_id"],
        ["aos_v2_organization_streams.tenant_id", "aos_v2_organization_streams.run_id"],
        ondelete="CASCADE",
    ),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _command_available_at(envelope: Mapping[str, Any], now: datetime) -> datetime:
    command = envelope.get("command")
    if not isinstance(command, Mapping) or command.get("kind") != CommandKind.SCHEDULE_RETRY.value:
        return now
    payload = command.get("payload")
    raw = payload.get("retry_at") if isinstance(payload, Mapping) else None
    if raw is None:
        return now
    if not isinstance(raw, str):
        raise ValueError("scheduled retry timestamp must be a string")
    try:
        due = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("scheduled retry timestamp must be RFC 3339") from exc
    if due.tzinfo is None:
        raise ValueError("scheduled retry timestamp must include a timezone")
    return max(now, due.astimezone(timezone.utc))


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _workflow_id(kind: str, organization_id: str, run_id: str, fingerprint: str) -> str:
    material = f"agent-os:v2:{kind}:{organization_id}:{run_id}:{fingerprint}"
    return f"aos-v2-{kind}-{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def sqlalchemy_url(database_url: str) -> str:
    """Select psycopg 3 explicitly when given an ordinary PostgreSQL URL."""

    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    return database_url


def _scope_dbos_transaction(organization_id: str) -> None:
    """Enter the least-privilege RLS role for a DBOS application transaction."""

    bind = DBOS.sql_session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    DBOS.sql_session.execute(text("SET LOCAL ROLE agentos_app"))
    DBOS.sql_session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": organization_id},
    )


class DBOSLifecycleEngine(WorkflowEngine, CommandOutbox, OrganizationLedger):
    """DBOS/Postgres bootstrap implementation of :class:`WorkflowEngine`."""

    def __init__(
        self,
        *,
        system_database_url: str,
        application_database_url: str | None = None,
        application_version: str = "v2",
        create_schema: bool = False,
        launch: bool = True,
    ) -> None:
        if not system_database_url.strip():
            raise ValueError("system_database_url is required")
        application_database_url = application_database_url or system_database_url
        self._system_database_url = sqlalchemy_url(system_database_url)
        self._application_database_url = sqlalchemy_url(application_database_url)
        self._engine = create_engine(self._application_database_url, pool_pre_ping=True)
        if create_schema:
            metadata.create_all(self._engine)

        config: DBOSConfig = {
            "name": "agent-os-v2",
            "system_database_url": self._system_database_url,
            "application_database_url": self._application_database_url,
            "application_version": application_version,
            "run_admin_server": False,
            "enable_otlp": False,
        }
        DBOS(config=config)
        self._register_workflows()
        if launch:
            DBOS.launch()

    def _register_workflows(self) -> None:
        @DBOS.transaction(name="agent_os_v2_create_lifecycle")
        def create_lifecycle(initial_raw: Mapping[str, Any]) -> Mapping[str, Any]:
            initial = LifecycleState.from_dict(initial_raw)
            if initial.version != 0 or initial.last_event_id is not None:
                raise ValueError("a new lifecycle must start at version zero")
            _scope_dbos_transaction(initial.organization_id)
            key = and_(
                runs.c.organization_id == initial.organization_id,
                runs.c.run_id == initial.run_id,
            )
            existing = DBOS.sql_session.execute(select(runs.c.state).where(key).with_for_update()).scalar_one_or_none()
            if existing is not None:
                if _canonical(existing) != _canonical(initial.to_dict()):
                    raise ValueError("run identity already exists with different initial state")
                return {"created": False, "state": existing}
            now = _now()
            DBOS.sql_session.execute(insert(runs).values(
                organization_id=initial.organization_id,
                run_id=initial.run_id,
                version=0,
                state=initial.to_dict(),
                created_at=now,
                updated_at=now,
            ))
            DBOS.sql_session.execute(insert(organization_streams).values(
                tenant_id=initial.organization_id,
                run_id=initial.run_id,
                version=0,
                created_at=now,
                updated_at=now,
            ))
            return {"created": True, "state": initial.to_dict()}

        @DBOS.transaction(name="agent_os_v2_apply_lifecycle_event")
        def apply_lifecycle_event(
            organization_id: str,
            run_id: str,
            event_raw: Mapping[str, Any],
        ) -> Mapping[str, Any]:
            event = Event.from_dict(event_raw)
            _scope_dbos_transaction(organization_id)
            fingerprint = event_fingerprint(event)
            event_key = and_(
                events.c.organization_id == organization_id,
                events.c.run_id == run_id,
                events.c.event_id == event.event_id,
            )
            duplicate = DBOS.sql_session.execute(
                select(events.c.fingerprint).where(event_key)
            ).scalar_one_or_none()
            if duplicate is not None:
                if duplicate != fingerprint:
                    raise ValueError("event_id was reused with different event content")
                current = DBOS.sql_session.execute(select(runs.c.state).where(and_(
                    runs.c.organization_id == organization_id,
                    runs.c.run_id == run_id,
                ))).scalar_one()
                return {"duplicate": True, "state": current, "commands": []}

            run_key = and_(
                runs.c.organization_id == organization_id,
                runs.c.run_id == run_id,
            )
            current_raw = DBOS.sql_session.execute(
                select(runs.c.state).where(run_key).with_for_update()
            ).scalar_one_or_none()
            if current_raw is None:
                raise LookupError("lifecycle run does not exist for this organization")
            current = LifecycleState.from_dict(current_raw)
            if current.organization_id != organization_id or current.run_id != run_id:
                raise PermissionError("lifecycle identity does not match the requested tenant/run")

            decision = plan_transition(current, event)
            if decision.transition.duplicate:
                return {"duplicate": True, "state": current.to_dict(), "commands": []}
            next_state = decision.transition.state
            changed = DBOS.sql_session.execute(update(runs).where(and_(
                run_key,
                runs.c.version == current.version,
            )).values(
                version=next_state.version,
                state=next_state.to_dict(),
                updated_at=_now(),
            ))
            if changed.rowcount != 1:
                raise RuntimeError("concurrent lifecycle writer lost its version fence")

            now = _now()
            DBOS.sql_session.execute(insert(events).values(
                organization_id=organization_id,
                run_id=run_id,
                event_id=event.event_id,
                fingerprint=fingerprint,
                aggregate_version=next_state.version,
                event=event.to_dict(),
                created_at=now,
            ))
            envelopes = [item.to_dict() for item in decision.commands]
            if envelopes:
                DBOS.sql_session.execute(insert(commands), [{
                    "command_id": item["command_id"],
                    "organization_id": organization_id,
                    "run_id": run_id,
                    "event_id": event.event_id,
                    "aggregate_version": item["aggregate_version"],
                    "position": item["index"],
                    "envelope": item,
                    "status": "pending",
                    "attempts": 0,
                    "available_at": _command_available_at(item, now),
                    "created_at": now,
                } for item in envelopes])
            if event.kind is EventKind.SCOPE_ACCEPTED:
                # The CEO directive is simultaneously the lifecycle trigger and
                # the first authoritative company fact.  Commit both in the
                # same application transaction so the rich organization history
                # can never lose the mission that created it.
                stream_key = and_(
                    organization_streams.c.tenant_id == organization_id,
                    organization_streams.c.run_id == run_id,
                )
                stream_version = DBOS.sql_session.execute(select(
                    organization_streams.c.version,
                ).where(stream_key).with_for_update()).scalar_one()
                mission_event = OrganizationEvent(
                    event_id="orgevt-" + hashlib.sha256(
                        f"mission:{organization_id}:{run_id}:{event.event_id}".encode()
                    ).hexdigest(),
                    tenant_id=organization_id,
                    run_id=run_id,
                    actor_id=str(event.payload.get("requested_by") or "system:control-api"),
                    kind=OrganizationEventKind.MISSION_CHARTERED,
                    expected_version=int(stream_version),
                    occurred_at=now.isoformat(),
                    payload={
                        "outcome": event.payload.get("prompt"),
                        "title": event.payload.get("title"),
                        "source_event_id": event.event_id,
                    },
                    causation_id=event.event_id,
                )
                mission_raw = mission_event.to_dict()
                mission_version = int(stream_version) + 1
                DBOS.sql_session.execute(update(organization_streams).where(and_(
                    stream_key,
                    organization_streams.c.version == stream_version,
                )).values(version=mission_version, updated_at=now))
                DBOS.sql_session.execute(insert(organization_events).values(
                    tenant_id=organization_id,
                    run_id=run_id,
                    event_id=mission_event.event_id,
                    stream_version=mission_version,
                    fingerprint=organization_event_fingerprint(mission_event),
                    kind=mission_event.kind.value,
                    actor_id=mission_event.actor_id,
                    occurred_at=mission_event.occurred_at,
                    causation_id=mission_event.causation_id,
                    correlation_id=None,
                    payload=dict(mission_event.payload),
                    event=mission_raw,
                    created_at=now,
                ))
            return {"duplicate": False, "state": next_state.to_dict(), "commands": envelopes}

        @DBOS.workflow(name="agent_os_v2_start_lifecycle")
        def start_lifecycle(
            initial_raw: Mapping[str, Any],
            initial_event_raw: Mapping[str, Any] | None = None,
        ) -> Mapping[str, Any]:
            created = create_lifecycle(initial_raw)
            if initial_event_raw is None:
                return created
            initial = LifecycleState.from_dict(initial_raw)
            applied = apply_lifecycle_event(
                initial.organization_id,
                initial.run_id,
                initial_event_raw,
            )
            return {"created": created["created"], **applied}

        @DBOS.workflow(name="agent_os_v2_submit_lifecycle_event")
        def submit_lifecycle_event(
            organization_id: str,
            run_id: str,
            event_raw: Mapping[str, Any],
        ) -> Mapping[str, Any]:
            return apply_lifecycle_event(organization_id, run_id, event_raw)

        self._create_lifecycle = start_lifecycle
        self._submit_lifecycle_event = submit_lifecycle_event

    def start_run(
        self,
        initial_state: LifecycleState,
        initial_event: Event | None = None,
    ) -> WorkflowReceipt:
        raw = initial_state.to_dict()
        request_raw: dict[str, Any] = {"state": raw}
        if initial_event is not None:
            if initial_event.expected_version != 0:
                raise ValueError("initial event must expect version zero")
            request_raw["initial_event"] = initial_event.to_dict()
        fingerprint = _digest(request_raw)
        workflow_id = _workflow_id("start", initial_state.organization_id, initial_state.run_id, fingerprint)
        duplicate = DBOS.get_workflow_status(workflow_id) is not None
        with SetWorkflowID(workflow_id):
            DBOS.start_workflow(
                self._create_lifecycle,
                raw,
                None if initial_event is None else initial_event.to_dict(),
            )
        return WorkflowReceipt(workflow_id=workflow_id, duplicate=duplicate)

    def submit_event(
        self,
        organization_id: str,
        run_id: str,
        event: Event,
    ) -> WorkflowReceipt:
        if not organization_id.strip() or not run_id.strip():
            raise ValueError("organization_id and run_id are required")
        raw = event.to_dict()
        # Include the content fingerprint so malicious/conflicting reuse of an
        # event ID reaches the application transaction and fails closed instead
        # of being hidden by DBOS workflow de-duplication.
        fingerprint = event_fingerprint(event)
        workflow_id = _workflow_id("event", organization_id, run_id, fingerprint)
        duplicate = DBOS.get_workflow_status(workflow_id) is not None
        with SetWorkflowID(workflow_id):
            DBOS.start_workflow(self._submit_lifecycle_event, organization_id, run_id, raw)
        return WorkflowReceipt(workflow_id=workflow_id, duplicate=duplicate)

    def get_run(self, organization_id: str, run_id: str) -> LifecycleState | None:
        with self._tenant_connection(organization_id) as connection:
            raw = connection.execute(select(runs.c.state).where(and_(
                runs.c.organization_id == organization_id,
                runs.c.run_id == run_id,
            ))).scalar_one_or_none()
        return None if raw is None else LifecycleState.from_dict(raw)

    def list_runs(
        self,
        organization_id: str,
        *,
        limit: int = 100,
    ) -> tuple[LifecycleState, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("run list limit must be between 1 and 500")
        with self._tenant_connection(organization_id) as connection:
            raw = connection.execute(
                select(runs.c.state).where(
                    runs.c.organization_id == organization_id
                ).order_by(runs.c.updated_at.desc(), runs.c.run_id.desc()).limit(limit)
            ).scalars().all()
        return tuple(LifecycleState.from_dict(item) for item in raw)

    def cancel_run(
        self,
        organization_id: str,
        run_id: str,
        *,
        reason: str,
        expected_version: int,
        event_id: str,
    ) -> WorkflowReceipt:
        return self.submit_event(
            organization_id,
            run_id,
            Event(
                event_id=event_id,
                kind=EventKind.CANCEL_REQUESTED,
                expected_version=expected_version,
                payload={"reason": reason},
            ),
        )

    def get_result(self, workflow_id: str, *, timeout_seconds: float = 30) -> Mapping[str, Any]:
        """Wait for a mutation in tests/CLI paths; HTTP handlers remain async."""

        deadline = time.monotonic() + timeout_seconds
        terminal = {"SUCCESS", "ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED", "CANCELLED"}
        while True:
            status = DBOS.get_workflow_status(workflow_id)
            if status is not None and status.status in terminal:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"workflow {workflow_id} did not complete within {timeout_seconds}s")
            time.sleep(min(0.02, remaining))
        return DBOS.retrieve_workflow(workflow_id).get_result(polling_interval_sec=0.02)

    def list_commands(self, organization_id: str, run_id: str) -> tuple[Mapping[str, Any], ...]:
        with self._tenant_connection(organization_id) as connection:
            rows = connection.execute(select(commands.c.envelope).where(and_(
                commands.c.organization_id == organization_id,
                commands.c.run_id == run_id,
            )).order_by(commands.c.aggregate_version, commands.c.position)).scalars().all()
        return tuple(rows)

    def claim_command(
        self,
        organization_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> CommandLease | None:
        """Atomically claim pending work or recover a claim abandoned by a dead worker."""

        if not organization_id.strip() or not worker_id.strip():
            raise ValueError("organization_id and worker_id are required")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        now = _now()
        due = or_(
            and_(commands.c.status == "pending", commands.c.available_at <= now),
            and_(commands.c.status == "executing", commands.c.lease_expires_at < now),
        )
        # The compare-and-update predicate also protects SQLite tests.  On
        # PostgreSQL SKIP LOCKED lets many tenant workers claim in parallel.
        for _ in range(8):
            with self._tenant_connection(organization_id) as connection:
                query = select(
                    commands.c.command_id,
                    commands.c.envelope,
                    commands.c.attempts,
                ).where(and_(
                    commands.c.organization_id == organization_id,
                    due,
                )).order_by(commands.c.available_at, commands.c.created_at).limit(1)
                if connection.dialect.name == "postgresql":
                    query = query.with_for_update(skip_locked=True)
                row = connection.execute(query).mappings().first()
                if row is None:
                    return None
                expires = now + timedelta(seconds=lease_seconds)
                changed = connection.execute(update(commands).where(and_(
                    commands.c.organization_id == organization_id,
                    commands.c.command_id == row["command_id"],
                    due,
                )).values(
                    status="executing",
                    attempts=commands.c.attempts + 1,
                    lease_owner=worker_id,
                    lease_expires_at=expires,
                ))
                if changed.rowcount == 1:
                    return CommandLease(
                        envelope=row["envelope"],
                        worker_id=worker_id,
                        attempt=int(row["attempts"]) + 1,
                        lease_expires_at=expires.isoformat(),
                    )
        raise RuntimeError("command claim contention exceeded retry bound")

    def heartbeat_command(
        self,
        organization_id: str,
        command_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> bool:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        with self._tenant_connection(organization_id) as connection:
            changed = connection.execute(update(commands).where(and_(
                commands.c.organization_id == organization_id,
                commands.c.command_id == command_id,
                commands.c.status == "executing",
                commands.c.lease_owner == worker_id,
                commands.c.lease_expires_at >= _now(),
            )).values(lease_expires_at=_now() + timedelta(seconds=lease_seconds)))
            return changed.rowcount == 1

    def complete_command(
        self,
        organization_id: str,
        command_id: str,
        *,
        worker_id: str,
        result: Mapping[str, Any],
    ) -> bool:
        with self._tenant_connection(organization_id) as connection:
            changed = connection.execute(update(commands).where(and_(
                commands.c.organization_id == organization_id,
                commands.c.command_id == command_id,
                commands.c.status == "executing",
                commands.c.lease_owner == worker_id,
            )).values(
                status="succeeded",
                result=dict(result),
                completed_at=_now(),
                lease_owner=None,
                lease_expires_at=None,
                last_error=None,
            ))
            return changed.rowcount == 1

    def retry_command(
        self,
        organization_id: str,
        command_id: str,
        *,
        worker_id: str,
        error: Mapping[str, Any],
        delay_seconds: int,
    ) -> bool:
        if delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")
        with self._tenant_connection(organization_id) as connection:
            changed = connection.execute(update(commands).where(and_(
                commands.c.organization_id == organization_id,
                commands.c.command_id == command_id,
                commands.c.status == "executing",
                commands.c.lease_owner == worker_id,
            )).values(
                status="pending",
                available_at=_now() + timedelta(seconds=delay_seconds),
                lease_owner=None,
                lease_expires_at=None,
                last_error=dict(error),
            ))
            return changed.rowcount == 1

    def fail_command(
        self,
        organization_id: str,
        command_id: str,
        *,
        worker_id: str,
        error: Mapping[str, Any],
    ) -> bool:
        with self._tenant_connection(organization_id) as connection:
            changed = connection.execute(update(commands).where(and_(
                commands.c.organization_id == organization_id,
                commands.c.command_id == command_id,
                commands.c.status == "executing",
                commands.c.lease_owner == worker_id,
            )).values(
                status="failed",
                completed_at=_now(),
                lease_owner=None,
                lease_expires_at=None,
                last_error=dict(error),
            ))
            return changed.rowcount == 1

    def get_command_record(
        self,
        organization_id: str,
        command_id: str,
    ) -> Mapping[str, Any] | None:
        """Bounded inspection surface used by operations and contract tests."""

        with self._tenant_connection(organization_id) as connection:
            row = connection.execute(select(
                commands.c.command_id,
                commands.c.status,
                commands.c.attempts,
                commands.c.available_at,
                commands.c.lease_owner,
                commands.c.lease_expires_at,
                commands.c.result,
                commands.c.last_error,
            ).where(and_(
                commands.c.organization_id == organization_id,
                commands.c.command_id == command_id,
            ))).mappings().first()
        return None if row is None else dict(row)

    def append_organization_event(
        self,
        event: OrganizationEvent,
    ) -> OrganizationEventReceipt:
        """Commit one internal-company fact with whole-stream idempotency."""

        return self.append_organization_events((event,))[0]

    def append_organization_events(
        self,
        batch: tuple[OrganizationEvent, ...],
    ) -> tuple[OrganizationEventReceipt, ...]:
        """Atomically commit a complete agent turn—never a torn prefix."""

        if not batch:
            return ()
        tenant_id, run_id = batch[0].tenant_id, batch[0].run_id
        if any(item.tenant_id != tenant_id or item.run_id != run_id for item in batch):
            raise ValueError("organization event batch must belong to one tenant/run")
        if len({item.event_id for item in batch}) != len(batch):
            raise ValueError("organization event IDs must be unique within a batch")
        expected = tuple(range(batch[0].expected_version, batch[0].expected_version + len(batch)))
        if tuple(item.expected_version for item in batch) != expected:
            raise ValueError("organization event batch versions must be contiguous")
        fingerprints = {item.event_id: organization_event_fingerprint(item) for item in batch}

        with self._tenant_connection(tenant_id) as connection:
            prior_rows = connection.execute(select(
                organization_events.c.event_id,
                organization_events.c.fingerprint,
                organization_events.c.stream_version,
            ).where(and_(
                organization_events.c.tenant_id == tenant_id,
                organization_events.c.run_id == run_id,
                organization_events.c.event_id.in_([item.event_id for item in batch]),
            ))).mappings().all()
            prior = {row["event_id"]: row for row in prior_rows}
            for event_id, row in prior.items():
                if row["fingerprint"] != fingerprints[event_id]:
                    raise ValueError("organization event_id was reused with different content")
            if len(prior) == len(batch):
                return tuple(OrganizationEventReceipt(
                    int(prior[item.event_id]["stream_version"]), duplicate=True
                ) for item in batch)
            if prior:
                raise RuntimeError("organization event batch has a torn historical prefix")

            stream_key = and_(
                organization_streams.c.tenant_id == tenant_id,
                organization_streams.c.run_id == run_id,
            )
            current = connection.execute(select(
                organization_streams.c.version,
            ).where(stream_key).with_for_update()).scalar_one_or_none()
            if current is None:
                raise LookupError("organization event stream does not exist for this tenant/run")
            if int(current) != batch[0].expected_version:
                raise ValueError(
                    f"stale organization event version {batch[0].expected_version}; current version is {current}"
                )
            final_version = int(current) + len(batch)
            changed = connection.execute(update(organization_streams).where(and_(
                stream_key,
                organization_streams.c.version == current,
            )).values(version=final_version, updated_at=_now()))
            if changed.rowcount != 1:
                raise RuntimeError("concurrent organization writer lost its version fence")
            now = _now()
            connection.execute(insert(organization_events), [{
                "tenant_id": tenant_id,
                "run_id": run_id,
                "event_id": item.event_id,
                "stream_version": int(current) + position,
                "fingerprint": fingerprints[item.event_id],
                "kind": item.kind.value,
                "actor_id": item.actor_id,
                "occurred_at": item.occurred_at,
                "causation_id": item.causation_id,
                "correlation_id": item.correlation_id,
                "payload": dict(item.payload),
                "event": item.to_dict(),
                "created_at": now,
            } for position, item in enumerate(batch, start=1)])
            return tuple(
                OrganizationEventReceipt(int(current) + position)
                for position in range(1, len(batch) + 1)
            )

    def load_organization_events(
        self,
        tenant_id: str,
        run_id: str,
        *,
        after_version: int = 0,
        limit: int = 500,
    ) -> tuple[Mapping[str, Any], ...]:
        if after_version < 0 or not 1 <= limit <= 5000:
            raise ValueError("after_version must be nonnegative and limit must be 1..5000")
        with self._tenant_connection(tenant_id) as connection:
            rows = connection.execute(select(
                organization_events.c.stream_version,
                organization_events.c.event,
            ).where(and_(
                organization_events.c.tenant_id == tenant_id,
                organization_events.c.run_id == run_id,
                organization_events.c.stream_version > after_version,
            )).order_by(organization_events.c.stream_version).limit(limit)).mappings().all()
        return tuple({"stream_version": int(row["stream_version"]), **row["event"]} for row in rows)

    def health(self) -> Mapping[str, Any]:
        try:
            # Exercise the same least-privilege role and tenant RLS path as a
            # real request. Runtime logins are deliberately NOINHERIT and must
            # never need unscoped table access merely to report readiness.
            with self._tenant_connection("__agent_os_readiness__") as connection:
                connection.execute(select(1)).scalar_one()
                connection.execute(select(runs.c.run_id).limit(1)).first()
            return {"ok": True, "workflow_engine": "dbos", "database": "ready"}
        except Exception as exc:
            return {
                "ok": False,
                "workflow_engine": "dbos",
                "database": "unavailable",
                "error_type": type(exc).__name__,
            }

    @contextmanager
    def _tenant_connection(self, organization_id: str):
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_app"))
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": organization_id},
                )
            yield connection

    def close(self) -> None:
        DBOS.destroy(workflow_completion_timeout_sec=5)
        self._engine.dispose()

    def __enter__(self) -> "DBOSLifecycleEngine":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
