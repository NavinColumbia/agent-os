"""Release-aware execution-plane health backed by a durable global lease."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Callable, Mapping, Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Uuid,
    and_,
    create_engine,
    delete,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agent_os.application.ports import (
    ExecutionReleaseGate,
    ExecutionHealthReader,
    ReadyTenantSource,
    ReadyWorkSummary,
    WorkerActivitySink,
)
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url


execution_health_metadata = MetaData()

worker_health = Table(
    "aos_v2_worker_health",
    execution_health_metadata,
    Column("cell_id", String(128), primary_key=True),
    Column("worker_id", String(256), primary_key=True),
    Column("generation_id", Uuid(as_uuid=False), nullable=False),
    Column("application_version", String(128), nullable=False),
    Column("state", String(16), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("heartbeat_at", DateTime(timezone=True), nullable=False),
    Column("last_discovery_at", DateTime(timezone=True)),
    Column("last_successful_discovery_at", DateTime(timezone=True)),
    Column("discovery_error_streak", Integer, nullable=False, default=0),
    Column("busy_since", DateTime(timezone=True)),
    Column("last_main_progress_at", DateTime(timezone=True), nullable=False),
    Column("queue_probe_at", DateTime(timezone=True)),
    Column("ready_count_capped", Integer),
    Column("ready_count_truncated", Boolean),
    Column("oldest_ready_at", DateTime(timezone=True)),
    Column("oldest_queue_kind", String(32)),
    Column("last_probe_error_type", String(128)),
    Column("stopped_at", DateTime(timezone=True)),
)

execution_release = Table(
    "aos_v2_execution_release",
    execution_health_metadata,
    Column("cell_id", String(128), primary_key=True),
    Column("active_application_version", String(128), nullable=False),
    Column("activation_generation", Uuid(as_uuid=False), nullable=False),
    Column("activated_at", DateTime(timezone=True), nullable=False),
)


def _bounded(name: str, value: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or "\0" in normalized:
        raise ValueError(f"{name} must contain 1 to {maximum} safe characters")
    return normalized


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class WorkerGenerationSuperseded(RuntimeError):
    """A newer process generation now owns this worker identity."""


class SQLExecutionReleaseGate(ExecutionReleaseGate):
    """Durable cell-level fence preventing mixed-release queue claims."""

    def __init__(
        self,
        database_url: str,
        *,
        cell_id: str,
        application_version: str,
        statement_timeout_seconds: int = 5,
    ) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        self._cell_id = _bounded("cell_id", cell_id, 128)
        self._application_version = _bounded(
            "application_version", application_version, 128,
        )
        if not 1 <= statement_timeout_seconds <= 86_400:
            raise ValueError("statement_timeout_seconds must be between 1 and 86400")
        self._statement_timeout_ms = statement_timeout_seconds * 1_000
        normalized_url = sqlalchemy_url(database_url)
        connect_args = (
            {"connect_timeout": min(statement_timeout_seconds, 10)}
            if normalized_url.startswith("postgresql") else {}
        )
        self._engine = create_engine(
            normalized_url, pool_pre_ping=True, connect_args=connect_args,
        )

    @contextmanager
    def _connection(self):
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_worker"))
                connection.execute(
                    text("SELECT set_config('statement_timeout', :timeout, true)"),
                    {"timeout": str(self._statement_timeout_ms)},
                )
            yield connection

    def is_active(self) -> bool:
        with self._connection() as connection:
            active = connection.execute(select(
                execution_release.c.active_application_version,
            ).where(
                execution_release.c.cell_id == self._cell_id,
            )).scalar_one_or_none()
        return active == self._application_version

    @contextmanager
    def claim_window(self):
        with self._connection() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text(
                    "SELECT pg_advisory_xact_lock_shared("
                    "hashtextextended(:cell_id, 1196439115))"
                ), {"cell_id": self._cell_id})
                active = connection.execute(select(
                    execution_release.c.active_application_version,
                ).where(
                    execution_release.c.cell_id == self._cell_id,
                )).scalar_one_or_none()
                yield active == self._application_version
                return
            # SQLite is an evaluation backend with no advisory locks. Close
            # its read transaction before yielding so the worker can write.
            active = connection.execute(select(
                execution_release.c.active_application_version,
            ).where(
                execution_release.c.cell_id == self._cell_id,
            )).scalar_one_or_none()
        yield active == self._application_version

    def activate(self) -> str:
        now = datetime.now(timezone.utc)
        generation = str(uuid4())
        values = {
            "cell_id": self._cell_id,
            "active_application_version": self._application_version,
            "activation_generation": generation,
            "activated_at": now,
        }
        with self._connection() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended(:cell_id, 1196439115))"
                ), {"cell_id": self._cell_id})
            base = (
                postgres_insert(execution_release)
                if connection.dialect.name == "postgresql"
                else sqlite_insert(execution_release)
            )
            connection.execute(base.values(**values).on_conflict_do_update(
                index_elements=[execution_release.c.cell_id],
                set_={key: value for key, value in values.items() if key != "cell_id"},
            ))
        return generation

    def close(self) -> None:
        self._engine.dispose()


class SQLExecutionHealthReader(ExecutionHealthReader):
    """Read-only, release-fenced health projection for the API process."""

    def __init__(
        self,
        database_url: str,
        *,
        create_schema: bool = False,
        statement_timeout_seconds: int = 5,
    ) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        if not 1 <= statement_timeout_seconds <= 30:
            raise ValueError("statement_timeout_seconds must be between 1 and 30")
        self._statement_timeout_ms = statement_timeout_seconds * 1_000
        normalized_url = sqlalchemy_url(database_url)
        connect_args = (
            {"connect_timeout": min(statement_timeout_seconds, 10)}
            if normalized_url.startswith("postgresql") else {}
        )
        self._engine = create_engine(
            normalized_url, pool_pre_ping=True, connect_args=connect_args,
        )
        if create_schema:
            execution_health_metadata.create_all(self._engine)

    @contextmanager
    def _connection(self):
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_app"))
                connection.execute(
                    text("SELECT set_config('statement_timeout', :timeout, true)"),
                    {"timeout": str(self._statement_timeout_ms)},
                )
            yield connection

    def readiness(
        self,
        *,
        now: datetime,
        cell_id: str,
        application_version: str,
        heartbeat_max_age_seconds: int,
        queue_probe_max_age_seconds: int,
        discovery_max_age_seconds: int,
        no_progress_max_age_seconds: int,
        backlog_max_age_seconds: int,
        discovery_failure_limit: int,
    ) -> Mapping[str, Any]:
        now = _utc(now)
        cell_id = _bounded("cell_id", cell_id, 128)
        application_version = _bounded("application_version", application_version, 128)
        if min(
            heartbeat_max_age_seconds,
            queue_probe_max_age_seconds,
            discovery_max_age_seconds,
            no_progress_max_age_seconds,
            backlog_max_age_seconds,
            discovery_failure_limit,
        ) < 1:
            raise ValueError("execution health thresholds must be positive")
        heartbeat_cutoff = now - timedelta(seconds=heartbeat_max_age_seconds)
        with self._connection() as connection:
            active_release = connection.execute(select(
                execution_release.c.active_application_version,
            ).where(
                execution_release.c.cell_id == cell_id,
            )).scalar_one_or_none()
            if active_release is None:
                return {
                    "ok": False,
                    "state": "unavailable",
                    "reason": "release_unconfigured",
                }
            rows = connection.execute(select(worker_health).where(and_(
                worker_health.c.cell_id == cell_id,
                worker_health.c.application_version == active_release,
                worker_health.c.state == "running",
                worker_health.c.heartbeat_at >= heartbeat_cutoff,
            ))).mappings().all()
        if not rows:
            return {"ok": False, "state": "unavailable", "reason": "worker_unavailable"}

        probe_cutoff = now - timedelta(seconds=queue_probe_max_age_seconds)
        fresh_probes = [
            row for row in rows
            if row["queue_probe_at"] is not None and _utc(row["queue_probe_at"]) >= probe_cutoff
        ]
        if not fresh_probes:
            return {"ok": False, "state": "degraded", "reason": "probe_stale"}

        backlog_cutoff = now - timedelta(seconds=backlog_max_age_seconds)
        latest_probe = max(fresh_probes, key=lambda row: _utc(row["queue_probe_at"]))
        if (
            latest_probe["oldest_ready_at"] is not None
            and _utc(latest_probe["oldest_ready_at"]) < backlog_cutoff
        ):
            return {"ok": False, "state": "degraded", "reason": "queue_stalled"}

        discovery_cutoff = now - timedelta(seconds=discovery_max_age_seconds)
        progress_cutoff = now - timedelta(seconds=no_progress_max_age_seconds)
        discovery_healthy = any(
            (
                _utc(row["last_main_progress_at"]) >= progress_cutoff
                if row["busy_since"] is not None
                else (
                    row["last_successful_discovery_at"] is not None
                    and _utc(row["last_successful_discovery_at"]) >= discovery_cutoff
                    and int(row["discovery_error_streak"]) < discovery_failure_limit
                )
            )
            for row in fresh_probes
        )
        if not discovery_healthy:
            if any(row["busy_since"] is not None for row in fresh_probes):
                return {"ok": False, "state": "degraded", "reason": "worker_stalled"}
            return {"ok": False, "state": "degraded", "reason": "discovery_failing"}
        return {"ok": True, "state": "ready", "reason": "ready"}

    def close(self) -> None:
        self._engine.dispose()


class SQLWorkerHealthReporter(WorkerActivitySink):
    """Independent heartbeat/probe publisher with generation-fenced updates."""

    def __init__(
        self,
        database_url: str,
        *,
        ready_source: ReadyTenantSource,
        cell_id: str,
        worker_id: str,
        application_version: str,
        heartbeat_seconds: float = 15,
        queue_sample_cap: int = 1_000,
        create_schema: bool = False,
        clock: Callable[[], datetime] | None = None,
        statement_timeout_seconds: int = 5,
        shutdown_timeout_seconds: float = 30,
        observer: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        if queue_sample_cap < 100 or queue_sample_cap > 10_000:
            raise ValueError("queue_sample_cap must be between 100 and 10000")
        if not 1 <= statement_timeout_seconds <= 30:
            raise ValueError("statement_timeout_seconds must be between 1 and 30")
        if shutdown_timeout_seconds <= 0 or shutdown_timeout_seconds > 120:
            raise ValueError("shutdown_timeout_seconds must be greater than 0 and at most 120")
        self._cell_id = _bounded("cell_id", cell_id, 128)
        self._worker_id = _bounded("worker_id", worker_id, 256)
        self._application_version = _bounded(
            "application_version", application_version, 128,
        )
        self._generation_id = str(uuid4())
        self._ready_source = ready_source
        self._heartbeat_seconds = heartbeat_seconds
        self._queue_sample_cap = queue_sample_cap
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._statement_timeout_ms = statement_timeout_seconds * 1_000
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._observer = observer or (lambda _: None)
        self._reported_failure: tuple[str, str] | None = None
        self._release_ready_observed = False
        self._release_probe_observed = False
        normalized_url = sqlalchemy_url(database_url)
        connect_args = (
            {"connect_timeout": min(statement_timeout_seconds, 10)}
            if normalized_url.startswith("postgresql") else {}
        )
        self._engine = create_engine(
            normalized_url, pool_pre_ping=True, connect_args=connect_args,
        )
        if create_schema:
            execution_health_metadata.create_all(self._engine)
        self._stop = Event()
        self._wake = Event()
        self._thread: Thread | None = None
        self._lock = Lock()
        self._fatal: BaseException | None = None
        self._last_discovery_at: datetime | None = None
        self._last_successful_discovery_at: datetime | None = None
        self._discovery_error_streak = 0
        self._busy_since: datetime | None = None
        self._last_main_progress_at: datetime | None = None
        self._accepting_work = False
        self._standby_ready_at: datetime | None = None

    @property
    def generation_id(self) -> str:
        return self._generation_id

    @contextmanager
    def _connection(self):
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_worker"))
                connection.execute(
                    text("SELECT set_config('statement_timeout', :timeout, true)"),
                    {"timeout": str(self._statement_timeout_ms)},
                )
            yield connection

    def _values(self, summary: ReadyWorkSummary) -> dict[str, Any]:
        return {
            "queue_probe_at": _utc(summary.observed_at),
            "ready_count_capped": summary.ready_count_capped,
            "ready_count_truncated": summary.truncated,
            "oldest_ready_at": (
                _utc(summary.oldest_ready_at) if summary.oldest_ready_at is not None else None
            ),
            "oldest_queue_kind": summary.oldest_queue_kind,
            "last_probe_error_type": None,
        }

    def _activity_values(self) -> dict[str, Any]:
        with self._lock:
            last_discovery_at = self._last_discovery_at
            last_successful_discovery_at = self._last_successful_discovery_at
            discovery_error_streak = self._discovery_error_streak
            busy_since = self._busy_since
            last_main_progress_at = self._last_main_progress_at
            accepting_work = self._accepting_work
            standby_ready_at = self._standby_ready_at
        return {
            "state": (
                "running" if accepting_work and last_successful_discovery_at is not None
                else "standby" if standby_ready_at is not None
                else "starting"
            ),
            "last_discovery_at": last_discovery_at,
            "last_successful_discovery_at": last_successful_discovery_at,
            "discovery_error_streak": discovery_error_streak,
            "busy_since": busy_since,
            "last_main_progress_at": last_main_progress_at,
        }

    def _ensure_current(self, rowcount: int | None) -> None:
        if rowcount != 1:
            raise WorkerGenerationSuperseded(
                "worker health generation was superseded by a newer process"
            )

    def _generation_where(self):
        return and_(
            worker_health.c.cell_id == self._cell_id,
            worker_health.c.worker_id == self._worker_id,
            worker_health.c.generation_id == self._generation_id,
            worker_health.c.state.in_(("starting", "standby", "running")),
        )

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("worker health reporter is already started")
        try:
            summary = self._ready_source.summarize_ready_work(limit=self._queue_sample_cap)
            now = _utc(self._clock())
            with self._lock:
                self._last_main_progress_at = now
            values = {
                "cell_id": self._cell_id,
                "worker_id": self._worker_id,
                "generation_id": self._generation_id,
                "application_version": self._application_version,
                "state": "starting",
                "started_at": now,
                "heartbeat_at": now,
                "last_discovery_at": None,
                "last_successful_discovery_at": None,
                "discovery_error_streak": 0,
                "busy_since": None,
                "last_main_progress_at": now,
                **self._values(summary),
                "stopped_at": None,
            }
            with self._connection() as connection:
                base = (
                    postgres_insert(worker_health)
                    if connection.dialect.name == "postgresql"
                    else sqlite_insert(worker_health)
                )
                statement = base.values(**values).on_conflict_do_update(
                    index_elements=[worker_health.c.cell_id, worker_health.c.worker_id],
                    set_={
                        key: value for key, value in values.items()
                        if key not in {"cell_id", "worker_id"}
                    },
                )
                connection.execute(statement)
                connection.execute(delete(worker_health).where(and_(
                    worker_health.c.heartbeat_at < now - timedelta(days=7),
                )))
        except Exception:
            self._engine.dispose()
            raise
        self._thread = Thread(
            target=self._run,
            name="agent-os-worker-health",
            daemon=True,
        )
        self._thread.start()

    def _record_fatal(self, exc: BaseException) -> None:
        with self._lock:
            self._fatal = exc
        self._stop.set()

    def _observe_failure(self, stage: str, exc: BaseException) -> None:
        failure = (stage, type(exc).__name__[:128])
        with self._lock:
            if self._reported_failure == failure:
                return
            self._reported_failure = failure
        try:
            self._observer({
                "event": "worker_health_error",
                "stage": failure[0],
                "error_type": failure[1],
            })
        except Exception:
            pass

    def _observe_recovery(self) -> None:
        with self._lock:
            previous = self._reported_failure
            self._reported_failure = None
        if previous is None:
            return
        try:
            self._observer({
                "event": "worker_health_recovered",
                "previous_stage": previous[0],
                "previous_error_type": previous[1],
            })
        except Exception:
            pass

    def _observe_release_ready(self, activity: Mapping[str, Any]) -> None:
        event_name = {
            "standby": "worker_release_probe_ready",
            "running": "worker_release_ready",
        }.get(str(activity["state"]))
        if event_name is None:
            return
        with self._lock:
            if event_name == "worker_release_ready":
                if self._release_ready_observed:
                    return
                self._release_ready_observed = True
            else:
                if self._release_probe_observed:
                    return
                self._release_probe_observed = True
        try:
            self._observer({
                "event": event_name,
                "worker_id": self._worker_id,
                "execution_cell_id": self._cell_id,
                "application_version": self._application_version,
            })
        except Exception:
            pass

    def _raise_if_fatal(self) -> None:
        with self._lock:
            failure = self._fatal
        if failure is not None:
            raise RuntimeError("worker health publisher failed") from failure

    def _publish(self) -> None:
        now = _utc(self._clock())
        try:
            summary = self._ready_source.summarize_ready_work(limit=self._queue_sample_cap)
        except Exception as exc:
            with self._connection() as connection:
                result = connection.execute(update(worker_health).where(
                    self._generation_where()
                ).values(
                    heartbeat_at=now,
                    last_probe_error_type=type(exc).__name__[:128],
                    **self._activity_values(),
                ))
                self._ensure_current(result.rowcount)
            self._observe_failure("queue_probe", exc)
            return
        activity = self._activity_values()
        with self._connection() as connection:
            result = connection.execute(update(worker_health).where(
                self._generation_where()
            ).values(
                heartbeat_at=now,
                **self._values(summary),
                **activity,
            ))
            self._ensure_current(result.rowcount)
        self._observe_recovery()
        self._observe_release_ready(activity)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._heartbeat_seconds)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self._publish()
            except WorkerGenerationSuperseded as exc:
                self._observe_failure("generation", exc)
                self._record_fatal(exc)
                return
            except Exception as exc:
                # A transient database outage is represented by an aging
                # heartbeat. Retrying lets a recovered database heal without
                # killing legitimate long-running work.
                self._observe_failure("publisher", exc)
                continue

    def _activity_update(self, update: Callable[[], None]) -> None:
        self._raise_if_fatal()
        with self._lock:
            update()

    def discovery_succeeded(self, at: datetime) -> None:
        at = _utc(at)

        def apply() -> None:
            self._last_discovery_at = at
            self._last_successful_discovery_at = at
            self._discovery_error_streak = 0
            self._accepting_work = True

        self._activity_update(apply)

    def standby_succeeded(self, at: datetime) -> None:
        at = _utc(at)

        def apply() -> None:
            self._standby_ready_at = at
            self._accepting_work = False
            self._busy_since = None
            self._last_main_progress_at = at

        self._activity_update(apply)

    def discovery_failed(self, at: datetime, error_type: str) -> None:
        del error_type  # Detailed failures remain in restricted structured logs.
        at = _utc(at)

        def apply() -> None:
            self._last_discovery_at = at
            self._discovery_error_streak += 1

        self._activity_update(apply)

    def work_started(self, at: datetime) -> None:
        at = _utc(at)

        def apply() -> None:
            self._busy_since = at
            self._last_main_progress_at = at

        self._activity_update(apply)

    def work_finished(self, at: datetime) -> None:
        at = _utc(at)

        def apply() -> None:
            self._busy_since = None
            self._last_main_progress_at = at

        self._activity_update(apply)

    def work_progressed(self, at: datetime) -> None:
        at = _utc(at)

        def apply() -> None:
            self._last_main_progress_at = at

        self._activity_update(apply)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._shutdown_timeout_seconds)
            if thread.is_alive():
                raise RuntimeError("worker health publisher did not stop within its deadline")
        now = _utc(self._clock())
        try:
            with self._connection() as connection:
                connection.execute(update(worker_health).where(and_(
                    worker_health.c.cell_id == self._cell_id,
                    worker_health.c.worker_id == self._worker_id,
                    worker_health.c.generation_id == self._generation_id,
                )).values(
                    state="stopped",
                    heartbeat_at=now,
                    busy_since=None,
                    stopped_at=now,
                ))
        finally:
            self._engine.dispose()
