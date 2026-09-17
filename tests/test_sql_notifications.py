from __future__ import annotations

from dataclasses import replace
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
