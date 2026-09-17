from __future__ import annotations

from pathlib import Path

import pytest

from agent_os.application.ports import MissionConversationStore
from agent_os.infrastructure.sql_mission_conversations import SQLMissionConversationStore
from agent_os.infrastructure.sql_notifications import SQLNotificationStore


@pytest.fixture
def store(tmp_path: Path):
    value = SQLMissionConversationStore(
        f"sqlite:///{tmp_path / 'mission-conversations.sqlite3'}", create_schema=True,
    )
    try:
        yield value
    finally:
        value.close()


def append(
    store: SQLMissionConversationStore,
    *,
    key: str,
    channel: str = "shared",
    kind: str = "comment",
    body: str = "A mission update",
    sender: str = "client-a",
    reply_to: str | None = None,
):
    return store.append_message(
        tenant_id="tenant-a",
        mission_id="mission-a",
        sender_id=sender,
        sender_persona="client" if sender == "client-a" else "reviewer",
        channel=channel,
        kind=kind,
        body=body,
        reply_to_message_id=reply_to,
        audience_ids=(sender, "role:manager", "human:ceo"),
        idempotency_key=key,
    )


def test_mission_messages_are_idempotent_ordered_and_channel_projected(store):
    assert isinstance(store, MissionConversationStore)
    shared = append(store, key="shared-message-one", body="Please clarify the goal")
    internal = append(
        store,
        key="internal-message-one",
        channel="internal",
        kind="update",
        body="The delivery team is reviewing the constraint",
        sender="reviewer-a",
        reply_to=shared["message_id"],
    )
    duplicate = append(store, key="shared-message-one", body="Please clarify the goal")

    assert duplicate["message_id"] == shared["message_id"]
    assert duplicate["duplicate"] is True
    assert [item["message_id"] for item in store.list_messages(
        "tenant-a", "mission-a", include_internal=True,
    )] == [shared["message_id"], internal["message_id"]]
    assert [item["message_id"] for item in store.list_messages(
        "tenant-a", "mission-a", include_internal=False,
    )] == [shared["message_id"]]
    assert store.list_messages(
        "tenant-b", "mission-a", include_internal=True,
    ) == ()

    with pytest.raises(ValueError, match="different content"):
        append(store, key="shared-message-one", body="Changed after acceptance")
    with pytest.raises(ValueError, match="reply target does not exist"):
        append(store, key="missing-reply-one", reply_to="message-missing")


def test_mission_message_event_is_safe_and_exactly_audienced(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'mission-conversation-events.sqlite3'}"
    conversations = SQLMissionConversationStore(database_url, create_schema=True)
    events = SQLNotificationStore(database_url, create_schema=True)
    try:
        message = conversations.append_message(
            tenant_id="tenant-a", mission_id="mission-sensitive",
            sender_id="client-a", sender_persona="client", channel="shared",
            kind="question", body="This confidential acquisition must remain private",
            reply_to_message_id=None,
            audience_ids=("client-a", "reviewer-a", "role:manager"),
            idempotency_key="confidential-question-one",
        )
        page = events.list_experience_events(
            "tenant-a", audience_ids=("reviewer-a",),
        )
        assert page.events[0]["resource_id"] == message["message_id"]
        assert page.events[0]["safe_summary"] == (
            "A mission participant asked a question."
        )
        assert "acquisition" not in str(page.events[0]).lower()
        assert events.list_experience_events(
            "tenant-a", audience_ids=("unassigned-a",),
        ).events == ()
    finally:
        events.close()
        conversations.close()


def test_mission_conversation_migration_is_append_only_and_tenant_fenced():
    migration = (
        Path(__file__).resolve().parents[1]
        / "postgres/initdb/99zzzzzzz-mission-conversations-v2.sql"
    ).read_text()

    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "tenant_id = current_setting('app.tenant_id', true)" in migration
    assert "GRANT SELECT, INSERT" in migration
    assert "GRANT UPDATE" not in migration
    assert "GRANT DELETE" not in migration
    assert "channel IN ('shared', 'internal')" in migration
