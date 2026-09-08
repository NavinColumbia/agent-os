from __future__ import annotations

from typing import Any, Mapping

from fastapi.testclient import TestClient

from agent_os.api.app import create_app
from agent_os.infrastructure.memory import InMemoryWorkflowEngine


class FakeIdentity:
    def authenticate(self, authorization: str | None, session: str | None) -> Mapping[str, Any]:
        if authorization == "Bearer org-a":
            return {"sub": "human-a", "org": "org-a", "roles": ["owner"]}
        if authorization == "Bearer org-b":
            return {"sub": "human-b", "org": "org-b", "roles": ["owner"]}
        if authorization == "Bearer agent-a":
            return {"sub": "agent-a", "org": "org-a", "roles": ["agent"]}
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
