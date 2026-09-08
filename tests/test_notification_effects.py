from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from agent_os.application.lifecycle import CommandEnvelope
from agent_os.domain.lifecycle import Command, CommandKind
from agent_os.domain.workflow_runtime import WorkflowAction, WorkflowActionKind
from agent_os.infrastructure.notification_effects import NotificationEffectHandlers
from agent_os.infrastructure.sql_notifications import SQLNotificationStore


def test_lifecycle_and_graph_effects_publish_real_idempotent_in_app_notifications(tmp_path: Path):
    store = SQLNotificationStore(f"sqlite:///{tmp_path / 'inbox.sqlite3'}", create_schema=True)
    effects = NotificationEffectHandlers(
        store, clock=lambda: datetime(2026, 9, 8, 15, tzinfo=timezone.utc),
    )
    lifecycle = CommandEnvelope(
        "a" * 64,
        "run-1",
        "tenant-a",
        "event-1",
        1,
        0,
        Command(CommandKind.NOTIFY_HUMAN, {
            "correlation_id": "question-1",
            "reason": "Which market should we launch in?",
        }),
    )
    graph = WorkflowAction(
        "b" * 64,
        WorkflowActionKind.RUN_SUCCEEDED,
        None,
        None,
        {"terminal_token_ids": ["done-1"]},
    )

    first = effects.lifecycle_handlers()[CommandKind.NOTIFY_HUMAN](lifecycle)
    duplicate = effects.lifecycle_handlers()[CommandKind.NOTIFY_HUMAN](lifecycle)
    completed = effects.graph_handlers()[WorkflowActionKind.RUN_SUCCEEDED](
        {"tenant_id": "tenant-a", "run_id": "run-1"}, graph,
    )

    assert first["created"] is True
    assert duplicate == {**first, "created": False}
    assert completed["channel"] == "in_app"
    records = store.list_notifications("tenant-a", run_id="run-1")
    assert {item["category"] for item in records} == {
        "human_action_required", "run_succeeded",
    }
    store.close()
