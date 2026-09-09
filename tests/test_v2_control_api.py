from __future__ import annotations

import base64
import hashlib
from typing import Any, Mapping

from fastapi.testclient import TestClient
import pytest

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


def test_ceo_workspace_assets_are_public_but_api_data_stays_authenticated():
    api = client()
    root = api.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/app"
    page = api.get("/app")
    assert page.status_code == 200
    assert "CEO Workspace" in page.text
    assert "unsafe-inline" not in page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert api.get("/assets/ceo.css").status_code == 200
    assert api.get("/assets/ceo.js").status_code == 200
    assert api.get("/v2/client-config").json() == {"identity_mode": "manual"}
    assert api.get("/v2/runs").status_code == 401


def test_ceo_workspace_publishes_only_validated_public_oidc_pkce_configuration():
    config = {
        "identity_mode": "oidc",
        "authorization_url": "https://identity.example.test/authorize",
        "token_url": "https://tokens.example.test/oauth/token",
        "client_id": "public-browser-client",
        "scope": "openid profile email",
        "audience": "agent-os-api",
        "authorization_audience_parameter": "audience",
        "redirect_uri": "https://app.example.test/app",
    }
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=FakeIdentity(),
        client_identity_config=config,
    ))
    response = api.get("/v2/client-config")
    assert response.json() == config
    assert response.headers["cache-control"] == "no-store"
    assert "https://tokens.example.test" in api.get("/app").headers["content-security-policy"]

    with pytest.raises(ValueError, match="audience parameter"):
        create_app(
            engine=InMemoryWorkflowEngine(), identity=FakeIdentity(),
            client_identity_config={**config, "authorization_audience_parameter": "bad; connect-src *"},
        )


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
    assert state.json()["objective"] == "Build my application"


def test_run_inventory_is_tenant_bound_bounded_and_keeps_objectives_compact():
    api = client()
    for tenant, suffix in (("org-a", "first"), ("org-b", "hidden"), ("org-a", "latest")):
        response = api.post(
            "/v2/runs",
            headers={
                "Authorization": f"Bearer {tenant}",
                "Idempotency-Key": f"request-inventory-{suffix}",
            },
            json={"prompt": f"Build the {suffix} product", "title": suffix.title()},
        )
        assert response.status_code == 202

    inventory = api.get("/v2/runs?limit=1", headers={"Authorization": "Bearer org-a"})
    assert inventory.status_code == 200
    assert inventory.json()["items"] == [{
        "run_id": inventory.json()["items"][0]["run_id"],
        "title": "Latest",
        "objective_preview": "Build the latest product",
        "phase": "research",
        "status": "active",
        "version": 1,
        "verification_cycle": 0,
        "artifact_revision": None,
    }]
    assert "hidden" not in str(inventory.json())


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


def test_mission_status_links_authenticated_lifecycle_planning_and_execution(tmp_path):
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
        (NodeToken(
            "work-token", "work", TokenStatus.READY, 1,
            output={"organization_actions": {"hiring_requests": [{
                "role": "security-specialist",
                "reason": "Independent security capacity is missing",
                "participant_kind": "agent",
                "capabilities": ["security-review"],
                "requested_count": 1,
                "estimated_budget_cents": 0,
            }]}},
        ),),
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

    directory = SQLCompanyDirectory(
        f"sqlite:///{tmp_path / 'mission-company.sqlite3'}", create_schema=True,
    )
    api = TestClient(create_app(
        engine=lifecycle, identity=FakeIdentity(), graph_engine=MissionGraphs(),
        company_directory=directory,
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
    proposal = management.json()["hiring_requests"][0]
    assert proposal["status"] == "pending"
    approved = api.post(
        f"/v2/runs/{created['run_id']}/management/proposals/"
        f"{proposal['proposal_id']}/hiring-decision",
        headers={"Authorization": "Bearer org-a"},
        json={
            "approved": True,
            "reason": "Approved within a zero-default spend boundary",
            "team_id": "engineering",
            "manager_id": "agent:engineering-manager",
            "tool_grants": ["artifact.read"],
            "spending_limit_cents": 0,
        },
    )
    assert approved.status_code == 200
    promoted_id = approved.json()["payload"]["agents"][0]["agent_id"]
    assert promoted_id in directory.get_organization("org-a").agents
    after_decision = api.get(
        f"/v2/runs/{created['run_id']}/management",
        headers={"Authorization": "Bearer org-a"},
    ).json()
    assert after_decision["hiring_requests"][0]["status"] == "approved"
    hidden = api.get(
        f"/v2/runs/{created['run_id']}/mission",
        headers={"Authorization": "Bearer org-b"},
    )
    assert hidden.status_code == 404
    directory.close()
