from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
from threading import Event, Thread
from typing import Any, Mapping

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from agent_os.api.app import create_app
from agent_os.application.command_worker import CommandRunReport, CommandRunStatus
from agent_os.application.worker_loop import CommandWorkerLoop
from agent_os.application.ports import ReadyWorkSummary
from agent_os.infrastructure.sql_execution_health import (
    SQLExecutionReleaseGate,
    SQLExecutionHealthReader,
    SQLWorkerHealthReporter,
)
from agent_os.infrastructure.sql_ready_tenants import SQLReadyTenantSource


QUEUES = (
    ("aos_v2_lifecycle_commands", "available_at"),
    ("aos_v2_workflow_actions", "available_at"),
    ("aos_v2_management_watches", "next_check_at"),
    ("aos_v2_notification_deliveries", "available_at"),
    ("aos_v2_web_push_deliveries", "available_at"),
    ("aos_v2_decision_responses", "available_at"),
)


def queue_database(tmp_path: Path) -> tuple[str, Any]:
    url = f"sqlite:///{tmp_path / 'execution-health.sqlite3'}"
    engine = create_engine(url)
    with engine.begin() as connection:
        for table_name, ready_column in QUEUES:
            connection.execute(text(f"""
                CREATE TABLE {table_name} (
                    tenant_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    {ready_column} DATETIME NOT NULL,
                    lease_expires_at DATETIME
                )
            """))
    return url, engine


def readiness(
    reader: SQLExecutionHealthReader,
    now: datetime,
    *,
    version: str = "release-a",
    no_progress_seconds: int = 7_200,
):
    return reader.readiness(
        now=now,
        cell_id="cell-a",
        application_version=version,
        heartbeat_max_age_seconds=60,
        queue_probe_max_age_seconds=60,
        discovery_max_age_seconds=60,
        no_progress_max_age_seconds=no_progress_seconds,
        backlog_max_age_seconds=120,
        discovery_failure_limit=3,
    )


def eventually(check, *, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition did not become true before its deadline")


def activate_release(url: str, version: str = "release-a") -> None:
    gate = SQLExecutionReleaseGate(
        url,
        cell_id="cell-a",
        application_version=version,
    )
    try:
        gate.activate()
    finally:
        gate.close()


def test_ready_work_summary_is_bounded_cross_queue_and_payload_free(tmp_path: Path):
    url, engine = queue_database(tmp_path)
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        for offset, (table_name, ready_column) in enumerate(QUEUES, start=1):
            connection.execute(text(
                f"INSERT INTO {table_name} "
                f"(tenant_id, status, {ready_column}, lease_expires_at) "
                "VALUES (:tenant, 'pending', :due, NULL)"
            ), {"tenant": f"secret-tenant-{offset}", "due": now - timedelta(seconds=offset)})
            connection.execute(text(
                f"INSERT INTO {table_name} "
                f"(tenant_id, status, {ready_column}, lease_expires_at) "
                "VALUES (:tenant, 'executing', :future, :expired)"
            ), {
                "tenant": f"other-secret-{offset}",
                "future": now + timedelta(hours=1),
                "expired": now - timedelta(seconds=offset + 20),
            })
            connection.execute(text(
                f"INSERT INTO {table_name} "
                f"(tenant_id, status, {ready_column}, lease_expires_at) "
                "VALUES ('not-ready', 'pending', :future, NULL)"
            ), {"future": now + timedelta(hours=1)})
    source = SQLReadyTenantSource(url)
    try:
        summary = source.summarize_ready_work(limit=100)
    finally:
        source.close()
        engine.dispose()

    assert summary.ready_count_capped == 12
    assert not summary.truncated
    assert summary.oldest_queue_kind == "decision"
    assert summary.oldest_ready_at is not None
    assert not hasattr(summary, "tenant_id")


def test_activity_is_coalesced_to_the_configured_health_interval(tmp_path: Path):
    url, engine = queue_database(tmp_path)
    calls = 0

    class CountingSource:
        def summarize_ready_work(self, *, limit=1000):
            nonlocal calls
            del limit
            calls += 1
            return ReadyWorkSummary(datetime.now(timezone.utc), 0, False, None, None)

    reporter = SQLWorkerHealthReporter(
        url,
        ready_source=CountingSource(),  # type: ignore[arg-type]
        cell_id="cell-a",
        worker_id="worker-a",
        application_version="release-a",
        heartbeat_seconds=0.2,
        queue_sample_cap=100,
        create_schema=True,
    )
    try:
        reporter.start()
        now = datetime.now(timezone.utc)
        for _ in range(100):
            reporter.discovery_succeeded(now)
            reporter.work_started(now)
            reporter.work_progressed(now)
            reporter.work_finished(now)
        time.sleep(0.05)
        assert calls == 1
        eventually(lambda: calls >= 2)
    finally:
        reporter.stop()
        engine.dispose()


def test_inactive_release_stays_probe_only_until_atomic_activation(tmp_path: Path):
    url, engine = queue_database(tmp_path)
    source = SQLReadyTenantSource(url)
    SQLExecutionHealthReader(url, create_schema=True).close()
    events: list[Mapping[str, Any]] = []
    calls = 0

    class Worker:
        def run_one(self, organization_id):
            nonlocal calls
            del organization_id
            calls += 1
            return CommandRunReport(CommandRunStatus.IDLE)

    gate = SQLExecutionReleaseGate(
        url, cell_id="cell-a", application_version="release-b",
    )
    reporter = SQLWorkerHealthReporter(
        url,
        ready_source=source,
        cell_id="cell-a",
        worker_id="candidate",
        application_version="release-b",
        heartbeat_seconds=0.02,
        queue_sample_cap=100,
        observer=events.append,
    )
    loop = CommandWorkerLoop(
        worker=Worker(),  # type: ignore[arg-type]
        organization_ids=("tenant-a",),
        activity=reporter,
        execution_gate=gate,
    )
    try:
        reporter.start()
        assert loop.run_cycle() == ()
        assert calls == 0
        eventually(lambda: any(
            item["event"] == "worker_release_probe_ready" for item in events
        ))

        gate.activate()
        loop.run_cycle()
        assert calls == 1
        eventually(lambda: any(
            item["event"] == "worker_release_ready" for item in events
        ))
    finally:
        reporter.stop()
        gate.close()
        source.close()
        engine.dispose()


def test_reporter_fences_generations_and_reader_detects_recovery(tmp_path: Path):
    url, engine = queue_database(tmp_path)
    source = SQLReadyTenantSource(url)
    reader = SQLExecutionHealthReader(url, create_schema=True)
    activate_release(url)
    first = SQLWorkerHealthReporter(
        url,
        ready_source=source,
        cell_id="cell-a",
        worker_id="worker-a",
        application_version="release-a",
        heartbeat_seconds=0.02,
        queue_sample_cap=100,
    )
    second = SQLWorkerHealthReporter(
        url,
        ready_source=source,
        cell_id="cell-a",
        worker_id="worker-a",
        application_version="release-a",
        heartbeat_seconds=0.02,
        queue_sample_cap=100,
    )
    try:
        first.start()
        now = datetime.now(timezone.utc)
        assert readiness(reader, now)["reason"] == "worker_unavailable"
        first.discovery_succeeded(now)
        eventually(lambda: readiness(reader, now)["ok"])
        assert readiness(reader, now, version="old-api-release")["ok"]

        second.start()
        first.work_started(now)

        def old_generation_is_fenced():
            try:
                first.work_started(now)
            except RuntimeError as exc:
                return "publisher failed" in str(exc)
            return False

        eventually(old_generation_is_fenced)
        second.discovery_succeeded(now)
        for _ in range(3):
            second.discovery_failed(now, "ConnectionError")
        eventually(lambda: readiness(reader, now)["reason"] == "discovery_failing")
        second.discovery_succeeded(now)
        eventually(lambda: readiness(reader, now)["ok"])
        assert readiness(reader, now + timedelta(seconds=61))["reason"] == "worker_unavailable"
    finally:
        first.stop()
        second.stop()
        reader.close()
        source.close()
        engine.dispose()


def test_release_ready_is_emitted_only_after_discovery_is_durably_published(tmp_path: Path):
    url, engine = queue_database(tmp_path)
    source = SQLReadyTenantSource(url)
    events: list[Mapping[str, Any]] = []
    reporter = SQLWorkerHealthReporter(
        url,
        ready_source=source,
        cell_id="cell-a",
        worker_id="worker-a",
        application_version="release-a",
        heartbeat_seconds=0.02,
        queue_sample_cap=100,
        create_schema=True,
        observer=events.append,
    )
    try:
        reporter.start()
        assert not any(item["event"] == "worker_release_ready" for item in events)
        reporter.discovery_succeeded(datetime.now(timezone.utc))
        event = eventually(lambda: next(
            (item for item in events if item["event"] == "worker_release_ready"), None,
        ))
        assert event["application_version"] == "release-a"
        assert event["execution_cell_id"] == "cell-a"
        reporter.work_started(datetime.now(timezone.utc))
        reporter.work_finished(datetime.now(timezone.utc))
        time.sleep(0.05)
        assert sum(item["event"] == "worker_release_ready" for item in events) == 1
    finally:
        reporter.stop()
        source.close()
        engine.dispose()


def test_independent_heartbeat_continues_while_main_loop_is_blocked(tmp_path: Path):
    url, engine = queue_database(tmp_path)
    source = SQLReadyTenantSource(url)
    reporter = SQLWorkerHealthReporter(
        url,
        ready_source=source,
        cell_id="cell-a",
        worker_id="worker-a",
        application_version="release-a",
        heartbeat_seconds=0.02,
        queue_sample_cap=100,
        create_schema=True,
    )
    try:
        reporter.start()
        reporter.discovery_succeeded(datetime.now(timezone.utc))
        reporter.work_started(datetime.now(timezone.utc))
        with engine.begin() as connection:
            before = connection.execute(text(
                "SELECT heartbeat_at FROM aos_v2_worker_health"
            )).scalar_one()
        time.sleep(0.08)
        with engine.begin() as connection:
            after = connection.execute(text(
                "SELECT heartbeat_at FROM aos_v2_worker_health"
            )).scalar_one()
        assert after > before
    finally:
        reporter.stop()
        source.close()
        engine.dispose()


def test_background_heartbeat_cannot_hide_a_main_executor_with_no_progress(tmp_path: Path):
    url, engine = queue_database(tmp_path)
    current = [datetime.now(timezone.utc)]
    source = SQLReadyTenantSource(url)
    reporter = SQLWorkerHealthReporter(
        url,
        ready_source=source,
        cell_id="cell-a",
        worker_id="worker-a",
        application_version="release-a",
        heartbeat_seconds=0.02,
        queue_sample_cap=100,
        create_schema=True,
        clock=lambda: current[0],
    )
    reader = SQLExecutionHealthReader(url)
    activate_release(url)
    entered = Event()
    release = Event()

    class BlockingWorker:
        def run_one(self, organization_id):
            del organization_id
            entered.set()
            release.wait(2)
            return CommandRunReport(CommandRunStatus.IDLE)

    loop = CommandWorkerLoop(
        worker=BlockingWorker(),  # type: ignore[arg-type]
        organization_ids=("tenant-a",),
        activity=reporter,
    )
    runner = Thread(target=loop.run_cycle)
    try:
        reporter.start()
        runner.start()
        assert entered.wait(1)
        eventually(lambda: readiness(reader, current[0], no_progress_seconds=60)["ok"])

        current[0] += timedelta(seconds=61)
        eventually(
            lambda: readiness(reader, current[0], no_progress_seconds=60)["reason"]
            == "worker_stalled"
        )
        assert runner.is_alive()  # Health diagnosis did not terminate the story.
    finally:
        release.set()
        runner.join(timeout=2)
        reporter.stop()
        reader.close()
        source.close()
        engine.dispose()


def test_startup_prunes_crashed_worker_rows_after_seven_days(tmp_path: Path):
    url, engine = queue_database(tmp_path)
    reader = SQLExecutionHealthReader(url, create_schema=True)
    now = datetime.now(timezone.utc)
    stale = now - timedelta(days=8)
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO aos_v2_worker_health (
                cell_id, worker_id, generation_id, application_version, state,
                started_at, heartbeat_at, discovery_error_streak,
                last_main_progress_at
            ) VALUES (
                'cell-a', 'crashed-worker', :generation, 'release-a', 'running',
                :stale, :stale, 0, :stale
            )
        """), {"generation": "9d6e5d31-b0ef-4e0a-a047-1a930391e321", "stale": stale})
    source = SQLReadyTenantSource(url)
    reporter = SQLWorkerHealthReporter(
        url,
        ready_source=source,
        cell_id="cell-a",
        worker_id="fresh-worker",
        application_version="release-a",
        queue_sample_cap=100,
    )
    try:
        reporter.start()
        with engine.begin() as connection:
            workers = set(connection.execute(text(
                "SELECT worker_id FROM aos_v2_worker_health"
            )).scalars())
        assert workers == {"fresh-worker"}
    finally:
        reporter.stop()
        source.close()
        reader.close()
        engine.dispose()


def test_shutdown_never_marks_a_live_blocked_publisher_stopped(tmp_path: Path):
    url, engine = queue_database(tmp_path)
    reader = SQLExecutionHealthReader(url, create_schema=True)
    entered = Event()
    release = Event()
    calls = 0

    class BlockingSource:
        def list_ready_tenants(self, **kwargs):
            del kwargs
            return ()

        def summarize_ready_work(self, *, limit=1000):
            nonlocal calls
            del limit
            calls += 1
            if calls > 1:
                entered.set()
                release.wait(2)
            now = datetime.now(timezone.utc)
            return ReadyWorkSummary(now, 0, False, None, None)

    reporter = SQLWorkerHealthReporter(
        url,
        ready_source=BlockingSource(),  # type: ignore[arg-type]
        cell_id="cell-a",
        worker_id="worker-a",
        application_version="release-a",
        heartbeat_seconds=0.02,
        queue_sample_cap=100,
        shutdown_timeout_seconds=0.05,
    )
    try:
        reporter.start()
        reporter.discovery_succeeded(datetime.now(timezone.utc))
        assert entered.wait(1)
        with pytest.raises(RuntimeError, match="did not stop"):
            reporter.stop()
        with engine.begin() as connection:
            state = connection.execute(text(
                "SELECT state FROM aos_v2_worker_health WHERE worker_id = 'worker-a'"
            )).scalar_one()
        assert state != "stopped"
        release.set()
        eventually(lambda: reporter._thread is not None and not reporter._thread.is_alive())
        reporter.stop()
    finally:
        release.set()
        if reporter._thread is not None and reporter._thread.is_alive():
            reporter._thread.join(timeout=2)
        reader.close()
        engine.dispose()


def test_old_eligible_backlog_degrades_readiness_without_timing_out_work(tmp_path: Path):
    url, engine = queue_database(tmp_path)
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO aos_v2_workflow_actions
                (tenant_id, status, available_at, lease_expires_at)
            VALUES ('private-tenant', 'pending', :due, NULL)
        """), {"due": now - timedelta(minutes=5)})
    source = SQLReadyTenantSource(url)
    reporter = SQLWorkerHealthReporter(
        url,
        ready_source=source,
        cell_id="cell-a",
        worker_id="worker-a",
        application_version="release-a",
        heartbeat_seconds=0.02,
        queue_sample_cap=100,
        create_schema=True,
    )
    reader = SQLExecutionHealthReader(url)
    activate_release(url)
    try:
        reporter.start()
        reporter.discovery_succeeded(now)
        report = eventually(lambda: (
            candidate if (candidate := readiness(reader, now))["reason"] == "queue_stalled"
            else None
        ))
        assert report == {
            "ok": False,
            "state": "degraded",
            "reason": "queue_stalled",
        }
    finally:
        reporter.stop()
        reader.close()
        source.close()
        engine.dispose()


class StubEngine:
    def health(self) -> Mapping[str, Any]:
        return {"ok": True, "workflow_engine": "stub", "database": "ready"}


class StubIdentity:
    def authenticate(self, authorization, session):
        del authorization, session
        return {"subject_id": "user", "organization_id": "tenant", "roles": ["owner"]}


class StubExecutionHealth:
    def __init__(self, report: Mapping[str, Any] | None = None, *, fail: bool = False):
        self.report = report or {"ok": True, "state": "ready", "reason": "ready"}
        self.fail = fail

    def readiness(self, **kwargs):
        del kwargs
        if self.fail:
            raise ConnectionError("private database details")
        return self.report


def test_public_readiness_is_coarse_and_fails_closed():
    app = create_app(
        engine=StubEngine(),  # type: ignore[arg-type]
        identity=StubIdentity(),  # type: ignore[arg-type]
        execution_health=StubExecutionHealth({
            "ok": False,
            "state": "degraded",
            "reason": "queue_stalled",
            "worker_id": "must-not-leak",
            "ready_count_capped": 999,
        }),
    )
    response = TestClient(app).get("/ready")
    assert response.status_code == 503
    assert response.json()["execution_plane"] == "degraded"
    assert response.json()["execution_reason"] == "queue_stalled"
    assert "worker_id" not in response.text
    assert "ready_count" not in response.text

    failed = create_app(
        engine=StubEngine(),  # type: ignore[arg-type]
        identity=StubIdentity(),  # type: ignore[arg-type]
        execution_health=StubExecutionHealth(fail=True),
    )
    response = TestClient(failed).get("/ready")
    assert response.status_code == 503
    assert response.json()["execution_reason"] == "health_probe_failed"
    assert "private database details" not in response.text


def test_worker_loop_reports_discovery_and_busy_boundaries():
    events: list[str] = []

    class Activity:
        def discovery_succeeded(self, at):
            assert at.tzinfo is not None
            events.append("discovery_succeeded")

        def discovery_failed(self, at, error_type):
            del at, error_type
            events.append("discovery_failed")

        def standby_succeeded(self, at):
            del at
            events.append("standby_succeeded")

        def work_started(self, at):
            del at
            events.append("work_started")

        def work_finished(self, at):
            del at
            events.append("work_finished")

        def work_progressed(self, at):
            del at
            events.append("work_progressed")

    class Worker:
        def run_one(self, organization_id):
            del organization_id
            return CommandRunReport(CommandRunStatus.IDLE)

    loop = CommandWorkerLoop(
        worker=Worker(),  # type: ignore[arg-type]
        organization_ids=("tenant-a",),
        activity=Activity(),
    )
    loop.run_cycle()
    assert events == [
        "discovery_succeeded", "work_started", "work_progressed", "work_finished",
    ]


def test_execution_health_migration_is_global_least_privilege_and_packaged():
    root = Path(__file__).resolve().parents[1]
    migration = (root / "postgres/initdb/108-execution-health-v2.sql").read_text()
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "GRANT SELECT ON TABLE public.aos_v2_worker_health TO agentos_app" in migration
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in migration
    assert "heartbeat_at < now() - interval '7 days'" in migration
    assert "last_main_progress_at" in migration
    assert "aos_v2_execution_release" in migration
    assert "active_application_version" in migration
    table_definition = migration.split(
        "CREATE TABLE IF NOT EXISTS public.aos_v2_worker_health", 1
    )[1].split(");", 1)[0]
    assert "tenant_id" not in table_definition
    assert "payload" not in table_definition
    assert "error text" not in table_definition.lower()
    assert "108-execution-health-v2.sql" in (
        root / "deploy/Dockerfile.migrations-v2"
    ).read_text()
    assert "108-execution-health-v2.sql" in (
        root / "deploy/docker-compose.v2.yml"
    ).read_text()
