from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import uuid

import pytest

from agent_os.application.ports import MissionWorkAssignmentStore
from agent_os.infrastructure.sql_mission_work_assignments import (
    SQLMissionWorkAssignmentStore,
)
from agent_os.infrastructure.sql_notifications import SQLNotificationStore


@pytest.fixture
def store(tmp_path: Path):
    value = SQLMissionWorkAssignmentStore(
        f"sqlite:///{tmp_path / 'mission-work-assignments.sqlite3'}",
        create_schema=True,
    )
    try:
        yield value
    finally:
        value.close()


def assign(
    store,
    *,
    key="assign-responsible-a",
    subject="builder-a",
    duty="responsible",
    role="builder",
    expected=0,
    fingerprint="a" * 64,
):
    return store.assign_work(
        tenant_id="tenant-a",
        mission_id="mission-a",
        work_id="work-a",
        duty=duty,
        work_fingerprint=fingerprint,
        subject_id=subject,
        participation_role=role,
        assigned_by="manager-a",
        reason="Own this bounded work contract",
        expected_version=expected,
        idempotency_key=key,
    )


def test_responsible_builder_and_independent_reviewer_coexist_with_visible_scope(store):
    assert isinstance(store, MissionWorkAssignmentStore)
    responsible = assign(store)
    reviewer = assign(
        store,
        key="assign-reviewer-a",
        subject="reviewer-a",
        duty="reviewer",
        role="reviewer",
    )

    assert responsible["status"] == "pending" and responsible["version"] == 1
    assert reviewer["duty"] == "reviewer" and reviewer["version"] == 1
    assert store.list_for_subject(
        "tenant-a", "builder-a", mission_ids=("mission-a",),
    )[0]["duty"] == "responsible"
    assert store.list_for_subject(
        "tenant-a", "builder-a", mission_ids=("other-mission",),
    ) == ()
    assert store.list_for_subject(
        "tenant-b", "builder-a", mission_ids=("mission-a",),
    ) == ()
    assert {item["duty"] for item in store.list_for_mission(
        "tenant-a", "mission-a",
    )} == {"responsible", "reviewer"}
    with pytest.raises(ValueError, match="does not match"):
        assign(
            store,
            key="reviewer-cannot-be-responsible",
            subject="reviewer-b",
            duty="responsible",
            role="reviewer",
            expected=1,
        )


def test_delayed_command_replays_never_reverse_newer_assignment_decisions(store):
    alice = assign(store, key="assign-alice-v1")
    revoked = store.revoke_assignment(
        tenant_id="tenant-a",
        mission_id="mission-a",
        work_id="work-a",
        duty="responsible",
        revoked_by="manager-a",
        reason="Alice changed teams",
        expected_version=alice["version"],
        idempotency_key="revoke-alice-v2",
    )
    assert revoked is not None and revoked["version"] == 2
    bob = assign(
        store,
        key="assign-bob-v3",
        subject="builder-b",
        expected=revoked["version"],
    )
    assert bob["version"] == 3 and bob["subject_id"] == "builder-b"

    late_revoke = store.revoke_assignment(
        tenant_id="tenant-a",
        mission_id="mission-a",
        work_id="work-a",
        duty="responsible",
        revoked_by="manager-a",
        reason="Alice changed teams",
        expected_version=1,
        idempotency_key="revoke-alice-v2",
    )
    late_assign = assign(store, key="assign-alice-v1")
    current = store.list_for_mission("tenant-a", "mission-a")[0]

    assert late_revoke is not None and late_revoke["duplicate"] is True
    assert late_revoke["version"] == 2
    assert late_assign["duplicate"] is True and late_assign["version"] == 1
    assert current["version"] == 3 and current["subject_id"] == "builder-b"
    with pytest.raises(ValueError, match="reused with different parameters"):
        assign(
            store,
            key="assign-alice-v1",
            subject="builder-c",
            expected=3,
        )
    with pytest.raises(ValueError, match="version changed"):
        assign(
            store,
            key="stale-manager-command",
            subject="builder-c",
            expected=2,
        )


def test_acceptance_reassignment_and_history_are_reconstructable_and_audienced(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'work-assignment-events.sqlite3'}"
    assignments = SQLMissionWorkAssignmentStore(database_url, create_schema=True)
    events = SQLNotificationStore(database_url, create_schema=True)
    try:
        pending = assign(assignments, key="assign-builder-pending")
        accepted = assignments.respond_to_assignment(
            tenant_id="tenant-a",
            mission_id="mission-a",
            work_id="work-a",
            duty="responsible",
            subject_id="builder-a",
            response="accept",
            reason="",
            expected_version=pending["version"],
            idempotency_key="accept-builder-work",
        )
        replacement = assign(
            assignments,
            key="replace-builder-atomically",
            subject="builder-b",
            expected=accepted["version"],
            fingerprint="b" * 64,
        )

        assert accepted["status"] == "accepted" and accepted["version"] == 2
        assert replacement["status"] == "pending" and replacement["version"] == 3
        history = assignments.list_history("tenant-a", "mission-a")
        assert [item["event_kind"] for item in reversed(history)] == [
            "assigned", "accepted", "reassigned",
        ]
        assert [item["subject_id"] for item in reversed(history)] == [
            "builder-a", "builder-a", "builder-b",
        ]
        builder_events = events.list_experience_events(
            "tenant-a", audience_ids=("builder-a",),
        ).events
        assert [item["kind"] for item in builder_events] == [
            "mission.work.assigned", "mission.work.accepted", "mission.work.reassigned",
        ]
        assert "mission-a" not in str(builder_events)
    finally:
        events.close()
        assignments.close()


def test_mission_work_accountability_migration_forces_rls_and_immutable_history():
    migration = (
        Path(__file__).resolve().parents[1]
        / "postgres/initdb/107-mission-work-accountability-v2.sql"
    ).read_text()

    assert migration.count("ENABLE ROW LEVEL SECURITY") == 2
    assert migration.count("FORCE ROW LEVEL SECURITY") == 2
    assert "tenant_id = current_setting('app.tenant_id', true)" in migration
    assert "GRANT SELECT, INSERT, UPDATE" in migration
    assert "GRANT SELECT, INSERT ON TABLE public.aos_v2_mission_work_assignment_events" in migration
    assert "GRANT DELETE" not in migration
    assert "UNIQUE (tenant_id, mission_id, work_id, duty, assignment_version)" in migration
    assert "work_fingerprint" in migration
    assert "aos_v2_work_assignment_subject_guard" in migration
    assert "aos_v2_participant_responsibility_guard" in migration
    assert "aos_v2_membership_participation_guard" in migration
    assert migration.count("FOR NO KEY UPDATE") == 2


@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") != "true" and not os.environ.get("AOS_TEST_POSTGRES_URL"),
    reason="requires the isolated PostgreSQL service provisioned by CI",
)
def test_postgres_runtime_role_enforces_assignment_tenant_rls_and_denies_delete():
    import psycopg
    from psycopg.errors import CheckViolation, InsufficientPrivilege

    database_url = os.environ.get(
        "AOS_TEST_POSTGRES_URL",
        "postgresql://agentos:ci@127.0.0.1:5433/agentos",
    )
    suffix = uuid.uuid4().hex
    tenants = (f"rls-work-a-{suffix}", f"rls-work-b-{suffix}")
    mission_id = f"mission-{suffix}"
    work_id = f"work-{suffix}"
    subject_id = f"builder-{suffix}"
    with psycopg.connect(database_url, autocommit=True) as admin:
        for tenant_id in tenants:
            admin.execute(
                """
                INSERT INTO public.aos_v2_memberships
                    (tenant_id, subject_id, roles, active, invitation_id,
                     created_at, updated_at)
                VALUES (%s, %s, '["builder"]'::jsonb, true, %s, now(), now())
                """,
                (tenant_id, subject_id, f"invitation-{suffix * 2}"),
            )
            admin.execute(
                """
                INSERT INTO public.aos_v2_mission_participants
                    (tenant_id, mission_id, subject_id, participation_role, active, version,
                     granted_by, granted_at, grant_key)
                VALUES (%s, %s, %s, 'builder', true, 1, 'manager', now(), %s)
                """,
                (tenant_id, mission_id, subject_id, f"participant-{tenant_id}"),
            )
            admin.execute(
                """
                INSERT INTO public.aos_v2_mission_work_assignments
                    (tenant_id, mission_id, work_id, duty, work_fingerprint, subject_id,
                     participation_role, status, active, version, assigned_by, assigned_at,
                     assignment_reason)
                VALUES (%s, %s, %s, 'responsible', %s, %s, 'builder', 'pending', true,
                        1, 'manager', now(), 'CI tenant fence probe')
                """,
                (tenant_id, mission_id, work_id, "a" * 64, subject_id),
            )
        try:
            with psycopg.connect(database_url, autocommit=True) as runtime:
                runtime.execute("SET ROLE agentos_app")
                runtime.execute(
                    "SELECT set_config('app.tenant_id', %s, false)", (tenants[0],),
                )
                visible = runtime.execute(
                    "SELECT tenant_id FROM public.aos_v2_mission_work_assignments"
                ).fetchall()
                assert visible == [(tenants[0],)]
                changed = runtime.execute(
                    "UPDATE public.aos_v2_mission_work_assignments SET version = 2 "
                    "WHERE tenant_id = %s",
                    (tenants[1],),
                )
                assert changed.rowcount == 0
            with psycopg.connect(database_url, autocommit=True) as no_scope:
                no_scope.execute("SET ROLE agentos_app")
                assert no_scope.execute(
                    "SELECT count(*) FROM public.aos_v2_mission_work_assignments"
                ).fetchone()[0] == 0
            with psycopg.connect(database_url, autocommit=True) as denied:
                denied.execute("SET ROLE agentos_app")
                denied.execute(
                    "SELECT set_config('app.tenant_id', %s, false)", (tenants[0],),
                )
                with pytest.raises(InsufficientPrivilege):
                    denied.execute(
                        "DELETE FROM public.aos_v2_mission_work_assignments "
                        "WHERE tenant_id = %s",
                        (tenants[0],),
                    )
            with pytest.raises(CheckViolation, match="responsibility"):
                admin.execute(
                    "UPDATE public.aos_v2_mission_participants SET active = false "
                    "WHERE tenant_id = %s AND mission_id = %s AND subject_id = %s",
                    (tenants[0], mission_id, subject_id),
                )
            with pytest.raises(CheckViolation, match="participation"):
                admin.execute(
                    "UPDATE public.aos_v2_memberships SET active = false "
                    "WHERE tenant_id = %s AND subject_id = %s",
                    (tenants[0], subject_id),
                )

            concurrent_store = SQLMissionWorkAssignmentStore(database_url)
            concurrent_work_id = f"concurrent-{work_id}"
            try:
                def concurrent_assign():
                    return concurrent_store.assign_work(
                        tenant_id=tenants[0],
                        mission_id=mission_id,
                        work_id=concurrent_work_id,
                        duty="responsible",
                        work_fingerprint="b" * 64,
                        subject_id=subject_id,
                        participation_role="builder",
                        assigned_by="manager",
                        reason="Concurrent command convergence probe",
                        expected_version=0,
                        idempotency_key=f"concurrent-assignment-{suffix}",
                    )

                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _: concurrent_assign(), range(2)))
                assert {result["version"] for result in results} == {1}
                assert sorted(result["duplicate"] for result in results) == [False, True]
            finally:
                concurrent_store.close()
        finally:
            admin.execute(
                "DELETE FROM public.aos_v2_mission_work_assignment_events "
                "WHERE tenant_id = ANY(%s)",
                (list(tenants),),
            )
            admin.execute(
                "DELETE FROM public.aos_v2_mission_work_assignments WHERE tenant_id = ANY(%s)",
                (list(tenants),),
            )
            admin.execute(
                "DELETE FROM public.aos_v2_mission_participants WHERE tenant_id = ANY(%s)",
                (list(tenants),),
            )
            admin.execute(
                "DELETE FROM public.aos_v2_memberships WHERE tenant_id = ANY(%s)",
                (list(tenants),),
            )
