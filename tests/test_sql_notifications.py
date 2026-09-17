from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_os.domain.notifications import (
    Notification,
    NotificationCategory,
    NotificationPreferenceMode,
    NotificationPreferences,
)
from agent_os.infrastructure.sql_notifications import SQLNotificationStore


def notification(**changes) -> Notification:
    value = Notification(
        notification_id="notification-1",
        tenant_id="tenant-a",
        run_id="run-1",
        category=NotificationCategory.HUMAN_ACTION_REQUIRED,
        recipient_ids=("human:ceo",),
        subject="Input needed",
        body="Approve the release",
        source_id="action-1",
        created_at="2026-09-08T15:00:00+00:00",
        correlation_id="approval-1",
        payload={"risk": "irreversible"},
    )
    return replace(value, **changes)


@pytest.fixture
def store(tmp_path: Path):
    value = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'notifications.sqlite3'}", create_schema=True,
    )
    try:
        yield value
    finally:
        value.close()


def test_notification_publication_is_idempotent_across_retry_time_and_conflicts_fail(store):
    first = notification()
    same_effect_later = replace(first, created_at="2026-09-08T15:01:00+00:00")

    assert store.publish_notification(first) is True
    assert store.publish_notification(same_effect_later) is False
    with pytest.raises(ValueError, match="different content"):
        store.publish_notification(replace(first, body="Forged replacement"))

    records = store.list_notifications("tenant-a")
    assert len(records) == 1
    assert records[0]["created_at"] == first.created_at


def test_notification_rejects_duplicate_recipient_identity():
    with pytest.raises(ValueError, match="recipients must be unique"):
        notification(recipient_ids=("human:ceo", "human:ceo"))


def test_notification_inbox_is_tenant_run_and_recipient_scoped(store):
    store.publish_notification(notification())
    store.publish_notification(notification(
        notification_id="notification-2",
        run_id="run-2",
        recipient_ids=("agent:engineer",),
        source_id="action-2",
    ))
    store.publish_notification(notification(
        notification_id="notification-3",
        tenant_id="tenant-b",
        source_id="action-3",
    ))

    assert len(store.list_notifications("tenant-a")) == 2
    assert [item["notification_id"] for item in store.list_notifications(
        "tenant-a", recipient_id="human:ceo",
    )] == ["notification-1"]
    assert [item["run_id"] for item in store.list_notifications(
        "tenant-a", run_id="run-2",
    )] == ["run-2"]
    assert [item["notification_id"] for item in store.list_notifications("tenant-b")] == [
        "notification-3"
    ]


def test_notification_inbox_supports_role_and_subject_audiences_without_duplicates(store):
    store.publish_notification(notification(
        recipient_ids=("reviewer-a", "role:reviewer"),
    ))

    records = store.list_notifications(
        "tenant-a", recipient_ids=("reviewer-a", "role:reviewer"),
    )

    assert [item["notification_id"] for item in records] == ["notification-1"]
    with pytest.raises(ValueError, match="one notification recipient filter"):
        store.list_notifications(
            "tenant-a", recipient_id="reviewer-a", recipient_ids=("role:reviewer",),
        )


def test_recipient_inbox_is_not_hidden_by_other_recipients_high_volume(store):
    target = notification(
        notification_id="notification-target",
        source_id="action-target",
        created_at="2026-09-08T14:00:00+00:00",
    )
    store.publish_notification(target)
    for position in range(600):
        store.publish_notification(notification(
            notification_id=f"notification-noise-{position:04d}",
            recipient_ids=("agent:someone-else",),
            source_id=f"action-noise-{position:04d}",
            created_at=f"2026-09-08T15:{position // 60:02d}:{position % 60:02d}+00:00",
        ))

    records = store.list_notifications("tenant-a", recipient_id="human:ceo", limit=1)

    assert [item["notification_id"] for item in records] == ["notification-target"]


def test_notification_cursor_is_stable_across_equal_timestamps_and_tenant_scoped(store):
    timestamp = "2026-09-08T15:00:00+00:00"
    for position in range(1, 4):
        store.publish_notification(notification(
            notification_id=f"notification-{position}",
            source_id=f"action-{position}", created_at=timestamp,
        ))

    first = store.list_notifications("tenant-a", limit=2)
    assert [item["notification_id"] for item in first] == [
        "notification-3", "notification-2",
    ]
    second = store.list_notifications(
        "tenant-a",
        before=(datetime(2026, 9, 8, 15, tzinfo=timezone.utc), "notification-2"),
        limit=2,
    )
    assert [item["notification_id"] for item in second] == ["notification-1"]
    assert store.list_notifications(
        "tenant-b",
        before=(datetime(2026, 9, 8, 15, tzinfo=timezone.utc), "notification-2"),
    ) == ()
    with pytest.raises(ValueError, match="cursor"):
        store.list_notifications(
            "tenant-a", before=(datetime(2026, 9, 8, 15), "notification-2"),
        )


def test_experience_events_are_monotonic_audience_scoped_and_replay_safe(store):
    first_notification = notification()
    second_notification = notification(
        notification_id="notification-2",
        recipient_ids=("operator:on-call",),
        source_id="action-2",
    )
    assert store.publish_notification(first_notification) is True
    assert store.publish_notification(second_notification) is True
    assert store.publish_notification(first_notification) is False

    first = store.list_experience_events("tenant-a", limit=1)
    assert [item["tenant_sequence"] for item in first.events] == [1]
    assert first.cursor_sequence == 1
    assert first.latest_sequence == 2
    assert first.has_more is True

    second = store.list_experience_events(
        "tenant-a", after_sequence=first.cursor_sequence, limit=1,
    )
    assert [item["tenant_sequence"] for item in second.events] == [2]
    assert second.cursor_sequence == 2
    assert second.has_more is False

    personal = store.list_experience_events(
        "tenant-a", audience_ids=("human:ceo",),
    )
    assert [item["resource_id"] for item in personal.events] == ["notification-1"]
    # Hidden events do not strand a recipient on an old cursor.
    assert personal.cursor_sequence == 2
    assert "Approve the release" not in personal.events[0]["safe_summary"]
    hidden = store.list_experience_events(
        "tenant-a", audience_ids=("someone-else",),
    )
    assert hidden.events == ()
    assert hidden.cursor_sequence == 2

    store.publish_notification(notification(
        notification_id="notification-b", tenant_id="tenant-b", source_id="action-b",
    ))
    other = store.list_experience_events("tenant-b")
    assert [item["tenant_sequence"] for item in other.events] == [1]
    with pytest.raises(ValueError, match="ahead"):
        store.list_experience_events("tenant-b", after_sequence=2)


def test_experience_retention_floor_requires_authoritative_snapshot_reset(store):
    store.publish_notification(notification())
    store.publish_notification(notification(
        notification_id="notification-2", source_id="action-2",
    ))
    with store._engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE aos_v2_experience_streams "
            "SET retained_from_sequence = 2 WHERE tenant_id = ?",
            ("tenant-a",),
        )

    expired = store.list_experience_events("tenant-a", after_sequence=0)
    assert expired.reset_required is True
    assert expired.events == ()
    assert expired.minimum_sequence == 2
    assert expired.cursor_sequence == 2
    resumed = store.list_experience_events("tenant-a", after_sequence=1)
    assert resumed.reset_required is False
    assert [item["tenant_sequence"] for item in resumed.events] == [2]


def test_notification_and_experience_event_share_one_transaction(store, monkeypatch):
    def fail_event_append(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("simulated event-log failure")

    monkeypatch.setattr(store, "_append_experience_event", fail_event_append)
    with pytest.raises(RuntimeError, match="event-log failure"):
        store.publish_notification(notification())
    assert store.list_notifications("tenant-a") == ()


def test_attention_mutations_append_exactly_one_transactional_experience_event(store):
    store.publish_notification(notification())
    state = store.set_notification_state(
        tenant_id="tenant-a", subject_id="human:ceo", notification_id="notification-1",
        status="read", snoozed_until=None, actor_id="human:ceo",
        idempotency_key="read-notification-once",
    )
    replay = store.set_notification_state(
        tenant_id="tenant-a", subject_id="human:ceo", notification_id="notification-1",
        status="read", snoozed_until=None, actor_id="human:ceo",
        idempotency_key="read-notification-once",
    )
    preferences = NotificationPreferences(
        tenant_id="tenant-a", subject_id="human:ceo",
        mode=NotificationPreferenceMode.FOCUSED,
    )
    store.set_notification_preferences(
        preferences, actor_id="human:ceo", idempotency_key="preferences-once",
    )
    store.set_notification_preferences(
        preferences, actor_id="human:ceo", idempotency_key="preferences-once",
    )

    assert state["duplicate"] is False
    assert replay["duplicate"] is True
    page = store.list_experience_events("tenant-a")
    assert [item["kind"] for item in page.events] == [
        "notification.published",
        "notification.state.changed",
        "notification.preferences.changed",
    ]
    assert [item["tenant_sequence"] for item in page.events] == [1, 2, 3]


def test_personal_state_is_idempotent_and_never_mutates_notification_truth(store):
    store.publish_notification(notification())

    first = store.set_notification_state(
        tenant_id="tenant-a", subject_id="human:ceo", notification_id="notification-1",
        status="read", snoozed_until=None, actor_id="human:ceo",
        idempotency_key="read-notification-1",
    )
    replay = store.set_notification_state(
        tenant_id="tenant-a", subject_id="human:ceo", notification_id="notification-1",
        status="read", snoozed_until=None, actor_id="human:ceo",
        idempotency_key="read-notification-1",
    )
    dismissed = store.set_notification_state(
        tenant_id="tenant-a", subject_id="human:ceo", notification_id="notification-1",
        status="dismissed", snoozed_until=None, actor_id="human:ceo",
        idempotency_key="dismiss-notification-1",
    )

    assert first["version"] == 1
    assert replay["duplicate"] is True
    assert dismissed["version"] == 2
    states = store.list_notification_states(
        "tenant-a", subject_id="human:ceo", notification_ids=("notification-1",),
    )
    assert states["notification-1"]["status"] == "dismissed"
    assert store.get_notification("tenant-a", "notification-1")["body"] == "Approve the release"
    assert len(store.list_notifications("tenant-a")) == 1


def test_preferences_are_personal_tenant_scoped_and_versioned(store):
    preferences = NotificationPreferences(
        tenant_id="tenant-a", subject_id="human:ceo",
        mode=NotificationPreferenceMode.FOCUSED,
        browser_notifications=True,
        quiet_hours_start="22:00", quiet_hours_end="07:00",
        timezone_name="America/Los_Angeles", digest_interval_minutes=240,
    )

    saved = store.set_notification_preferences(
        preferences, actor_id="human:ceo", idempotency_key="save-preferences-1",
    )
    replay = store.set_notification_preferences(
        preferences, actor_id="human:ceo", idempotency_key="save-preferences-1",
    )

    assert saved["version"] == 1
    assert replay["duplicate"] is True
    assert store.get_notification_preferences(
        "tenant-a", subject_id="human:ceo",
    )["quiet_hours_start"] == "22:00"
    assert store.get_notification_preferences(
        "tenant-b", subject_id="human:ceo",
    )["mode"] == "balanced"


def test_notification_preferences_fail_closed_on_invalid_quiet_hours():
    with pytest.raises(ValueError, match="both a start and end"):
        NotificationPreferences(
            tenant_id="tenant-a", subject_id="human:ceo", quiet_hours_start="22:00",
        )


def test_personal_attention_migration_is_tenant_fenced_and_non_destructive():
    migration = (
        Path(__file__).resolve().parents[1]
        / "postgres/initdb/102-personal-attention-state-v2.sql"
    ).read_text()

    assert migration.count("ENABLE ROW LEVEL SECURITY") == 2
    assert migration.count("FORCE ROW LEVEL SECURITY") == 2
    assert migration.count("current_setting('app.tenant_id', true)") == 4
    assert "GRANT SELECT, INSERT, UPDATE" in migration
    assert "GRANT DELETE" not in migration
    assert "REFERENCES public.aos_v2_notifications" in migration


def test_experience_event_migration_is_tenant_fenced_append_only_and_audience_indexed():
    migration = (
        Path(__file__).resolve().parents[1]
        / "postgres/initdb/105-experience-events-v2.sql"
    ).read_text()

    assert migration.count("ENABLE ROW LEVEL SECURITY") == 3
    assert migration.count("FORCE ROW LEVEL SECURITY") == 3
    assert migration.count("current_setting('app.tenant_id', true)") == 6
    assert "retained_from_sequence" in migration
    assert "UNIQUE (tenant_id, source_key)" in migration
    assert "aos_v2_experience_event_audiences_lookup_idx" in migration
    assert "GRANT SELECT, INSERT ON TABLE public.aos_v2_experience_events" in migration
    assert "GRANT UPDATE ON TABLE public.aos_v2_experience_events" not in migration
    assert "GRANT DELETE" not in migration
