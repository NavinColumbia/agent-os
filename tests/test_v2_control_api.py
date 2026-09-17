from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Mapping

from fastapi.testclient import TestClient
import pytest

from agent_os.api.app import create_app
from agent_os.application.mission import mission_planning_run_id
from agent_os.domain.notifications import Notification, NotificationCategory
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import (
    NodeToken,
    TokenStatus,
    WorkflowRunState,
    WorkflowRunStatus,
)
from agent_os.infrastructure.memory import InMemoryWorkflowEngine
from agent_os.infrastructure.sql_company_directory import SQLCompanyDirectory
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore
from agent_os.infrastructure.sql_connectors import SQLConnectorRegistry
from agent_os.infrastructure.sql_memberships import SQLMembershipStore
from agent_os.infrastructure.sql_notifications import SQLNotificationStore
from agent_os.infrastructure.sql_tenant_models import SQLTenantModelStore
from agent_os.infrastructure.sql_usage_meter import SQLUsageMeter


class FakeIdentity:
    def authenticate(self, authorization: str | None, session: str | None) -> Mapping[str, Any]:
        if authorization == "Bearer org-a":
            return {"sub": "human-a", "org": "org-a", "roles": ["owner"]}
        if authorization == "Bearer org-b":
            return {"sub": "human-b", "org": "org-b", "roles": ["owner"]}
        if authorization == "Bearer agent-a":
            return {"sub": "agent-a", "org": "org-a", "roles": ["agent"]}
        if authorization == "Bearer operator-a":
            return {"sub": "operator-a", "org": "org-a", "roles": ["operator"]}
        if authorization == "Bearer builder-a":
            return {"sub": "builder-a", "org": "org-a", "roles": ["builder"]}
        if authorization == "Bearer reviewer-a":
            return {"sub": "reviewer-a", "org": "org-a", "roles": ["reviewer"]}
        if authorization == "Bearer admin-a":
            return {"sub": "admin-a", "org": "org-a", "roles": ["admin"]}
        if authorization == "Bearer manager-a":
            return {"sub": "manager-a", "org": "org-a", "roles": ["manager"]}
        if authorization == "Bearer billing-a":
            return {"sub": "billing-a", "org": "org-a", "roles": ["billing"]}
        if authorization == "Bearer client-a":
            return {"sub": "client-a", "org": "org-a", "roles": ["client"]}
        if authorization == "Bearer viewer-a":
            return {"sub": "viewer-a", "org": "org-a", "roles": ["viewer"]}
        if authorization == "Bearer guest-b":
            return {"sub": "guest-b", "org": "org-b", "roles": ["viewer"]}
        raise ValueError("authentication required")


def client() -> TestClient:
    return TestClient(create_app(engine=InMemoryWorkflowEngine(), identity=FakeIdentity()))


def test_identity_bound_team_invitations_select_and_revoke_tenant_access(tmp_path):
    memberships = SQLMembershipStore(
        f"sqlite:///{tmp_path / 'api-memberships.sqlite3'}",
        signing_secret="membership-api-secret-that-is-long-enough",
        create_schema=True,
    )
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=FakeIdentity(),
        membership_store=memberships, shutdown=memberships.close,
    ))
    owner = {"Authorization": "Bearer org-a", "Idempotency-Key": "invite-human-b"}
    with api:
        invitation = api.post(
            "/v2/invitations", headers=owner,
            json={"roles": ["viewer"], "expires_in_seconds": 3600},
        )
        assert invitation.status_code == 201
        assert invitation.json()["organization_id"] == "org-a"
        assert "claim_token" in invitation.json()

        claim = api.post(
            "/v2/invitations/claim",
            headers={"Authorization": "Bearer org-b"},
            json={"token": invitation.json()["claim_token"]},
        )
        assert claim.status_code == 200
        assert claim.json()["roles"] == ["viewer"]

        organizations = api.get(
            "/v2/organizations", headers={"Authorization": "Bearer org-b"},
        ).json()
        assert [item["organization_id"] for item in organizations["items"]] == ["org-a", "org-b"]

        selected = {
            "Authorization": "Bearer org-b",
            "X-Agent-OS-Organization": "org-a",
        }
        assert api.get("/v2/runs", headers=selected).status_code == 200
        assert api.post(
            "/v2/invitations",
            headers={**selected, "Idempotency-Key": "viewer-cannot-invite"},
            json={"roles": ["viewer"]},
        ).status_code == 403
        assert api.get("/v2/memberships", headers=selected).status_code == 403

        members = api.get(
            "/v2/memberships", headers={"Authorization": "Bearer org-a"},
        )
        assert members.status_code == 200
        assert members.json()["items"][0]["subject_id"] == "human-b"

        revoked = api.request(
            "DELETE", "/v2/memberships/human-b",
            headers={"Authorization": "Bearer org-a", "Idempotency-Key": "revoke-human-b"},
            json={"reason": "Project access ended"},
        )
        assert revoked.status_code == 200
        assert revoked.json()["active"] is False
        assert api.get("/v2/runs", headers=selected).status_code == 403


def test_invitation_capability_cannot_be_tampered_or_claimed_by_two_subjects(tmp_path):
    memberships = SQLMembershipStore(
        f"sqlite:///{tmp_path / 'api-membership-capability.sqlite3'}",
        signing_secret="membership-api-secret-that-is-long-enough",
        create_schema=True,
    )
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=FakeIdentity(),
        membership_store=memberships, shutdown=memberships.close,
    ))
    with api:
        invitation = api.post(
            "/v2/invitations",
            headers={"Authorization": "Bearer org-a", "Idempotency-Key": "single-claim"},
            json={"roles": ["operator"]},
        ).json()
        token = invitation["claim_token"]
        tampered = api.post(
            "/v2/invitations/claim", headers={"Authorization": "Bearer org-b"},
            json={"token": token[:-1] + ("a" if token[-1] != "a" else "b")},
        )
        assert tampered.status_code == 409
        assert api.post(
            "/v2/invitations/claim", headers={"Authorization": "Bearer org-b"},
            json={"token": token},
        ).status_code == 200
        second = api.post(
            "/v2/invitations/claim", headers={"Authorization": "Bearer guest-b"},
            json={"token": token},
        )
        assert second.status_code == 409


def test_administrator_can_manage_members_without_granting_or_revoking_ownership(tmp_path):
    memberships = SQLMembershipStore(
        f"sqlite:///{tmp_path / 'api-membership-delegation.sqlite3'}",
        signing_secret="membership-api-secret-that-is-long-enough",
        create_schema=True,
    )
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=FakeIdentity(),
        membership_store=memberships, shutdown=memberships.close,
    ))
    with api:
        owner_headers = {
            "Authorization": "Bearer org-a", "Idempotency-Key": "invite-admin-a",
        }
        admin_invitation = api.post(
            "/v2/invitations", headers=owner_headers,
            json={"roles": ["admin"]},
        )
        assert admin_invitation.status_code == 201
        assert api.post(
            "/v2/invitations/claim", headers={"Authorization": "Bearer admin-a"},
            json={"token": admin_invitation.json()["claim_token"]},
        ).status_code == 200

        delegated = api.post(
            "/v2/invitations",
            headers={
                "Authorization": "Bearer admin-a",
                "Idempotency-Key": "admin-invite-builder",
            },
            json={"roles": ["builder"]},
        )
        assert delegated.status_code == 201
        assert api.post(
            "/v2/invitations/claim", headers={"Authorization": "Bearer builder-a"},
            json={"token": delegated.json()["claim_token"]},
        ).status_code == 200

        for role in ("admin", "owner"):
            escalation = api.post(
                "/v2/invitations",
                headers={
                    "Authorization": "Bearer admin-a",
                    "Idempotency-Key": f"admin-cannot-grant-{role}",
                },
                json={"roles": [role]},
            )
            assert escalation.status_code == 403

        assert api.request(
            "DELETE", "/v2/memberships/builder-a",
            headers={
                "Authorization": "Bearer admin-a",
                "Idempotency-Key": "admin-revoke-builder",
            },
            json={"reason": "Builder assignment ended"},
        ).status_code == 200
        assert api.request(
            "DELETE", "/v2/memberships/admin-a",
            headers={
                "Authorization": "Bearer admin-a",
                "Idempotency-Key": "admin-revoke-admin",
            },
            json={"reason": "Privilege escalation attempt"},
        ).status_code == 403


def test_tenant_model_settings_are_owner_controlled_secret_free_and_isolated(tmp_path):
    settings = SQLTenantModelStore(
        f"sqlite:///{tmp_path / 'api-tenant-models.sqlite3'}", create_schema=True,
    )
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=FakeIdentity(),
        tenant_model_store=settings, shutdown=settings.close,
    ))
    with api:
        configured = api.put(
            "/v2/settings/model",
            headers={"Authorization": "Bearer org-a", "Idempotency-Key": "model-api-one"},
            json={
                "provider": "anthropic", "model_name": "claude-opus-4-1",
                "credential_ref": "tenant-anthropic-key",
            },
        )
        assert configured.status_code == 200
        assert configured.json()["credential_source"] == "tenant"
        assert "key" not in configured.json()
        assert api.get(
            "/v2/settings/model", headers={"Authorization": "Bearer org-a"},
        ).json()["setting"]["model_name"] == "claude-opus-4-1"
        assert api.get(
            "/v2/settings/model", headers={"Authorization": "Bearer org-b"},
        ).json() == {"configured": False, "setting": None}
        assert api.put(
            "/v2/settings/model",
            headers={"Authorization": "Bearer viewer-a", "Idempotency-Key": "viewer-model"},
            json={"provider": "google", "model_name": "gemini-2.5-pro"},
        ).status_code == 403


def test_deployment_inventory_projects_immutable_release_receipts_by_tenant(tmp_path):
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'deployment-inventory.sqlite3'}", create_schema=True,
    )
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=FakeIdentity(),
        artifact_store=artifacts, shutdown=artifacts.close,
    ))
    with api:
        receipt = {
            "kind": "static_site", "deployment_id": "static-one", "app_slug": "income-app",
            "revision": "a" * 64, "public_url": "https://apps.example.test/p/opaque/",
        }
        artifacts.put(
            organization_id="org-a", content=json.dumps(receipt).encode(),
            media_type="application/vnd.agent-os.static-site-release+json",
            idempotency_key="static-release-receipt",
        )
        artifacts.put(
            organization_id="org-b", content=b'{"kind":"hidden"}',
            media_type="application/vnd.agent-os.static-site-release+json",
            idempotency_key="hidden-release-receipt",
        )

        inventory = api.get(
            "/v2/deployments", headers={"Authorization": "Bearer org-a"},
        )
        assert inventory.status_code == 200
        assert inventory.json()["items"][0]["deployment_id"] == "static-one"
        assert inventory.json()["items"][0]["status"] == "active"
        assert "hidden" not in str(inventory.json())


def test_ceo_workspace_assets_are_public_but_api_data_stays_authenticated():
    api = client()
    root = api.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/app"
    page = api.get("/app")
    assert page.status_code == 200
    assert "CEO Workspace" in page.text
    assert "Maximum external spend" in page.text
    assert "Ready your company" in page.text
    assert 'id="mobile-more-toggle"' in page.text
    assert 'data-mobile-view="company"' in page.text
    assert "unsafe-inline" not in page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert api.get("/assets/ceo.css").status_code == 200
    assert api.get("/assets/app-icon.svg").status_code == 200
    manifest = api.get("/app.webmanifest")
    assert manifest.status_code == 200
    assert manifest.json()["display"] == "standalone"
    assert api.get("/service-worker.js").status_code == 200
    script = api.get("/assets/ceo.js")
    assert script.status_code == 200
    assert "Program command" in script.text
    assert "Independent work is continuing" in script.text
    assert "Execution diagnostics" in script.text
    assert "Execution timeline" in script.text
    assert "Retry recorded response" in script.text
    assert "Request changes" in script.text
    assert "Safe default" in script.text
    assert "Load older updates" in script.text
    assert "decisionDrafts" in script.text
    assert "/v2/events/stream" in script.text
    assert "/v2/me/push-subscriptions" in script.text
    assert "/v2/me/push-deliveries/" in script.text
    assert "routeFromHash" in script.text
    assert "focusInboxItem" in script.text
    assert "pushDeliveryId" in script.text
    assert "openMissionDeepLink" in script.text
    assert "workspaceRoute" in script.text
    assert 'parameters.get("run")' in script.text
    assert "Copy authorized mission link" in script.text
    assert "PushManager" in script.text
    assert "AbortController" in script.text
    assert "EventSource" not in script.text
    service_worker = api.get("/service-worker.js").text
    assert 'addEventListener("push"' in service_worker
    assert 'addEventListener("notificationclick"' in service_worker
    assert "agent-os-shell-v3" in service_worker
    assert '"Authorization": `Bearer ${state.token}`' in script.text
    assert "#view=" in script.text
    assert 'querySelectorAll(".nav-item[data-view]")' in script.text
    assert 'can("membership.manage")' in script.text
    assert 'can("model.manage")' in script.text
    assert 'can("mission.steer")' in script.text
    assert "CEO workspace" not in script.text
    assert api.get("/v2/client-config").json() == {"identity_mode": "manual"}
    assert api.get("/v2/runs").status_code == 401


def test_session_capabilities_and_mission_creation_are_role_consistent():
    api = client()
    owner = api.get("/v2/me", headers={"Authorization": "Bearer org-a"}).json()
    operator = api.get("/v2/me", headers={"Authorization": "Bearer operator-a"}).json()
    builder = api.get("/v2/me", headers={"Authorization": "Bearer builder-a"}).json()
    reviewer = api.get("/v2/me", headers={"Authorization": "Bearer reviewer-a"}).json()
    admin = api.get("/v2/me", headers={"Authorization": "Bearer admin-a"}).json()
    manager = api.get("/v2/me", headers={"Authorization": "Bearer manager-a"}).json()
    billing = api.get("/v2/me", headers={"Authorization": "Bearer billing-a"}).json()
    external_client = api.get("/v2/me", headers={"Authorization": "Bearer client-a"}).json()
    viewer = api.get("/v2/me", headers={"Authorization": "Bearer viewer-a"}).json()

    assert owner["persona"] == "executive"
    assert "mission.create" in owner["capabilities"]
    assert "decision.redrive" in owner["capabilities"]
    assert operator["persona"] == "operator"
    assert {"mission.create", "mission.steer", "integration.manage"} <= set(
        operator["capabilities"]
    )
    assert not {"membership.manage", "model.manage", "billing.manage"} & set(
        operator["capabilities"]
    )
    assert builder["persona"] == "builder"
    assert {"work.execute", "artifact.publish", "notification.respond"} <= set(
        builder["capabilities"]
    )
    assert "mission.create" not in builder["capabilities"]
    assert reviewer["persona"] == "reviewer"
    assert {"review.read", "notification.respond"} <= set(reviewer["capabilities"])
    assert "artifact.publish" not in reviewer["capabilities"]
    assert "mission.create" not in reviewer["capabilities"]
    assert admin["persona"] == "administrator"
    assert "membership.manage" in admin["capabilities"]
    assert "mission.create" not in admin["capabilities"]
    assert manager["persona"] == "manager"
    assert {"mission.create", "workforce.manage"} <= set(manager["capabilities"])
    assert billing["persona"] == "billing"
    assert "billing.manage" in billing["capabilities"]
    assert "mission.create" not in billing["capabilities"]
    assert external_client["persona"] == "client"
    assert "review.read" in external_client["capabilities"]
    assert "company.read" not in external_client["capabilities"]
    assert viewer["persona"] == "viewer"
    assert "mission.create" not in viewer["capabilities"]
    forbidden = api.post(
        "/v2/runs",
        headers={"Authorization": "Bearer viewer-a", "Idempotency-Key": "viewer-mission"},
        json={"prompt": "Viewer must not launch this"},
    )
    assert forbidden.status_code == 403
    managed = api.post(
        "/v2/runs",
        headers={"Authorization": "Bearer manager-a", "Idempotency-Key": "manager-mission"},
        json={"prompt": "Manager launches an authorized mission"},
    )
    assert managed.status_code == 202
    assert api.post(
        "/v2/runs",
        headers={"Authorization": "Bearer admin-a", "Idempotency-Key": "admin-mission"},
        json={"prompt": "Administrative access must not imply execution"},
    ).status_code == 403


def test_first_mission_readiness_is_honest_role_aware_and_resumable(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'first-mission-readiness.sqlite3'}"
    company = SQLCompanyDirectory(database_url, create_schema=True)
    connectors = SQLConnectorRegistry(database_url, create_schema=True)
    notifications = SQLNotificationStore(database_url, create_schema=True)
    models = SQLTenantModelStore(database_url, create_schema=True)
    usage = SQLUsageMeter(database_url, monthly_budget_cents=2_000, create_schema=True)
    engine = InMemoryWorkflowEngine()
    api = TestClient(create_app(
        engine=engine, identity=FakeIdentity(), company_directory=company,
        connector_registry=connectors, notification_store=notifications,
        tenant_model_store=models, usage_meter=usage,
    ))
    try:
        initial = api.get(
            "/v2/readiness", headers={"Authorization": "Bearer org-a"},
        )
        assert initial.status_code == 200
        assert initial.json()["overall"] == "ready_for_first_mission"
        assert initial.json()["can_start_mission"] is True
        assert initial.json()["show_onboarding"] is True
        assert initial.json()["blockers"] == []
        steps = {item["id"]: item for item in initial.json()["steps"]}
        assert steps["control_plane"]["status"] == "complete"
        assert steps["organization"]["status"] == "complete"
        assert steps["spend_guard"]["status"] == "complete"
        assert steps["model_runtime"]["status"] == "verify_on_first_use"
        assert "does not pretend" in steps["model_runtime"]["detail"]
        assert steps["integrations"]["status"] == "optional"

        viewer = api.get(
            "/v2/readiness", headers={"Authorization": "Bearer viewer-a"},
        ).json()
        assert viewer["can_start_mission"] is False
        assert viewer["show_onboarding"] is False

        configured = api.put(
            "/v2/settings/model",
            headers={
                "Authorization": "Bearer org-a",
                "Idempotency-Key": "readiness-model-setting",
            },
            json={
                "provider": "openai", "model_name": "gpt-5.6",
                "credential_ref": "private-model-key",
            },
        )
        assert configured.status_code == 200
        with_model = api.get(
            "/v2/readiness", headers={"Authorization": "Bearer org-a"},
        ).json()
        model_step = next(
            item for item in with_model["steps"] if item["id"] == "model_runtime"
        )
        assert model_step["status"] == "verify_on_first_use"
        assert "private-model-key" not in str(with_model)

        launched = api.post(
            "/v2/runs",
            headers={
                "Authorization": "Bearer org-a",
                "Idempotency-Key": "readiness-first-mission",
            },
            json={"prompt": "Prove onboarding can resume from durable truth"},
        )
        assert launched.status_code == 202
        pending = api.get(
            "/v2/readiness", headers={"Authorization": "Bearer org-a"},
        ).json()
        assert pending["overall"] == "verification_pending"
        assert pending["show_onboarding"] is True

        usage.reserve_model_turn(
            tenant_id="org-a", source_id="readiness-model-proof", run_id="proof-run",
            category="mission_planning", model="openai:gpt-5.6",
            maximum_cost_cents=25,
        )
        usage.settle_model_turn(
            tenant_id="org-a", source_id="readiness-model-proof",
            usage={
                "requests": 1, "tool_calls": 0, "input_tokens": 10,
                "output_tokens": 5, "total_tokens": 15,
                "provider_cost_usd_micros": 10_000,
            },
        )
        active = api.get(
            "/v2/readiness", headers={"Authorization": "Bearer org-a"},
        ).json()
        assert active["overall"] == "active"
        assert active["show_onboarding"] is False
        assert next(
            item for item in active["steps"] if item["id"] == "model_runtime"
        )["status"] == "complete"
        assert next(
            item for item in active["steps"] if item["id"] == "first_mission"
        )["status"] == "complete"
    finally:
        usage.close()
        models.close()
        notifications.close()
        connectors.close()
        company.close()


def test_readiness_fails_closed_when_required_production_adapters_are_absent():
    response = client().get(
        "/v2/readiness", headers={"Authorization": "Bearer org-a"},
    )

    assert response.status_code == 200
    assert response.json()["overall"] == "blocked"
    assert response.json()["can_start_mission"] is False
    assert set(response.json()["blockers"]) == {
        "organization", "spend_guard", "model_runtime", "attention",
    }


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


def test_directive_budget_authority_is_bounded_at_the_api_boundary():
    api = client()
    headers = {"Authorization": "Bearer org-a", "Idempotency-Key": "request-budget"}

    accepted = api.post(
        "/v2/runs", headers=headers,
        json={"prompt": "Build within this ceiling", "budget_limit_cents": 25_000},
    )
    rejected = api.post(
        "/v2/runs",
        headers={**headers, "Idempotency-Key": "request-budget-invalid"},
        json={"prompt": "Invent spending authority", "budget_limit_cents": -1},
    )

    assert accepted.status_code == 202
    assert rejected.status_code == 422


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

        def list_notifications(
            self, tenant_id, *, run_id=None, recipient_id=None,
            recipient_ids=None, limit=100,
        ):
            self.calls.append((tenant_id, run_id, recipient_id, recipient_ids, limit))
            return ({"notification_id": f"notice-{tenant_id}"},)

    store = FakeNotifications()
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=FakeIdentity(), notification_store=store,
    ))

    owner = api.get("/v2/notifications?run_id=run-1&limit=25", headers={"Authorization": "Bearer org-a"})
    agent = api.get("/v2/notifications", headers={"Authorization": "Bearer agent-a"})

    assert owner.status_code == 200
    assert owner.json()["items"][0]["notification_id"] == "notice-org-a"
    assert store.calls[0] == ("org-a", "run-1", None, None, 26)
    assert store.calls[1] == (
        "org-a", None, None, ("agent-a", "agent:agent-a", "role:agent"), 101,
    )


def test_notification_inbox_accepts_role_audiences_without_leaking_between_roles(tmp_path):
    notifications = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'api-role-attention.sqlite3'}", create_schema=True,
    )
    try:
        notifications.publish_notification(Notification(
            notification_id="review-required", tenant_id="org-a", run_id="run-review",
            category=NotificationCategory.HUMAN_ACTION_REQUIRED,
            recipient_ids=("role:reviewer",), subject="Review release evidence",
            body="Independent review is required", source_id="review-gate",
            created_at="2026-09-17T12:00:00+00:00",
            payload={"severity": "warning"},
        ))
        api = TestClient(create_app(
            engine=InMemoryWorkflowEngine(), identity=FakeIdentity(),
            notification_store=notifications,
        ))

        reviewer = api.get(
            "/v2/notifications", headers={"Authorization": "Bearer reviewer-a"},
        )
        builder = api.get(
            "/v2/notifications", headers={"Authorization": "Bearer builder-a"},
        )
        viewer = api.get(
            "/v2/notifications", headers={"Authorization": "Bearer viewer-a"},
        )

        assert [item["notification_id"] for item in reviewer.json()["items"]] == [
            "review-required"
        ]
        assert builder.json()["items"] == []
        assert viewer.json()["items"] == []
    finally:
        notifications.close()


def test_notification_inbox_cursor_is_opaque_stable_and_tenant_fenced(tmp_path):
    notifications = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'api-cursor-attention.sqlite3'}", create_schema=True,
    )
    try:
        for position in range(3):
            notifications.publish_notification(Notification(
                notification_id=f"notice-page-{position}", tenant_id="org-a",
                run_id=f"run-{position}",
                category=NotificationCategory.MANAGEMENT_ATTENTION,
                recipient_ids=("human:ceo",), subject=f"Update {position}",
                body="Bounded attention update", source_id=f"source-{position}",
                created_at=f"2026-09-17T12:00:0{position}+00:00",
                payload={"severity": "warning"},
            ))
        api = TestClient(create_app(
            engine=InMemoryWorkflowEngine(), identity=FakeIdentity(),
            notification_store=notifications,
        ))

        first = api.get(
            "/v2/notifications?limit=2", headers={"Authorization": "Bearer org-a"},
        )
        assert first.status_code == 200
        assert [item["notification_id"] for item in first.json()["items"]] == [
            "notice-page-2", "notice-page-1",
        ]
        cursor = first.json()["next_cursor"]
        assert cursor and "notice-page" not in cursor
        second = api.get(
            f"/v2/notifications?limit=2&cursor={cursor}",
            headers={"Authorization": "Bearer org-a"},
        )
        assert second.status_code == 200
        assert [item["notification_id"] for item in second.json()["items"]] == [
            "notice-page-0",
        ]
        assert second.json()["next_cursor"] is None
        other_tenant = api.get(
            f"/v2/notifications?limit=2&cursor={cursor}",
            headers={"Authorization": "Bearer org-b"},
        )
        assert other_tenant.status_code == 200
        assert other_tenant.json()["items"] == []
        assert api.get(
            "/v2/notifications?cursor=not-a-real-cursor",
            headers={"Authorization": "Bearer org-a"},
        ).status_code == 400
    finally:
        notifications.close()


def test_personal_notification_preferences_state_and_attention_projection(tmp_path):
    notifications = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'api-personal-attention.sqlite3'}", create_schema=True,
    )
    try:
        notifications.publish_notification(Notification(
            notification_id="attention-one", tenant_id="org-a", run_id="run-one",
            category=NotificationCategory.HUMAN_ACTION_REQUIRED,
            recipient_ids=("human:ceo",), subject="Approve the release",
            body="A consequential release is waiting.", source_id="approval-one",
            created_at="2026-09-17T12:00:00+00:00", correlation_id="decision-one",
            payload={"risk": "irreversible"},
        ))
        notifications.publish_notification(Notification(
            notification_id="operator-one", tenant_id="org-a", run_id="run-two",
            category=NotificationCategory.OPERATOR_ATTENTION,
            recipient_ids=("operator:on-call",), subject="Restore the worker",
            body="A durable queue lease needs attention.", source_id="operator-source-one",
            created_at="2026-09-17T12:01:00+00:00",
        ))
        api = TestClient(create_app(
            engine=InMemoryWorkflowEngine(), identity=FakeIdentity(),
            notification_store=notifications,
        ))
        headers = {"Authorization": "Bearer org-a"}
        inbox = api.get("/v2/notifications", headers=headers).json()
        assert inbox["items"][0]["subject"] == "Approve the release"
        assert inbox["items"][0]["presentation"]["level"] == "time_sensitive"
        assert inbox["items"][0]["user_state"]["status"] == "unread"
        assert api.get(
            "/v2/notifications", headers={"Authorization": "Bearer viewer-a"},
        ).json()["items"] == []
        operator_inbox = api.get(
            "/v2/notifications", headers={"Authorization": "Bearer operator-a"},
        ).json()["items"]
        assert [item["notification_id"] for item in operator_inbox] == ["operator-one"]

        preference = api.put(
            "/v2/notification-preferences",
            headers={**headers, "Idempotency-Key": "preferences-api-one"},
            json={
                "mode": "focused", "browser_notifications": True,
                "quiet_hours_start": "22:00", "quiet_hours_end": "07:00",
                "timezone": "America/Los_Angeles", "digest_interval_minutes": 240,
            },
        )
        assert preference.status_code == 200
        assert preference.json()["mode"] == "focused"
        dismissed = api.put(
            "/v2/notifications/attention-one/state",
            headers={**headers, "Idempotency-Key": "dismiss-attention-one"},
            json={"status": "dismissed"},
        )
        assert dismissed.status_code == 200
        updated = api.get("/v2/notifications", headers=headers).json()["items"][0]
        assert updated["user_state"]["status"] == "dismissed"
        assert updated["presentation"]["disposition"] == "hidden"
        assert api.put(
            "/v2/notifications/attention-one/state",
            headers={
                "Authorization": "Bearer viewer-a",
                "Idempotency-Key": "viewer-cannot-dismiss",
            },
            json={"status": "dismissed"},
        ).status_code == 404
    finally:
        notifications.close()


def test_experience_event_catchup_cursor_is_tenant_bound_and_advances_hidden_events(tmp_path):
    notifications = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'api-experience-events.sqlite3'}", create_schema=True,
    )
    notifications.publish_notification(Notification(
        notification_id="event-notification", tenant_id="org-a", run_id="run-one",
        category=NotificationCategory.HUMAN_ACTION_REQUIRED,
        recipient_ids=("human:ceo",), subject="Approve",
        body="Sensitive decision detail", source_id="approval-source",
        created_at="2026-09-17T12:00:00+00:00", correlation_id="decision-one",
    ))
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=FakeIdentity(),
        notification_store=notifications,
    ))
    try:
        owner_headers = {"Authorization": "Bearer org-a"}
        initial = api.get("/v2/events", headers=owner_headers)
        assert initial.status_code == 200
        assert [item["kind"] for item in initial.json()["items"]] == [
            "notification.published",
        ]
        assert initial.json()["reset_required"] is False
        cursor = initial.json()["cursor"]
        assert "org-a" not in cursor
        assert "event-notification" not in cursor

        hidden = api.get(
            "/v2/events", headers={"Authorization": "Bearer viewer-a"},
        )
        assert hidden.status_code == 200
        assert hidden.json()["items"] == []
        assert hidden.json()["cursor"] == cursor

        assert api.get(
            f"/v2/events?cursor={cursor}",
            headers={"Authorization": "Bearer org-b"},
        ).status_code == 400

        changed = api.put(
            "/v2/notifications/event-notification/state",
            headers={**owner_headers, "Idempotency-Key": "read-experience-event"},
            json={"status": "read"},
        )
        assert changed.status_code == 200
        catchup = api.get(
            f"/v2/events?cursor={cursor}", headers=owner_headers,
        )
        assert catchup.status_code == 200
        assert [item["kind"] for item in catchup.json()["items"]] == [
            "notification.state.changed",
        ]
    finally:
        api.close()
        notifications.close()


def test_experience_sse_uses_authenticated_resume_cursor_and_bounded_connection(tmp_path):
    notifications = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'api-experience-sse.sqlite3'}", create_schema=True,
    )
    notifications.publish_notification(Notification(
        notification_id="sse-notification", tenant_id="org-a", run_id="run-one",
        category=NotificationCategory.HUMAN_ACTION_REQUIRED,
        recipient_ids=("human:ceo",), subject="Approve",
        body="Sensitive decision detail", source_id="approval-source",
        created_at="2026-09-17T12:00:00+00:00",
    ))
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=FakeIdentity(),
        notification_store=notifications, experience_stream_seconds=0.02,
    ))
    try:
        with api.stream(
            "GET", "/v2/events/stream",
            headers={"Authorization": "Bearer org-a"},
        ) as response:
            body = "".join(response.iter_text())
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-store, no-transform"
        assert "retry: 3000" in body
        assert "event: experience" in body
        assert '"kind":"notification.published"' in body
        cursor = next(
            line.removeprefix("id: ") for line in body.splitlines()
            if line.startswith("id: ")
        )

        resumed = api.get(
            "/v2/events/stream",
            headers={"Authorization": "Bearer org-a", "Last-Event-ID": cursor},
        )
        assert resumed.status_code == 200
        assert "event: experience" not in resumed.text
        assert api.get(
            f"/v2/events/stream?cursor={cursor}",
            headers={
                "Authorization": "Bearer org-a",
                "Last-Event-ID": "different-cursor",
            },
        ).status_code == 400

        ag_ui_page = api.get(
            "/v2/events?protocol=ag-ui",
            headers={"Authorization": "Bearer org-a"},
        )
        assert ag_ui_page.status_code == 200
        assert ag_ui_page.json()["protocol"] == "ag-ui-protocol/0.1.22"
        ag_ui_item = ag_ui_page.json()["items"][0]
        assert ag_ui_item["type"] == "CUSTOM"
        assert ag_ui_item["name"] == "agent_os.experience.v1"
        assert ag_ui_item["value"]["kind"] == "notification.published"
        assert "Sensitive decision detail" not in str(ag_ui_item)

        with api.stream(
            "GET", "/v2/events/stream?protocol=ag-ui",
            headers={"Authorization": "Bearer org-a"},
        ) as ag_ui_response:
            ag_ui_body = "".join(ag_ui_response.iter_text())
            assert ag_ui_response.headers[
                "x-agent-os-event-protocol"
            ] == "ag-ui-protocol/0.1.22"
        assert "event: experience" not in ag_ui_body
        data = next(
            json.loads(line.removeprefix("data: "))
            for line in ag_ui_body.splitlines()
            if line.startswith("data: ")
        )
        assert data["type"] == "CUSTOM"
        assert data["name"] == "agent_os.experience.v1"
        assert data["value"]["resource"]["type"] == "notification"
        assert "Sensitive decision detail" not in str(data)
    finally:
        api.close()
        notifications.close()


def test_usage_api_is_tenant_scoped_and_event_detail_requires_owner(tmp_path):
    meter = SQLUsageMeter(
        f"sqlite:///{tmp_path / 'api-usage.sqlite3'}", monthly_budget_cents=250,
        create_schema=True,
    )
    try:
        meter.reserve_model_turn(
            tenant_id="org-a", source_id="turn-1", run_id="run-1",
            category="graph_agent", model="provider:model", maximum_cost_cents=10,
        )
        meter.settle_model_turn(
            tenant_id="org-a", source_id="turn-1",
            usage={
                "requests": 1, "tool_calls": 0, "input_tokens": 20,
                "output_tokens": 10, "total_tokens": 30,
                "provider_cost_usd_micros": 1_000,
            },
        )
        api = TestClient(create_app(
            engine=InMemoryWorkflowEngine(), identity=FakeIdentity(), usage_meter=meter,
        ))
        assert api.get(
            "/v2/usage/summary", headers={"Authorization": "Bearer org-a"},
        ).json()["total_tokens"] == 30
        assert api.get(
            "/v2/usage/summary", headers={"Authorization": "Bearer org-b"},
        ).json()["total_tokens"] == 0
        assert api.get(
            "/v2/usage/events", headers={"Authorization": "Bearer viewer-a"},
        ).status_code == 403
        owner_events = api.get(
            "/v2/usage/events", headers={"Authorization": "Bearer org-a"},
        )
        assert owner_events.status_code == 200
        assert owner_events.json()["items"][0]["source_id"] == "turn-1"
    finally:
        meter.close()


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
    builder_upload = api.post(
        "/v2/artifacts",
        headers={"Authorization": "Bearer builder-a", "Idempotency-Key": "artifact-builder"},
        json={
            "content_base64": base64.b64encode(b"builder evidence").decode(),
            "media_type": "text/plain",
        },
    )
    assert builder_upload.status_code == 201
    reviewer_upload = api.post(
        "/v2/artifacts",
        headers={"Authorization": "Bearer reviewer-a", "Idempotency-Key": "artifact-reviewer"},
        json={
            "content_base64": base64.b64encode(b"must remain read only").decode(),
            "media_type": "text/plain",
        },
    )
    assert reviewer_upload.status_code == 403


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
    subprogram_run_id = "mission-run-child-security"
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
            output={"child_run_id": subprogram_run_id,
                    "organization_actions": {"hiring_requests": [{
                "role": "security-specialist",
                "reason": "Independent security capacity is missing",
                "participant_kind": "agent",
                "capabilities": ["security-review"],
                "requested_count": 1,
                "estimated_budget_cents": 0,
            }]}},
        ), NodeToken(
            "publish-token", "publish", TokenStatus.SUCCEEDED, 1,
            evidence_ids=("artifact-service-receipt",),
            output={
                "deployment_id": "service-deployment-one",
                "public_url": "https://customer-api.run.app",
                "receipt_artifact_id": "artifact-service-receipt",
            },
        )),
    )
    subprogram = WorkflowRunState(
        subprogram_run_id,
        "org-a",
        "security-workflow",
        1,
        3,
        WorkflowRunStatus.SUCCEEDED,
        (NodeToken(
            "security-done", "done", TokenStatus.SUCCEEDED, 1,
            evidence_ids=("artifact-security-review",),
        ),),
        {"mission_program": {"objective": "Verify the release security boundary."}},
        ("security-done",),
    )

    class MissionGraphs:
        def get_graph_run(self, tenant_id, run_id):
            if tenant_id != "org-a":
                return None
            return {
                planning_run_id: planning,
                child_run_id: execution,
                subprogram_run_id: subprogram,
            }.get(run_id)

        def get_workflow_definition(self, tenant_id, workflow_id, version):
            if tenant_id != "org-a" or workflow_id != "mission-workflow" or version != 1:
                return None
            return WorkflowDefinition(
                "mission-workflow", "org-a", "Mission", 1, "work",
                (
                    WorkflowNode("work", NodeKind.AGENT, "Build the product", "engineer"),
                    WorkflowNode(
                        "publish", NodeKind.TOOL, "Publish the approved service",
                        configuration={"tool": "deploy.service"},
                    ),
                    WorkflowNode("done", NodeKind.TERMINAL, "Accept proof"),
                ),
                (WorkflowEdge("work", "publish"), WorkflowEdge("publish", "done")),
                "agent:mission-architect",
            )

        def inspect_graph_run(self, tenant_id, run_id, *, action_limit=1_000):
            assert tenant_id == "org-a" and action_limit == 1_000
            return {
                "run_id": run_id,
                "workflow_id": "mission-workflow",
                "workflow_version": 1,
                "state_version": 4,
                "created_at": "2026-09-17T12:00:00+00:00",
                "updated_at": "2026-09-17T12:00:09+00:00",
                "actions": [{
                    "action_id": "action-work-one",
                    "state_version": 4,
                    "action": {
                        "action_id": "action-work-one", "kind": "execute_node",
                        "token_id": "work-token", "node_id": "work",
                        "payload": {"secret": "must-not-project"},
                    },
                    "status": "executing", "attempts": 2,
                    "available_at": "2026-09-17T12:00:01+00:00",
                    "lease_owner": "private-worker-host",
                    "lease_expires_at": "2026-09-17T12:01:00+00:00",
                    "created_at": "2026-09-17T12:00:01+00:00",
                    "completed_at": None, "last_error": None,
                }],
            }

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
    assert response.json()["subprograms"] == [{
        "run_id": subprogram_run_id,
        "parent_run_id": child_run_id,
        "parent_token_id": "work-token",
        "depth": 1,
        "workflow_id": "security-workflow",
        "workflow_version": 1,
        "state_version": 3,
        "status": "succeeded",
        "objective": "Verify the release security boundary.",
        "token_counts": {
            "ready": 0, "running": 0, "waiting": 0, "succeeded": 1,
            "failed": 0, "cancelled": 0,
        },
        "failure": None,
    }]
    assert response.json()["subprograms_truncated"] is False
    assert response.json()["deliverables"] == [{
        "kind": "cloud_run_service",
        "node_id": "publish",
        "deployment_id": "service-deployment-one",
        "public_url": "https://customer-api.run.app",
        "receipt_artifact_id": "artifact-service-receipt",
    }]
    management = api.get(
        f"/v2/runs/{created['run_id']}/management",
        headers={"Authorization": "Bearer org-a"},
    )
    assert management.status_code == 200
    assert management.json()["execution_run_id"] == child_run_id
    assert management.json()["work_items"][0]["owner_id"] == "agent:engineer"
    assert management.json()["progress"]["live"] == 1
    assert management.json()["execution_timeline"] == [{
        "action_id": "action-work-one", "state_version": 4,
        "kind": "execute_node", "node_id": "work", "token_id": "work-token",
        "status": "executing", "attempts": 2,
        "available_at": "2026-09-17T12:00:01+00:00",
        "created_at": "2026-09-17T12:00:01+00:00",
        "completed_at": None, "error": None,
    }]
    assert "must-not-project" not in str(management.json())
    assert "private-worker-host" not in str(management.json())
    viewer_management = api.get(
        f"/v2/runs/{created['run_id']}/management",
        headers={"Authorization": "Bearer viewer-a"},
    )
    assert viewer_management.status_code == 200
    assert "execution_timeline" not in viewer_management.json()
    assert management.json()["subprograms"][0]["run_id"] == subprogram_run_id
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


def test_external_staffing_api_requires_owner_attestation_before_activation(tmp_path):
    directory = SQLCompanyDirectory(
        f"sqlite:///{tmp_path / 'external-company.sqlite3'}", create_schema=True,
    )
    try:
        decision = directory.decide_hiring_proposal(
            tenant_id="org-a", proposal_id="proposal-human-qa", approved=True,
            participant_kind="human", reason="A customer QA reviewer is required",
            role="customer-qa", requested_count=1, team_id="quality",
            manager_id="agent:quality-manager", capabilities=("acceptance-testing",),
            tool_grants=("artifact.read",), spending_limit_cents=0,
            actor_id="human-a",
        )
        onboarding_id = decision["payload"]["onboarding_cases"][0]["onboarding_id"]
        api = TestClient(create_app(
            engine=InMemoryWorkflowEngine(), identity=FakeIdentity(),
            company_directory=directory,
        ))
        assert api.get(
            "/v2/company/external-onboarding",
            headers={"Authorization": "Bearer viewer-a"},
        ).status_code == 403
        inventory = api.get(
            "/v2/company/external-onboarding",
            headers={"Authorization": "Bearer org-a"},
        )
        assert inventory.status_code == 200
        assert inventory.json()["items"][0]["status"] == "awaiting_external_onboarding"

        response = api.post(
            f"/v2/company/external-onboarding/{onboarding_id}/confirm",
            headers={
                "Authorization": "Bearer org-a",
                "Idempotency-Key": "confirm-external-qa",
            },
            json={
                "display_name": "External QA Reviewer",
                "identity_subject": "human:external-qa",
                "response_sla_seconds": 7_200,
                "quality_criteria": ["Attach acceptance evidence"],
                "attestations": [
                    "identity_verified", "terms_accepted", "access_approved",
                ],
            },
        )
        assert response.status_code == 200
        organization = api.get(
            "/v2/company/organization", headers={"Authorization": "Bearer org-a"},
        ).json()
        assert any(
            item["participant_id"] == "human:external-qa"
            for item in organization["humans"]
        )
        assert api.get(
            "/v2/company/external-onboarding",
            headers={"Authorization": "Bearer org-b"},
        ).json()["items"] == []
    finally:
        directory.close()


def test_connector_api_is_owner_governed_and_tenant_isolated(tmp_path):
    registry = SQLConnectorRegistry(
        f"sqlite:///{tmp_path / 'api-connectors.sqlite3'}", create_schema=True,
    )
    try:
        api = TestClient(create_app(
            engine=InMemoryWorkflowEngine(), identity=FakeIdentity(),
            connector_registry=registry,
        ))
        definition = {
            "connector_id": "public-data",
            "display_name": "Public data",
            "base_url": "https://data.example.test",
            "allowed_path_prefixes": ["/v1/records"],
            "allowed_methods": ["GET"],
            "auth_kind": "none",
        }
        assert api.post(
            "/v2/connectors",
            headers={
                "Authorization": "Bearer viewer-a", "Idempotency-Key": "viewer-register",
            },
            json=definition,
        ).status_code == 403
        created = api.post(
            "/v2/connectors",
            headers={
                "Authorization": "Bearer org-a", "Idempotency-Key": "owner-register",
            },
            json=definition,
        )
        assert created.status_code == 201
        assert created.json()["connector_id"] == "public-data"
        assert api.get(
            "/v2/connectors", headers={"Authorization": "Bearer org-a"},
        ).json()["items"][0]["active"] is True
        assert api.get(
            "/v2/connectors", headers={"Authorization": "Bearer org-b"},
        ).json()["items"] == []
        disabled = api.request(
            "DELETE", "/v2/connectors/public-data",
            headers={
                "Authorization": "Bearer org-a", "Idempotency-Key": "owner-disable",
            },
            json={"reason": "No longer required"},
        )
        assert disabled.status_code == 200
        assert disabled.json()["active"] is False
    finally:
        registry.close()


def test_notification_route_and_delivery_api_are_owner_governed_and_tenant_isolated(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'api-notification-routes.sqlite3'}"
    registry = SQLConnectorRegistry(database_url, create_schema=True)
    notifications = SQLNotificationStore(database_url, create_schema=True)
    try:
        registry.register_connector(
            tenant_id="org-a",
            actor_id="human-a",
            idempotency_key="register-notifier",
            definition={
                "connector_id": "notifier",
                "display_name": "Notifier",
                "base_url": "https://notify.example.test",
                "allowed_path_prefixes": ["/v1/events/"],
                "allowed_methods": ["POST"],
                "auth_kind": "bearer",
                "credential_ref": "notifier-token",
                "idempotency_header": "Idempotency-Key",
            },
        )
        api = TestClient(create_app(
            engine=InMemoryWorkflowEngine(), identity=FakeIdentity(),
            connector_registry=registry, notification_store=notifications,
        ))
        route = {
            "route_id": "executive-alerts",
            "display_name": "Executive alerts",
            "connector_id": "notifier",
            "path": "/v1/events/agent-os",
            "categories": ["human_action_required"],
            "payload_format": "agent-os",
        }
        assert api.post(
            "/v2/notification-routes",
            headers={"Authorization": "Bearer viewer-a", "Idempotency-Key": "viewer-route"},
            json=route,
        ).status_code == 403
        assert api.post(
            "/v2/notification-routes",
            headers={"Authorization": "Bearer org-a", "Idempotency-Key": "bad-route-path"},
            json={**route, "path": "/admin"},
        ).status_code == 409
        created = api.post(
            "/v2/notification-routes",
            headers={"Authorization": "Bearer org-a", "Idempotency-Key": "owner-route"},
            json=route,
        )
        assert created.status_code == 201
        assert api.get(
            "/v2/notification-routes", headers={"Authorization": "Bearer org-b"},
        ).json()["items"] == []

        notifications.publish_notification(Notification(
            notification_id="notification-api",
            tenant_id="org-a",
            run_id="run-api",
            category=NotificationCategory.HUMAN_ACTION_REQUIRED,
            recipient_ids=("human:ceo",),
            subject="Approve",
            body="Approve this action",
            source_id="source-api",
            created_at="2026-09-13T12:00:00+00:00",
        ))
        lease = notifications.claim_notification_delivery(
            "org-a", worker_id="worker-a",
        )
        notifications.fail_notification_delivery(
            "org-a", lease.delivery_id, worker_id="worker-a",
            error={"type": "MissingCredential", "message": "provision credential"},
        )
        deliveries = api.get(
            "/v2/notification-deliveries", headers={"Authorization": "Bearer org-a"},
        ).json()["items"]
        assert deliveries[0]["status"] == "failed"
        assert api.get(
            "/v2/notification-deliveries", headers={"Authorization": "Bearer org-b"},
        ).json()["items"] == []
        redrive = api.post(
            f"/v2/notification-deliveries/{lease.delivery_id}/redrive",
            headers={"Authorization": "Bearer org-a", "Idempotency-Key": "redrive-api"},
        )
        assert redrive.status_code == 200
        assert redrive.json()["status"] == "pending"
    finally:
        notifications.close()
        registry.close()
