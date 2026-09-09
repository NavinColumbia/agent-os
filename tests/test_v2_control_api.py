from __future__ import annotations

import base64
import hashlib
from typing import Any, Mapping

from fastapi.testclient import TestClient

from agent_os.api.app import create_app
from agent_os.application.mission import mission_planning_run_id
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import (
    NodeToken,
    TokenStatus,
    WorkflowRunState,
    WorkflowRunStatus,
)
from agent_os.infrastructure.memory import InMemoryWorkflowEngine
from agent_os.infrastructure.sql_company_directory import SQLCompanyDirectory


class FakeIdentity:
    def authenticate(self, authorization: str | None, session: str | None) -> Mapping[str, Any]:
        if authorization == "Bearer org-a":
            return {"sub": "human-a", "org": "org-a", "roles": ["owner"]}
        if authorization == "Bearer org-b":
            return {"sub": "human-b", "org": "org-b", "roles": ["owner"]}
        if authorization == "Bearer agent-a":
            return {"sub": "agent-a", "org": "org-a", "roles": ["agent"]}
        if authorization == "Bearer viewer-a":
            return {"sub": "viewer-a", "org": "org-a", "roles": ["viewer"]}
        raise ValueError("authentication required")


def client() -> TestClient:
    return TestClient(create_app(engine=InMemoryWorkflowEngine(), identity=FakeIdentity()))


def test_directive_is_tenant_bound_idempotent_and_immediately_enters_research():
    api = client()
    headers = {"Authorization": "Bearer org-a", "Idempotency-Key": "request-123"}
    first = api.post("/v2/runs", headers=headers, json={"prompt": "Build my application"})
    repeated = api.post("/v2/runs", headers=headers, json={"prompt": "Build my application"})

    assert first.status_code == 202
    assert repeated.status_code == 202
    assert first.json()["run_id"] == repeated.json()["run_id"]
    assert repeated.json()["duplicate"] is True

    state = api.get(f"/v2/runs/{first.json()['run_id']}", headers={"Authorization": "Bearer org-a"})
    assert state.status_code == 200
    assert state.json()["phase"] == "research"
    assert state.json()["version"] == 1


def test_tenant_isolation_comes_from_identity_not_request_headers():
    api = client()
    created = api.post(
        "/v2/runs",
        headers={"Authorization": "Bearer org-a", "Idempotency-Key": "request-tenant"},
        json={"prompt": "Private tenant A objective"},
    ).json()

    hidden = api.get(
        f"/v2/runs/{created['run_id']}",
        headers={"Authorization": "Bearer org-b", "X-Organization-Id": "org-a"},
    )
    assert hidden.status_code == 404


def test_humans_cannot_forge_internal_completion_events_but_agents_can():
    api = client()
    created = api.post(
        "/v2/runs",
        headers={"Authorization": "Bearer org-a", "Idempotency-Key": "request-events"},
        json={"prompt": "Build safely"},
    ).json()
    payload = {
        "event_id": "research-done",
        "kind": "research_completed",
        "expected_version": 1,
        "payload": {"report_id": "report-1"},
    }

    forbidden = api.post(
        f"/v2/runs/{created['run_id']}/events",
        headers={"Authorization": "Bearer org-a"},
        json=payload,
    )
    accepted = api.post(
        f"/v2/runs/{created['run_id']}/events",
        headers={"Authorization": "Bearer agent-a"},
        json=payload,
    )

    assert forbidden.status_code == 403
    assert accepted.status_code == 202
    state = api.get(
        f"/v2/runs/{created['run_id']}", headers={"Authorization": "Bearer org-a"}
    )
    assert state.json()["phase"] == "specify"


def test_authentication_idempotency_and_body_limits_fail_closed():
    api = client()
    assert api.get("/v2/runs/unknown").status_code == 401
    missing_key = api.post(
        "/v2/runs",
        headers={"Authorization": "Bearer org-a"},
        json={"prompt": "Build"},
    )
    assert missing_key.status_code == 422
    extra_tenant = api.post(
        "/v2/runs",
        headers={"Authorization": "Bearer org-a", "Idempotency-Key": "request-extra"},
        json={"prompt": "Build", "organization_id": "org-b"},
    )
    assert extra_tenant.status_code == 422


def test_notification_inbox_uses_authenticated_tenant_and_role_scope():
    class FakeNotifications:
        def __init__(self):
            self.calls = []

        def list_notifications(self, tenant_id, *, run_id=None, recipient_id=None, limit=100):
            self.calls.append((tenant_id, run_id, recipient_id, limit))
            return ({"notification_id": f"notice-{tenant_id}"},)

    store = FakeNotifications()
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=FakeIdentity(), notification_store=store,
    ))

    owner = api.get("/v2/notifications?run_id=run-1&limit=25", headers={"Authorization": "Bearer org-a"})
    agent = api.get("/v2/notifications", headers={"Authorization": "Bearer agent-a"})

    assert owner.status_code == 200
    assert owner.json()["items"][0]["notification_id"] == "notice-org-a"
    assert store.calls[0] == ("org-a", "run-1", None, 25)
    assert store.calls[1] == ("org-a", None, "agent-a", 100)


def test_standing_company_agents_are_managed_by_tenant_authority_and_survive_runs(tmp_path):
    directory = SQLCompanyDirectory(
        f"sqlite:///{tmp_path / 'api-company.sqlite3'}", create_schema=True,
    )
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=FakeIdentity(), company_directory=directory,
    ))
    payload = {
        "role": "growth-researcher",
        "team_id": "research",
        "manager_id": "agent:research-lead",
        "capabilities": ["market-research"],
        "tool_grants": ["artifact.read"],
        "spending_limit_cents": 100,
    }
    try:
        forbidden = api.post(
            "/v2/company/agents",
            headers={"Authorization": "Bearer viewer-a", "Idempotency-Key": "company-agent-viewer"},
            json=payload,
        )
        first = api.post(
            "/v2/company/agents",
            headers={"Authorization": "Bearer org-a", "Idempotency-Key": "company-agent-001"},
            json=payload,
        )
        replay = api.post(
            "/v2/company/agents",
            headers={"Authorization": "Bearer org-a", "Idempotency-Key": "company-agent-001"},
            json=payload,
        )

        assert forbidden.status_code == 403
        assert first.status_code == 201 and first.json()["duplicate"] is False
        assert replay.status_code == 201 and replay.json()["duplicate"] is True
        agent_id = first.json()["payload"]["agent_id"]
        organization = api.get(
            "/v2/company/organization", headers={"Authorization": "Bearer org-a"},
        ).json()
        assert any(item["agent_id"] == agent_id for item in organization["agents"])
        other = api.get(
            "/v2/company/organization", headers={"Authorization": "Bearer org-b"},
        ).json()
        assert all(item["agent_id"] != agent_id for item in other["agents"])
        activity = api.get(
            "/v2/company/activity", headers={"Authorization": "Bearer org-a"},
        ).json()
        assert activity["next_version"] == 1

        retired = api.post(
            f"/v2/company/agents/{agent_id}/retire",
            headers={"Authorization": "Bearer org-a", "Idempotency-Key": "company-agent-retire"},
            json={"reason": "Capacity no longer required"},
        )
        assert retired.status_code == 200
        after = api.get(
            "/v2/company/organization", headers={"Authorization": "Bearer org-a"},
        ).json()
        assert next(item for item in after["agents"] if item["agent_id"] == agent_id)["status"] == "retired"
    finally:
        directory.close()


def test_artifact_upload_download_and_metadata_are_tenant_scoped():
    class FakeArtifacts:
        def __init__(self):
            self.values = {}

        def put(self, *, organization_id, content, media_type, idempotency_key):
            artifact_id = f"artifact-{organization_id}"
            self.values[(organization_id, artifact_id)] = (
                content,
                {"artifact_id": artifact_id, "tenant_id": organization_id,
                 "media_type": media_type, "digest": "digest"},
            )
            return artifact_id

        def get(self, organization_id, artifact_id):
            found = self.values.get((organization_id, artifact_id))
            return None if found is None else found[0]

        def describe(self, organization_id, artifact_id):
            found = self.values.get((organization_id, artifact_id))
            return None if found is None else found[1]

    store = FakeArtifacts()
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=FakeIdentity(), artifact_store=store,
    ))
    uploaded = api.post(
        "/v2/artifacts",
        headers={"Authorization": "Bearer org-a", "Idempotency-Key": "artifact-request-1"},
        json={
            "content_base64": base64.b64encode(b"customer source").decode(),
            "media_type": "text/plain",
        },
    )

    assert uploaded.status_code == 201
    artifact_id = uploaded.json()["artifact_id"]
    downloaded = api.get(
        f"/v2/artifacts/{artifact_id}/content", headers={"Authorization": "Bearer org-a"},
    )
    assert downloaded.content == b"customer source"
    assert downloaded.headers["etag"] == '"digest"'
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    hidden = api.get(
        f"/v2/artifacts/{artifact_id}", headers={"Authorization": "Bearer org-b"},
    )
    assert hidden.status_code == 404
    forbidden = api.post(
        "/v2/artifacts",
        headers={"Authorization": "Bearer viewer-a", "Idempotency-Key": "artifact-request-viewer"},
        json={
            "content_base64": base64.b64encode(b"no authority").decode(),
            "media_type": "text/plain",
        },
    )
    assert forbidden.status_code == 403


def test_artifact_upload_rejects_invalid_base64():
    class UnusedArtifacts:
        def put(self, **kwargs):
            raise AssertionError("invalid content must not reach storage")

        def get(self, *args):
            return None

        def describe(self, *args):
            return None

    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=FakeIdentity(),
        artifact_store=UnusedArtifacts(),
    ))

    response = api.post(
        "/v2/artifacts",
        headers={"Authorization": "Bearer org-a", "Idempotency-Key": "artifact-request-2"},
        json={"content_base64": "not base64!", "media_type": "text/plain"},
    )

    assert response.status_code == 422


def test_mission_status_links_authenticated_lifecycle_planning_and_execution():
    lifecycle = InMemoryWorkflowEngine()
    planning_run_id = mission_planning_run_id(
        "run-" + hashlib.sha256(
            b"agent-os:directive:v2:org-a:request-mission-status"
        ).hexdigest()[:32]
    )
    child_run_id = "mission-run-child"
    planning = WorkflowRunState(
        planning_run_id,
        "org-a",
        "agent-os-mission-bootstrap",
        1,
        2,
        WorkflowRunStatus.ACTIVE,
        (
            NodeToken(
                "launch-token",
                "launch",
                TokenStatus.SUCCEEDED,
                1,
                evidence_ids=("artifact-launch",),
                output={"child_run_id": child_run_id},
            ),
            NodeToken("done-token", "done", TokenStatus.READY, 1),
        ),
    )
    execution = WorkflowRunState(
        child_run_id,
        "org-a",
        "mission-workflow",
        1,
        0,
        WorkflowRunStatus.ACTIVE,
        (NodeToken("work-token", "work", TokenStatus.READY, 1),),
    )

    class MissionGraphs:
        def get_graph_run(self, tenant_id, run_id):
            if tenant_id != "org-a":
                return None
            return {planning_run_id: planning, child_run_id: execution}.get(run_id)

        def get_workflow_definition(self, tenant_id, workflow_id, version):
            if tenant_id != "org-a" or workflow_id != "mission-workflow" or version != 1:
                return None
            return WorkflowDefinition(
                "mission-workflow", "org-a", "Mission", 1, "work",
                (
                    WorkflowNode("work", NodeKind.AGENT, "Build the product", "engineer"),
                    WorkflowNode("done", NodeKind.TERMINAL, "Accept proof"),
                ),
                (WorkflowEdge("work", "done"),),
                "agent:mission-architect",
            )

    api = TestClient(create_app(
        engine=lifecycle, identity=FakeIdentity(), graph_engine=MissionGraphs(),
    ))
    created = api.post(
        "/v2/runs",
        headers={
            "Authorization": "Bearer org-a",
            "Idempotency-Key": "request-mission-status",
        },
        json={"prompt": "Build a product"},
    ).json()

    response = api.get(
        f"/v2/runs/{created['run_id']}/mission",
        headers={"Authorization": "Bearer org-a"},
    )

    assert response.status_code == 200
    assert response.json()["planning_run_id"] == planning_run_id
    assert response.json()["execution_run_id"] == child_run_id
    assert response.json()["execution"]["status"] == "active"
    assert response.json()["deliverables"] == []
    management = api.get(
        f"/v2/runs/{created['run_id']}/management",
        headers={"Authorization": "Bearer org-a"},
    )
    assert management.status_code == 200
    assert management.json()["execution_run_id"] == child_run_id
    assert management.json()["work_items"][0]["owner_id"] == "agent:engineer"
    assert management.json()["progress"]["live"] == 1
    hidden = api.get(
        f"/v2/runs/{created['run_id']}/mission",
        headers={"Authorization": "Bearer org-b"},
    )
    assert hidden.status_code == 404
