from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from fastapi.testclient import TestClient

from agent_os.api.app import create_app
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import WorkflowEvent, WorkflowEventKind
from agent_os.infrastructure.memory import InMemoryWorkflowEngine
from agent_os.infrastructure.sql_workflow_graph import SQLGraphWorkflowEngine


class Identity:
    def authenticate(self, authorization, session) -> Mapping[str, Any]:
        del session
        if authorization == "Bearer owner":
            return {"sub": "ceo-subject", "org": "tenant-a", "roles": ["owner"]}
        if authorization == "Bearer viewer":
            return {"sub": "viewer-subject", "org": "tenant-a", "roles": ["viewer"]}
        raise ValueError("authentication required")


class Notifications:
    item: Mapping[str, Any]

    def list_notifications(self, tenant_id, **kwargs):
        del kwargs
        assert tenant_id == "tenant-a"
        return (self.item,)


def waiting_graph(graph: SQLGraphWorkflowEngine) -> None:
    definition = WorkflowDefinition(
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
    )
    graph.register_workflow(definition)
    started = graph.start_graph_run(
        "tenant-a", "release", 1,
        run_id="release-run", request_id="release-start",
    )
    token_id = str(started.actions[0].token_id)
    graph.submit_graph_event(
        "tenant-a", "release-run",
        WorkflowEvent(
            "release-began", WorkflowEventKind.NODE_BEGAN, 0,
            {"token_id": token_id},
        ),
    )
    graph.submit_graph_event(
        "tenant-a", "release-run",
        WorkflowEvent(
            "release-waited", WorkflowEventKind.NODE_WAITED, 1,
            {
                "token_id": token_id,
                "correlation_id": "release-approval",
                "reason": "Approve production release",
                "recipient_ids": ["human:ceo"],
            },
        ),
    )


def test_ceo_inbox_resolves_only_current_authorized_human_wait_and_replay_is_safe(
    tmp_path: Path,
):
    graph = SQLGraphWorkflowEngine(
        f"sqlite:///{tmp_path / 'human-api.sqlite3'}", create_schema=True,
    )
    waiting_graph(graph)
    notifications = Notifications()
    notifications.item = {
        "notification_id": "notice-release",
        "run_id": "release-run",
        "category": "human_action_required",
        "correlation_id": "release-approval",
        "subject": "Workflow needs your input",
        "body": "Approve production release",
    }
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(),
        identity=Identity(),
        graph_engine=graph,
        notification_store=notifications,
    ))
    try:
        inbox = api.get("/v2/notifications", headers={"Authorization": "Bearer owner"})
        assert inbox.status_code == 200
        assert inbox.json()["items"][0]["actionable"] is True

        payload = {
            "event_id": "ceo-release-decision",
            "kind": "wait_resumed",
            "expected_version": 2,
            "payload": {
                "correlation_id": "release-approval",
                "response": {"approved": True, "answer": "Ship it"},
            },
        }
        forbidden = api.post(
            "/v2/graph-runs/release-run/events",
            headers={"Authorization": "Bearer viewer"},
            json=payload,
        )
        assert forbidden.status_code == 403

        oversized = {
            **payload,
            "event_id": "oversized-decision",
            "payload": {
                "correlation_id": "release-approval",
                "response": {"answer": "x" * 17_000},
            },
        }
        assert api.post(
            "/v2/graph-runs/release-run/events",
            headers={"Authorization": "Bearer owner"},
            json=oversized,
        ).status_code == 413

        raw_revision = api.post(
            "/v2/graph-runs/release-run/events",
            headers={"Authorization": "Bearer owner"},
            json={
                "event_id": "bypass-program-authority", "kind": "run_revised",
                "expected_version": 2, "payload": {},
            },
        )
        assert raw_revision.status_code == 403
        assert "program-revision authority" in raw_revision.json()["detail"]

        accepted = api.post(
            "/v2/graph-runs/release-run/events",
            headers={"Authorization": "Bearer owner"},
            json=payload,
        )
        replay = api.post(
            "/v2/graph-runs/release-run/events",
            headers={"Authorization": "Bearer owner"},
            json=payload,
        )
        assert accepted.status_code == 202
        assert replay.status_code == 202
        assert replay.json()["duplicate"] is True
        assert api.get(
            "/v2/notifications", headers={"Authorization": "Bearer owner"},
        ).json()["items"][0]["actionable"] is False
    finally:
        api.close()
        graph.close()


def test_owner_can_recover_a_failed_graph_node_but_viewer_cannot(tmp_path: Path):
    graph = SQLGraphWorkflowEngine(
        f"sqlite:///{tmp_path / 'failed-graph-api.sqlite3'}", create_schema=True,
    )
    definition = WorkflowDefinition(
        "recover", "tenant-a", "Recover", 1, "work",
        (
            WorkflowNode("work", NodeKind.AGENT, "Do work", "engineer"),
            WorkflowNode("done", NodeKind.TERMINAL, "Done"),
        ),
        (WorkflowEdge("work", "done", "done"),),
        "architect",
    )
    graph.register_workflow(definition)
    started = graph.start_graph_run(
        "tenant-a", "recover", 1, run_id="failed-run", request_id="failed-start",
    )
    token_id = started.state.ready()[0].token_id
    running = graph.submit_graph_event("tenant-a", "failed-run", WorkflowEvent(
        "failed-begin", WorkflowEventKind.NODE_BEGAN, 0, {"token_id": token_id},
    ))
    failed = graph.submit_graph_event("tenant-a", "failed-run", WorkflowEvent(
        "failed-result", WorkflowEventKind.NODE_FAILED, running.state.version,
        {"token_id": token_id, "reason": "fixed later", "retryable": False},
    ))
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=Identity(), graph_engine=graph,
    ))
    payload = {
        "event_id": "operator-recovery",
        "kind": "node_retry_requested",
        "expected_version": failed.state.version,
        "payload": {"token_id": token_id, "reason": "The blocker is repaired."},
    }
    try:
        denied = api.post(
            "/v2/graph-runs/failed-run/events",
            headers={"Authorization": "Bearer viewer"},
            json=payload,
        )
        accepted = api.post(
            "/v2/graph-runs/failed-run/events",
            headers={"Authorization": "Bearer owner"},
            json=payload,
        )
        assert denied.status_code == 403
        assert accepted.status_code == 202
        assert accepted.json()["state"]["status"] == "active"
    finally:
        api.close()
        graph.close()


def test_viewer_cannot_cancel_a_ceo_mission():
    api = TestClient(create_app(engine=InMemoryWorkflowEngine(), identity=Identity()))
    try:
        created = api.post(
            "/v2/runs",
            headers={"Authorization": "Bearer owner", "Idempotency-Key": "mission-to-protect"},
            json={"prompt": "Build safely"},
        ).json()
        denied = api.post(
            f"/v2/runs/{created['run_id']}/cancel",
            headers={"Authorization": "Bearer viewer"},
            json={"event_id": "viewer-cancel", "expected_version": 1, "reason": "No"},
        )
        assert denied.status_code == 403
    finally:
        api.close()
