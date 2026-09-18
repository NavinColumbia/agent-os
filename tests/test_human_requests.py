from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest
from fastapi.testclient import TestClient

from agent_os.api.app import create_app
from agent_os.application.lifecycle import CommandEnvelope
from agent_os.domain.lifecycle import Command, CommandKind, LifecycleState
from agent_os.domain.notifications import Notification, NotificationCategory
from agent_os.domain.workflow_runtime import WorkflowAction, WorkflowActionKind
from agent_os.infrastructure.memory import InMemoryWorkflowEngine
from agent_os.infrastructure.notification_effects import NotificationEffectHandlers
from agent_os.infrastructure.sql_notifications import SQLNotificationStore


ROOT = Path(__file__).resolve().parents[1]


class Identity:
    def authenticate(self, authorization, session) -> Mapping[str, Any]:
        del session
        identities = {
            "Bearer owner": {
                "sub": "owner-subject", "org": "tenant-a", "roles": ["owner"],
            },
            "Bearer reviewer": {
                "sub": "reviewer-subject", "org": "tenant-a", "roles": ["reviewer"],
            },
            "Bearer viewer": {
                "sub": "viewer-subject", "org": "tenant-a", "roles": ["viewer"],
            },
        }
        if authorization not in identities:
            raise ValueError("authentication required")
        return identities[authorization]


def test_advisory_request_is_nonblocking_recipient_owned_and_replay_safe(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'advisory-requests.sqlite3'}"
    engine = InMemoryWorkflowEngine()
    engine.start_run(LifecycleState(run_id="mission-one", organization_id="tenant-a"))
    store = SQLNotificationStore(database_url, create_schema=True)
    api = TestClient(create_app(
        engine=engine, identity=Identity(), notification_store=store,
    ))
    owner_headers = {
        "Authorization": "Bearer owner", "Idempotency-Key": "request-review-one",
    }
    request_body = {
        "recipient_id": "role:reviewer",
        "subject": "Review the launch copy",
        "body": "Please flag unsupported claims without pausing the mission.",
    }
    try:
        created = api.post(
            "/v2/runs/mission-one/human-requests",
            headers=owner_headers,
            json=request_body,
        )
        assert created.status_code == 201
        request = created.json()
        assert request["request_kind"] == "advisory"
        assert request["recipient_id"] == "role:reviewer"
        assert request["requested_by"] == "owner-subject"
        assert request["status"] == "open"
        assert request["duplicate"] is False
        assert engine.get_run("tenant-a", "mission-one").version == 0

        replay = api.post(
            "/v2/runs/mission-one/human-requests",
            headers=owner_headers,
            json=request_body,
        )
        assert replay.status_code == 201
        assert replay.json()["request_id"] == request["request_id"]
        assert replay.json()["duplicate"] is True
        conflict = api.post(
            "/v2/runs/mission-one/human-requests",
            headers=owner_headers,
            json={**request_body, "body": "Replace the original request."},
        )
        assert conflict.status_code == 409

        assert [item["request_id"] for item in api.get(
            "/v2/human-requests", headers={"Authorization": "Bearer owner"},
        ).json()["items"]] == [request["request_id"]]
        assert [item["request_id"] for item in api.get(
            "/v2/human-requests", headers={"Authorization": "Bearer reviewer"},
        ).json()["items"]] == [request["request_id"]]
        assert api.get(
            "/v2/human-requests", headers={"Authorization": "Bearer viewer"},
        ).json()["items"] == []

        response_path = f"/v2/human-requests/{request['request_id']}/responses"
        denied = api.post(
            response_path,
            headers={
                "Authorization": "Bearer owner",
                "Idempotency-Key": "owner-cannot-answer",
            },
            json={"response": {"answer": "Looks good"}},
        )
        assert denied.status_code == 404
        reviewer_headers = {
            "Authorization": "Bearer reviewer",
            "Idempotency-Key": "reviewer-answer-one",
        }
        answered = api.post(
            response_path, headers=reviewer_headers,
            json={"response": {"answer": "Remove the guaranteed claim."}},
        )
        assert answered.status_code == 200
        assert answered.json()["status"] == "answered"
        assert answered.json()["responded_by"] == "reviewer-subject"
        duplicate = api.post(
            response_path, headers=reviewer_headers,
            json={"response": {"answer": "Remove the guaranteed claim."}},
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["duplicate"] is True
        changed = api.post(
            response_path, headers=reviewer_headers,
            json={"response": {"answer": "A different answer"}},
        )
        assert changed.status_code == 409
        assert engine.get_run("tenant-a", "mission-one").version == 0

        second_headers = {
            "Authorization": "Bearer owner",
            "Idempotency-Key": "request-review-two",
        }
        second = api.post(
            "/v2/runs/mission-one/human-requests",
            headers=second_headers,
            json={**request_body, "subject": "One more review"},
        )
        assert second.status_code == 201
        cancelled = api.post(
            "/v2/runs/mission-one/cancel",
            headers={"Authorization": "Bearer owner"},
            json={
                "event_id": "cancel-after-request", "expected_version": 0,
                "reason": "Stop this mission",
            },
        )
        assert cancelled.status_code == 202
        assert store.get_human_request(
            "tenant-a", request_id=second.json()["request_id"],
        )["status"] == "cancelled"
        rejected = api.post(
            "/v2/runs/mission-one/human-requests",
            headers={
                "Authorization": "Bearer owner",
                "Idempotency-Key": "request-after-cancel",
            },
            json=request_body,
        )
        assert rejected.status_code == 409
    finally:
        store.close()


def test_visibility_filter_is_applied_before_request_page_limit(tmp_path: Path):
    store = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'request-pagination.sqlite3'}", create_schema=True,
    )
    try:
        target = Notification(
            notification_id="notice-target", tenant_id="tenant-a", run_id="run-one",
            category=NotificationCategory.HUMAN_ACTION_REQUIRED,
            recipient_ids=("role:reviewer",), subject="Target", body="Old but visible",
            source_id="source-target", created_at="2026-09-17T10:00:00+00:00",
            correlation_id=None,
            payload={
                "request_kind": "advisory",
                "request_recipient_id": "role:reviewer",
                "requested_by": "owner-subject",
            },
        )
        store.publish_notification(target)
        for position in range(20):
            store.publish_notification(Notification(
                notification_id=f"notice-noise-{position}",
                tenant_id="tenant-a", run_id="run-one",
                category=NotificationCategory.HUMAN_ACTION_REQUIRED,
                recipient_ids=("role:builder",), subject="Noise", body="Not visible",
                source_id=f"source-noise-{position}",
                created_at=f"2026-09-17T11:{position:02d}:00+00:00",
                correlation_id=None,
                payload={
                    "request_kind": "advisory",
                    "request_recipient_id": "role:builder",
                    "requested_by": "another-owner",
                },
            ))

        page = store.list_human_requests(
            "tenant-a", audience_ids=("role:reviewer",),
            requested_by="reviewer-subject", limit=1,
        )
        assert [item["notification_id"] for item in page] == ["notice-target"]
    finally:
        store.close()


def test_workflow_request_is_authoritative_and_terminal_effect_closes_stale_wait(
    tmp_path: Path,
):
    store = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'workflow-requests.sqlite3'}", create_schema=True,
    )
    effects = NotificationEffectHandlers(
        store, clock=lambda: datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
    )
    waiting = WorkflowAction(
        "wait-action-one", WorkflowActionKind.NOTIFY_HUMAN, "token-one", "review",
        {
            "correlation_id": "question-one",
            "reason": "Choose the supported launch claim",
            "recipient_ids": ["role:reviewer"],
        },
    )
    terminal = WorkflowAction(
        "terminal-action-one", WorkflowActionKind.RUN_CANCELLED, None, None,
        {"reason": "The owner cancelled the run"},
    )
    envelope = {"tenant_id": "tenant-a", "run_id": "graph-one"}
    try:
        first = effects.graph_handlers()[WorkflowActionKind.NOTIFY_HUMAN](
            envelope, waiting,
        )
        repeated = effects.graph_handlers()[WorkflowActionKind.NOTIFY_HUMAN](
            envelope, waiting,
        )
        assert first["created"] is True
        assert repeated["created"] is False
        request = store.get_human_request(
            "tenant-a", notification_id=first["notification_id"],
        )
        assert request is not None
        assert request["request_kind"] == "workflow_blocking"
        assert request["recipient_id"] == "role:reviewer"
        assert request["correlation_id"] == "question-one"
        assert request["status"] == "open"

        closed = effects.graph_handlers()[WorkflowActionKind.RUN_CANCELLED](
            envelope, terminal,
        )
        assert closed["closed_human_requests"] == 1
        assert store.get_human_request(
            "tenant-a", request_id=request["request_id"],
        )["status"] == "cancelled"
        assert effects.graph_handlers()[WorkflowActionKind.RUN_CANCELLED](
            envelope, terminal,
        )["closed_human_requests"] == 0
    finally:
        store.close()


def test_legacy_notice_does_not_invent_an_authoritative_request(tmp_path: Path):
    store = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'legacy-notice.sqlite3'}", create_schema=True,
    )
    notice = Notification(
        notification_id="legacy-notice", tenant_id="tenant-a", run_id="run-one",
        category=NotificationCategory.HUMAN_ACTION_REQUIRED,
        recipient_ids=("human:ceo",), subject="Legacy notice", body="Review it",
        source_id="legacy-source", created_at="2026-09-17T12:00:00+00:00",
        correlation_id="legacy-correlation", payload={"risk": "legacy"},
    )
    try:
        assert store.publish_notification(notice) is True
        assert store.get_human_request(
            "tenant-a", notification_id="legacy-notice",
        ) is None
    finally:
        store.close()


def test_terminal_run_supersedes_recorded_response_and_forbids_new_redrive(tmp_path: Path):
    store = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'superseded-response.sqlite3'}", create_schema=True,
    )
    effects = NotificationEffectHandlers(store)
    envelope = {"tenant_id": "tenant-a", "run_id": "graph-response"}
    waiting = WorkflowAction(
        "wait-response", WorkflowActionKind.NOTIFY_HUMAN, "token-one", "review",
        {
            "correlation_id": "question-response",
            "reason": "Approve the candidate",
            "recipient_ids": ["human:ceo"],
        },
    )
    terminal = WorkflowAction(
        "terminal-response", WorkflowActionKind.RUN_CANCELLED, None, None,
        {"reason": "Cancelled before response execution"},
    )
    try:
        notice = effects.graph_handlers()[WorkflowActionKind.NOTIFY_HUMAN](
            envelope, waiting,
        )
        admitted = store.admit_decision_response(
            tenant_id="tenant-a", notification_id=notice["notification_id"],
            run_id="graph-response", correlation_id="question-response",
            response={"approved": True}, expected_version=2,
            actor_id="ceo-subject", idempotency_key="response-before-cancel",
        )
        assert admitted["status"] == "pending"
        effects.graph_handlers()[WorkflowActionKind.RUN_CANCELLED](envelope, terminal)
        assert store.get_human_request(
            "tenant-a", notification_id=notice["notification_id"],
        )["status"] == "superseded"

        lease = store.claim_decision_response(
            "tenant-a", worker_id="decision-worker", lease_seconds=30,
        )
        assert lease is not None
        assert store.fail_decision_response(
            "tenant-a", lease.response_id, worker_id="decision-worker",
            error={"type": "Cancelled", "message": "run ended"},
        ) is True
        with pytest.raises(ValueError, match="no longer recoverable"):
            store.redrive_decision_response(
                tenant_id="tenant-a", notification_id=notice["notification_id"],
                actor_id="operator-subject", idempotency_key="do-not-redrive-closed",
            )
    finally:
        store.close()


def test_lifecycle_completion_closes_open_requests(tmp_path: Path):
    store = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'lifecycle-close.sqlite3'}", create_schema=True,
    )
    effects = NotificationEffectHandlers(store)
    request_envelope = CommandEnvelope(
        "request-command", "run-one", "tenant-a", "event-one", 1, 0,
        Command(CommandKind.NOTIFY_HUMAN, {
            "correlation_id": "lifecycle-question",
            "reason": "Clarify the customer promise",
        }),
    )
    completion = CommandEnvelope(
        "completion-command", "run-one", "tenant-a", "event-two", 2, 0,
        Command(CommandKind.PUBLISH_COMPLETION, {"summary": "Accepted"}),
    )
    try:
        effects.lifecycle_handlers()[CommandKind.NOTIFY_HUMAN](request_envelope)
        result = effects.lifecycle_handlers()[CommandKind.PUBLISH_COMPLETION](completion)
        assert result["closed_human_requests"] == 1
        assert store.list_human_requests("tenant-a", run_id="run-one")[0][
            "status"
        ] == "cancelled"
    finally:
        store.close()


def test_only_nonrecoverable_lifecycle_failure_closes_requests(tmp_path: Path):
    store = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'lifecycle-failure-close.sqlite3'}", create_schema=True,
    )
    effects = NotificationEffectHandlers(store)
    request_envelope = CommandEnvelope(
        "failure-request", "run-failure", "tenant-a", "event-one", 1, 0,
        Command(CommandKind.NOTIFY_HUMAN, {
            "correlation_id": "failure-question", "reason": "Clarify recovery",
        }),
    )
    recoverable = CommandEnvelope(
        "recoverable-failure", "run-failure", "tenant-a", "event-two", 2, 0,
        Command(CommandKind.NOTIFY_OPERATOR, {
            "operation": "build", "reason": "Provider unavailable", "recoverable": True,
        }),
    )
    terminal = CommandEnvelope(
        "terminal-failure", "run-failure", "tenant-a", "event-three", 3, 0,
        Command(CommandKind.NOTIFY_OPERATOR, {
            "operation": "build", "reason": "Invalid invariant", "recoverable": False,
        }),
    )
    try:
        effects.lifecycle_handlers()[CommandKind.NOTIFY_HUMAN](request_envelope)
        assert effects.lifecycle_handlers()[CommandKind.NOTIFY_OPERATOR](recoverable)[
            "closed_human_requests"
        ] == 0
        assert store.list_human_requests("tenant-a", run_id="run-failure")[0][
            "status"
        ] == "open"
        assert effects.lifecycle_handlers()[CommandKind.NOTIFY_OPERATOR](terminal)[
            "closed_human_requests"
        ] == 1
        assert store.list_human_requests("tenant-a", run_id="run-failure")[0][
            "status"
        ] == "cancelled"
    finally:
        store.close()


def test_human_request_migration_is_tenant_fenced_and_not_worker_writable():
    migration = (ROOT / "postgres/initdb/109-human-requests-v2.sql").read_text()

    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "UNIQUE (tenant_id, notification_id)" in migration
    assert "UNIQUE (tenant_id, source_id)" in migration
    assert "WHERE correlation_id IS NOT NULL" in migration
    assert "REVOKE ALL ON TABLE public.aos_v2_human_requests FROM agentos_worker" in migration
    assert "GRANT SELECT, INSERT, UPDATE" in migration
    assert "TO agentos_worker" not in migration.split(
        "REVOKE ALL ON TABLE public.aos_v2_human_requests FROM agentos_worker"
    )[-1]
