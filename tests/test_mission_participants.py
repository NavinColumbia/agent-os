from __future__ import annotations

from pathlib import Path

import pytest

from agent_os.application.ports import MissionParticipantStore
from agent_os.infrastructure.sql_mission_participants import SQLMissionParticipantStore
from agent_os.infrastructure.sql_notifications import SQLNotificationStore


@pytest.fixture
def store(tmp_path: Path):
    value = SQLMissionParticipantStore(
        f"sqlite:///{tmp_path / 'mission-participants.sqlite3'}", create_schema=True,
    )
    try:
        yield value
    finally:
        value.close()


def test_participation_is_tenant_mission_subject_scoped_and_idempotent(store):
    assert isinstance(store, MissionParticipantStore)

    granted = store.grant_participant(
        tenant_id="tenant-a", mission_id="mission-a", subject_id="reviewer-a",
        participation_role="reviewer", actor_id="manager-a",
        idempotency_key="grant-reviewer-a",
    )
    duplicate = store.grant_participant(
        tenant_id="tenant-a", mission_id="mission-a", subject_id="reviewer-a",
        participation_role="reviewer", actor_id="manager-a",
        idempotency_key="grant-reviewer-a",
    )

    assert granted["active"] is True
    assert duplicate["duplicate"] is True
    assert store.can_access("tenant-a", "mission-a", "reviewer-a") is True
    assert store.can_access("tenant-a", "mission-a", "someone-else") is False
    assert store.can_access("tenant-b", "mission-a", "reviewer-a") is False
    assert store.mission_ids_for_subject("tenant-a", "reviewer-a") == ("mission-a",)
    assert store.mission_ids_for_subject("tenant-b", "reviewer-a") == ()
    assert store.list_participants("tenant-a", "mission-a")[0]["subject_id"] == "reviewer-a"

    with pytest.raises(ValueError, match="revoked before changing role"):
        store.grant_participant(
            tenant_id="tenant-a", mission_id="mission-a", subject_id="reviewer-a",
            participation_role="builder", actor_id="manager-a",
            idempotency_key="change-active-role",
        )


def test_revocation_removes_access_and_regrant_retains_monotonic_version(store):
    store.grant_participant(
        tenant_id="tenant-a", mission_id="mission-a", subject_id="client-a",
        participation_role="client", actor_id="manager-a",
        idempotency_key="grant-client-a",
    )
    revoked = store.revoke_participant(
        tenant_id="tenant-a", mission_id="mission-a", subject_id="client-a",
        actor_id="manager-a", reason="Engagement ended",
        idempotency_key="revoke-client-a",
    )
    duplicate = store.revoke_participant(
        tenant_id="tenant-a", mission_id="mission-a", subject_id="client-a",
        actor_id="manager-a", reason="Engagement ended",
        idempotency_key="revoke-client-a",
    )

    assert revoked is not None and revoked["version"] == 2
    assert duplicate is not None and duplicate["duplicate"] is True
    assert store.can_access("tenant-a", "mission-a", "client-a") is False
    assert store.mission_ids_for_subject("tenant-a", "client-a") == ()

    regranted = store.grant_participant(
        tenant_id="tenant-a", mission_id="mission-a", subject_id="client-a",
        participation_role="viewer", actor_id="manager-a",
        idempotency_key="regrant-client-a",
    )
    assert regranted["version"] == 3
    assert regranted["participation_role"] == "viewer"
    assert store.can_access("tenant-a", "mission-a", "client-a") is True


def test_participation_changes_emit_safe_subject_scoped_experience_events(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'participant-events.sqlite3'}"
    participants = SQLMissionParticipantStore(database_url, create_schema=True)
    events = SQLNotificationStore(database_url, create_schema=True)
    try:
        participants.grant_participant(
            tenant_id="tenant-a", mission_id="mission-sensitive",
            subject_id="reviewer-a", participation_role="reviewer",
            actor_id="manager-a", idempotency_key="grant-sensitive-review",
        )
        page = events.list_experience_events(
            "tenant-a", audience_ids=("reviewer-a",),
        )
        assert [item["kind"] for item in page.events] == [
            "mission.participant.granted"
        ]
        assert "sensitive" not in page.events[0]["safe_summary"].lower()
        assert events.list_experience_events(
            "tenant-a", audience_ids=("someone-else",),
        ).events == ()
    finally:
        events.close()
        participants.close()


def test_mission_participant_migration_forces_tenant_scoped_rls():
    migration = (
        Path(__file__).resolve().parents[1]
        / "postgres/initdb/99zzzzzz-mission-participants-v2.sql"
    ).read_text()

    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "tenant_id = current_setting('app.tenant_id', true)" in migration
    assert "GRANT SELECT, INSERT, UPDATE" in migration
    assert "GRANT DELETE" not in migration
    for role in ("builder", "reviewer", "client", "viewer"):
        assert f"'{role}'" in migration
