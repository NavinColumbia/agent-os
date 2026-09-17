from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from fastapi.testclient import TestClient

from agent_os.api.app import create_app
from agent_os.application.command_worker import CommandRunStatus
from agent_os.application.decision_response_worker import DurableDecisionResponseWorker
from agent_os.domain.notifications import Notification, NotificationCategory
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import WorkflowEvent, WorkflowEventKind
from agent_os.infrastructure.memory import InMemoryWorkflowEngine
from agent_os.infrastructure.sql_notifications import SQLNotificationStore
from agent_os.infrastructure.sql_workflow_graph import SQLGraphWorkflowEngine


ROOT = Path(__file__).resolve().parents[1]


class Identity:
    def authenticate(self, authorization, session) -> Mapping[str, Any]:
        del session
        if authorization == "Bearer owner":
            return {"sub": "ceo-subject", "org": "tenant-a", "roles": ["owner"]}
        if authorization == "Bearer viewer":
            return {"sub": "viewer-subject", "org": "tenant-a", "roles": ["viewer"]}
        raise ValueError("authentication required")


def waiting_graph(graph: SQLGraphWorkflowEngine) -> None:
    graph.register_workflow(WorkflowDefinition(
        "release", "tenant-a", "Release", 1, "approve",
        (
            WorkflowNode(
                "approve", NodeKind.HUMAN, "Approve production release",
                configuration={"recipient_ids": ["human:ceo"]},
            ),
            WorkflowNode("done", NodeKind.TERMINAL, "Done"),
        ),
        (WorkflowEdge("approve", "done"),),
        "architect",
    ))
    started = graph.start_graph_run(
        "tenant-a", "release", 1, run_id="release-run", request_id="release-start",
    )
    token_id = str(started.actions[0].token_id)
    graph.submit_graph_event("tenant-a", "release-run", WorkflowEvent(
        "release-began", WorkflowEventKind.NODE_BEGAN, 0, {"token_id": token_id},
    ))
    graph.submit_graph_event("tenant-a", "release-run", WorkflowEvent(
        "release-waited", WorkflowEventKind.NODE_WAITED, 1,
        {
            "token_id": token_id,
            "correlation_id": "release-approval",
            "reason": "Approve production release",
            "recipient_ids": ["human:ceo"],
        },
    ))


def publish_decision(store: SQLNotificationStore) -> None:
    store.publish_notification(Notification(
        notification_id="notice-release",
        tenant_id="tenant-a",
        run_id="release-run",
        category=NotificationCategory.HUMAN_ACTION_REQUIRED,
        recipient_ids=("human:ceo",),
        subject="Approve the production release",
        body="The verified release is waiting for your decision.",
        source_id="release-waited-action",
        created_at="2026-09-17T12:00:00+00:00",
        correlation_id="release-approval",
        payload={"risk": "production"},
    ))


def test_structured_decision_is_durable_authorized_and_applied_once(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'structured-decision.sqlite3'}"
    graph = SQLGraphWorkflowEngine(database_url, create_schema=True)
    notifications = SQLNotificationStore(database_url, create_schema=True)
    waiting_graph(graph)
    publish_decision(notifications)
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(),
        identity=Identity(),
        graph_engine=graph,
        notification_store=notifications,
    ))
    worker = DurableDecisionResponseWorker(
        store=notifications, graph=graph, worker_id="decision-worker", lease_seconds=30,
    )
    headers = {"Authorization": "Bearer owner", "Idempotency-Key": "release-answer-one"}
    try:
        before = api.get(
            "/v2/notifications", headers={"Authorization": "Bearer owner"},
        ).json()["items"][0]
        assert before["actionable"] is True

        raw_bypass = api.post(
            "/v2/graph-runs/release-run/events",
            headers={"Authorization": "Bearer owner"},
            json={
                "event_id": "unsafe-direct-response",
                "kind": "wait_resumed",
                "expected_version": 2,
                "payload": {
                    "correlation_id": "release-approval",
                    "response": {"approved": True},
                },
            },
        )
        assert raw_bypass.status_code == 409
        assert "structured decision endpoint" in raw_bypass.json()["detail"]

        forbidden = api.post(
            "/v2/decisions/notice-release/responses",
            headers={"Authorization": "Bearer viewer", "Idempotency-Key": "viewer-answer-one"},
            json={"response": {"approved": True}},
        )
        assert forbidden.status_code == 404

        accepted = api.post(
            "/v2/decisions/notice-release/responses",
            headers=headers,
            json={"response": {"approved": True, "answer": "Ship it"}},
        )
        assert accepted.status_code == 202
        assert accepted.json()["status"] == "pending"
        assert "expected_version" not in accepted.json()

        replay = api.post(
            "/v2/decisions/notice-release/responses",
            headers=headers,
            json={"response": {"approved": True, "answer": "Ship it"}},
        )
        assert replay.status_code == 202
        assert replay.json()["duplicate"] is True
        conflict = api.post(
            "/v2/decisions/notice-release/responses",
            headers=headers,
            json={"response": {"approved": False}},
        )
        assert conflict.status_code == 409

        queued = api.get(
            "/v2/notifications", headers={"Authorization": "Bearer owner"},
        ).json()["items"][0]
        assert queued["actionable"] is False
        assert queued["decision_response"]["status"] == "pending"
        assert "expected_version" not in queued["decision_response"]

        report = worker.run_one("tenant-a")
        assert report.status is CommandRunStatus.SUCCEEDED
        assert worker.run_one("tenant-a").status is CommandRunStatus.IDLE
        settled = notifications.get_decision_response(
            "tenant-a", notification_id="notice-release",
        )
        assert settled is not None and settled["status"] == "applied"
        state = notifications.list_notification_states(
            "tenant-a", subject_id="ceo-subject", notification_ids=("notice-release",),
        )
        assert state["notice-release"]["status"] == "resolved"
        resumed = graph.get_graph_run("tenant-a", "release-run")
        assert resumed is not None and resumed.version == 3
        assert resumed.ready()[0].output["human_response"]["approved"] is True
    finally:
        notifications.close()
        graph.close()


def test_decision_recovers_after_graph_commit_before_inbox_settlement(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'decision-crash-window.sqlite3'}"
    now = [datetime(2026, 9, 17, 12, tzinfo=timezone.utc)]
    graph = SQLGraphWorkflowEngine(database_url, create_schema=True)
    notifications = SQLNotificationStore(
        database_url, create_schema=True, clock=lambda: now[0],
    )
    waiting_graph(graph)
    publish_decision(notifications)
    try:
        admitted = notifications.admit_decision_response(
            tenant_id="tenant-a",
            notification_id="notice-release",
            run_id="release-run",
            correlation_id="release-approval",
            response={"approved": True},
            expected_version=2,
            actor_id="ceo-subject",
            idempotency_key="crash-window-answer",
        )
        lease = notifications.claim_decision_response(
            "tenant-a", worker_id="worker-that-stops", lease_seconds=3,
        )
        assert lease is not None
        committed = graph.submit_graph_event(
            "tenant-a",
            lease.run_id,
            WorkflowEvent(
                lease.event_id,
                WorkflowEventKind.WAIT_RESUMED,
                lease.expected_version,
                {
                    "correlation_id": lease.correlation_id,
                    "response": dict(lease.response),
                    "responded_by": lease.actor_id,
                    "notification_id": lease.notification_id,
                },
            ),
        )
        assert committed.duplicate is False
        assert notifications.get_decision_response(
            "tenant-a", notification_id="notice-release",
        )["status"] == "executing"

        now[0] += timedelta(seconds=4)
        recovered = DurableDecisionResponseWorker(
            store=notifications, graph=graph, worker_id="recovery-worker", lease_seconds=30,
        ).run_one("tenant-a")

        assert recovered.status is CommandRunStatus.SUCCEEDED
        settled = notifications.get_decision_response(
            "tenant-a", notification_id="notice-release",
        )
        assert settled["response_id"] == admitted["response_id"]
        assert settled["status"] == "applied"
        assert settled["attempts"] == 2
        assert settled["result"]["duplicate_event"] is True
        assert graph.get_graph_run("tenant-a", "release-run").version == 3
    finally:
        notifications.close()
        graph.close()


def test_structured_decision_migration_is_tenant_fenced_and_worker_narrow():
    migration = (
        ROOT / "postgres/initdb/103-structured-decision-responses-v2.sql"
    ).read_text()

    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "UNIQUE (tenant_id, notification_id)" in migration
    assert "FOR SELECT TO agentos_worker" in migration
    assert "SELECT (tenant_id, status, available_at, lease_expires_at)" in migration
    assert "GRANT INSERT" not in migration.split("FROM agentos_worker")[-1]
