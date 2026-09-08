from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, insert

from agent_os.infrastructure.dbos_lifecycle import commands, metadata
from agent_os.infrastructure.sql_ready_tenants import SQLReadyTenantSource
from agent_os.infrastructure.sql_workflow_graph import graph_metadata, workflow_actions


ROOT = Path(__file__).resolve().parents[1]


def test_ready_tenant_discovery_unifies_queues_excludes_future_work_and_rotates(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'ready.sqlite3'}"
    engine = create_engine(database_url)
    metadata.create_all(engine)
    graph_metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    command_base = {
        "run_id": "run",
        "event_id": "event",
        "aggregate_version": 1,
        "position": 0,
        "envelope": {},
        "attempts": 0,
        "created_at": now,
        "lease_owner": None,
        "lease_expires_at": None,
    }
    action_base = {
        "run_id": "run",
        "source_event_id": "event",
        "state_version": 0,
        "position": 0,
        "action": {},
        "attempts": 0,
        "created_at": now,
        "lease_owner": None,
        "lease_expires_at": None,
    }
    with engine.begin() as connection:
        connection.execute(insert(commands), [
            {
                **command_base,
                "command_id": "a" * 64,
                "organization_id": "tenant-a",
                "status": "pending",
                "available_at": now - timedelta(seconds=1),
            },
            {
                **command_base,
                "command_id": "c" * 64,
                "organization_id": "tenant-c",
                "status": "pending",
                "available_at": now + timedelta(hours=1),
            },
        ])
        connection.execute(insert(workflow_actions), [
            {
                **action_base,
                "action_id": "b" * 64,
                "tenant_id": "tenant-b",
                "status": "executing",
                "available_at": now - timedelta(hours=1),
                "lease_owner": "dead-worker",
                "lease_expires_at": now - timedelta(seconds=1),
            },
            {
                **action_base,
                "action_id": "d" * 64,
                "tenant_id": "tenant-d",
                "status": "succeeded",
                "available_at": now - timedelta(hours=1),
            },
        ])
    source = SQLReadyTenantSource(database_url)
    try:
        assert source.list_ready_tenants(limit=10) == ("tenant-a", "tenant-b")
        assert source.list_ready_tenants(after_tenant_id="tenant-a", limit=1) == ("tenant-b",)
        assert source.list_ready_tenants(after_tenant_id="tenant-b", limit=1) == ("tenant-a",)
    finally:
        source.close()
        engine.dispose()


def test_ready_tenant_discovery_bounds_database_work(tmp_path: Path):
    source = SQLReadyTenantSource(f"sqlite:///{tmp_path / 'empty.sqlite3'}")
    try:
        for invalid in (0, 1_001):
            try:
                source.list_ready_tenants(limit=invalid)
            except ValueError as exc:
                assert "between 1 and 1000" in str(exc)
            else:
                raise AssertionError("invalid discovery limit was accepted")
    finally:
        source.close()


def test_ready_tenant_migration_exposes_only_scheduling_columns_to_a_narrow_role():
    migration = (ROOT / "postgres/initdb/91-ready-tenant-discovery-v2.sql").read_text()

    assert "CREATE ROLE agentos_worker NOLOGIN NOSUPERUSER" in migration
    assert "NOBYPASSRLS" in migration
    assert "ALL TABLES IN SCHEMA public FROM agentos_worker" in migration
    assert "REVOKE agentos_app FROM agentos_worker" in migration
    assert "SELECT (tenant_id, status, available_at, lease_expires_at)" in migration
    assert "FOR SELECT TO agentos_worker" in migration
    assert "GRANT SELECT ON TABLE" not in migration
    assert migration.count("_ready_global_idx") == 2
    assert migration.count("_expired_global_idx") == 2
